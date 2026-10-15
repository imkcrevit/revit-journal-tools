"use strict";
const $ = id => document.getElementById(id);
// `revit-journal gui` loads the page with ?desktop=1 inside WebView2; pywebview adds window.pywebview.api.
const DESKTOP = new URLSearchParams(location.search).has("desktop");
const BLOCK_PAGE = 200;
// view: "blocks" lists paragraphs like `browse` (repeats folded), "lines" every source line.
// detail: what the right panel shows, redrawn when the language changes.
// scope: search in the current "file" or in "all" files (not remembered); terms: the search terms of
// the last answer (null before the first one); shown: which list the rows show (see listKey).
const state = {files: [], file: null, view: "blocks", category: "all", phase: "all", fold: true, query: "", cursors: [0], next: 0, hasMore: false, selected: null, autoSelect: false, sequence: 0, detailSequence: 0, readyCount: -1, loaded: false, detail: null, scope: "file", terms: null, shown: null};
let listController, detailController, sessionController, pollTimer, toastTimer;

// Per-viewer preferences (language, layout, theme, collapsed versions); the page works without storage.
const prefs = {
  get(key) { try { return localStorage.getItem("rjt." + key); } catch { return null; } },
  set(key, value) { try { value === null ? localStorage.removeItem("rjt." + key) : localStorage.setItem("rjt." + key, value); } catch { /* not persisted */ } },
};

// ---- Language --------------------------------------------------------------
const I18N = {
  zh: {
    "title": "revit-journal-tool", "brand.sub": "本地日志查看器",
    "open.files": "打开文件…", "open.folder": "打开文件夹…", "open.local": "本机Journals",
    "open.local.title": "%LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit 版本\\Journals", "open.recursive": "文件夹包含子文件夹",
    "open.partial": "部分路径未打开：{errors}",
    "workspace": "工作区", "files.count": "{count}个文件", "files.filter": "筛选文件名或目录", "files.list": "日志文件列表",
    "files.loading": "正在读取文件列表…", "files.nomatch": "没有匹配的文件。", "files.none": "工作区没有文件。",
    "files.none.desktop": "尚未打开文件。\n使用上方按钮打开Journal文件或文件夹。",
    "sort.desc": "最新在前", "sort.asc": "最早在前", "sort.title": "按会话时间排序，点击切换",
    "groups.collapse": "全部折叠", "groups.expand": "全部展开",
    "version": "Revit {year}", "version.unknown": "未知版本", "version.count": "{sessions}个会话 · {files}个文件",
    "foot": "本机读取 · 原文件只读",
    "split.sidebar": "调整左侧工作区宽度", "split.panels": "调整列表与原文的宽度", "split.hint": "拖动调整宽度，双击恢复默认",
    "file.none": "未选择文件", "status.connecting": "正在连接本地工作区", "status.empty": "本地 · 未打开文件",
    "status.ready": "本地 · {count}个文件已就绪", "status.indexing": "索引中 {ready}/{total}", "status.failed": " · {count}个失败",
    "connect.failed": "无法连接本地查看器：{error}",
    "theme.switch": "切换主题", "theme.system": "跟随系统", "theme.light": "浅色", "theme.dark": "深色",
    "theme.title": "主题：{name}（点击切换）", "theme.toast": "主题：{name}",
    "select.title": "选择一个Journal", "select.hint": "在左侧选择文件，按记录类型快速定位。", "session.show": "会话摘要",
    "metric.lines": "记录行数", "metric.errors": "错误相关", "metric.api": "API错误", "metric.operations": "操作记录",
    "categories": "记录分类", "all": "全部", "origin": "来源", "addin": "Add-in",
    "view.mode": "显示方式", "view.blocks": "段落", "view.lines": "逐行",
    "search": "搜索", "search.label": "搜索当前分类", "search.blocks": "搜索关键词，命中所在的段落只列一次",
    "search.lines": "搜索关键词，如ExceptionCode、GeomStepList",
    "phase": "阶段", "phase.filter": "按会话阶段筛选段落", "phase.all": "全部阶段", "fold": "折叠重复段落",
    "jump": "跳至行", "jump.placeholder": "行号", "jump.label": "跳至行号",
    "heading.blocks": "全部段落", "heading.lines": "全部记录",
    "col.line": "行号", "col.first": "起始行", "col.category": "分类", "col.origin": "来源",
    "col.text": "原文记录", "col.block": "段落（同一事件的连续记录）", "col.addin": "Add-in名称",
    "page.previous": "← 上一页", "page.next": "下一页 →", "page.info": "第{page}页 · {count}{unit}",
    "unit.blocks": "段", "unit.lines": "条", "page.search": " · 搜索结果", "page.folded": " · 相同段落已折叠",
    "list.indexing": "正在建立磁盘索引。可先查看左侧已经就绪的文件。", "list.failed": "读取失败：{error}",
    "list.changed": "源文件已经变化。当前显示已索引的版本，重新打开即可更新索引。",
    "list.empty.blocks": "当前分类、阶段和关键词下没有段落。", "list.empty.lines": "当前分类和关键词下没有记录。",
    "list.error": "查询失败，请调整关键词后重试。", "list.blank": "（空白段落）",
    "workspace.empty": "工作区还没有Journal", "workspace.empty.desktop": "打开Journal文件或包含Journal的文件夹；原文件只读，不会被修改。",
    "workspace.empty.browser": "启动查看器时传入文件或目录，例如revit-journal view <目录>。",
    "workspace.empty.context": "打开文件后，在这里查看段落原文。",
    "inspector.context": "原文上下文", "inspector.block": "段落原文", "inspector.session": "会话摘要",
    "detail.hint.blocks": "点击一个段落，查看它的完整原文。", "detail.hint.lines": "点击一条记录，查看它前后发生了什么。",
    "detail.empty": "选择记录查看连续原文，也可以输入行号直接定位。",
    "source.default": "记录分类用于定位；错误候选仍需结合上下文复核。", "source.line": "索引完整原文，分类仅用于定位。",
    "source.block": "段落只是阅读分组，不代表同一原因。",
    "source.session": "与命令行summary相同的会话摘要；行号指本会话的主Journal，点击可定位。",
    "line": "第{line}行", "range": "第{start}–{end}行", "unclosed": "段落未闭合", "from.line": "从第{line}行显示",
    "fold.info": "相同内容共{count}段（时间戳与计数不同），起始行：{lines}", "list.sep": "、",
    "more": "继续显示本段落（已显示到第{shown}行，共到第{end}行）",
    "note.cut": "\n[显示前{shown}个字符，本行共{total}个，完整记录仍在源文件中]", "note.degraded": "\n[含无法解码字节，以\\xNN显示]",
    "enc.degraded": "（退化解码{count}行，字节以\\xNN显示）", "enc.unlikely": "（编码存疑：内容不像中文，可能是乱码）",
    "enc.basis": "（{basis}）", "enc.fallback": "自动回退",
    "session.unknown": "结束状态未知", "session.findings": "会话级发现{count}项，均为候选，需结合原文复核",
    "session.link": "定位到主Journal第{line}行",
    "copy.text": "复制原文", "copy.position": "复制位置", "copied": "已复制", "copy.failed": "无法写入剪贴板，可以选中原文后按Ctrl+C复制。",
    "addin.unnamed": "未提供名称的Add-in", "addin.truncated": "名称/Class摘要截断",
    "row.title": "{range} · {label}", "row.fold": " · 相同内容共{count}段，最后一段在第{last}行", "row.cut": " [预览已截断]",
    "row.hit": " · 首个命中在第{line}行",
    "scope.label": "搜索范围", "scope.file": "当前文件", "scope.all": "全部文件",
    "search.all": "在全部已就绪的文件中搜索，如ExceptionCode",
    "search.syntax": "多个词须出现在同一行，顺序不限；加英文双引号的\"短语\"须相邻；最后一个词也按前缀匹配；标点不参与搜索",
    "page.hits": " · 命中{count}行", "search.hits": "命中{count}行", "heading.all": "全部文件 · 搜索结果",
    "search.all.prompt": "输入关键词，在全部已就绪的文件中搜索。", "search.all.empty": "全部文件中没有符合当前分类和关键词的记录。",
    "search.all.info": "命中{lines}行 · {files}个文件", "search.all.skipped": " · 跳过{count}个",
    "search.all.skipped.title": "未完成索引、索引来自旧版本或源文件已变化的文件不参与搜索",
  },
  en: {
    "title": "revit-journal-tool", "brand.sub": "Revit journal viewer",
    "open.files": "Open files…", "open.folder": "Open folder…", "open.local": "Local journals",
    "open.local.title": "%LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <version>\\Journals", "open.recursive": "Include subfolders",
    "open.partial": "Some paths were not opened: {errors}",
    "workspace": "Workspace", "files.count": "{count} file{s}", "files.filter": "Filter by file or folder name", "files.list": "Log files",
    "files.loading": "Reading the file list…", "files.nomatch": "No matching files.", "files.none": "The workspace has no files.",
    "files.none.desktop": "No files open yet.\nUse the buttons above to open journal files or a folder.",
    "sort.desc": "Newest first", "sort.asc": "Oldest first", "sort.title": "Sort by session time; click to switch",
    "groups.collapse": "Collapse all", "groups.expand": "Expand all",
    "version": "Revit {year}", "version.unknown": "Unknown version", "version.count": "{sessions} session{ss} · {files} file{fs}",
    "foot": "Read locally · sources are read-only",
    "split.sidebar": "Resize the workspace", "split.panels": "Resize the list and the text", "split.hint": "Drag to resize, double-click to reset",
    "file.none": "No file selected", "status.connecting": "Connecting to the local workspace", "status.empty": "Local · no files open",
    "status.ready": "Local · {count} file{s} ready", "status.indexing": "Indexing {ready}/{total}", "status.failed": " · {count} failed",
    "connect.failed": "Cannot reach the local viewer: {error}",
    "theme.switch": "Switch theme", "theme.system": "System", "theme.light": "Light", "theme.dark": "Dark",
    "theme.title": "Theme: {name} (click to switch)", "theme.toast": "Theme: {name}",
    "select.title": "Select a journal", "select.hint": "Select a file on the left, then filter by record type.", "session.show": "Session summary",
    "metric.lines": "Lines", "metric.errors": "Error-related", "metric.api": "API errors", "metric.operations": "Operations",
    "categories": "Record categories", "all": "All", "origin": "Origin", "addin": "Add-ins",
    "view.mode": "View", "view.blocks": "Paragraphs", "view.lines": "Lines",
    "search": "Search", "search.label": "Search the current category", "search.blocks": "Search words; a paragraph is listed once",
    "search.lines": "Search words, e.g. ExceptionCode, GeomStepList",
    "phase": "Phase", "phase.filter": "Filter paragraphs by session phase", "phase.all": "All phases", "fold": "Fold repeats",
    "jump": "Go to line", "jump.placeholder": "Line", "jump.label": "Go to line number",
    "heading.blocks": "All paragraphs", "heading.lines": "All records",
    "col.line": "Line", "col.first": "First line", "col.category": "Category", "col.origin": "Origin",
    "col.text": "Source text", "col.block": "Paragraph (consecutive records of one event)", "col.addin": "Add-in name",
    "page.previous": "← Previous", "page.next": "Next →", "page.info": "Page {page} · {count} {unit}",
    "unit.blocks": "paragraph{s}", "unit.lines": "line{s}", "page.search": " · search results", "page.folded": " · repeats folded",
    "list.indexing": "Building the index. Files already ready on the left can be viewed now.", "list.failed": "Could not read: {error}",
    "list.changed": "The source file has changed. The indexed version is shown; open it again to update the index.",
    "list.empty.blocks": "No paragraphs for this category, phase and search.", "list.empty.lines": "No records for this category and search.",
    "list.error": "The query failed; adjust the search and try again.", "list.blank": "(blank paragraph)",
    "workspace.empty": "No journals in the workspace yet", "workspace.empty.desktop": "Open journal files or a folder with journals; sources are only read, never changed.",
    "workspace.empty.browser": "Pass files or folders when starting the viewer, e.g. revit-journal view <folder>.",
    "workspace.empty.context": "Open a file to read its paragraphs here.",
    "inspector.context": "Context", "inspector.block": "Paragraph text", "inspector.session": "Session summary",
    "detail.hint.blocks": "Select a paragraph to read all of its lines.", "detail.hint.lines": "Select a record to see what happened around it.",
    "detail.empty": "Select a record to read the source, or enter a line number.",
    "source.default": "Categories help locating; error candidates still need checking in context.", "source.line": "Full source text from the index; categories only help locating.",
    "source.block": "A paragraph only groups lines for reading; it does not mean one cause.",
    "source.session": "The same session summary as the summary command; line numbers refer to the session's main journal, click to go there.",
    "line": "Line {line}", "range": "Lines {start}–{end}", "unclosed": "paragraph not closed", "from.line": "shown from line {line}",
    "fold.info": "{count} paragraphs with the same content (timestamps and counters differ), first lines: {lines}", "list.sep": ", ",
    "more": "Show more of this paragraph (shown up to line {shown}, it ends on line {end})",
    "note.cut": "\n[first {shown} of {total} characters shown; the full line is in the source file]", "note.degraded": "\n[contains undecodable bytes, shown as \\xNN]",
    "enc.degraded": " ({count} lines decoded with fallback, bytes shown as \\xNN)", "enc.unlikely": " (doubtful encoding: does not look like Chinese, possibly garbled)",
    "enc.basis": " ({basis})", "enc.fallback": "automatic fallback",
    "session.unknown": "End state unknown", "session.findings": "{count} session finding{s}, all candidates to check against the journal",
    "session.link": "Go to line {line} of the main journal",
    "copy.text": "Copy text", "copy.position": "Copy location", "copied": "Copied", "copy.failed": "Cannot write to the clipboard; select the text and press Ctrl+C.",
    "addin.unnamed": "Add-in without a name", "addin.truncated": "name/class shortened",
    "row.title": "{range} · {label}", "row.fold": " · {count} paragraphs with the same content, the last on line {last}", "row.cut": " [preview shortened]",
    "row.hit": " · first match on line {line}",
    "scope.label": "Search scope", "scope.file": "This file", "scope.all": "All files",
    "search.all": "Search every ready file, e.g. ExceptionCode",
    "search.syntax": "Every word must occur in the same line, in any order; words in \"double quotes\" must be adjacent; the last word also matches as a prefix; punctuation is not searched",
    "page.hits": " · {count} matching line{s}", "search.hits": "{count} matching line{s}", "heading.all": "All files · search results",
    "search.all.prompt": "Enter a search term to search every ready file.", "search.all.empty": "No lines in any file match this category and search.",
    "search.all.info": "{lines} matching line{s} · {files} file{fs}", "search.all.skipped": " · {count} skipped",
    "search.all.skipped.title": "Files not indexed yet, indexed by an earlier version, or changed since indexing are not searched",
  },
};
const LABELS = {
  zh: {
    category: {startup: "启动", system: "系统数据", error: "错误", api_error: "API错误", operation: "操作内容", warning: "警告", api: "API活动", other: "其他记录", dialog: "对话框"},
    origin: {autodesk: "Autodesk", third_party: "第三方", unknown: "来源待定"},
    phase: {startup: "启动", licensing: "许可", addin_loading: "Add-in加载", document_open: "打开文档", interactive: "交互操作", shutdown: "退出"},
    role: {main: "主Journal", abbrev: "精简Journal", worker: "Worker日志", steel_log: "钢连接错误日志", copy: "副本", dump: "崩溃转储"},
    basis: {explicit: "按指定编码", bom: "按BOM", "utf-8": "严格UTF-8", language_code: "按Journal声明的界面语言{code}", chinese_text: "内容为常用中文字符", not_chinese: "gb18030可以解码但内容不像中文，改用后续候选", candidate: "前面的候选无法严格解码", unlikely_chinese: "只有gb18030能严格解码，但内容不像中文，可能是乱码", degraded: "所有候选都无法完整解码"},
  },
  en: {
    category: {startup: "Startup", system: "System data", error: "Error", api_error: "API error", operation: "Operation", warning: "Warning", api: "API activity", other: "Other", dialog: "Dialog"},
    origin: {autodesk: "Autodesk", third_party: "Third-party", unknown: "Unknown origin"},
    phase: {startup: "Startup", licensing: "Licensing", addin_loading: "Add-in loading", document_open: "Opening documents", interactive: "Interactive", shutdown: "Shutdown"},
    role: {main: "Main journal", abbrev: "Abbreviated journal", worker: "Worker log", steel_log: "Steel connection log", copy: "Copy", dump: "Crash dump"},
    basis: {explicit: "as specified", bom: "by BOM", "utf-8": "strict UTF-8", language_code: "by the UI language {code} declared in the journal", chinese_text: "common Chinese characters", not_chinese: "gb18030 decodes it but it does not look like Chinese; a later candidate is used", candidate: "earlier candidates could not decode it strictly", unlikely_chinese: "only gb18030 decodes it strictly, but it does not look like Chinese: possibly garbled", degraded: "no candidate decodes it completely"},
  },
};
const CATEGORY_KEYS = Object.keys(LABELS.zh.category);
const ORIGINS = ["addin_autodesk", "addin_third_party", "addin_unknown"];
const ROLE_ORDER = {main: 0, abbrev: 1, worker: 2, steel_log: 3, unrecognized: 4, copy: 5};
let lang = I18N[prefs.get("lang")] ? prefs.get("lang") : (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";

// English plural endings: {s} (count), {ss} (sessions), {fs} (files); Chinese has none.
const plural = value => Number(value) === 1 ? "" : "s";
function t(key, vars = {}) {
  vars = {s: plural(vars.count), ss: plural(vars.sessions), fs: plural(vars.files), ...vars};
  const text = I18N[lang][key] ?? I18N.zh[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? "");
}
function label(kind, key) { return LABELS[lang][kind]?.[key] ?? LABELS.zh[kind]?.[key] ?? key; }
function number(value) { return Number(value || 0).toLocaleString(lang === "zh" ? "zh-CN" : "en-US"); }
function filterLabel(key) {
  if (key === "all") return t("all");
  if (key === "addin") return t("addin");
  return key.startsWith("addin_") ? label("origin", key.slice(6)) : label("category", key);
}
function rangeText(start, end) {
  return start === end ? t("line", {line: number(start)}) : t("range", {start: number(start), end: number(end)});
}
function applyStatic() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.title = t("title");
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
  for (const [attribute, data] of [["placeholder", "i18nPlaceholder"], ["title", "i18nTitle"], ["aria-label", "i18nAriaLabel"]]) {
    for (const node of document.querySelectorAll(`[data-${attribute === "aria-label" ? "i18n-aria-label" : "i18n-" + attribute}]`)) node.setAttribute(attribute, t(node.dataset[data]));
  }
  $("lang-zh").setAttribute("aria-pressed", String(lang === "zh"));
  $("lang-en").setAttribute("aria-pressed", String(lang === "en"));
  for (const option of $("phase").options) option.textContent = option.value === "all" ? t("phase.all") : label("phase", option.value);
  applyTheme(theme);
}
function setLang(next) {
  if (next === lang) return;
  lang = next; prefs.set("lang", next);
  applyStatic(); renderView(); renderFiles(); renderStatus(); renderSort();
  if (currentFile()) {renderOverview(); loadList(); loadSession(); redrawDetail();}
  else if (state.loaded && !state.files.length) emptyWorkspace();
  else resetInspector();
}

// ---- Shared helpers --------------------------------------------------------
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(message = "") { $("notice").textContent = message; $("notice").hidden = !message; }
function toast(message) {
  clearTimeout(toastTimer); $("toast").textContent = message; $("toast").hidden = !message;
  if (message) toastTimer = setTimeout(() => {$("toast").hidden = true;}, 9000);
}
function currentFile() { return state.files.find(file => file.id === state.file); }
async function api(path, params = {}, signal) {
  const response = await fetch(path + "?" + new URLSearchParams({...params, lang}), {signal});
  const data = await response.json();
  // server: the message is the server's (e.g. a rejected query), not a failed connection.
  if (!response.ok) throw Object.assign(new Error(data.error || "HTTP " + response.status), {status: response.status, server: Boolean(data.error)});
  return data;
}
function empty(target, message, error = false) {
  $(target).replaceChildren(element("div", "empty" + (error ? " error-text" : ""), message));
}
function isAddin(category) { return category === "addin" || category.startsWith("addin_"); }

// ---- Workspace: Revit versions > sessions > files --------------------------
function fileStatus(file) {
  if (file.status === "ready") return [number(file.line_count) + (lang === "zh" ? "行" : " lines"), "ready"];
  if (file.status === "indexing") return [Math.min(100, Math.round(file.indexed_bytes / Math.max(file.size, 1) * 100)) + "%", "indexing"];
  if (file.status === "error") return [lang === "zh" ? "读取失败" : "failed", "error"];
  return [lang === "zh" ? "待索引" : "queued", "indexing"];
}
function sessionTime(session) {
  const main = session.files.find(file => file.role === "main") || session.files[0];
  const start = Date.parse(main.summary?.start_time || "");
  return Number.isFinite(start) ? start : Math.max(...session.files.map(file => (file.mtime_ns || 0) / 1e6));
}
function versionGroups(files) {
  const sessions = new Map();
  for (const file of files) {  // the catalog lists each session's files together
    if (!sessions.has(file.session)) sessions.set(file.session, {key: file.session, label: file.session_label, directory: file.session_directory, files: []});
    sessions.get(file.session).files.push(file);
  }
  const groups = new Map(), order = prefs.get("sort") === "asc" ? 1 : -1;
  for (const session of sessions.values()) {
    session.files.sort((a, b) => (ROLE_ORDER[a.role] ?? 9) - (ROLE_ORDER[b.role] ?? 9));
    session.time = sessionTime(session);
    // A session's version is its main journal's (worker logs have no header of their own).
    const year = (session.files.find(file => file.role === "main") || session.files.find(file => file.revit_year))?.revit_year;
    const key = year ? String(year) : "unknown";
    if (!groups.has(key)) groups.set(key, {key, year, sessions: []});
    groups.get(key).sessions.push(session);
  }
  for (const group of groups.values()) group.sessions.sort((a, b) => order * (a.time - b.time));
  return [...groups.values()].sort((a, b) => (b.year || 0) - (a.year || 0));
}
function collapsedSet() { try { return new Set(JSON.parse(prefs.get("collapsed") || "[]")); } catch { return new Set(); } }
function saveCollapsed(set) { prefs.set("collapsed", set.size ? JSON.stringify([...set]) : null); }
function shortTime(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "";
  const date = new Date(ms), pad = value => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function fileButton(file) {
  const button = element("button", "file-button" + (file.id === state.file ? " active" : ""));
  button.type = "button"; button.title = file.path; button.dataset.fileId = file.id;
  button.setAttribute("aria-pressed", String(file.id === state.file));
  button.append(element("span", "name", file.name));
  const sub = element("span", "sub"), [text, kind] = fileStatus(file);
  const live = file.writing ? (lang === "zh" ? " · 正在写入" : " · being written") : file.live ? (lang === "zh" ? " · 实时快照" : " · live snapshot") : "";
  const role = file.role in LABELS.zh.role ? label("role", file.role) : file.kind;  // unrecognized names: Journal/Worker
  sub.append(element("span", "", role + " · " + (file.size / 1024 / 1024).toFixed(1) + " MB" + live), element("span", kind, text));
  button.append(sub);
  button.addEventListener("click", () => selectFile(file.id));
  return button;
}
function renderFiles() {
  const needle = $("file-search").value.trim().toLowerCase();
  const scroll = $("files").scrollTop, collapsed = collapsedSet();
  const fragment = document.createDocumentFragment();
  for (const group of versionGroups(state.files)) {
    const sessions = group.sessions.map(session => ({...session, shown: session.files.filter(file => !needle || file.path.toLowerCase().includes(needle))}))
      .filter(session => session.shown.length);
    if (!sessions.length) continue;
    const open = Boolean(needle) || !collapsed.has(group.key);
    const box = element("section", "version-group" + (open ? "" : " collapsed"));
    const head = element("button", "version-head"); head.type = "button";
    head.setAttribute("aria-expanded", String(open));
    head.append(element("span", "chevron"), element("span", "version-name", group.year ? t("version", {year: group.year}) : t("version.unknown")),
      element("span", "version-count", t("version.count", {sessions: sessions.length, files: sessions.reduce((sum, session) => sum + session.shown.length, 0)})));
    head.addEventListener("click", () => {
      const set = collapsedSet(); set.has(group.key) ? set.delete(group.key) : set.add(group.key); saveCollapsed(set); renderFiles();
    });
    box.append(head);
    if (open) {
      const body = element("div", "version-body");
      for (const session of sessions) {
        const sessionHead = element("div", "session-head");
        sessionHead.title = session.directory;
        sessionHead.append(element("span", "session-name", session.label), element("span", "session-time", shortTime(session.time)));
        body.append(sessionHead, ...session.shown.map(fileButton));
      }
      box.append(body);
    }
    fragment.append(box);
  }
  if (!fragment.childNodes.length) fragment.append(element("div", "files-empty", state.files.length ? t("files.nomatch") : DESKTOP ? t("files.none.desktop") : t("files.none")));
  $("files").replaceChildren(fragment); $("files").scrollTop = scroll;
  $("file-count").textContent = t("files.count", {count: state.files.length});
  const allCollapsed = versionGroups(state.files).every(group => collapsed.has(group.key));
  $("toggle-groups").textContent = allCollapsed ? t("groups.expand") : t("groups.collapse");
}
function renderSort() {
  const ascending = prefs.get("sort") === "asc";
  $("sort").textContent = (ascending ? t("sort.asc") + " ↑" : t("sort.desc") + " ↓");
  $("sort").title = t("sort.title");
}
function renderStatus() {
  const ready = state.files.filter(file => file.status === "ready").length;
  const failed = state.files.filter(file => file.status === "error").length;
  $("index-status").textContent = !state.loaded ? t("status.connecting") : !state.files.length ? t("status.empty")
    : (ready === state.files.length ? t("status.ready", {count: ready}) : t("status.indexing", {ready, total: state.files.length})) + (failed ? t("status.failed", {count: failed}) : "");
}

// ---- Categories, with add-in origins under one Add-in filter -----------------
function renderCategories() {
  const file = currentFile(), blocks = state.view === "blocks";
  const counts = (blocks ? file?.block_counts : file?.counts) || {};
  const fragment = document.createDocumentFragment();
  const chip = (key, count, active, onClick, className = "category") => {
    const button = element("button", className + (active ? " active" : ""));
    button.type = "button"; button.dataset.key = key; button.id = "cat-" + key;
    button.style.setProperty("--dot", `var(--c-${isAddin(key) ? "api" : key})`);
    button.setAttribute("aria-pressed", String(active));
    button.append(document.createTextNode(filterLabel(key)), element("span", "count", number(count)));
    button.addEventListener("click", onClick);
    return button;
  };
  const choose = key => () => {state.category = key; state.cursors = [0]; renderCategories(); renderView(); loadList();};
  fragment.append(chip("all", blocks ? file?.block_count : file?.line_count, state.category === "all", choose("all")));
  for (const key of CATEGORY_KEYS) fragment.append(chip(key, counts[key], state.category === key, choose(key)));
  if (blocks && (counts.addin || isAddin(state.category))) {
    fragment.append(chip("addin", counts.addin, isAddin(state.category), choose("addin")));
    if (isAddin(state.category)) {
      // Origins of the add-in list: one filter, not three more categories.
      const origins = element("span", "origin-filter");
      origins.append(element("span", "origin-label", t("origin")));
      for (const key of ["addin", ...ORIGINS]) {
        if (key !== "addin" && !counts[key] && state.category !== key) continue;
        const pill = chip(key, counts[key], state.category === key, choose(key), "origin-pill");
        if (key === "addin") pill.firstChild.textContent = t("all");
        origins.append(pill);
      }
      fragment.append(origins);
    }
  }
  $("categories").replaceChildren(fragment);
}
function renderView() {
  // All-files hits are lines, whatever the view: phase and folding do not apply to them.
  const all = state.scope === "all", blocks = state.view === "blocks", lines = all || !blocks, addins = !lines && isAddin(state.category);
  $("view-blocks").setAttribute("aria-pressed", String(blocks));
  $("view-lines").setAttribute("aria-pressed", String(!blocks));
  $("scope-file").setAttribute("aria-pressed", String(!all));
  $("scope-all").setAttribute("aria-pressed", String(all));
  $("phase-wrap").hidden = lines; $("fold-wrap").hidden = lines;
  $("col-line").textContent = lines ? t("col.line") : t("col.first");
  $("col-category").textContent = addins ? t("col.origin") : t("col.category");
  $("col-text").textContent = addins ? t("col.addin") : lines ? t("col.text") : t("col.block");
  $("search").placeholder = all ? t("search.all") : blocks ? t("search.blocks") : t("search.lines");
  $("search").setAttribute("aria-label", all ? t("search.all") : t("search.label"));
}
function setScope(scope) {
  if (state.scope === scope) return;
  // Switching scope searches what the box holds now.
  state.scope = scope; state.query = $("search").value; state.cursors = [0];
  renderView(); loadList();
}
function setView(view) {
  if (state.view === view) return;
  state.view = view; state.cursors = [0];
  if (view === "lines" && !CATEGORY_KEYS.includes(state.category)) state.category = "all";
  renderView(); renderCategories(); resetInspector(); loadList();
}
function renderOverview() {
  const file = currentFile(); if (!file) return;
  $("file-title").textContent = file.name; $("file-path").textContent = file.path;
  $("file-path").title = file.path; $("crumb-file").textContent = file.name;
  $("metric-lines").textContent = number(file.line_count);
  $("metric-errors").textContent = number((file.counts.error || 0) + (file.counts.api_error || 0));
  $("metric-api").textContent = number(file.counts.api_error);
  $("metric-operations").textContent = number(file.counts.operation);
  renderCategories();
}
function resetInspector() {
  state.selected = null; state.detail = null; state.detailSequence++; detailController?.abort();
  $("copy-position").disabled = true; $("copy-text").disabled = true; $("inspector-title").textContent = t("inspector.context");
  $("detail-meta").textContent = state.view === "blocks" ? t("detail.hint.blocks") : t("detail.hint.lines");
  empty("context", t("detail.empty"));
  $("source-meta").textContent = t("source.default");
}
function selectFile(id) {
  // A file picked on the left, or a hit opened from all files, is listed on its own (same search).
  if (state.scope !== "file") {state.scope = "file"; renderView();}
  state.file = id; state.cursors = [0]; state.autoSelect = true;
  resetInspector(); renderFiles(); renderOverview(); loadList(); loadSession();
}
function resetWorkspace() {
  // Another set of inputs replaced the workspace: forget the previous files.
  state.files = []; state.file = null; state.readyCount = -1; state.cursors = [0]; state.loaded = false;
  state.scope = "file"; state.shown = null; renderView();
  listController?.abort(); sessionController?.abort();
  $("file-title").textContent = t("select.title"); $("file-path").textContent = t("select.hint");
  $("crumb-file").textContent = t("file.none"); $("session-label").textContent = "";
  for (const id of ["metric-lines", "metric-errors", "metric-api", "metric-operations"]) $(id).textContent = "—";
  $("show-session").disabled = true; $("categories").replaceChildren(); $("page-info").textContent = "";
  resetInspector(); empty("rows", t("files.loading")); renderFiles(); notice();
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
function fillPhases(phases) {
  if ($("phase").options.length > 1) return;
  for (const key of Object.keys(phases)) $("phase").append(new Option(label("phase", key), key));
}
function emptyWorkspace() {
  const box = element("div", "empty");
  box.append(element("strong", "", t("workspace.empty")), element("p", "", DESKTOP ? t("workspace.empty.desktop") : t("workspace.empty.browser")));
  if (DESKTOP) {
    const open = element("button", "button primary", t("open.files")); open.type = "button";
    open.addEventListener("click", () => openWith("open_files"));
    box.append(open);
  }
  $("rows").replaceChildren(box);
  empty("context", t("workspace.empty.context"));
}
async function refreshCatalog() {
  clearTimeout(pollTimer);
  try {
    const previous = currentFile()?.status;
    const data = await api("/api/files");
    state.files = data.files; fillPhases(data.phases);
    const ready = state.files.filter(file => file.status === "ready").length;
    const first = !state.loaded; state.loaded = true; renderStatus();
    if (state.file === null && state.files.length) {
      // Newest main journal first: the server lists sessions newest first.
      const preferred = state.files.find(file => file.role === "main" && file.size > 0) || state.files[0];
      state.readyCount = ready; selectFile(preferred.id);
    } else if (!state.files.length) {
      renderFiles(); if (first) emptyWorkspace();
    } else {
      renderFiles(); renderOverview();
      if (state.scope === "file" && previous !== currentFile()?.status) loadList();
      // A session summary comes from its main journal, which may finish later.
      if (ready !== state.readyCount) {state.readyCount = ready; loadSession();}
    }
  } catch (error) { notice(t("connect.failed", {error: error.message})); }
  pollTimer = setTimeout(refreshCatalog, 2500);
}

// ---- List --------------------------------------------------------------------
// Search terms split as the server does (viewer_index.parse_query): "phrases" and words with a letter or digit.
function queryTerms(query) {
  return [...query.matchAll(/"([^"]*)(?:"|$)|([^\s"]+)/g)].map(match => match[2] ?? match[1].trim()).filter(term => /[\p{L}\p{N}]/u.test(term));
}
let marks = {key: null, pattern: null};
function termPattern() {
  // The terms of the last answer; before the first one, the query as typed.
  const terms = state.terms ?? queryTerms(state.query), key = terms.join("\n");
  if (key !== marks.key) {
    const words = [...new Set(terms)].filter(Boolean).sort((a, b) => b.length - a.length);
    marks = {key, pattern: words.length ? new RegExp(words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "giu") : null};
  }
  return marks.pattern;
}
// Marks every occurrence of every term, ignoring case; where several start at one place, the longest.
function highlight(node, text) {
  const pattern = termPattern();
  if (!pattern || !text) {node.textContent = text; return;}
  let at = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > at) node.append(text.slice(at, match.index));
    node.append(element("mark", "", match[0]));
    at = match.index + match[0].length;
  }
  if (at < text.length) node.append(text.slice(at));
}
function badge(category) { return element("span", "badge " + category, label("category", category)); }
// A line of this file (click: its context) or, with `file`, an all-files hit (click: open that file at the line).
function lineRow(item, file = null) {
  const row = element("button", "log-row" + (!file && item.line_number === state.selected ? " selected" : ""));
  row.type = "button";
  if (file) {row.dataset.file = file.id; row.dataset.hit = item.line_number;} else row.dataset.line = item.line_number;
  row.append(element("span", "line-no", number(item.line_number)));
  const tag = element("span"); tag.append(badge(item.category)); row.append(tag);
  const text = element("span", "log-text"), body = element("span"); highlight(body, item.text); text.append(body); row.append(text);
  row.title = (file ? `${file.path}:${item.line_number}\n` : "") + item.text + (item.text_length > item.text.length ? t("row.cut") : "");
  row.addEventListener("click", () => file ? jumpTo(file.id, item.line_number) : selectLine(item.line_number));
  return row;
}
// The heading of one file's hits; not a row to select.
function hitGroup(file) {
  const head = element("div", "hit-group");
  head.title = file.path;
  // A file outside any recognized session is its own session: its name once.
  if (file.session_label && file.session_label !== file.name) head.append(element("span", "hit-session", file.session_label), element("span", "hit-sep", "·"));
  head.append(element("span", "hit-file", file.name), element("span", "hit-count", t("search.hits", {count: number(file.hit_lines)})));
  return head;
}
function blockLabel(item) {
  return item.addin_origin ? `${t("addin")} · ${label("origin", item.addin_origin)}` : label("category", item.category);
}
function blockRow(item) {
  const row = element("button", "log-row" + (item.start_line === state.selected ? " selected" : ""));
  row.type = "button"; row.dataset.line = item.start_line;
  row.append(element("span", "line-no", number(item.start_line)));
  const tag = element("span"), text = element("span", "log-text"), body = element("span");
  if (item.addin_origin) {
    // An add-in load: origin and name; the record kind only when it is an error.
    tag.append(element("span", "badge origin-" + item.addin_origin, label("origin", item.addin_origin)));
    highlight(body, item.addin_name || item.addin_class || t("addin.unnamed")); text.append(body);
    if (["error", "api_error", "warning"].includes(item.category)) text.append(badge(item.category));
    if (item.addin_identity_truncated) text.append(element("span", "phase-tag", t("addin.truncated")));
  } else {
    tag.append(badge(item.category));
    if (item.phase) text.append(element("span", "phase-tag", label("phase", item.phase)));
    highlight(body, item.title || t("list.blank")); text.append(body);
  }
  if (item.fold_count > 1) text.append(element("span", "fold-count", "×" + number(item.fold_count)));
  row.append(tag, text);
  row.title = t("row.title", {range: rangeText(item.start_line, item.end_line), label: blockLabel(item)})
    + (item.fold_count > 1 ? t("row.fold", {count: item.fold_count, last: number(item.fold_last_line)}) : "")
    + (item.hit_line ? t("row.hit", {line: number(item.hit_line)}) : "");
  // A search result opens at its first matching line.
  row.addEventListener("click", () => selectBlock(item, item.hit_line || 0));
  return row;
}
// Which list the rows show: a query the server rejects keeps the rows of the same list.
function listKey() {
  return state.scope === "all" ? "all|" + state.category : [state.file, state.view, state.category, state.phase, state.fold].join("|");
}
function listFailed(error) {
  notice(error.message);
  if (error.server && state.shown === listKey()) return;
  state.shown = null; $("page-info").textContent = ""; $("page-info").title = ""; $("query-time").textContent = "";
  empty("rows", error.server ? error.message : t("list.error"), true);
}
async function loadList() {
  if (state.scope === "all") {loadWorkspace(); return;}
  const sequence = ++state.sequence; listController?.abort(); listController = new AbortController();
  const file = currentFile(); if (!file) return;
  const blocks = state.view === "blocks";
  $("record-heading").textContent = (state.category === "all" ? (blocks ? t("heading.blocks") : t("heading.lines"))
    : state.category.startsWith("addin_") ? `${t("addin")} · ${filterLabel(state.category)}` : filterLabel(state.category))
    + (blocks && state.phase !== "all" ? " · " + label("phase", state.phase) : "");
  $("previous").disabled = true; $("next").disabled = true;
  if (file.status !== "ready") {
    const message = file.status === "error" ? t("list.failed", {error: file.error}) : t("list.indexing");
    state.shown = null; notice(message); empty("rows", message, file.status === "error"); return;
  }
  const changed = file.source_changed ? t("list.changed") : "";
  notice(changed);
  $("rows").classList.add("loading");
  try {
    const params = {file: state.file, category: state.category, q: state.query, after: state.cursors.at(-1), limit: 100};
    const data = blocks ? await api("/api/blocks", {...params, phase: state.phase, fold: state.fold ? 1 : 0}, listController.signal)
                        : await api("/api/events", params, listController.signal);
    if (sequence !== state.sequence) return;
    state.hasMore = data.has_more; state.next = data.next_after; state.terms = data.terms || [];
    // A CJK search is matched per run of characters: the server says so (hint).
    notice([changed, data.hint].filter(Boolean).join(" "));
    const fragment = document.createDocumentFragment();
    for (const item of data.items) fragment.append(blocks ? blockRow(item) : lineRow(item));
    $("rows").replaceChildren(fragment); $("rows").scrollTop = 0;
    if (!data.items.length) empty("rows", blocks ? t("list.empty.blocks") : t("list.empty.lines"));
    $("query-time").textContent = data.elapsed_ms + " ms";
    $("page-info").textContent = t("page.info", {page: state.cursors.length, count: data.items.length, unit: t(blocks ? "unit.blocks" : "unit.lines", {count: data.items.length})})
      + ("hit_lines" in data ? t("page.hits", {count: number(data.hit_lines)}) : state.query ? t("page.search") : blocks && state.fold ? t("page.folded") : "");
    $("page-info").title = ""; state.shown = listKey();
    $("previous").disabled = state.cursors.length === 1;
    $("next").disabled = !data.has_more;
    if (state.autoSelect && data.items.length) {
      // A newly opened file shows its first entry without a click.
      state.autoSelect = false;
      blocks ? selectBlock(data.items[0], data.items[0].hit_line || 0) : selectLine(data.items[0].line_number);
    }
  } catch (error) { if (error.name !== "AbortError" && sequence === state.sequence) listFailed(error); }
  finally { if (sequence === state.sequence) $("rows").classList.remove("loading"); }
}
// All files: one search over every ready file (/api/search), up to 3 hits per file.
async function loadWorkspace() {
  const sequence = ++state.sequence; listController?.abort(); listController = new AbortController();
  // Hits are lines: a paragraph-only filter (Add-in) searches every category.
  const category = CATEGORY_KEYS.includes(state.category) ? state.category : "all";
  $("record-heading").textContent = t("heading.all") + (category === "all" ? "" : " · " + filterLabel(category));
  $("previous").disabled = true; $("next").disabled = true; state.hasMore = false; $("page-info").title = "";
  if (!state.query.trim()) {
    state.terms = []; state.shown = null; notice();
    $("page-info").textContent = ""; $("query-time").textContent = "";
    empty("rows", t("search.all.prompt")); return;
  }
  $("rows").classList.add("loading");
  try {
    const data = await api("/api/search", {q: state.query, category, per_file: 3}, listController.signal);
    if (sequence !== state.sequence) return;
    state.terms = data.terms; notice(data.hint || "");
    const fragment = document.createDocumentFragment();
    for (const file of data.files) fragment.append(hitGroup(file), ...file.items.map(item => lineRow(item, file)));
    $("rows").replaceChildren(fragment); $("rows").scrollTop = 0;
    if (!data.files.length) empty("rows", t("search.all.empty"));
    $("query-time").textContent = data.elapsed_ms + " ms";
    $("page-info").textContent = t("search.all.info", {count: data.total_lines, lines: number(data.total_lines), files: number(data.file_count)})
      + (data.skipped ? t("search.all.skipped", {count: number(data.skipped)}) : "");
    $("page-info").title = data.skipped ? t("search.all.skipped.title") : "";
    state.shown = listKey();
  } catch (error) { if (error.name !== "AbortError" && sequence === state.sequence) listFailed(error); }
  finally { if (sequence === state.sequence) $("rows").classList.remove("loading"); }
}

// ---- Detail ------------------------------------------------------------------
function contextRow(item, selected) {
  const row = element("div", "context-line" + (item.line_number === selected ? " selected" : ""));
  row.dataset.line = item.line_number; row.dataset.text = item.text;
  const pre = element("pre"); highlight(pre, item.text);
  if (item.text_length > item.text.length) pre.append(element("span", "note", t("note.cut", {shown: number(item.text.length), total: number(item.text_length)})));
  if (item.degraded) pre.append(element("span", "note", t("note.degraded")));
  row.append(element("span", "line-no", number(item.line_number)), pre);
  return row;
}
function scrollToSelected() {
  const selectedRow = $("context").querySelector(".context-line.selected");
  $("context").scrollTop = 0;
  if (selectedRow) $("context").scrollTop = Math.max(0, selectedRow.getBoundingClientRect().top - $("context").getBoundingClientRect().top - $("context").clientHeight * .35);
}
function basisLabel(basis) {
  const [key, code] = String(basis || "").split(":");
  return (label("basis", key) || "").replace("{code}", code || "");
}
function sourceMeta(data, note) {
  const decoding = data.decode_degraded_line_count ? t("enc.degraded", {count: number(data.decode_degraded_line_count)})
    : data.encoding_basis === "unlikely_chinese" ? t("enc.unlikely")
    : data.encoding_fallback ? t("enc.basis", {basis: (data.encoding_basis && basisLabel(data.encoding_basis)) || t("enc.fallback")}) : "";
  $("source-meta").textContent = `${data.encoding.toUpperCase()}${decoding} · SHA-256 ${data.sha256.slice(0, 20)}… · ${note}`;
  $("source-meta").title = data.sha256;
}
function markSelected(line) {
  state.selected = line;
  for (const row of document.querySelectorAll(".log-row")) row.classList.toggle("selected", Number(row.dataset.line) === line);
}
function detailReady() { $("copy-position").disabled = false; $("copy-text").disabled = false; }
function meta(text) { return element("span", "rule-label", text); }
async function selectLine(line) {
  const sequence = ++state.detailSequence; detailController?.abort(); detailController = new AbortController();
  state.detail = {kind: "line", line};
  markSelected(line); $("inspector-title").textContent = t("inspector.context");
  try {
    const data = await api("/api/context", {file: state.file, line, radius: 8}, detailController.signal);
    if (sequence !== state.detailSequence) return;
    const selected = data.items.find(item => item.line_number === line);
    $("detail-meta").replaceChildren(element("strong", "", `${t("line", {line: number(line)})} · ${selected ? label("category", selected.category) : ""}`));
    if (selected?.rules) $("detail-meta").append(meta(selected.rules.split(",").join(" · ")));
    const fragment = document.createDocumentFragment();
    for (const item of data.items) fragment.append(contextRow(item, line));
    $("context").replaceChildren(fragment); scrollToSelected();
    sourceMeta(data, t("source.line"));
    detailReady();
  } catch (error) { if (error.name !== "AbortError" && sequence === state.detailSequence) notice(error.message); }
}
async function selectBlock(block, focus = 0) {
  const sequence = ++state.detailSequence; detailController?.abort(); detailController = new AbortController();
  state.detail = {kind: "block", block, focus};
  markSelected(block.start_line); $("inspector-title").textContent = t("inspector.block");
  // A located line deep inside a long paragraph: start the page a little before it.
  const offset = focus ? Math.max(0, focus - block.start_line - 20) : 0;
  try {
    const data = await api("/api/block", {file: state.file, start: block.start_line, offset, limit: BLOCK_PAGE}, detailController.signal);
    if (sequence !== state.detailSequence) return;
    const info = data.block, addin = Boolean(info.addin_origin);
    const parts = [rangeText(info.start_line, info.end_line), blockLabel(info)];
    if (addin && ["error", "api_error", "warning"].includes(info.category)) parts.push(label("category", info.category));
    // An add-in's phase is (nearly always) add-in loading: not repeated.
    if (info.phase && !(addin && info.phase === "addin_loading")) parts.push(label("phase", info.phase));
    if (!info.complete) parts.push(t("unclosed"));
    $("detail-meta").replaceChildren(element("strong", "", parts.join(" · ")));
    if (addin && info.addin_class && info.addin_class !== info.addin_name) $("detail-meta").append(meta("Class: " + info.addin_class));
    if (info.rules) $("detail-meta").append(meta(info.rules.split(",").join(" · ")));
    if (offset) $("detail-meta").append(meta(t("from.line", {line: number(info.start_line + offset)})));
    $("context").replaceChildren(); appendBlockLines(data, offset, focus, sequence);
    if (focus) scrollToSelected();
    sourceMeta(data, t("source.block"));
    detailReady();
    if (block.fold_count > 1) showFold(info, sequence);
  } catch (error) { if (error.name !== "AbortError" && sequence === state.detailSequence) notice(error.message); }
}
function appendBlockLines(data, offset, focus, sequence) {
  const fragment = document.createDocumentFragment();
  for (const item of data.items) fragment.append(contextRow(item, focus));
  $("context").append(fragment);
  if (!data.has_more) return;
  const more = element("button", "button small more", t("more", {shown: number(data.items.at(-1).line_number), end: number(data.block.end_line)}));
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
    const lines = data.start_lines.map(number).join(t("list.sep")) + (data.truncated ? "…" : "");
    $("detail-meta").append(meta(t("fold.info", {count: number(data.count), lines})));
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
  // From all files: back to that file's own list, keeping the search.
  if (fileId !== state.file || state.scope !== "file") selectFile(fileId);
  state.autoSelect = false;
  locate(line);
}
async function showSession() {
  const sequence = ++state.detailSequence; detailController?.abort(); detailController = new AbortController();
  state.detail = {kind: "session"};
  markSelected(null);
  try {
    const data = await api("/api/session", {file: state.file}, detailController.signal);
    if (sequence !== state.detailSequence) return;
    $("inspector-title").textContent = t("inspector.session");
    const endState = data.end_state && lang === "en" ? {clean: "Ended normally", no_clean_exit: "Did not end normally", live: "Being written"}[data.end_state] : data.end_state_label;
    $("detail-meta").replaceChildren(element("strong", "", endState || t("session.unknown")), meta(t("session.findings", {count: data.finding_count})));
    const fragment = document.createDocumentFragment();
    data.lines.forEach((text, index) => {
      // The server says which main-journal line a summary line names.
      const line = data.refs?.[index];
      const row = element(line ? "button" : "div", "summary-line" + (line ? " link" : ""), text);
      if (line) {
        row.type = "button"; row.title = t("session.link", {line: number(line)});
        row.addEventListener("click", () => jumpTo(data.main_id, line));
      }
      fragment.append(row);
    });
    $("context").replaceChildren(fragment); $("context").scrollTop = 0;
    $("source-meta").textContent = t("source.session");
    $("copy-position").disabled = true; $("copy-text").disabled = false;
  } catch (error) { if (error.name !== "AbortError" && sequence === state.detailSequence) notice(error.message); }
}
function redrawDetail() {
  const detail = state.detail;
  if (!detail) resetInspector();
  else if (detail.kind === "block") selectBlock(detail.block, detail.focus);
  else if (detail.kind === "line") selectLine(detail.line);
  else showSession();
}
async function copy(text, button, key) {
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = t("copied"); setTimeout(() => {button.textContent = t(key);}, 1200);
  } catch { toast(t("copy.failed")); }
}

// ---- Desktop window: system dialogs through pywebview ------------------------
async function openWith(method, ...args) {
  const desktop = window.pywebview?.api;
  if (!desktop) return;
  const buttons = [...document.querySelectorAll("#desktop-actions button")];
  buttons.forEach(button => {button.disabled = true;});
  try {
    const result = await desktop[method](...args, lang);
    if (!result || result.cancelled) return;
    if (result.error) {toast(result.error); return;}
    if (result.errors?.length) toast(t("open.partial", {errors: result.errors.join("; ")}));
    resetWorkspace();
    await refreshCatalog();
  } catch (error) { toast(String(error?.message || error)); }
  finally { buttons.forEach(button => {button.disabled = false;}); }
}
function enableDesktop() {
  if (!DESKTOP || !window.pywebview?.api) return;
  for (const id of ["open-files", "open-folder", "open-local"]) $(id).disabled = false;
}

// ---- Resizable columns: drag, arrow keys, double click resets; widths are remembered.
function splitter(handle, {key, initial, step, clamp, fromPointer, apply}) {
  let value = Number(prefs.get(key));
  value = Number.isFinite(value) && value > 0 ? clamp(value) : initial;
  apply(value);
  const save = () => prefs.set(key, String(value));
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    event.preventDefault();
    try { handle.setPointerCapture(event.pointerId); } catch { /* keeps working while over the handle */ }
    handle.classList.add("dragging"); document.body.classList.add("resizing");
    const move = moved => {value = clamp(fromPointer(moved.clientX)); apply(value);};
    const stop = () => {
      handle.removeEventListener("pointermove", move);
      handle.classList.remove("dragging"); document.body.classList.remove("resizing"); save();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop, {once: true});
    handle.addEventListener("pointercancel", stop, {once: true});
  });
  handle.addEventListener("dblclick", () => {value = initial; apply(value); prefs.set(key, null);});
  handle.addEventListener("keydown", event => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault(); value = clamp(value + (event.key === "ArrowRight" ? step : -step)); apply(value); save();
  });
  window.addEventListener("resize", () => {value = clamp(value); apply(value);});
}
splitter($("split-sidebar"), {
  key: "sidebar", initial: 300, step: 16,
  clamp: width => Math.round(Math.max(220, Math.min(width, Math.max(220, Math.min(640, innerWidth - 640))))),
  fromPointer: x => x - $("app").getBoundingClientRect().left,
  apply: width => $("app").style.setProperty("--sidebar-w", width + "px"),
});
splitter($("split-panels"), {
  key: "records", initial: 0.56, step: 0.02,
  clamp: share => Math.max(0.22, Math.min(share, 0.8)),
  fromPointer: x => {
    const panels = $("panels"), box = panels.getBoundingClientRect(), style = getComputedStyle(panels);
    const left = box.left + parseFloat(style.paddingLeft), width = box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    return (x - left) / Math.max(width, 1);
  },
  apply: share => $("panels").style.setProperty("--records-w", (share * 100).toFixed(2) + "%"),
});

// ---- Theme: follow the system, or a remembered light/dark choice.
const THEMES = ["system", "light", "dark"];
let theme = THEMES.includes(prefs.get("theme")) ? prefs.get("theme") : "system";
function applyTheme(next) {
  if (next === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = next;
  $("theme").title = t("theme.title", {name: t("theme." + next)});
}
$("theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme); prefs.set("theme", theme === "system" ? null : theme); toast(t("theme.toast", {name: t("theme." + theme)}));
});

$("lang-zh").addEventListener("click", () => setLang("zh"));
$("lang-en").addEventListener("click", () => setLang("en"));
$("sort").addEventListener("click", () => {prefs.set("sort", prefs.get("sort") === "asc" ? null : "asc"); renderSort(); renderFiles();});
$("toggle-groups").addEventListener("click", () => {
  const keys = versionGroups(state.files).map(group => group.key), set = collapsedSet();
  saveCollapsed(keys.every(key => set.has(key)) ? new Set() : new Set(keys)); renderFiles();
});
$("file-search").addEventListener("input", renderFiles);
$("search-form").addEventListener("submit", event => {event.preventDefault(); state.query = $("search").value; state.cursors = [0]; loadList();});
$("jump-form").addEventListener("submit", event => {event.preventDefault(); const line = Number($("jump").value); if (line >= 1) locate(line);});
$("next").addEventListener("click", () => {if (state.hasMore) {state.cursors.push(state.next); loadList();}});
$("previous").addEventListener("click", () => {if (state.cursors.length > 1) {state.cursors.pop(); loadList();}});
$("view-blocks").addEventListener("click", () => setView("blocks"));
$("view-lines").addEventListener("click", () => setView("lines"));
$("scope-file").addEventListener("click", () => setScope("file"));
$("scope-all").addEventListener("click", () => setScope("all"));
$("phase").addEventListener("change", () => {state.phase = $("phase").value; state.cursors = [0]; loadList();});
$("fold").addEventListener("change", () => {state.fold = $("fold").checked; state.cursors = [0]; loadList();});
$("show-session").addEventListener("click", showSession);
$("copy-position").addEventListener("click", () => copy(`${currentFile().path}:${state.selected}`, $("copy-position"), "copy.position"));
$("copy-text").addEventListener("click", () => {
  const rows = [...$("context").querySelectorAll(".context-line")];
  const text = rows.length ? rows.map(row => row.dataset.text).join("\n") : [...$("context").querySelectorAll(".summary-line")].map(row => row.textContent).join("\n");
  copy(text, $("copy-text"), "copy.text");
});
// Up/down in the list moves the selection and shows the entry.
$("rows").addEventListener("keydown", event => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const rows = [...$("rows").querySelectorAll(".log-row")]; if (!rows.length) return;
  event.preventDefault();
  const index = rows.indexOf(document.activeElement);
  const next = rows[index < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))];
  next.focus(); next.scrollIntoView({block: "nearest"});
  // An all-files hit opens its file: there the arrows only move and Enter opens.
  if (state.scope === "file") next.click();
});
document.addEventListener("keydown", event => {
  if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === "f") {event.preventDefault(); $("search").focus(); $("search").select();}
  else if (key === "g") {event.preventDefault(); $("jump").focus(); $("jump").select();}
  else if (key === "o" && DESKTOP) {event.preventDefault(); openWith("open_files");}
});
if (DESKTOP) {
  $("desktop-actions").hidden = false;
  $("open-files").addEventListener("click", () => openWith("open_files"));
  $("open-folder").addEventListener("click", () => openWith("open_folder", $("recursive").checked));
  $("open-local").addEventListener("click", () => openWith("open_local"));
  window.addEventListener("pywebviewready", enableDesktop);
  enableDesktop();
}
window.addEventListener("beforeunload", () => clearTimeout(pollTimer));
applyStatic();
$("file-title").textContent = t("select.title"); $("file-path").textContent = t("select.hint"); $("crumb-file").textContent = t("file.none");
renderView(); renderSort(); renderStatus(); resetInspector();
refreshCatalog();
