"use strict";
const $ = id => document.getElementById(id);
const state = {files: [], file: null, category: "all", query: "", cursors: [0], next: 0, hasMore: false, selected: null, sequence: 0, detailSequence: 0, categories: {}};
const number = value => Number(value || 0).toLocaleString("zh-CN");
let listController, detailController, pollTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(message = "") { $("notice").textContent = message; $("notice").hidden = !message; }
function currentFile() { return state.files.find(file => file.id === state.file); }
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
    sub.append(element("span", "", file.kind + " · " + (file.size / 1024 / 1024).toFixed(1) + " MB"));
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
  const file = currentFile(), counts = file?.counts || {};
  const fragment = document.createDocumentFragment();
  for (const [key, label] of Object.entries({all: "全部", ...state.categories})) {
    const button = element("button", "category" + (state.category === key ? " active" : ""));
    button.id = "cat-" + key; button.setAttribute("aria-pressed", String(state.category === key));
    button.append(document.createTextNode(label), element("span", "count", number(key === "all" ? file?.line_count : counts[key])));
    button.addEventListener("click", () => {state.category = key; state.cursors = [0]; renderCategories(); loadEvents();});
    fragment.append(button);
  }
  $("categories").replaceChildren(fragment);
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
function selectFile(id) {
  state.file = id; state.cursors = [0]; state.selected = null; state.detailSequence++;
  detailController?.abort(); $("copy-position").disabled = true;
  $("detail-meta").textContent = "点击一条记录，查看它前后发生了什么。";
  empty("context", "选择记录查看连续原文，也可以输入行号直接定位。");
  $("source-meta").textContent = "记录分类用于定位；错误候选仍需结合上下文复核。";
  renderFiles(); renderOverview(); loadEvents();
}
async function refreshCatalog() {
  try {
    const previous = currentFile()?.status;
    const data = await api("/api/files"); state.files = data.files; state.categories = data.categories;
    const ready = state.files.filter(file => file.status === "ready").length;
    const failed = state.files.filter(file => file.status === "error").length;
    $("index-status").textContent = ready === state.files.length ? `本地 · ${ready}个文件已就绪` : `索引中 ${ready}/${state.files.length}` + (failed ? ` · ${failed}个失败` : "");
    if (state.file === null && state.files.length) {
      const preferred = [...state.files].filter(file => file.kind === "Journal" && file.size > 0).sort((a,b) => a.size - b.size)[0] || state.files[0];
      selectFile(preferred.id);
    } else {
      renderFiles(); renderOverview();
      if (previous !== currentFile()?.status) loadEvents();
    }
  } catch (error) { notice("无法连接本地查看器：" + error.message); }
  pollTimer = setTimeout(refreshCatalog, 2500);
}
function highlight(node, text) {
  const query = state.query.trim(), at = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (at < 0) {node.textContent = text; return;}
  node.append(document.createTextNode(text.slice(0, at)), element("mark", "", text.slice(at, at + query.length)), document.createTextNode(text.slice(at + query.length)));
}
async function loadEvents() {
  const sequence = ++state.sequence; listController?.abort(); listController = new AbortController();
  const file = currentFile(); if (!file) return;
  $("record-heading").textContent = state.category === "all" ? "全部记录" : state.categories[state.category];
  $("previous").disabled = true; $("next").disabled = true;
  if (file.status !== "ready") {
    const message = file.status === "error" ? "读取失败：" + file.error : "正在建立磁盘索引。可先查看左侧已经就绪的文件。";
    notice(message); empty("rows", message, file.status === "error"); return;
  }
  notice(file.source_changed ? "源文件已经变化。当前显示已索引的版本，请重启查看器更新索引。" : "");
  $("rows").classList.add("loading");
  try {
    const data = await api("/api/events", {file: state.file, category: state.category, q: state.query, after: state.cursors.at(-1), limit: 100}, listController.signal);
    if (sequence !== state.sequence) return;
    state.hasMore = data.has_more; state.next = data.next_after;
    const fragment = document.createDocumentFragment();
    for (const item of data.items) {
      const row = element("button", "log-row" + (item.line_number === state.selected ? " selected" : ""));
      row.dataset.line = item.line_number;
      row.append(element("span", "line-no", number(item.line_number)));
      const tag = element("span"); tag.append(element("span", "badge " + item.category, state.categories[item.category])); row.append(tag);
      const text = element("span", "log-text"); highlight(text, item.text); row.append(text);
      row.title = item.text + (item.text_length > item.text.length ? " [预览已截断]" : "");
      row.addEventListener("click", () => selectLine(item.line_number)); fragment.append(row);
    }
    $("rows").replaceChildren(fragment); $("rows").scrollTop = 0;
    if (!data.items.length) empty("rows", "当前分类和关键词下没有记录。");
    $("query-time").textContent = data.elapsed_ms + " ms";
    $("page-info").textContent = `第${state.cursors.length}页 · ${data.items.length}条` + (state.query ? " · 搜索结果" : "");
    $("previous").disabled = state.cursors.length === 1;
    $("next").disabled = !data.has_more;
  } catch (error) { if (error.name !== "AbortError" && sequence === state.sequence) {notice(error.message); empty("rows", "查询失败，请调整关键词后重试。", true);} }
  finally { if (sequence === state.sequence) $("rows").classList.remove("loading"); }
}
async function selectLine(line) {
  const sequence = ++state.detailSequence; detailController?.abort(); detailController = new AbortController();
  state.selected = line;
  for (const row of document.querySelectorAll(".log-row")) row.classList.toggle("selected", Number(row.dataset.line) === line);
  try {
    const data = await api("/api/context", {file: state.file, line, radius: 8}, detailController.signal);
    if (sequence !== state.detailSequence) return;
    const selected = data.items.find(item => item.line_number === line);
    const meta = element("strong", "", `第${number(line)}行 · ${state.categories[selected?.category] || "原文"}`);
    $("detail-meta").replaceChildren(meta);
    if (selected?.rules) $("detail-meta").append(element("span", "rule-label", selected.rules.split(",").join(" · ")));
    const fragment = document.createDocumentFragment();
    for (const item of data.items) {
      const row = element("div", "context-line" + (item.line_number === line ? " selected" : ""));
      row.append(element("span", "line-no", number(item.line_number)), element("pre", "", item.text + (item.text_length > item.text.length ? "\n[显示截断，完整记录仍在索引中]" : "")));
      fragment.append(row);
    }
    $("context").replaceChildren(fragment);
    const selectedRow = $("context").querySelector(".context-line.selected");
    $("context").scrollTop = 0;
    if (selectedRow) $("context").scrollTop = Math.max(0, selectedRow.getBoundingClientRect().top - $("context").getBoundingClientRect().top - $("context").clientHeight * .35);
    $("source-meta").textContent = `${data.encoding.toUpperCase()} · SHA-256 ${data.sha256.slice(0,20)}… · 索引完整原文，分类仅用于定位。`;
    $("source-meta").title = data.sha256;
    $("copy-position").disabled = false;
  } catch (error) { if (error.name !== "AbortError" && sequence === state.detailSequence) notice(error.message); }
}
$("file-search").addEventListener("input", renderFiles);
$("search-form").addEventListener("submit", event => {event.preventDefault(); state.query = $("search").value; state.cursors = [0]; loadEvents();});
$("jump-form").addEventListener("submit", event => {event.preventDefault(); const line = Number($("jump").value); if (line >= 1) selectLine(line);});
$("next").addEventListener("click", () => {if (state.hasMore) {state.cursors.push(state.next); loadEvents();}});
$("previous").addEventListener("click", () => {if (state.cursors.length > 1) {state.cursors.pop(); loadEvents();}});
$("copy-position").addEventListener("click", async () => {try {await navigator.clipboard.writeText(`${currentFile().path}:${state.selected}`); $("copy-position").textContent = "已复制"; setTimeout(() => $("copy-position").textContent = "复制位置", 1200);} catch {notice(`${currentFile().path}:${state.selected}`);}});
window.addEventListener("beforeunload", () => clearTimeout(pollTimer));
refreshCatalog();
