# 检索能力审查与修改计划（2026-10-14）

审查对象：工作树（含`bc522cf`之后尚未提交的桌面窗口与i18n改动）。审查目标：把本项目当作“日志快速检索工具”使用时的表现。本文第1、2节只记录测量与结论，第3节是修改计划，第4节是验收标准；修改由执行代理按阶段实施，审查者逐阶段核对。

环境：Windows 10 19045、Python 3.12.10、SQLite 3.49.1、系统代码页cp936、16个逻辑核。真实Journal只读使用，临时索引库放在会话scratchpad，不写用户缓存目录。测试套件217项全部通过。

## 1. 实测

本机6个版本的Journals目录，114个输入，其中105个文本文件被索引（38.9MB、458,256行），其余为`.dmp`、空文件和字节相同的副本。

| 场景 | 实测 |
| --- | --- |
| 冷建索引，8进程并行 | 6.2s；索引库84MB，约源文件2.2倍 |
| `browse --print` 冷启动 / 热启动 | 7.3s / 1.0s |
| `summary --latest` 热启动 | 0.5s |
| 单文件任意关键词查询（最大文件85,387行） | 3–11ms |
| 用API循环105个文件查同一关键词 | 0.25s |
| 对照：`grep -r` 同一批文件一次性查找 | 0.5s |

结论：一次性查一个词，grep更快；在同一批日志里反复查，本工具从第二次启动起全面占优，并附带分类、阶段与会话归组。引擎层已满足“快速”，差距在检索的交互面。

## 2. 发现

按对使用的影响排序。

1. **多词查询是相邻短语，不是同时包含。** `events`/`blocks`把整个输入当作一个短语并给末词加前缀（`viewer_index.py`）。实测`Jrn.Command`命中，`Command Jrn`为0；子串`rror`为0；纯标点`}`静默返回空列表。grep用户默认的“都包含”语义不成立，而且界面没有任何提示。
2. **段落搜索结果不带命中行。** `_search_blocks`只返回`blocks`表的行，不记录命中的行号。终端展开段落后没有高亮；Web只在标题预览里标记第一次出现。API错误段落最长200行，用户要逐页肉眼找。
3. **交互界面没有跨文件搜索。** 终端`/`与Web搜索框只搜当前文件。非交互的`browse --print --query`会遍历全部文件，但以逐文件分页输出。引擎循环全部文件只需0.25s，补一个工作区级搜索成本很低。
4. **没有命中总数。** 结果只有`has_more`，界面显示“本页N块”，无法判断一个词是稀有还是泛滥。
5. **中文片段匹配。** FTS5默认unicode61分词把连续汉字当成一个词：`不满足约束`命中11行，`满足`为0，`约束`只有4行。经确认，Journal正文为英文，中文、日文、葡萄牙文等只出现在项目文件名中，因此本轮不改分词，只在查询含CJK字符时给出提示。`trigram`分词的试验结论记录如下备用：3字符以上子串（含CJK）可命中，2字中文仍需LIKE兜底，索引体积会增大，需另做基准。
6. **正在写入的Journal是快照。** 不能跟随新内容；本轮不处理。

## 3. 修改计划

索引结构不变（查询语义、命中行与计数都在查询时计算），不需要提升`INDEX_VERSION`，用户缓存不重建。所有新增接口保持只读；查询参数继续受长度与范围校验。

### 阶段A：引擎（`viewer_index.py`、`i18n.py`）

A1. 查询解析。新增模块级函数`parse_query(query: str) -> SearchQuery`，`SearchQuery`为`dataclass`，字段：`match`（FTS5 MATCH表达式）、`terms`（供高亮的原文词列表）、`cjk`（是否含CJK字符）。规则：

- 按空白拆分；双引号包住的部分是一个短语，短语内保留空白与标点。
- 每个词/短语都写成FTS5带引号的字符串（内部`"`写成`""`），词之间用`AND`连接。
- 只有最后一个未加引号的词带前缀`*`（与现行“末词前缀”一致）；带引号的短语不加前缀。
- 一个词若不含任何字母或数字（按`str.isalnum()`判断，与unicode61的分词字符基本一致），则该词不参与检索；若所有词都不含，抛出`ValueError("搜索词不含可检索的字母或数字；标点和符号不参与分词")`。
- 空白或空字符串表示无查询，行为与现在相同。

A2. `events`与`blocks`改用`parse_query`。带查询的返回值新增：`terms`（列表）、`hit_lines`（该文件中满足分类条件的命中行数，整数）、`hint`（含CJK时为`"中文、日文等连续文字按整段匹配，只能从段首开始查找"`，否则不返回该键）。计数SQL：`events`按`lines`表与FTS子查询联合计数；`blocks`同样返回命中行数（不是段落数），文档中说明两者区别。

A3. `blocks`带查询时每个段落新增`hit_line`：该段落范围内第一个命中行。在命中驱动的循环里直接取自`hits`；在`hit_inside`路径（Add-in前缀或小分类）对每个返回的段落补一条`SELECT rowid FROM {fts} WHERE {fts} MATCH ? AND rowid BETWEEN ? AND ? ORDER BY rowid LIMIT 1`。新增方法`block_hits(file_id, start_line, end_line, query, limit=200) -> list[int]`，供终端与Web在展开的段落内跳到下一个/上一个命中。

A4. 工作区搜索。新增`search_workspace(query, category="all", per_file=3) -> dict`：遍历`catalog()`中状态为`ready`的文件，对每个文件取`hit_lines`与前`per_file`条命中行（复用`events`的行结构，含`text`）；只返回有命中的文件，按目录顺序。返回：`{"terms", "hint"?, "total_lines", "file_count", "skipped"（未就绪文件数）, "files": [{"id","name","path","role","session","session_label","hit_lines","items"}]}`。`per_file`限制1到20，`query`长度限制与现有一致。

A5. `i18n.py`为新的错误文本与提示加英文翻译（`error_text`）。

A6. 测试（`tests/test_viewer.py`）：AND语义（非相邻两词命中、相邻短语只在加引号时命中）、末词前缀、纯标点报错、`hit_line`落在段落范围内且为首个命中、`hit_lines`与逐行命中数一致、`search_workspace`跨两个文件的命中与`skipped`、未就绪文件不参与。使用现有的合成fixture。

### 阶段B：终端与CLI（`terminal.py`、`cli.py`）

B1. 状态行“搜索: 词”后追加“命中N行”；查询报错（纯标点）时在消息行显示错误文本，列表保持上一次结果。
B2. 展开带查询命中的段落时，`detail_offset`定位到`hit_line`之前约3行；展开视图中含任一`terms`的行用选中样式显示（`colored(..., selected=True)`）。新增按键`h`/`H`在当前段落内跳到下一个/上一个命中（用`block_hits`），底部按键提示行补上。
B3. 工作区搜索。按`?`进入提示“全部文件搜索关键词: ”，回车后`self.view = "workspace"`，列表显示“全部文件命中N行 / M个文件”，按会话与文件分组，每个文件最多显示`per_file=3`条命中行（行号与原文）。上下键在命中行之间移动，Enter切换到该文件并像`g`一样定位到该行（把现有goto分支抽成`goto_line(file_id, line)`复用），Esc或`?`回到段落视图。
B4. CLI新增子命令`search`：`revit-journal search "<query>" [路径…] [--category] [--per-file N] [--format text|json]`，共用`_workspace_options`与索引缓存；文本输出按会话分组，每行`路径:行号: 原文`，末尾一行总数；JSON直接输出`search_workspace`的结果加`schema_version`。未就绪或失败的文件在末尾列出，退出码与`browse --print`一致。
B5. 测试（`tests/test_terminal.py`、`tests/test_cli.py`）：`?`搜索到另一个文件并Enter定位；`h`在段落内移动到下一个命中；`search`子命令文本与JSON输出；纯标点查询给出错误而非空列表。
B6. 文档：`docs/terminal.md`与README按键表、`search`子命令说明；更新查询语义描述。

### 阶段C：Web（`viewer_server.py`、`web/app.js`、`web/index.html`、`web/style.css`）

C1. `/api/events`与`/api/blocks`原样透传新增字段。新增`/api/search?q=&category=&per_file=`，调用`search_workspace`；`items`经`_labelled`不需要，行结构与`/api/events`相同。
C2. 搜索框旁增加范围开关：“当前文件 / 全部文件”（`segmented`样式，默认当前文件）。全部文件模式下结果渲染在`#rows`：会话标题行、文件名与“命中N行”、每文件最多3条命中行；点击命中行调用现有`jumpTo(fileId, line)`；分页按钮禁用；清空关键词回到当前文件列表。
C3. `highlight()`改为按响应中的`terms`逐词、全部出现、大小写不敏感地标记；展开段落（`contextRow`）与上下文行也高亮。段落视图点击带`hit_line`的结果时用`selectBlock(item, item.hit_line)`定位到命中行。
C4. `#page-info`在有查询时显示“命中N行”，有`hint`时在`#notice`显示提示；查询报错时显示服务端错误文本（已有`notice`机制）。
C5. i18n：新增键的中英文文案；`?lang=en`下错误与提示为英文。
C6. 测试（`tests/test_viewer.py`的HTTP用例）：`/api/search`返回跨文件结果；纯标点查询返回400与错误文本；`lang=en`翻译。
C7. 文档：`docs/viewer.md`接口列表与搜索语义；README的Web段落。

### 不做

- 不改分词器、不重建索引、不改`INDEX_VERSION`。
- 不加NOT/OR语法；不做正则搜索。
- 不处理正在写入的Journal的跟随读取。

## 4. 验收

1. `PYTHONPATH=src python3 -m unittest discover -s tests -v`全部通过（Windows：`$env:PYTHONPATH='src'; py -3 -m unittest discover -s tests`）。
2. 真实数据复核（只读，临时库）：在最大文件上`Command Jrn`与`Jrn.Command`都有结果；`"Jrn.Command"`加引号后只匹配相邻短语；`}`得到错误文本；带查询的`blocks`每项都有`hit_line`且`start_line <= hit_line <= end_line`；`search_workspace("ExceptionCode")`命中1个文件，与`grep -rl`一致（去重副本除外）；全部文件搜索常见词`Jrn`在0.5s内返回。
3. 终端：`?`搜索、Enter定位、`h`/`H`跳命中，在Windows Terminal手动走一遍。
4. Web：`revit-journal view`中切到“全部文件”，搜索、点击定位、高亮；`?lang=en`文案。
5. 文档与README同步；本文第5节记录执行结果与偏差。

## 5. 执行记录

执行方式：每个阶段由一个Opus 5.5代理编码，审查者读diff、跑测试套件、用真实Journal在临时索引库上复核。

### 阶段A：引擎

- `viewer_index.py`新增`parse_query`/`SearchQuery`与常量`NO_SEARCH_TOKENS`、`CJK_HINT`、`EMPTY_QUERY`；`events`、`blocks`带查询时返回`terms`、`hit_lines`、`hint`，每个段落带`hit_line`；新增`block_hits`与`search_workspace`。`i18n.py`补英文。`tests/test_viewer.py`新增`SearchTests`，10项。
- 偏差：空白查询不报错而返回空`match`，只有`search_workspace`对空查询抛`EMPTY_QUERY`；前缀`*`加在最后一个未加引号的词上，不论其位置；`search_workspace`把源文件已变化的偏移模式文件也计入`skipped`，整个循环用一个读快照；分类为`all`时逐行视图直接从FTS取一页行号，比原来的`IN (子查询)`快。
- 已知语义：`blocks`的`hit_lines`按行计数，错误分类下续行若属于`other`分类则不计，段落仍会列出。
- 验收（最大文件85,387行，真实数据）：`Jrn.Command`与`Command Jrn`均命中（288/320行），`"Command Jrn"`加引号为0；`}`与`::`得到错误文本；50个段落的`hit_line`全部落在段落范围内；`search_workspace("ExceptionCode")`命中1个文件，与`grep -rl`去重后一致；`Jrn`全部文件105个用时109ms。测试227项通过。

### 阶段B：终端与CLI

- `terminal.py`：状态行“搜索: 词 命中N行”；纯标点查询在`/`回车前用`parse_query`检查，`refresh`也捕获启动参数带坏查询的情况，原查询与列表保留；展开带`hit_line`的段落定位到命中行前3行；命中行反色并在行号后标`*`，当前命中标`>`；`h`/`H`段内跳命中；`?`全部文件搜索视图，Enter经新抽出的`goto_line(file_id, line, query)`打开并定位，关键词成为该文件的当前搜索。`cli.py`新增`search`子命令。测试新增6项。
- 偏差：命中标记不只靠颜色；从全部文件结果跳转时保留关键词以便高亮与`h`/`H`；JSON多出`errors`列表，与`summary --format json`一致；文本输出在只列部分命中时注明“列出前N行”。
- 验收（真实数据，临时库）：`search ExceptionCode`全部版本0.7s含启动，命中journal.0026第990行；`search "Command Jrn" --latest`命中22行；`search "}"`退出码2并打印错误；JSON含`files`、`errors`。脚本驱动`Browser`：`?`→`Command Jrn`得到48个文件1673行，Enter打开journal.0038并在L5571展开，状态行“命中22行”，`h`/`H`给出段内位置；100列帧无超宽行。未在Windows Terminal中手工走一遍，交互验收由`Browser.handle`/`frame`脚本与单元测试代替。

### 阶段C：Web

- `viewer_server.py`新增`/api/search`，`hint`经`error_text`翻译；`app.js`新增范围开关“当前文件/全部文件”、全部文件结果列表（文件标题+前3条命中行，点击`jumpTo`）、按`terms`逐词全量高亮、段落结果以`hit_line`定位、底部“命中N行”、`hint`提示、服务端错误保留原列表；`index.html`、`style.css`相应改动；`docs/viewer.md`更新。测试新增1项HTTP用例（跨文件搜索、400错误中英文、`hit_line`、`hint`）。
- 偏差：全部文件范围下清空关键词只显示提示、不发请求；点左侧文件也回到当前文件范围；上下键在全部文件结果中只移动焦点，Enter打开；全部文件结果不随索引进度自动刷新，以免重置滚动位置。
- 验收：代理用无头Edge驱动页面检查了范围切换、高亮、错误保留列表、跳转定位、英文文案与深浅主题截图；审查者核对了正则特殊字符转义、无内联脚本、CSP不变。

### 总结

测试234项通过（新增17项）。索引结构未变，用户缓存无需重建。遗留：验收第3项的Windows Terminal手工走查未做；第5项中文片段匹配按用户确认降级为提示，未改分词。
