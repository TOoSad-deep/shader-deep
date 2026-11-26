"use strict";
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="shader-token"]').content;
const labels = {running:"运行中",completed:"已完成",partial:"部分完成",failed:"分析失败",error:"运行失败",cancelled:"已停止",blocked:"需要补充输入 / 更换方案",attempt_limit:"渲染次数已用尽",model_limit:"模型调用次数已用尽",gate_a:"待确认路线",gate_b:"待验收"};
const modes = {generate:"直接生成",analyze:"全图观察",report:"方案生成",workflow:"分阶段生成"};
let jobs = [], selected = "", uploadDataUrl = "", currentPreview = "", shownCandidate = "", shown = "", submitting = false;
let shownGate = "", gateSubmitting = false, recommendedEvidence = false, numericReroute = false, refreshVersion = 0;
let shownEvidence = "";

function error(message = "") { $("error").textContent = message; $("error").hidden = !message; }
async function api(path, payload) {
  const options = payload === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Shader-Token":token},body:JSON.stringify(payload)};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}
function setImageSource(element, url) {
  if (!url) element.removeAttribute("src");
  else if (element.getAttribute("src") !== url) element.src = url;
}
function image(id, url, empty) {
  $(id).hidden = !url; $(empty).hidden = !!url;
  setImageSource($(id), url);
}
function isShowing(id, file) {
  const element = $(id);
  return Boolean(file && !element.hidden && element.complete && element.naturalWidth &&
    element.getAttribute("src") === file.url && element.currentSrc === new URL(file.url, document.baseURI).href);
}
function option(value, text) { const node = document.createElement("option"); node.value = value; node.textContent = text; return node; }
function syncOptions(id, entries) {
  const element = $(id), previous = element.value;
  const signature = JSON.stringify(entries);
  if (element.dataset.signature === signature) return;
  element.dataset.signature = signature;
  element.replaceChildren(...entries.map(([value,text]) => option(value,text)));
  if (entries.some(([value]) => value === previous)) element.value = previous;
}
function setMode() {
  const mode = $("mode").value;
  $("upload-fields").hidden = mode === "report";
  $("report-fields").hidden = mode !== "report";
  $("generation-fields").hidden = mode === "analyze" || mode === "workflow";
  $("analysis-fields").hidden = mode !== "analyze";
  $("workflow-fields").hidden = mode !== "workflow";
  for (const id of ["width","height","attempts"]) $(id).disabled = mode === "analyze" || mode === "workflow";
  $("submit").replaceChildren(document.createTextNode(mode === "analyze" || mode === "workflow" ? "开始观察" : "开始生成"), Object.assign(document.createElement("span"), {textContent:"↗"}));
  updateSketches();
}
function updateSketches() {
  const source = jobs.find(job => job.id === $("source").value);
  syncOptions("sketch", [["","请选择草图方案"], ...(source?.sketches || []).map(item => [item.id, `${item.id} · ${item.name}`])]);
  const sketch = source?.sketches.find(item => item.id === $("sketch").value);
  syncOptions("alternative", [["","默认方案"], ...(sketch?.alternatives || []).map((item,index) => [String(index), `备选 ${index + 1} · ${item.reason}`])]);
  const draft = $("mode").value === "report" ? source?.files.find(file => file.name === "reference.png")?.url : uploadDataUrl;
  $("draft-preview").hidden = !draft;
  setImageSource($("draft-image"), draft);
}
function link(file, title) {
  const anchor = document.createElement("a"); anchor.href = file.url; anchor.textContent = title; anchor.download = file.name.split("/").pop(); return anchor;
}
function workflowFile(job, path) {
  if (!path) return null;
  const absolute = path.startsWith("/") ? path : `${job.details.directory}/${path}`;
  return job.files.find(file => `${job.directory}/${file.name}` === absolute);
}
function gateInputs() {
  const job = jobs.find(item => item.id === selected);
  const sketchId = job?.status === "gate_b" ? job.details.sketch_id : $("gate-sketch").value;
  const sketch = job?.workflow.sketches?.find(item => item.id === sketchId);
  syncOptions("gate-alternative", [["","默认方案"], ...(sketch?.alternatives || []).map((item,index) => [String(index), `备选 ${index + 1} · ${item.reason}`])]);
  const action = $("gate-action").value;
  const compare = action === "compare_plans";
  const planChoices = (job?.workflow.sketches || []).flatMap(plan =>
    [[null,"默认"],...(plan.alternatives || []).map((alt,i) => [i,alt.reason])].map(([alternative_index,label]) =>
      [JSON.stringify({sketch_id:plan.id,alternative_index}),`${plan.id} · ${label}`]));
  const firstPlan = JSON.stringify({sketch_id:sketchId,alternative_index:$("gate-alternative").value === "" ? null : Number($("gate-alternative").value)});
  syncOptions("gate-rival", [["","请选择另一条路线"], ...planChoices.filter(([value]) => value !== firstPlan)]);
  $("gate-rival-fields").hidden = !compare;
  $("gate-selection-field").hidden = action !== "select_comparison";
  $("gate-route-numeric").disabled = !numericReroute;
  if (!numericReroute && $("gate-route").value === "numeric_tuning") $("gate-route").value = "";
  const route = action === "reroute" ? $("gate-route").value : "";
  const localRevision = action === "reroute" && ["code_structure","evidence_gap"].includes(route);
  syncOptions("gate-probe", [
    ["",localRevision ? "继续当前修订路线" : compare ? "对照整图" : "请选择高风险点"],
    ...(sketch?.risk_probes || []).map((item,index) => [String(index), `${item.target_ids.join("、")} · ${item.reason}`]),
    ...(localRevision ? [["manual","指定新试验或更换上下文"]] : []),
  ]);
  $("gate-probe-fields").hidden = action !== "run_probe" && !localRevision && !compare;
  $("gate-second-fields").hidden = action !== "run_probe";
  const others = (sketch?.risk_probes || []).map((item,index) => [String(index), `${item.target_ids.join("、")} · ${item.reason}`]);
  syncOptions("gate-second", [["","只做第一项"],...others.filter(([i]) => i !== $("gate-probe").value)]);
  $("gate-second-context").disabled = !$("gate-second").value;
  $("gate-probe-context").placeholder = $("gate-probe").value === "manual"
    ? '{"probe_override":{"target_ids":["F1"],"reason":"验证边缘","context_mode":"isolated_solid"},"target_region":{"left":0,"top":0,"right":128,"bottom":128}}'
    : '{"target_region":{"left":0,"top":0,"right":128,"bottom":128}}';
  const showTime = route === "evidence_gap" || (action === "revise" && recommendedEvidence);
  $("gate-time-field").hidden = !showTime;
  $("gate-route-field").hidden = action !== "reroute";
  $("gate-dispute-field").hidden = route !== "observation_error";
  $("gate-revision-fields").hidden = action !== "reroute" && !showTime;
  $("gate-plan-fields").hidden = job?.status !== "gate_a" || !["generate_scene","run_probe","compare_plans"].includes(action);
  const redraft = job?.status === "gate_a" && action === "retry_criteria";
  $("gate-reason-field").hidden = job?.status === "blocked";
  $("gate-reason").required = redraft;
  $("gate-reason-label").textContent = redraft ? "重拟原因（必填）" : "备注";
}
function element(tag,text) { const node = document.createElement(tag); node.textContent = text; return node; }
function regionText(r) { return r ? `(${r.left}, ${r.top})–(${r.right}, ${r.bottom})` : ""; }
async function gateEvidence(job,candidate,key) {
  const box = $("gate-evidence");
  const identity = `${key}-${candidate?.id || ""}`;
  if (shownEvidence === identity) return;
  shownEvidence = identity;
  box.replaceChildren();
  const folder = job.details.observation_path;
  const load = async (path) => {
    const file = workflowFile(job,path); if (!file) return null;
    const response = await fetch(file.url); if (!response.ok) throw new Error("读取材料失败");
    return response.json();
  };
  const nodes = [];
  try {
    if (job.status === "gate_a" && folder) {
      const [manifest,elements,features,relations] = await Promise.all(["manifest","elements","features","relations"].map(name => load(`${folder}/${name}.json`)));
      const facts = [...(elements || []),...(features || []),...(relations || [])];
      const summary = document.createElement("details");
      summary.append(element("summary",`观察摘要 · ${facts.length} 项`), ...facts.map(row => element("p",`${row.id} · ${row.description || [row.name,row.region].join(" · ")}`)));
      nodes.push(summary,...(manifest?.gaps || []).map(row => element("p",`观察缺口 · ${row.description}`)));
      const anchors = manifest?.reference_measurements || [];
      if (anchors.length) nodes.push(element("h3","参考图测量"), ...anchors.map(row => element("p",
        `${row.id} · ${row.question} · ${regionText(row.region)}${row.mean_rgb ? ` · RGB ${row.mean_rgb.map(n => n.toFixed(1)).join(", ")} · 亮度 ${row.mean_luma.toFixed(1)}` : " · 裁图"}`)));
    } else if (candidate?.review_path) {
      const report = await load(`${candidate.review_path}/review.json`);
      const major = (report?.submission?.issues || []).filter(row => row.severity === "major");
      if (major.length) nodes.push(element("h3","重大问题"),...major.map(row => element("p",
        `${row.source_ref || row.dimension} · ${row.region} · ${row.reference_observation} → ${row.rendered_observation} · ${row.impact}`)));
      const unassessed = report?.submission?.unassessed || [];
      if (unassessed.length) nodes.push(element("h3","待补证项"),...unassessed.map(row => element("p",
        `${row.dimension} · ${row.subject} · ${row.reason}`)));
      const samples = report?.evidence?.samples || [];
      if (samples.length) nodes.push(element("h3","参考 → 候选像素测量"),...samples.map(row => {
        const a = row.reference, b = row.rendered;
        const delta = b.mean_luma - a.mean_luma;
        return element("p",`${row.label} · ${regionText(a.region)} · 亮度 ${a.mean_luma.toFixed(1)} → ${b.mean_luma.toFixed(1)} (Δ ${delta.toFixed(1)}) · ΔRGB ${b.mean_rgb.map((v,i) => (v-a.mean_rgb[i]).toFixed(1)).join(", ")}`);
      }));
    }
  } catch { nodes.push(element("p","材料读取失败，请查看本轮制品。")); }
  if (shownGate === key && shownEvidence === identity) box.replaceChildren(...nodes);
}
async function showComparison(job,key) {
  const box = $("gate-comparison"); box.replaceChildren();
  const file = workflowFile(job,job.details.comparison_path); if (!file || job.status !== "gate_a") return;
  try {
    const record = await (await fetch(file.url)).json();
    if (shownGate !== key) return;
    box.append(element("h3","方案对照"));
    for (const row of record.items) {
      const title = `${row.plan.sketch_id} · ${row.plan.alternative == null ? "默认" : "备选 " + (row.plan.alternative + 1)}`;
      const result = document.createElement("figure");
      result.append(element("figcaption",`${title} · ${row.review_verdict || row.review_error || row.error || row.stop_reason}`));
      const preview = workflowFile(job,row.preview_path), review = workflowFile(job,row.review_report_dir && `${row.review_report_dir}/review.json`);
      if (preview) { const img = document.createElement("img"); img.alt = title; img.src = preview.url; result.append(img); }
      if (review) result.append(link(review,"查看独立评审"));
      box.append(result);
    }
  } catch { if (shownGate === key) box.textContent = "请打开对照记录查看两项结果。"; }
}
async function showDiagnosis(file, key) {
  const box = $("gate-diagnosis");
  recommendedEvidence = false;
  numericReroute = false;
  box.hidden = true;
  if (!file) return;
  try {
    const card = await (await fetch(file.url)).json();
    if (shownGate !== key || !card.items?.length) return;
    recommendedEvidence = card.items.every(item => item.route === "evidence_gap");
    numericReroute = card.items.some(item => item.route === "numeric_tuning" && item.parameter_names?.length);
    gateInputs();
    box.textContent = card.items.map(item => `${item.source_ref} · ${item.route} · ${item.target}\n${item.evidence.join("\n")}`).join("\n\n");
    box.hidden = false;
  } catch { if (shownGate === key) box.hidden = true; }
}
function renderGate(job,viewedCandidate) {
  const phase = job.status;
  const criteriaRetry = job.workflow?.criteria_retry_available;
  const blockedDraft = phase === "blocked" && criteriaRetry;
  $("workflow-panel").hidden = job.mode !== "workflow" || (!["gate_a","gate_b"].includes(phase) && !blockedDraft);
  if ($("workflow-panel").hidden) { if (job.mode !== "workflow" || phase !== "running") shownGate = ""; return; }
  const state = job.details, current = state.current_candidate;
  $("gate-submit").disabled = gateSubmitting || jobs.some(item => item.status === "running");
  const key = `${job.id}-${phase}-${state.revision}-${current?.id || ""}`;
  const viewed = viewedCandidate || current;
  const failure = job.workflow.decision_error;
  if (failure) { $("gate-error").textContent = failure; $("gate-error").hidden = false; }
  if (shownGate === key) { gateEvidence(job,viewed,key); return; }
  shownGate = key;
  gateEvidence(job,viewed,key); showComparison(job,key);
  $("gate-title").textContent = phase === "gate_a" ? "确认方案与评审标准" : blockedDraft ? "标准草拟受阻" : "验收或修订";
  $("gate-budget").textContent = [
    `大轮 ${state.round} / ${state.max_rounds}`, `剩余模型调用 ${state.remaining_model_calls}`,
    state.remaining_output_tokens == null ? "" : `剩余输出 token ${state.remaining_output_tokens}`,
    state.remaining_total_tokens == null ? "" : `剩余总 token ${state.remaining_total_tokens}`,
  ].filter(Boolean).join(" · ");
  $("gate-regressions").hidden = !current?.regressions?.length;
  $("gate-regressions").textContent = current?.regressions?.length ? `已匹配项出现回归：${current.regressions.join("、")}` : "";
  $("gate-summary").textContent = [
    state.reason, current?.verdict, current ? `轻微问题 ${current.minor_issue_count} 项` : "",
    current?.major_issues?.length ? `重大问题 ${current.major_issues.length} 项` : "",
    current?.remaining_issues?.length ? `待补证 ${current.remaining_issues.length} 项` : "",
    state.queued_probes?.length ? `晋升后依次验证：${state.queued_probes.map(p => p.target_ids.join("、")).join(" / ")}` : "",
    ...(state.escalations || []).map(item => typeof item === "string" ? item : JSON.stringify(item)),
  ].filter(Boolean).join(" · ");
  const artifacts = [
    [state.observation_path && `${state.observation_path}/README.md`, "观察与测量"],
    [state.planning_path && `${state.planning_path}/README.md`, "规划方案"],
    [current?.review_path && `${current.review_path}/review.json`, "评审与像素测量"],
    [current?.diagnosis_path,"诊断建议"],
    [state.comparison_path,"对照记录"],
    ["workflow-trace.jsonl","全流程追踪"],
    [state.vista_history_record && `history/${state.vista_history_record}`,"候选历史"],
  ];
  $("gate-review").replaceChildren(...artifacts.flatMap(([path,title]) => {
    const file = workflowFile(job,path); return file ? [link(file,title)] : [];
  }));
  showDiagnosis(workflowFile(job,current?.diagnosis_path),key);
  const actions = blockedDraft ? [["retry_criteria","重试标准草拟"]] : phase === "gate_a"
    ? [
      ...(state.comparison_candidate_ids?.length ? [["select_comparison","采用已评审的对照候选"]] : []),
      ...(state.planning_path ? [["generate_scene","确认标准并生成整图"],["run_probe","先验证高风险局部"],["compare_plans","并行对照两套路线"]] : []),
      ...(state.observation_revision_request ? [["revise_observation","复核争议观察"]] : []),
      ...(state.observation_path ? [["replan","重新规划"]] : []),
      ...(criteriaRetry ? [["retry_criteria","重新草拟评审标准"]] : []),
      ["archive","归档"],
    ]
    : [
      ...(current?.verdict ? [["accept",current.scope === "local" ? "晋升局部片段" : "验收并交付"]] : []),
      ...(current?.diagnosis_path ? [["revise","采纳建议修订"]] : []),
      ["reroute","人工改派路线"],
      ...(state.best_candidate ? [["rollback","回滚历史最佳候选"]] : []),
      ["archive","归档"],
    ];
  $("gate-action").replaceChildren(...actions.map(([id,title]) => option(id,title)));
  syncOptions("gate-sketch", [["","请选择方案"], ...(job.workflow.sketches || []).map(item => [item.id,`${item.id} · ${item.name}`])]);
  syncOptions("gate-selection",[["","请选择要继续的对照候选"], ...(state.comparison_candidate_ids || []).map(id => {
    const row = state.candidates.find(item => item.id === id);
    return [id,`${row.sketch_id} · ${row.alternative_index == null ? "默认" : "备选 " + (row.alternative_index + 1)} · ${row.verdict}`];
  })]);
  $("gate-criteria").value = job.workflow.criteria ? JSON.stringify(job.workflow.criteria,null,2) : "";
  $("gate-reason").value = $("gate-time").value = $("gate-route").value = $("gate-disputed").value = $("gate-probe-context").value = $("gate-second-context").value = "";
  $("gate-error").hidden = !failure;
  gateInputs();
}
function workflowDecision(job) {
  const action = $("gate-action").value, decision = {action,expected_revision:job.details.revision};
  const reason = job.status === "blocked" ? "" : $("gate-reason").value.trim();
  if (reason) decision.reason = reason;
  if (action === "retry_criteria" && job.status === "gate_a" && !reason) throw new Error("请填写重新草拟的原因");
  if (job.status === "gate_a" && ["generate_scene","run_probe","compare_plans"].includes(action)) {
    decision.sketch_id = $("gate-sketch").value;
    if (!decision.sketch_id) throw new Error("请选择一套整图方案");
    if ($("gate-alternative").value !== "") decision.alternative_index = Number($("gate-alternative").value);
    if ($("gate-criteria").value) decision.criteria = JSON.parse($("gate-criteria").value);
    if (action === "run_probe" && $("gate-probe").value === "") throw new Error("请选择要验证的高风险点");
    if (action === "compare_plans") {
      if (!$("gate-rival").value) throw new Error("请选择另一条对照路线");
      decision.comparison_plan = JSON.parse($("gate-rival").value);
    }
  } else if (action === "select_comparison") {
    if (!$("gate-selection").value) throw new Error("请选择要继续的已评审候选");
    decision.candidate_id = $("gate-selection").value;
  } else if (["revise","reroute"].includes(action)) {
    if (action === "reroute") {
      decision.route = $("gate-route").value;
      if (!decision.route || !decision.reason) throw new Error("请选择改派路线并说明原因");
      if (decision.route === "numeric_tuning" && !numericReroute) throw new Error("本轮诊断未指定可调整的外置参数");
    }
    if (!$("gate-time-field").hidden && $("gate-time").value !== "") decision.time = Number($("gate-time").value);
    if (decision.route === "observation_error" && $("gate-disputed").value.trim()) decision.disputed_ids = $("gate-disputed").value.split(/[\s,，、]+/).filter(Boolean);
  }
  if (!$("gate-probe-fields").hidden && $("gate-probe").value !== "") {
    const {target_region, solid_rgba, proxy_context, patch_source, probe_override} = JSON.parse($("gate-probe-context").value || "{}");
    const settings = {target_region,solid_rgba,proxy_context,patch_source};
    if (action === "run_probe" && $("gate-second").value) {
      decision.probes = [
        {...settings,probe_id:Number($("gate-probe").value)},
        {...JSON.parse($("gate-second-context").value || "{}"),probe_id:Number($("gate-second").value)},
      ];
    } else Object.assign(decision,settings);
    if ($("gate-probe").value === "manual") {
      if (!probe_override) throw new Error("请提供新试验的目标、原因及上下文模式");
      decision.probe_override = probe_override;
    } else if (!decision.probes) decision.probe_id = Number($("gate-probe").value);
  }
  if (action === "accept") {
    const candidate = job.details.current_candidate;
    if (!isShowing("reference",workflowFile(job,job.details.reference_path))) throw new Error("请等待所选任务的归档参考图加载完成。");
    if (!candidate || candidate.id !== shownCandidate || !isShowing("preview",workflowFile(job,candidate.preview_path))) {
      throw new Error("请切回本轮候选并等待预览加载完成。");
    }
    decision.candidate_id = candidate.id;
  }
  return decision;
}
function previewOptions(job) {
  if (job.mode !== "workflow") return job.files.filter(file => /\/candidate-[^/]+\.png$/.test(file.name)).map(file => ({key:file.url,file}));
  const {current_candidate: current, best_candidate: best} = job.details;
  const candidates = current?.id === best?.id ? [current] : [current,best];
  return candidates.flatMap(candidate => {
    const file = workflowFile(job,candidate?.preview_path);
    return file ? [{key:candidate.id,file,candidate}] : [];
  });
}
function renderJob(job) {
  if (!job) return;
  if (shown !== job.id) { currentPreview = ""; shown = job.id; }
  $("status").textContent = labels[job.status] || job.status; $("status").dataset.state = job.status;
  $("stop").hidden = job.status !== "running";
  $("task-prompt").textContent = job.prompt;
  $("directory").textContent = job.directory;
  $("log").textContent = job.log || (job.status === "running" ? "模型正在处理；生成过程可能暂时没有日志，可在预览区查看已渲染的候选。" : "本任务没有控制台日志。");
  const workflow = job.mode === "workflow";
  const reference = workflow ? workflowFile(job,job.details.reference_path) : job.files.find(file => file.name === "reference.png");
  image("reference", reference?.url, "reference-empty");
  const candidates = previewOptions(job);
  const selectedId = job.details.selected_candidate_id;
  const chosenPath = job.details.blackboard?.candidates?.[selectedId]?.preview_path;
  const chosenOption = workflow ? candidates.find(item => item.key === job.details.current_candidate?.id)
    : candidates.find(item => `${job.directory}/${item.file.name}` === chosenPath);
  const selectedOption = candidates.find(item => item.key === currentPreview) || chosenOption || candidates.at(-1);
  const chosen = chosenOption?.file, preview = selectedOption?.file;
  shownCandidate = selectedOption?.candidate?.id || "";
  image("preview", preview?.url, "preview-empty");
  $("render-label").textContent = preview ? (workflow && job.status === "completed" ? "人工已验收" :
    selectedOption === chosenOption ? "模型选定 · 待你验收" : workflow ? "历史最佳预览 · 可回滚" : "候选预览 · 未选定") : "生成预览";
  $("candidates").replaceChildren(...candidates.map((item,index) => {
    const button = document.createElement("button"); button.type = "button";
    button.textContent = workflow ? (item === chosenOption ? "本轮候选" : "历史最佳") : `候选 ${index + 1}${item === chosenOption ? " · 已选定" : ""}`;
    button.setAttribute("aria-pressed", String(item === selectedOption));
    button.onclick = () => { currentPreview = item.key; renderJob(job); }; return button;
  }));
  const links = [];
  if (workflow && job.details.delivery_path) {
    const delivery = job.details.delivery_path;
    for (const [name,title] of [["shader.baked.glsl","下载单文件 GLSL"],["shader.glsl","下载基线 GLSL"],["uniforms.json","下载生效参数"],["preview.png","下载验收预览"]]) {
      const file = workflowFile(job,`${delivery}/${name}`); if (file) links.push(link(file,title));
    }
  }
  else if (chosen && workflow) {
    const candidate = job.details.current_candidate;
    for (const [path,title] of [[candidate.baked_code_path,"下载单文件 GLSL"],[candidate.code_path,"下载基线 GLSL"],[candidate.render_record_path,"下载渲染参数"]]) {
      const file = workflowFile(job,path); if (file) links.push(link(file,title));
    }
    links.push(link(chosen,"下载候选预览"));
  }
  else if (chosen) { const code = job.files.find(file => file.name === chosen.name.replace(/\.png$/, ".glsl")); if (code) links.push(link(code,"下载最终 GLSL")); links.push(link(chosen,"下载最终预览")); }
  else if (preview) { const code = job.files.find(file => file.name === preview.name.replace(/\.png$/, ".glsl")); if (code) links.push(link(code,"下载当前候选 GLSL")); }
  const run = job.files.find(file => /\/run.json$/.test(file.name)); if (run) links.push(link(run,"运行记录"));
  for (const file of job.files.filter(file => /\/report\/[^/]+\.(json|md)$/.test(file.name))) links.push(link(file,file.name.split("/").pop()));
  $("file-links").replaceChildren(...links);
  $("report-result").hidden = !job.report; $("report-text").textContent = job.report || "";
  $("use-report").hidden = !job.sketches.length;
  renderGate(job,selectedOption?.candidate);
}
async function refresh() {
  const version = ++refreshVersion;
  try {
    const latest = await api("/api/jobs");
    if (version !== refreshVersion) return;
    jobs = latest;
    if (!selected && jobs.length) selected = jobs[0].id;
    syncOptions("history", jobs.length ? jobs.map(job => [job.id, `${modes[job.mode]} · ${labels[job.status] || job.status} · ${job.id}`]) : [["","尚未开始任务"]]);
    $("history").value = selected;
    syncOptions("source", [["","请选择分析报告"], ...jobs.filter(job => job.sketches.length).map(job => [job.id, `${labels[job.status] || job.status} · ${job.prompt.slice(0,28)}`])]);
    updateSketches(); renderJob(jobs.find(job => job.id === selected));
    $("submit").disabled = submitting || jobs.some(job => job.status === "running");
  } catch (failure) { if (version === refreshVersion) error(`无法连接本地服务：${failure.message}。请确认终端中的服务仍在运行。`); }
}
$("image").addEventListener("change", async () => {
  uploadDataUrl = "";
  $("upload-label").textContent = "选择一张参考图";
  updateSketches();
  error(); const file = $("image").files[0]; if (!file) return;
  if (file.size > 12 * 1024 * 1024 || !file.name.toLowerCase().endsWith(".png")) { error("请选择不超过 12 MB 的 PNG 图片。"); return; }
  try {
    const data = await new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error("图片读取失败")); reader.readAsDataURL(file); });
    if ($("image").files[0] !== file) return;
    uploadDataUrl = data;
    $("upload-label").textContent = file.name;
    updateSketches();
  } catch (failure) { if ($("image").files[0] === file) error(failure.message); }
});
$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault(); error(); const mode = $("mode").value;
  if (mode !== "report" && !uploadDataUrl) { error("请先选择参考 PNG 图片。"); return; }
  if (mode === "report" && !$("sketch").value) { error("请先运行分析，再选择报告和草图方案。"); return; }
  submitting = true; $("submit").disabled = true;
  try {
    const result = await api("/api/jobs", {mode,prompt:$("prompt").value,image:mode === "report" ? "" : uploadDataUrl.split(",")[1],width:Number($("width").value),height:Number($("height").value),attempts:Number($("attempts").value),source:$("source").value,sketch:$("sketch").value,alternative:$("alternative").value === "" ? null : Number($("alternative").value),background:$("background").value});
    selected = result.id; currentPreview = "";
    await refresh();
  } catch (failure) { error(failure.message); }
  finally { submitting = false; $("submit").disabled = jobs.some(job => job.status === "running"); }
});
$("stop").onclick = async () => { $("stop").disabled = true; try { await api(`/api/stop/${selected}`, {}); await refresh(); } catch (failure) { error(failure.message); } finally { $("stop").disabled = false; } };
$("history").onchange = () => { selected = $("history").value; renderJob(jobs.find(job => job.id === selected)); };
$("mode").onchange = setMode; $("source").onchange = updateSketches; $("sketch").onchange = updateSketches;
$("use-report").onclick = () => { $("mode").value = "report"; $("source").value = selected; setMode(); $("prompt").value = ""; $("prompt").focus(); };
for (const id of ["gate-action","gate-sketch"]) $(id).onchange = () => {
  for (const field of ["gate-probe","gate-second","gate-rival","gate-probe-context","gate-second-context"]) $(field).value = "";
  gateInputs();
};
for (const id of ["gate-alternative","gate-route","gate-probe","gate-second"]) $(id).onchange = gateInputs;
$("gate-form").onsubmit = async (event) => {
  event.preventDefault();
  if (gateSubmitting) return;
  const job = jobs.find(item => item.id === selected), box = $("gate-error");
  box.hidden = true; gateSubmitting = true; $("gate-submit").disabled = true;
  try { await api(`/api/advance/${job.id}`,workflowDecision(job)); await refresh(); }
  catch (failure) { box.textContent = failure.message; box.hidden = false; }
  finally { gateSubmitting = false; $("gate-submit").disabled = jobs.some(item => item.status === "running"); }
};
async function poll() { await refresh(); window.setTimeout(poll,1500); }
setMode(); poll();
