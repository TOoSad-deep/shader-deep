"""大工具回执的离线边界探针; 不调用模型服务."""
import json
import tempfile
from pathlib import Path
from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from evaluate import ROOT, OPTIONS, selected, save
from tests.unit_tests.workflows.test_five_analysis import RoutingFakeModel
from shader_deep.agents.analysis.execution import run_role
from shader_deep.runtime.analysis_control import commit_submission
from shader_deep.runtime.execution import AnalysisExecution
from shader_deep.runtime.task_store import TaskStore
from shader_deep.infrastructure.llm.messages import _message

SENTINEL='END_OF_REQUIRED_EVIDENCE'
class ProbeModel(RoutingFakeModel):
    received: list[dict] = Field(default_factory=list)
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        receipts=[m for m in messages if isinstance(m,ToolMessage) and m.name=='read_item']
        if not receipts:
            msg=self._call('read_item',{},1)
        else:
            text=str(receipts[-1].content)
            self.received.append({'characters':len(text),'contains_tail':SENTINEL in text,'is_preview':'/large_tool_results/' in text})
            msg=self._call('submit_exploration',{'report':'receipt observed'},2)
        return ChatResult(generations=[ChatGeneration(message=msg)])
records=[]
for condition in ['deep_agent','create_agent']:
    with tempfile.TemporaryDirectory() as tmp, TaskStore(Path(tmp)) as store:
        store.register('worker','fixed',{})
        attempt=store.start_attempt('worker')
        @tool
        def read_item()->str:
            """读取一个已登记条目的完整正文."""
            return 'x'*90000+SENTINEL
        @tool
        def submit_exploration(report:str)->str:
            """提交读取结果."""
            return commit_submission(store,attempt,{'report':report})
        model=ProbeModel()
        with selected(condition):
            run_role(store,attempt,model,OPTIONS,AnalysisExecution(),'读取条目再提交',
                     lambda:_message({'user_request':'读取完整正文'},'data:image/png;base64,AA=='),
                     [read_item,submit_exploration],phase='worker',limit=2)
        records.append({'condition':condition,'receipt':model.received,'status':store.task_view('worker').status})
save(ROOT/'edge-probe.json',records)
print(json.dumps(records,ensure_ascii=False,indent=2))
