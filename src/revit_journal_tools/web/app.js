"use strict";
const $ = id => document.getElementById(id);
// view: "blocks" lists paragraphs like `browse` (repeats folded), "lines" every source line.
const state = {files: [], file: null, view: "blocks", category: "all", phase: "all", fold: true, query: "", cursors: [0], next: 0, hasMore: false, selected: null, sequence: 0, detailSequence: 0, categories: {}, blockFilters: {}, phases: {}, readyCount: -1};
const number = value => Number(value || 0).toLocaleString("zh-CN");
const BLOCK_PAGE = 200;
let listController, detailController, sessionController, pollTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(message = "") { $("notice").textContent = message; $("notice").hidden = !message; }
function currentFile() { return state.files.find(file => file.id === state.file); }
function filters() { return state.view === "blocks" ? state.blockFilters : state.categories; }
async function api(path, params = {}, signal) {
  const response = await fetch(path + "?" + new URLSearchParams(params), {signal});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "读取失败");
  return data;
}
function empty(target, message, error = false) {
  $(target).replaceChildren(element("div", "empty" + (error ? " error-text" : ""), message));
}
function renderFiles() {
  const needle = $("file-search").value.toLowerCase();
  const scroll = $("files").scrollTop;
  const fragment = document.createDocumentFragment();
  for (const file of state.files.filter(file => file.path.toLowerCase().includes(needle))) {
    const button = element("button", "file-button" + (file.id === state.file ? " active" : ""));
    button.dataset.fileId = file.id; button.title = file.path;
    button.setAttribute("aria-pressed", String(file.id === state.file));
    button.append(element("span", "name", file.name));
    const sub = element("span", "sub");
    sub.append(element("span", "", file.kind + " · " + (file.size / 1024 / 1024).toFixed(1) + " MB" + (file.writing ? " · 正在写入" : file.live ? " · 实时快照" : "")));
    const label = file.status === "ready" ? "就绪" : file.status === "indexing" ? Math.min(100, Math.round(file.indexed_bytes / Math.max(file.size, 1) * 100)) + "%" : file.status === "error" ? "读取失败" : "待索引";
    sub.append(element("span", file.status, label)); button.append(sub);
    const parts = file.path.replaceAll("\\", "/").split("/");
    button.append(element("span", "folder", parts.slice(-3, -1).join(" / ")));
    button.addEventListener("click", () => selectFile(file.id)); fragment.append(button);
  }
  $("files").replaceChildren(fragment); $("files").scrollTop = scroll;
  $("file-count").textContent = state.files.length + "个文件";
}
function renderCategories() {
  const file = currentFile(), blocks = state.view === "blocks";
  const counts = (blocks ? file?.block_counts : file?.counts) || {};
  const fragment = document.createDocumentFragment();
  for (const [key, label] of Object.entries({all: "全部", ...filters()})) {
    if (blocks && key.startsWith("addin_") && !counts[key] && state.category !== key) continue;
    const button = element("button", "category" + (state.category === key ? " active" : ""));
    button.id = "cat-" + key; button.setAttribute("aria-pressed", String(state.category === key));
    button.append(document.createTextNode(label), element("span", "count", number(key === "all" ? (blocks ? file?.block_count : file?.line_count) : counts[key])));
    button.addEventListener("click", () => {state.category = key; state.cursors = [0]; renderCategories(); loadList();});
    fragment.append(button);
  }
  $("categories").replaceChildren(fragment);
}
function renderView() {
  const blocks = state.view === "blocks";
  $("view-blocks").setAttribute("aria-pressed", String(blocks));
  $("view-lines").setAttribute("aria-pressed", String(!blocks));
  $("phase-wrap").hidden = !blocks; $("fold-wrap").hidden = !blocks;
  $("col-line").textContent = blocks ? "起始行" : "行号";
  $("col-text").textContent = blocks ? "段落（同一事件的连续记录）" : "原文记录";
  $("search").placeholder = blocks ? "搜索关键词，命中所在的段落只列一次" : "搜索关键词，如ExceptionCode、GeomStepList";
}
function setView(view) {
  if (state.view === view) return;
  state.view = view; state.cursors = [0];
  if (state.category !== "all" && !(state.category in filters())) state.category = "all";
  renderView(); renderCategories(); resetInspector(); loadList();
}
function renderOverview() {
  const file = currentFile(); if (!file) return;
  $("file-title").textContent = file.name; $("file-path").textContent = file.path;
  $("file-path").title = file.path;
  $("metric-lines").textContent = number(file.line_count);
  $("metric-errors").textContent = number((file.counts.error || 0) + (file.counts.api_error || 0));
  $("metric-api").textContent = number(file.counts.api_error);
  $("metric-operations").textContent = number(file.counts.operation);
  renderCategories();
}
function resetInspector() {
  state.selected = null; state.detailSequence++; detailController?.abort();
  $("copy-position").disabled = true; $("inspector-title").textContent = "原文上下文";
  $("detail-meta").textContent = state.view === "blocks" ? "点击一个段落，查看它的完整原文。" : "点击一条记录，查看它前后发生了什么。";
  empty("context", "选择记录查看连续原文，也可以输入行号直接定位。");
  $("source-meta").textContent = "记录分类用于定位；错误候选仍需结合上下文复核。";
}
function selectFile(id) {
  state.file = id; state.cursors = [0];
  resetInspector(); renderFiles(); renderOverview(); loadList(); loadSession();
}
async function loadSession() {
  const id = state.file; sessionController?.abort(); sessionController = new AbortController();
  try {
    const data = await api("/api/session", {file: id}, sessionController.signal);
    if (id !== state.file) return;
    $("session-label").textContent = data.label; $("session-label").title = data.label;
    $("show-session").disabled = false;
  } catch (error) { if (error.name !== "AbortError" && id === state.file) {$("session-label").textContent = ""; $("show-session").disabled = true;} }
}
function fillPhases() {
  if ($("phase").options.length > 1) return;
  for (const [key, label] of Object.entries(state.phases)) $("phase").append(new Option(label, key));
}
async function refreshCatalog() {
  try {
    const previous = currentFile()?.status;
    const data = await api("/api/files"); state.files = data.files; state.categories = data.categories;
    state.blockFilters = data.block_filters; state.phases = data.phases; fillPhases();
    const ready = state.files.filter(file => file.status === "ready").length;
    const failed = state.files.filter(file => file.status === "error").length;
    $("index-status").textContent = ready === state.files.length ? `本地 · ${ready}个文件已就绪` : `索引中 ${ready}/${state.files.length}` + (failed ? ` · ${failed}个失败` : "");
    if (state.file === null && state.files.length) {
      // Newest main journal first: the server lists sessions newest first.
      const preferred = state.files.find(file => file.role === "main" && file.size > 0) || state.files[0];
      state.readyCount = ready; selectFile(preferred.id);
    } else {
      renderFiles(); renderOverview();
      if (previous !== currentFile()?.status) loadList();
      // A session summary comes from its main journal, which may finish later.
      if (ready !== state.readyCount) {state.readyCount = ready; loadSession();}
    }
  } catch (error) { notice("无法连接本地查看器：" + error.message); }
  pollTimer = setTimeout(refreshCatalog, 2500);
}
function highlight(node, text) {
  const query = state.query.trim(), at = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (at < 0) {node.textContent = text; return;}
  node.append(document.createTextNode(text.slice(0, at)), element("mark", "", text.slice(at, at + query.length)), document.createTextNode(text.slice(at + query.length)));
}
function lineRow(item) {
  const row = element("button", "log-row" + (item.line_number === state.selected ? " selected" : ""));
  row.dataset.line = item.line_number;
  row.append(element("span", "line-no", number(item.line_number)));
  const tag = element("span"); tag.append(element("span", "badge " + item.category, state.categories[item.category])); row.append(tag);
  const text = element("span", "log-text"); highlight(text, item.text); row.append(text);
  row.title = item.text + (item.text_length > item.text.length ? " [预览已截断]" : "");
  row.addEventListener("click", () => selectLine(item.line_number));
  return row;
}
function blockRow(item) {
  const row = element("button", "log-row" + (item.start_line === state.selected ? " selected" : ""));
  row.dataset.line = item.start_line;
  row.append(element("span", "line-no", number(item.start_line)));
  const tag = element("span"); tag.append(element("span", "badge " + item.category, state.categories[item.category])); row.append(tag);
  const text = element("span", "log-text");
  if (item.phase) text.append(element("span", "phase-tag", state.phases[item.phase] || item.phase));
  // Add-in loads are listed by name, like the terminal list.
  const body = element("span"); highlight(body, (item.addin_origin ? item.summary : item.title) || "（空白段落）"); text.append(body);
  if (item.fold_count > 1) text.append(element("span", "fold-count", "×" + number(item.fold_count)));
  row.append(text);
  row.title = `第${item.start_line}–${item.end_line}行 · ${item.label}` + (item.fold_count > 1 ? ` · 相同内容共${item.fold_count}段，最后一段在第${item.fold_last_line}行` : "");
  row.addEventListener("click", () => selectBlock(item));
  return row;
}
async function loadList() {
  const sequence = ++state.sequence; listController?.abort(); listController = new AbortController();
  const file = currentFile(); if (!file) return;
  const blocks = state.view === "blocks";
  $("record-heading").textContent = (state.category === "all" ? (blocks ? "全部段落" : "全部记录") : filters()[state.category]) + (blocks && state.phase !== "all" ? " · " + state.phases[state.phase] : "");
  $("previous").disabled = true; $("next").disabled = true;
  if (file.status !== "ready") {
    const message = file.status === "error" ? "读取失败：" + file.error : "正在建立磁盘索引。可先查看左侧已经就绪的文件。";
    notice(message); empty("rows", message, file.status === "error"); return;
  }
  notice(file.source_changed ? "源文件已经变化。当前显示已索引的版本，请重启查看器更新索引。" : "");
  $("rows").classList.add("loading");
  try {
    const params = {file: state.file, category: state.category, q: state.query, after: state.cursors.at(-1), limit: 100};
    const data = blocks ? await api("/api/blocks", {...params, phase: state.phase, fold: state.fold ? 1 : 0}, listController.signal)
                        : await api("/api/events", params, listController.signal);
    if (sequence !== state.sequence) return;
    state.hasMore = data.has_more; state.next = data.next_after;
    const fragment = document.createDocumentFragment();
    for (const item of data.items) fragment.append(blocks ? blockRow(item) : lineRow(item));
    $("rows").replaceChildren(fragment); $("rows").scrollTop = 0;
    if (!data.items.length) empty("rows", blocks ? "当前分类、阶段和关键词下没有段落。" : "当前分类和关键词下没有记录。");
    $("query-time").textContent = data.elapsed_ms + " ms";
    $("page-info").textContent = `第${state.cursors.length}页 · ${data.items.length}${blocks ? "段" : "条"}` + (state.query ? " · 搜索结果" : blocks && state.fold ? " · 相同段落已折叠" : "");
    $("previous").disabled = state.cursors.length === 1;
    $("next").disabled = !data.has_more;
  } catch (error) { if (error.name !== "AbortError" && sequence === state.sequence) {notice(error.message); empty("rows", "查询失败，请调整关键词后重试。", true);} }
  finally { if (sequence === state.sequence) $("rows").classList.remove("loading"); }
}
function contextRow(item, selected) {
  const row = element("div", "context-line" + (item.line_number === selected ? " selected" : ""));
  row.append(element("span", "line-no", number(item.line_number)), element("pre", "", item.text + (item.text_length > item.text.length ? "\n[显示截断，完整记录仍在索引中]" : "") + (item.degraded ? "\n[含无法解码字节，以\\xNN显示]" : "")));
  return row;
}
function scrollToSelected() {
  const selectedRow = $("context").querySelector(".context-line.selected");
  $("context").scrollTop = 0;
  if (selectedRow) $("context").scrollTop = Math.max(0, selectedRow.getBoundingClientRect().top - $("context").getBoundingClientRect().top - $("context").clientHeight * .35);
}
function sourceMeta(data, note) {
  const decoding = data.decode_degraded_line_count ? `（退化解码${data.decode_degraded_line_count}行，字节以\\xNN显示）` : data.encoding_basis === "unlikely_chinese" ? "（编码存疑：内容不像中文，可能是乱码）" : data.encoding_fallback ? `（${data.encoding_basis_label || "自动回退"}）` : "";
  $("source-meta").textContent = `${data.encoding.toUpperCase()}${decoding} · SHA-256 ${data.sha256.slice(0,20)}… · ${note}`;
  $("source-meta").title = data.sha256;
}
function markSelected(line) {
  state.selected = line;
  for (const row of document.querySelectorAll(".log-row")) row.classList.toggle("selected", Number(row.dataset.line) === line);
}
async function selectLine(line) {
  const sequence = ++state.detailSequence; detailController?.abort(); detailController = new AbortController();
  markSelected(line); $("inspector-title").textContent = "原文上下文";
  try {
    const data = await api("/api/context", {file: state.file, line, radius: 8}, detailController.signal);
    if (sequence !== state.detailSequence) return;
    const selected = data.items.find(item => item.line_number === line);
    $("detail-meta").replaceChildren(element("strong", "", `第${number(line)}行 · ${state.categories[selected?.category] || "原文"}`));
    if (selected?.rules) $("detail-meta").append(element("span", "rule-label", selected.rules.split(",").join(" · ")));
    const fragment = document.createDocumentFragment();
    for (const item of data.items) fragment.append(contextRow(item, line));
    $("context").replaceChildren(fragment); scrollToSelected();
    sourceMeta(data, "索引完整原文，分类仅用于定位。");
    $("copy-position").disabled = false;
  } catch (error) { if (error.name !== "AbortError" && sequence === state.detailSequence) notice(error.message); }
}
async function selectBlock(block, focus = 0) {
  const sequence = ++state.detailSequence; detailController?.abort(); detailController = new AbortController();
  markSelected(block.start_line); $("inspector-title").textContent = "段落原文";
  // A located line deep inside a long paragraph: start the page a little before it.
  const offset = focus ? Math.max(0, focus - block.start_line - 20) : 0;
  try {
    const data = await api("/api/block", {file: state.file, start: block.start_line, offset, limit: BLOCK_PAGE}, detailController.signal);
    if (sequence !== state.detailSequence) return;
    const info = data.block;
    $("detail-meta").replaceChildren(element("strong", "", `第${number(info.start_line)}–${number(info.end_line)}行 · ${info.label}` + (info.phase ? " · " + (state.phases[info.phase] || info.phase) : "") + (info.complete ? "" : " · 段落未闭合")));
    if (info.rules) $("detail-meta").append(element("span", "rule-label", info.rules.split(",").join(" · ")));
    if (offset) $("detail-meta").append(element("span", "rule-label", `从第${number(info.start_line + offset)}行显示`));
    $("context").replaceChildren(); appendBlockLines(data, offset, focus, sequence);
    if (focus) scrollToSelected();
    sourceMeta(data, "段落只是阅读分组，不代表同一原因。");
    $("copy-position").disabled = false;
    if (block.fold_count > 1) showFold(info, sequence);
  } catch (error) { if (error.name !== "AbortError" && sequence === state.detailSequence) notice(error.message); }
}
function appendBlockLines(data, offset, focus, sequence) {
  const fragment = document.createDocumentFragment();
  for (const item of data.items) fragment.append(contextRow(item, focus));
  $("context").append(fragment);
  if (!data.has_more) return;
  const more = element("button", "more", `继续显示本段落（已显示到第${number(data.items.at(-1).line_number)}行，共到第${number(data.block.end_line)}行）`);
  more.type = "button";
  more.addEventListener("click", async () => {
    more.disabled = true;
    try {
      const next = await api("/api/block", {file: state.file, start: data.block.start_line, offset: offset + BLOCK_PAGE, limit: BLOCK_PAGE});
      if (sequence !== state.detailSequence) return;
      more.remove(); appendBlockLines(next, offset + BLOCK_PAGE, focus, sequence);
    } catch (error) { more.disabled = false; notice(error.message); }
  });
  $("context").append(more);
}
async function showFold(block, sequence) {
  try {
    const data = await api("/api/fold", {file: state.file, fingerprint: block.fingerprint});
    if (sequence !== state.detailSequence) return;
    const lines = data.start_lines.map(number).join("、") + (data.truncated ? "…" : "");
    $("detail-meta").append(element("span", "rule-label", `相同内容共${number(data.count)}段（时间戳与计数不同），起始行：${lines}`));
  } catch (error) { /* the fold list is supplementary */ }
}
async function locate(line) {
  if (state.view !== "blocks") {selectLine(line); return;}
  try {
    const block = await api("/api/block_at", {file: state.file, line});
    selectBlock(block, line);
  } catch (error) { notice(error.message); }
}
function jumpTo(fileId, line) {
  if (fileId !== state.file) selectFile(fileId);
  locate(line);
}
async function showSession() {
  const sequence = ++state.detailSequence; detailController?.abort(); detailController = new AbortController();
  markSelected(null);
  try {
    const data = await api("/api/session", {file: state.file}, detailController.signal);
    if (sequence !== state.detailSequence) return;
    $("inspector-title").textContent = "会话摘要";
    $("detail-meta").replaceChildren(element("strong", "", data.end_state_label || "结束状态未知"), element("span", "rule-label", `会话级发现${data.finding_count}项，均为候选，需结合原文复核`));
    const fragment = document.createDocumentFragment();
    for (const text of data.lines) {
      // "第N行" and "  N | text" refer to the session's main journal.
      const match = data.main_id === null ? null : /第(\d+)行/.exec(text) || /^\s+(\d+) \|/.exec(text);
      const row = element(match ? "button" : "div", "summary-line" + (match ? " link" : ""), text);
      if (match) {
        row.type = "button"; row.title = `定位到主Journal第${match[1]}行`;
        row.addEventListener("click", () => jumpTo(data.main_id, Number(match[1])));
      }
      fragment.append(row);
    }
    $("context").replaceChildren(fragment); $("context").scrollTop = 0;
    $("source-meta").textContent = "与命令行summary相同的会话摘要；行号指本会话的主Journal，点击可定位。";
    $("copy-position").disabled = true;
  } catch (error) { if (error.name !== "AbortError" && sequence === state.detailSequence) notice(error.message); }
}
$("file-search").addEventListener("input", renderFiles);
$("search-form").addEventListener("submit", event => {event.preventDefault(); state.query = $("search").value; state.cursors = [0]; loadList();});
$("jump-form").addEventListener("submit", event => {event.preventDefault(); const line = Number($("jump").value); if (line >= 1) locate(line);});
$("next").addEventListener("click", () => {if (state.hasMore) {state.cursors.push(state.next); loadList();}});
$("previous").addEventListener("click", () => {if (state.cursors.length > 1) {state.cursors.pop(); loadList();}});
$("view-blocks").addEventListener("click", () => setView("blocks"));
$("view-lines").addEventListener("click", () => setView("lines"));
$("phase").addEventListener("change", () => {state.phase = $("phase").value; state.cursors = [0]; loadList();});
$("fold").addEventListener("change", () => {state.fold = $("fold").checked; state.cursors = [0]; loadList();});
$("show-session").addEventListener("click", showSession);
$("copy-position").addEventListener("click", async () => {try {await navigator.clipboard.writeText(`${currentFile().path}:${state.selected}`); $("copy-position").textContent = "已复制"; setTimeout(() => $("copy-position").textContent = "复制位置", 1200);} catch {notice(`${currentFile().path}:${state.selected}`);}});
window.addEventListener("beforeunload", () => clearTimeout(pollTimer));
renderView();
refreshCatalog();
