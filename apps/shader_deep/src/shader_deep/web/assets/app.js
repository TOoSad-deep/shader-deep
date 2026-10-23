"use strict";
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="shader-token"]').content;
const labels = {running:"运行中",completed:"已完成",partial:"部分完成",failed:"分析失败",error:"运行失败",cancelled:"已停止",blocked:"需要补充输入 / 更换方案",attempt_limit:"渲染次数已用尽",model_limit:"模型调用次数已用尽"};
const modes = {generate:"直接生成",analyze:"多视角分析",report:"方案生成"};
let jobs = [], selected = "", encoded = "", localImage = "", currentPreview = "", shown = "", submitting = false, draftReference = false;

function error(message = "") { $("error").textContent = message; $("error").hidden = !message; }
async function api(path, payload) {
  const options = payload === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Shader-Token":token},body:JSON.stringify(payload)};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}
function image(id, url, empty) { $(id).hidden = !url; $(empty).hidden = !!url; if (url && $(id).getAttribute("src") !== url) $(id).src = url; }
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
  draftReference = true;
  if (mode !== "report") image("reference",localImage,"reference-empty");
  $("upload-fields").hidden = mode === "report";
  $("report-fields").hidden = mode !== "report";
  $("generation-fields").hidden = mode === "analyze";
  $("analysis-fields").hidden = mode !== "analyze";
  for (const id of ["width","height","attempts"]) $(id).disabled = mode === "analyze";
  $("submit").replaceChildren(document.createTextNode(mode === "analyze" ? "开始分析" : "开始生成"), Object.assign(document.createElement("span"), {textContent:"↗"}));
  updateSketches();
}
function updateSketches() {
  const source = jobs.find(job => job.id === $("source").value);
  syncOptions("sketch", [["","请选择草图方案"], ...(source?.sketches || []).map(item => [item.id, `${item.id} · ${item.name}`])]);
  const sketch = source?.sketches.find(item => item.id === $("sketch").value);
  syncOptions("alternative", [["","默认方案"], ...(sketch?.alternatives || []).map((item,index) => [String(index), `备选 ${index + 1} · ${item.reason}`])]);
  if ($("mode").value === "report" && draftReference) image("reference", source?.files.find(file => file.name === "reference.png")?.url, "reference-empty");
}
function link(file, title) {
  const anchor = document.createElement("a"); anchor.href = file.url; anchor.textContent = title; anchor.download = file.name.split("/").pop(); return anchor;
}
function renderJob(job) {
  if (!job) return;
  if (shown !== job.id) { currentPreview = ""; shown = job.id; }
  $("status").textContent = labels[job.status] || job.status; $("status").dataset.state = job.status;
  $("stop").hidden = job.status !== "running";
  $("task-prompt").textContent = job.prompt;
  $("directory").textContent = job.directory;
  $("log").textContent = job.log || (job.status === "running" ? "模型正在处理；生成过程可能暂时没有日志，可在预览区查看已渲染的候选。" : "本任务没有控制台日志。");
  if (!draftReference) image("reference", job.files.find(file => file.name === "reference.png")?.url, "reference-empty");
  const candidates = job.files.filter(file => /\/candidate-[^/]+\.png$/.test(file.name));
  const selectedId = job.details.selected_candidate_id;
  const chosenPath = job.details.blackboard?.candidates?.[selectedId]?.preview_path;
  const chosen = candidates.find(file => `${job.directory}/${file.name}` === chosenPath);
  const preview = candidates.find(file => file.url === currentPreview) || chosen || candidates.at(-1);
  image("preview", preview?.url, "preview-empty");
  $("render-label").textContent = preview ? (preview === chosen ? "模型选定 · 待你验收" : "候选预览 · 未选定") : "生成预览";
  $("candidates").replaceChildren(...candidates.map((file,index) => {
    const button = document.createElement("button"); button.type = "button";
    button.textContent = `候选 ${index + 1}${file === chosen ? " · 已选定" : ""}`;
    button.setAttribute("aria-pressed", String(file === preview));
    button.onclick = () => { currentPreview = file.url; renderJob(job); }; return button;
  }));
  const links = [];
  if (chosen) { const code = job.files.find(file => file.name === chosen.name.replace(/\.png$/, ".glsl")); if (code) links.push(link(code,"下载最终 GLSL")); links.push(link(chosen,"下载最终预览")); }
  else if (preview) { const code = job.files.find(file => file.name === preview.name.replace(/\.png$/, ".glsl")); if (code) links.push(link(code,"下载当前候选 GLSL")); }
  const run = job.files.find(file => /\/run.json$/.test(file.name)); if (run) links.push(link(run,"运行记录"));
  for (const file of job.files.filter(file => /\/report\/[^/]+\.(json|md)$/.test(file.name))) links.push(link(file,file.name.split("/").pop()));
  $("file-links").replaceChildren(...links);
  $("report-result").hidden = !job.report; $("report-text").textContent = job.report || "";
  $("use-report").hidden = !job.sketches.length;
}
async function refresh() {
  try {
    jobs = await api("/api/jobs");
    if (!selected && jobs.length) selected = jobs[0].id;
    syncOptions("history", jobs.length ? jobs.map(job => [job.id, `${modes[job.mode]} · ${labels[job.status] || job.status} · ${job.id}`]) : [["","尚未开始任务"]]);
    $("history").value = selected;
    syncOptions("source", [["","请选择分析报告"], ...jobs.filter(job => job.sketches.length).map(job => [job.id, `${labels[job.status] || job.status} · ${job.prompt.slice(0,28)}`])]);
    updateSketches(); renderJob(jobs.find(job => job.id === selected));
    $("submit").disabled = submitting || jobs.some(job => job.status === "running");
  } catch (failure) { error(`无法连接本地服务：${failure.message}。请确认终端中的服务仍在运行。`); }
}
$("image").addEventListener("change", async () => {
  encoded = ""; error(); const file = $("image").files[0]; if (!file) return;
  if (file.size > 12 * 1024 * 1024 || !file.name.toLowerCase().endsWith(".png")) { error("请选择不超过 12 MB 的 PNG 图片。"); return; }
  try {
    encoded = await new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error("图片读取失败")); reader.readAsDataURL(file); });
    if (localImage) URL.revokeObjectURL(localImage); localImage = URL.createObjectURL(file);
    draftReference = true; $("upload-label").textContent = file.name; image("reference",localImage,"reference-empty");
  } catch (failure) { error(failure.message); }
});
$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault(); error(); const mode = $("mode").value;
  if (mode !== "report" && !encoded) { error("请先选择参考 PNG 图片。"); return; }
  if (mode === "report" && !$("sketch").value) { error("请先运行分析，再选择报告和草图方案。"); return; }
  submitting = true; $("submit").disabled = true;
  try {
    const result = await api("/api/jobs", {mode,prompt:$("prompt").value,image:mode === "report" ? "" : encoded,width:Number($("width").value),height:Number($("height").value),attempts:Number($("attempts").value),perspectives:Number($("perspectives").value),source:$("source").value,sketch:$("sketch").value,alternative:$("alternative").value === "" ? null : Number($("alternative").value),background:$("background").value});
    selected = result.id; currentPreview = ""; draftReference = false;
    await refresh();
  } catch (failure) { error(failure.message); }
  finally { submitting = false; $("submit").disabled = jobs.some(job => job.status === "running"); }
});
$("stop").onclick = async () => { $("stop").disabled = true; try { await api(`/api/stop/${selected}`, {}); await refresh(); } catch (failure) { error(failure.message); } finally { $("stop").disabled = false; } };
$("history").onchange = () => { draftReference = false; selected = $("history").value; renderJob(jobs.find(job => job.id === selected)); };
$("mode").onchange = setMode; $("source").onchange = () => { draftReference = true; updateSketches(); }; $("sketch").onchange = updateSketches;
$("use-report").onclick = () => { $("mode").value = "report"; $("source").value = selected; setMode(); $("prompt").value = ""; $("prompt").focus(); };
async function poll() { await refresh(); window.setTimeout(poll,1500); }
setMode(); draftReference = false; poll();
