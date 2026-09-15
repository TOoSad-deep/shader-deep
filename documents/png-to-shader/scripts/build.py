"""Generate a plain offline HTML document and diagrams from the Markdown source."""

import html
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS = ROOT / "diagrams"
GENERATED = ROOT / "generated"
NAMES = ['architecture', 'main', 'detail1', 'detail2', 'detail3', 'detail4']


def parse_flow(source):
    nodes, edges = [], []
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith(('flowchart ', '%%')):
            continue
        node = re.fullmatch(r'(\w+)(\(\[|\[|\{)"([^"]*)"(\]\)|\]|\})', line)
        edge = re.fullmatch(r'(\w+) -->\s*(?:\|"([^"]*)"\|\s*)?(\w+)', line)
        if node:
            nodes.append({'id': node[1], 'label': node[3], 'kind': {'[': 'step', '([': 'terminal', '{': 'decision'}[node[2]]})
        elif edge:
            edges.append({'a': edge[1], 'b': edge[3], 'label': edge[2] or ''})
        else:
            raise ValueError(f'Unsupported flow syntax: {line}')
    known = {node['id'] for node in nodes}
    assert len(known) == len(nodes)
    assert all(edge['a'] in known and edge['b'] in known for edge in edges)
    return {'nodes': nodes, 'edges': edges}


def render_svg(source, name):
    flow = parse_flow(source)
    q = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ['digraph flow {', 'graph [rankdir=TB,bgcolor="white",pad="0.2",nodesep="0.4",ranksep="0.5",splines=polyline];',
             'node [fontname="PingFang SC",fontsize=16,shape=box,style="rounded,filled",fillcolor="#f5f5f5",color="#888888",margin="0.16,0.12"];',
             'edge [fontname="PingFang SC",fontsize=12,color="#777777",arrowsize=0.65];']
    order = {node['id']: i for i, node in enumerate(flow['nodes'])}
    for node in flow['nodes']:
        label = re.sub(r'^(【[^】]+】)', r'\1\n', node['label'])
        attrs = [f'label={q(label)}']
        if node['kind'] == 'decision':
            attrs += ['shape=diamond', 'style=filled']
        if node['label'].startswith('【分析'):
            attrs += ['fillcolor="#e9f1ff"']
        elif node['label'].startswith('【生成'):
            attrs += ['fillcolor="#edf6ec"']
        elif node['label'].startswith('【评审'):
            attrs += ['fillcolor="#fff2dc"']
        lines.append(f'{node["id"]} [{",".join(attrs)}];')
    for edge in flow['edges']:
        attrs = [f'label={q(edge["label"])}']
        if order[edge['b']] < order[edge['a']]:
            attrs.append('constraint=false')
        lines.append(f'{edge["a"]} -> {edge["b"]} [{",".join(attrs)}];')
    result = subprocess.run(['dot', '-Tsvg'], input='\n'.join(lines + ['}']), text=True, capture_output=True, check=True)
    svg = result.stdout[result.stdout.index('<svg'):]
    (DIAGRAMS / f'{name}.svg').write_text(svg)
    (DIAGRAMS / f'{name}.mmd').write_text(source.rstrip() + '\n')
    return flow


def inline(text):
    text = html.escape(text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    return re.sub(r'`([^`]+)`', r'<code>\1</code>', text)


def build():
    DIAGRAMS.mkdir(parents=True, exist_ok=True)
    GENERATED.mkdir(parents=True, exist_ok=True)
    lines = (ROOT / '分层生成流程.md').read_text().splitlines()
    body, toc, graphs = [], [], []
    i, graph_index, section_index = 0, 0, 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('```mermaid'):
            i += 1
            source = []
            while lines[i] != '```':
                source.append(lines[i]); i += 1
            name = NAMES[graph_index]
            code = '\n'.join(source) + '\n'
            flow = render_svg(code, name)
            graphs.append({'id': name, **flow})
            body.append(f'<figure id="{name}"><div class="diagram"><img src="diagrams/{name}.svg" alt="{name} 对应流程图"></div><figcaption><a href="diagrams/{name}.svg">查看 SVG</a> · <a href="diagrams/{name}.mmd" download>下载 Mermaid</a></figcaption></figure><details><summary>查看 Mermaid 源码</summary><pre><code>{html.escape(code)}</code></pre></details>')
            graph_index += 1
        elif line.startswith('#'):
            level = len(line) - len(line.lstrip('#'))
            title = line[level:].strip()
            anchor = f'section-{section_index}'
            section_index += 1
            body.append(f'<h{level} id="{anchor}">{inline(title)}</h{level}>')
            if level == 2:
                toc.append(f'<a href="#{anchor}">{html.escape(title)}</a>')
        elif line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                cells = [cell.strip() for cell in lines[i].strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?', cell) for cell in cells):
                    rows.append(cells)
                i += 1
            body.append('<div class="table"><table><thead><tr>' + ''.join('<th>'+inline(c)+'</th>' for c in rows[0]) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>'+inline(c)+'</td>' for c in row) + '</tr>' for row in rows[1:]) + '</tbody></table></div>')
            continue
        elif line.startswith('- '):
            items = []
            while i < len(lines) and lines[i].startswith('- '):
                items.append('<li>'+inline(lines[i][2:])+'</li>'); i += 1
            body.append('<ul>'+''.join(items)+'</ul>')
            continue
        elif line.strip():
            body.append('<p>'+inline(line)+'</p>')
        i += 1
    assert graph_index == len(NAMES)
    css = 'body{max-width:1050px;margin:32px auto;padding:0 22px;color:#222;font:16px/1.8 system-ui,-apple-system,"PingFang SC",sans-serif}h1{font-size:28px}h2{font-size:23px;margin-top:44px;border-bottom:1px solid #ddd}h3{font-size:19px}a{color:#275fa0}nav{display:flex;flex-wrap:wrap;gap:6px 20px;font-size:14px;padding:12px 0;border-bottom:1px solid #ddd}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #ddd;padding:9px;text-align:left;vertical-align:top}th{background:#f5f5f5}.table,.diagram{overflow:auto}figure{margin:20px 0}.diagram img{display:block;max-width:100%;height:auto;margin:auto}figcaption,summary{font-size:13px;color:#555}pre{padding:15px;background:#f5f5f5;overflow:auto;font-size:13px;line-height:1.6}details{margin:12px 0}summary{cursor:pointer}.download{font-size:14px}footer{margin:40px 0;color:#666;font-size:13px}@media(max-width:650px){body{padding:0 14px}.diagram img{min-width:700px;max-width:none;width:900px}h1{font-size:24px}}'
    document = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PNG → Shader：三 Agent 架构与分层生成流程</title><style>'+css+'</style></head><body><p class="download"><a href="分层生成流程.md" download>下载完整 Markdown（含 Mermaid）</a></p><nav>'+' '.join(toc)+'</nav>'+''.join(body)+'<footer>HTML 与图示由同一份 Markdown 生成。</footer></body></html>'
    (ROOT / 'index.html').write_text(document)
    (GENERATED / 'flow-content.json').write_text(json.dumps({'revision': 'V3', 'diagrams': graphs}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'graphs': len(graphs), 'html_bytes': len(document.encode()), 'output': str(ROOT)}, ensure_ascii=False))


if __name__ == '__main__':
    build()
