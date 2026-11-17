"""真实规划阶段探针: 在已登记派发任务、启动线程前截停."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from shader_deep.agents.five_analysis.audit import RoleAudit
from shader_deep.infrastructure.analysis_logging import _safe, analysis_console, log_analysis
from shader_deep.infrastructure.llm.client import build_model
from shader_deep.runtime.task_store import TaskStore
from shader_deep.workflows import five_analysis as flow
from shader_deep.workflows.configuration import load_analysis_options

ROOT = Path(__file__).resolve().parent
APP = ROOT.parents[1] / 'apps' / 'shader_deep'
NAMES = ('hypnotic-ripples', 'auroras', 'drive-home', '2d-physics-balls', 'windows-95')
REQUEST = '为后续 Shader 复刻分析这张参考图: 先拆分可辨识的视觉元素, 给出各元素的名称和明确范围; 自主选择本轮一个目标元素, 为它制定默认三个相互独立的开放探索方向. 主阶段只做元素定位、范围划定和任务编排.'


class ProbeStop(BaseException):
    """测试专用截停, 不进入业务失败/恢复/交付逻辑."""


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def forbidden(*args: object, **kwargs: object) -> None:
    raise ProbeStop('禁止启动探索、整合或恢复子角色')


def capture_dispatch(store: TaskStore, jobs: dict, parallel: int, **kwargs: object) -> None:
    state = store.snapshot()
    tasks = {key: state['tasks'][key] for key in jobs}
    assert len(tasks) == 3
    assert all(item['status'] == 'pending' and not item['attempts'] and item.get('model_calls', 0) == 0 for item in tasks.values())
    assert all(key.startswith('exploration-') for key in tasks)
    write_json(store.directory / 'dispatch.json', {
        'status': 'dispatch_prepared_not_started',
        'boundary': '_explore -> _wait_workers (before ThreadPoolExecutor)',
        'parallel_requested': parallel,
        'worker_started_count': 0,
        'tasks': tasks,
    })
    write_json(store.directory / 'boundary-state.json', state)
    log_analysis(store.directory, '测试截停: 派发指令已登记, 子 agent 尚未启动', details={'task_ids': list(tasks), 'worker_started_count': 0})
    raise ProbeStop('dispatch_prepared_not_started')


ORIGINAL_HISTORY = RoleAudit.history


def capture_history(self: RoleAudit, messages: list) -> None:
    ORIGINAL_HISTORY(self, messages)
    with (self.directory / 'model-output.jsonl').open('a', encoding='utf-8') as stream:
        for message in messages:
            if isinstance(message, AIMessage):
                content = message.content
                if isinstance(content, list):
                    content = [block for block in content if isinstance(block, str) or block.get('type') == 'text']
                output = _safe({
                    **self.identity, 'model_call': self.execution.model_calls,
                    'content': content, 'tool_calls': message.tool_calls,
                    'invalid_tool_calls': message.invalid_tool_calls,
                    'response_metadata': message.response_metadata,
                    'usage_metadata': message.usage_metadata,
                })
                stream.write(json.dumps(output, ensure_ascii=False) + '\n')


def run_case(name: str, model: object, options: object) -> dict:
    directory = ROOT / name
    directory.mkdir(exist_ok=False)
    started = time.monotonic()
    reference = (APP / 'test_pic' / f'{name}.png').read_bytes()
    (directory / 'reference.png').write_bytes(reference)
    reference_url = 'data:image/png;base64,' + base64.b64encode(reference).decode()
    version = hashlib.sha256((REQUEST + reference_url).encode()).hexdigest()
    write_json(directory / 'input.json', {
        'image': str(APP / 'test_pic' / f'{name}.png'), 'image_sha256': hashlib.sha256(reference).hexdigest(),
        'user_request': REQUEST, 'model': model.model_name, 'options': {key: str(value) if isinstance(value, Path) else value for key, value in asdict(options).items()},
    })
    result = {'image': name, 'run_dir': str(directory), 'status': 'planning_failed'}
    with (directory / 'console.log').open('w', encoding='utf-8') as console, analysis_console(console, 'DEBUG'), TaskStore(directory) as store:
        log_analysis(directory, '元素拆分实测开始', details={'image': name, 'model': model.model_name})
        try:
            plan, calls = flow._planning(store, model, options, REQUEST, reference_url, version)
            result['planning_model_calls'] = calls
            if plan is not None:
                write_json(directory / 'plan.json', plan.model_dump(mode='json'))
                result['elements'] = len(plan.elements)
                result['target'] = plan.target.model_dump(mode='json')
                result['directions'] = list(plan.directions)
                flow._explore(store, model, options, plan, REQUEST, reference_url, version)
        except ProbeStop as stop:
            result['status'] = str(stop)
        except Exception as error:
            result['error'] = f'{type(error).__name__}: {error}'
            log_analysis(directory, '探针运行异常', details={'error': result['error']})
        state = store.snapshot()
        write_json(directory / 'final-state.json', state)
        workers = [task for key, task in state['tasks'].items() if key != 'planning']
        result['worker_attempts'] = sum(len(task['attempts']) for task in workers)
        result['worker_model_calls'] = sum(task.get('model_calls', 0) for task in workers)
        assert result['worker_attempts'] == result['worker_model_calls'] == 0
    result['elapsed_seconds'] = round(time.monotonic() - started, 2)
    write_json(directory / 'output.json', result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    options = load_analysis_options()
    model = build_model() if not args.smoke else None
    with patch.object(flow, '_decide_last_attempt', return_value=False), patch.object(flow, '_wait_workers', capture_dispatch), patch.object(flow, 'run_exploration', forbidden), patch.object(flow, 'run_integration', forbidden), patch.object(flow, 'run_recovery', forbidden), patch.object(RoleAudit, 'history', capture_history):
        if args.smoke:
            from tempfile import TemporaryDirectory
            from tests.unit_tests.workflows.test_five_analysis import RoutingFakeModel
            from shader_deep.workflows.options import AnalysisOptions
            with TemporaryDirectory() as temporary, TaskStore(Path(temporary)) as store:
                fake = RoutingFakeModel()
                plan, _ = flow._planning(store, fake, AnalysisOptions(), REQUEST, 'data:image/png;base64,AA==', 'smoke')
                try:
                    flow._explore(store, fake, AnalysisOptions(), plan, REQUEST, 'data:image/png;base64,AA==', 'smoke')
                except ProbeStop as stop:
                    assert str(stop) == 'dispatch_prepared_not_started'
                assert len(fake.requests) == 1
                print('Smoke passed: planning only, 3 pending dispatches, 0 worker calls.')
            return
        results = []
        for name in NAMES:
            print(f'START {name}', flush=True)
            results.append(run_case(name, model, options))
            write_json(ROOT / 'summary.json', results)
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
