# Revit Journal Tools

[English](README.en.md) | 中文

本地Revit Journal查看器：在CLI终端中按完整段落和事件浏览大型日志，用颜色区分启动、Add-in、退出、错误、API错误、操作内容等分类，支持折叠/展开、搜索和原始行号跳转。同一报错的多行内容与堆栈保留在一个块中。

Add-in加载支持Autodesk、第三方和来源待定筛选，按同一事件内的Vendor字段及其后续行判断来源，并保留原始依据行号。加载失败仍可在原有错误分类中查看。

按`i`（或`2`）打开当前日志的Add-in分类：加载、服务、服务器、清单、程序集等每条Add-in记录显示为`名称 [种类] [来源]`、错误标记和原始行号，名称与来源只取记录本身的文字（`Name(...)`、`VendorId(...)`、`AddInName`等），服务器注册与其`[Jrn.RegisterExternalServer]`记录合为一段。程序从加载事件前缀提取名称和Class；按`v`可输入名称或Class前缀（只列Add-in记录，含加载失败），例如`ATFTranslationServer`。Enter展开后才按页读取原始数据，再按Enter或Esc折叠。

当前优先迭代CLI终端查看器和可复用解析模块，另有桌面窗口（`gui`，内嵌WebView2）和浏览器查看器（`view`），两者使用同一个页面。使用Python 3.10及以上版本和启用FTS5的SQLite；核心功能无运行时第三方依赖，桌面窗口另需可选依赖pywebview（`[desktop]`）。日志数据全部在本机处理。真实案例可以同步放入`cases/inbox/`，逐步校准规则。

## 快速开始

Windows（中文系统也无需指定编码）：

```powershell
# 方式一：单文件，只需Python 3.10+；在仓库中构建一次，复制dist\revit-journal.pyz即可分发
py -3 scripts\build_zipapp.py
py dist\revit-journal.pyz summary          # 本机各版本Revit最新会话的摘要
py dist\revit-journal.pyz browse           # 打开最新会话的终端查看器
py dist\revit-journal.pyz report -o Journal报告.md

# 方式二：安装到虚拟环境，得到revit-journal命令
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\revit-journal.exe browse --revit 2024
```

不传路径时自动查找`%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit <year>\Journals`；也可以直接传文件或目录，例如`browse "$env:LOCALAPPDATA\Autodesk\Revit\Autodesk Revit 2024\Journals\journal.0011.txt"`。编码默认`auto`逐文件判定（中文Windows的cp936文件按gb18030读取），一般不需要`--encoding`。交互界面建议在Windows Terminal中使用；Windows 10自带控制台（conhost）已实测可用，不支持VT控制序列的旧控制台会提示并改为输出一页摘要（等同`--print`）。

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
revit-journal browse examples/paragraphs.journal.txt
```

不安装包也可以运行：`PYTHONPATH=src python3 -m revit_journal_tools browse examples/paragraphs.journal.txt`（Windows：`$env:PYTHONPATH='src'; py -3 -m revit_journal_tools browse ...`）。

## CLI终端查看器

```bash
# 不传路径：自动查找本机各版本Revit的Journals目录，打开最新的会话
revit-journal browse
revit-journal browse --revit 2024 --latest 3

# 会话摘要：是否正常结束、阶段、许可、打开的文档、Add-in、对话框、侧车日志、结束前操作
revit-journal summary --latest
revit-journal summary --revit 2024 --format json

revit-journal browse cases/inbox --recursive

# 直接进入API错误分类
revit-journal browse journal.0001.txt --category api_error

# 筛选第三方Add-in加载，含加载失败事件
revit-journal browse cases/inbox --recursive --category addin_third_party

# Add-in分类与名称/Class前缀筛选
revit-journal browse journal.0001.txt --category addin
revit-journal browse journal.0001.txt --addin-prefix ATFTranslationServer

# 其他语言系统产生的Journal（如德文系统的cp1252）：auto逐文件判定并显示依据；判错时再显式指定
revit-journal browse cases/inbox --recursive
revit-journal browse journal.0076.txt --encoding cp1252

# 用人工样例检查官方、第三方、来源缺失和加载失败
revit-journal browse examples/addin-loading.journal.txt

# 非交互摘要和可复用的段落元数据
revit-journal browse examples/paragraphs.journal.txt --print --color always
revit-journal browse examples/paragraphs.journal.txt --format json --page-size 20

# 在全部文件中搜索关键词：按会话列出命中行，与browse共用索引缓存
revit-journal search "Command Jrn" --latest
revit-journal search ExceptionCode cases/inbox --recursive --format json --per-file 10
```

上下键选择段落，Enter展开/折叠；展开后用上下键或PgUp/PgDn滚动原文，左右键横向滚动长行。按`0`显示全部，`1`至`9`切换分类（启动、Add-in、退出、错误、API错误、操作、警告、API活动、对话框），`c`循环切换（含其他记录），`/`搜索当前文件，`?`搜索全部文件，`h`/`H`在段内跳到下一个/上一个命中行，`g`跳到原始行，`f`选择文件，`n`/`p`前后翻页，`q`退出。`+`/`-`调整段落预览行数；字体放大/缩小使用终端自身的缩放功能，窗口调整后自动重排。

关键词搜索（`/`、`?`、`browse --query`与`search`子命令）使用同一种语法：以空白分隔的各个词必须出现在同一行中，顺序不限；`"引号"`内的部分是相邻短语；最后一个未加引号的词同时按前缀匹配（`DBG_ERR`能找到`DBG_ERROR`）。匹配基于FTS5分词，不是子串或正则搜索；只含标点的搜索词（如`}`）会给出错误提示而不是空结果。状态行的“命中N行”统计的是行数，不是段落数。有搜索时按Enter展开的段落直接滚动到第一个命中行，命中行反色并在行号后标`*`，当前命中标`>`。按`?`搜索全部已索引文件时，结果按文件列出命中行数和前3个命中行，Enter打开该文件并定位到该行，Esc返回。`search`子命令输出按会话分组，每个文件一行`路径 | 命中N行`，其下为`  行号: 原文`，末行为总数；`--category`只看某个行分类，`--per-file`设置每个文件列出的行数（1–20），`--format json`输出结构化结果；有文件索引失败时退出码为2。在PowerShell 5.1中，关键词里的双引号需写成`'\"open command\" Ribbon'`。

按`a`筛选Autodesk来源，`t`筛选第三方来源，`u`筛选来源待定（各种Add-in记录）；`c`可循环切换全部分类。Autodesk来源依据为Vendor中的`ADSK`或`autodesk.com`及其子域名；其他明确厂商归入第三方，缺失、冲突或不完整的信息保留为来源待定。名称/Class前缀只用于筛选，不替代Vendor来源判断。来源标签不证明该组件由Revit默认安装，也不代表加载成功。仅有DLL名称或安装路径中的Autodesk字样不作为厂商依据。更新后首次打开旧缓存会自动重建所选文件的索引。

正常段落、系统配置、`Jrn.*`操作及其`_`续行、API花括号段、同一异常及其堆栈均按块显示。新的独立错误另起一块。每个块保留起止行、分类和分段依据；展开不改变源文件。未闭合的API段或操作续行会显式标记。分段是阅读辅助，不代表已确认错误根因。

按`f`打开的文件列表以Revit会话为单位：同一目录下`journal.NNNN.txt`、`journal.NNNN.workerN.log`、`journal.NNNN.txt.copy.txt`和`journal.NNNN.NNNN.dmp`归入同一会话。字节相同的副本只索引一次并在会话下注明，`.dmp`只计数和显示大小，0字节文件（如`dump.0001.txt`）列出但不索引；不符合上述命名的文件作为“未识别命名”单独成组，仍正常读取。`--print`与`--format json`同样按会话输出，JSON新增`sessions`节。

首次启动在后台建索引，可先切换到已完成的文件。索引缓存默认在用户缓存目录（Windows：`%LOCALAPPDATA%\revit-journal-tools\cache\terminal.sqlite3`；Linux：`$XDG_CACHE_HOME`或`~/.cache`下的`revit-journal-tools`），不再写到当前目录；`revit-journal cache`显示位置与大小，`revit-journal cache --clear`删除本工具的索引数据库，也可用环境变量`REVIT_JOURNAL_CACHE`或`--database`改变位置。索引默认只保存每行在源Journal中的字节位置（`--store offsets`），查看时按位置从源文件读取原文，因此源文件要保持原位、不被修改；读取前核对大小、修改时间和文件标识，不一致时明确提示重新打开以重建，不会显示错位的文字。`--store text`把全文存入索引（实测为偏移方式的1.7–2.3倍），之后源文件移走也能查看。正在写入的Journal（读快照）和UTF-16等无法按字节定位行的文件总是存全文。段落再大也按页加载原文。搜索命中续行时返回整个段落，不重复列出同一块；统计“块数”与原有扫描的“命中行数”不同。

按`s`看当前会话摘要，`e`看结束前最后的操作与对话框，`P`按阶段（启动、许可、Add-in加载、打开文档、交互操作、退出）筛选，`9`看对话框，`z`切换“相同内容段落折叠为×N”。Revit仍在写入的Journal默认读取完整行的快照（`--snapshot`），并在界面中标注。

详细分段规则、按键和限制见[docs/terminal.md](docs/terminal.md)。

## 桌面窗口

```powershell
# 一次性安装：本包与可选依赖pywebview（Windows使用系统自带的WebView2运行时）
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"

# 打开窗口：不传路径时打开本机各版本Revit的Journals；也可以传文件或目录
.\.venv\Scripts\revit-journal-gui.exe
.\.venv\Scripts\revit-journal.exe gui cases\inbox --recursive
```

窗口标题为`revit-journal-tool`，窗口与页面使用`web/icon.ico`、`web/icon.png`图标（取自ImagePusher的`selfpush.ico`）。窗口内嵌WebView2，显示与`view`相同的页面，另有“打开文件…”“打开文件夹…”“本机Journals”三个按钮（Ctrl+O打开文件）。新打开的文件替换当前工作区：先停止正在进行的索引，再开始新的。左侧工作区与右侧内容之间、段落列表与原文之间的分隔条都可以拖动（方向键微调，双击恢复默认），宽度和主题（浅色/深色/跟随系统）会被记住。页面只从本机`127.0.0.1`读取，原文件只读。

`revit-journal-gui.exe`没有控制台窗口，启动前的错误（如编码参数无效）以对话框显示。没有安装pywebview时，`gui`会提示安装命令，也可以改用`view`在浏览器中查看。索引缓存为缓存目录中的`desktop.sqlite3`；窗口的布局与主题保存在缓存目录的`webview`子目录（WebView2用户数据，约10MB），`cache --clear`不删除它。

## Web查看器

```bash
# 查看第一批已上传的英/德文案例（cp1252）；首次在后台建立磁盘索引
revit-journal view cases/inbox --recursive --encoding cp1252

# 默认地址为http://127.0.0.1:8765；端口被占用时可调整
revit-journal view --revit 2024 --port 8766
```

浏览器打开命令显示的本地地址即可。左侧切换文件，上方切换分类；默认按段落列出（与`browse`相同的分段、阶段筛选和重复折叠），可切换为逐行列表。关键词搜索与CLI同一语法（多词同时包含、引号为相邻短语、末词前缀），搜索框旁的“当前文件/全部文件”切换范围：当前文件下列表底部显示“命中N行”，点击段落结果时右侧从首个命中行开始显示；全部文件在所有已就绪的文件中搜索（`/api/search`），每个文件显示命中行数和前3条命中行，点击即切换到该文件并定位，搜索词保留。列表、段落原文和上下文中每个搜索词的每次出现都会高亮。列表每页最多100项；点击段落查看它的完整原文（长段落每次加载200行），点击行查看前后文，也可以直接跳到任意有效行号。“会话摘要”显示与`summary`命令相同的内容，其中的行号可点击定位到主Journal。分类包含启动、Add-in、退出、错误、API错误、操作内容、警告、API活动、对话框和其他记录：第一次用户操作之前未被其他分类命中的行归启动，退出标记之后归退出，其间为其他记录。页面本身不滚动（窄窗口下分类片收紧换行，低窗口下文件概览收为一行）；点击列头可按分类、来源或Add-in记录种类筛选，种类筛选对应`browse --kind`。

首次索引逐文件进行，已经就绪的文件可先浏览。索引缓存位于缓存目录的`viewer.sqlite3`，再次启动会复用未变化文件的索引。存储方式与`browse`相同（默认`--store offsets`，见上文），不受CLI的500条明细限制，默认不进入Git。文件内容、编码或索引规则变化后会重建相关索引。

页面支持中文/English切换（会话摘要与错误提示同样切换；命令行与工单报告仍为中文）和浅色/深色主题，左侧工作区、段落列表与原文三栏之间可拖动调整宽度；左侧按Revit版本分组（可折叠），版本下按会话时间排序（最新或最早在前），每个会话列出其文件；Add-in只占一个分类，段落视图中选中后再按Autodesk/第三方/来源待定筛选（来源含加载失败）；打开文件后自动显示第一个段落；列表中用上下键逐项查看，Ctrl+F搜索，Ctrl+G跳至行；原文区可复制原文或“路径:行号”。Web界面的段落、折叠、阶段与会话摘要直接调用CLI使用的同一组索引查询和摘要渲染，不另写一套逻辑；交互式折叠展开、Add-in名称前缀筛选等仍只在CLI中提供。Web用法见[docs/viewer.md](docs/viewer.md)。

## 支持工单报告

```powershell
# 本机最新会话（不传路径时默认--latest 1）；写出UTF-8 Markdown
revit-journal report -o reports\journal-report.md
# 指定Journal或目录；JSON格式；保留个人信息（默认替换）
revit-journal report "$env:LOCALAPPDATA\Autodesk\Revit\Autodesk Revit 2024\Journals" --revit 2024 --latest 3
revit-journal report journal.0011.txt --format json --include-personal-info
```

报告按会话组织，便于直接贴进Autodesk支持工单，结构与RevitLocalDbFix报告一致（顶部要点列表、两列事实表、原文代码块）：每个文件的SHA-256、实际编码、行数与字节；会话摘要（结束状态、阶段、许可、文档、Add-in、对话框、结束前操作、最后几行、侧车日志及关联依据）；规则候选按严重级别列出，带原始行号、前后文、规则ID和依据，相同行只列一次，同一规则组合最多3例并注明其余行数，Revit界面提示只计数；末尾是未验证声明。默认把Windows用户名、域名和计算机名替换为`<user>`、`<domain>`、`<machine>`（只作用于报告文字，SHA-256仍针对原始文件）。退出码：`0`成功，`2`有文件读取失败（报告中显式列出）或参数错误。

## 扫描与导出

```bash
# 单文件，保留每处命中前后各5行
revit-journal scan /path/to/journal.0001.txt --context 5

# 多文件或目录；目录选择.txt、.log、.journal和.txt.abbrev
revit-journal scan cases/inbox --recursive --format json -o reports/cases.json

# 默认auto逐文件判定编码；需要严格按某个编码读取时显式指定
revit-journal scan journal.0001.txt --encoding gb18030

# 混合来源目录：按顺序尝试候选编码
revit-journal scan cases/inbox --recursive --encoding utf-8,gb18030,cp1252

# 仅保留error及fatal规则的候选，增大明细容量
revit-journal scan journal.0001.txt --min-severity error --max-findings 2000

# 用自定义规则扩展内置规则；JSONL每个文件一条报告
revit-journal scan cases/inbox --recursive --rules examples/custom-rules.json --format jsonl

# 可供脚本使用：达到error或fatal时退出码为1
revit-journal scan journal.0001.txt --fail-on error

# 查看规则定义、级别和来源
revit-journal rules
```

`python -m revit_journal_tools`与`revit-journal`等价。输出文件以UTF-8原子替换，不能覆盖输入Journal或自定义规则文件。一次批量任务中的单个文件读取失败会单独记为`status: error`，其余文件继续处理。与另一输入字节完全相同的文件（如`journal.NNNN.txt.copy.txt`）只扫描一次，副本记为`status: duplicate`并给出`duplicate_of`与SHA-256；目录中的`.dmp`不扫描。

退出码：`0`表示扫描成功；`1`表示成功扫描且达到显式指定的`--fail-on`级别；`2`表示参数、规则、输入或输出错误；`130`表示中断。默认发现错误候选不会导致非零退出码。

## 报告内容

- 来源：文件绝对路径、原始字节SHA-256、实际解码方式、请求的编码、是否回退、退化行数与行号、文件大小和行数。
- 定位：从1开始的文本行号与字符列号、命中片段、前后文。CRLF、LF和单独的CR均作为换行；混合换行的文件可能与仅按LF计行的工具不同。
- 依据：规则ID、候选级别、解释、来源链接以及`heuristic`/`documented_marker`/`case_verified`标签。
- 统计：各级别的命中行数、各规则命中数、完整原文相同的重复组。
- 边界：扫描是否完成、保留明细数、省略明细数、未纳入分组的命中数。

一行命中多条规则时只产生一个候选，候选级别取当前启用规则中的最高级别。各规则计数的总和可能大于命中行数。规则级别是检查优先级，不能直接视为真实事故严重度。

`scan_complete: true`只表示已完整解码并扫描输入，并不表示所有错误都能识别。无命中表示现有规则未找到候选。所有命中仍为`candidate`，当前没有自动根因诊断。

## 大文件与编码

逐行读取，不将整个Journal加载到内存。默认保留最多500条候选明细和500个重复组；超过上限后仍继续扫描全文并累计候选数量。明细按`fatal > error > warning`优先保留，同级优先较早出现的候选，最终按行号展示；这样后出现的严重错误能替换已保存的普通警告。报告明确给出省略数量；可使用`--min-severity`减少警告干扰或提高`--max-findings`。分组只合并完全相同的解码行及规则集合，不模糊合并元素ID、路径或时间。

每个展示行最多保存4096个字符，超出时标记`text_truncated`。识别会检查整行，匹配点附近的独立片段与列号仍会保留。默认单行上限为1,048,576字符，超限返回明确错误，可通过`--max-line-chars`调整，不会静默跳过后半行。

`--encoding auto`（默认）逐文件判定编码：有BOM按BOM；否则先严格UTF-8；再按Journal头部声明的界面语言（`Actual language code=DEU`等：德、法、意、西、葡、荷为cp1252，波兰、捷克、匈牙利为cp1250，俄语cp1251，简体中文gb18030，繁体cp950，日文cp932，韩文cp949；英文ENU/ENG不说明系统代码页）；再依次尝试gb18030、系统ANSI代码页的超集与cp1252。gb18030几乎能“解码”任何字节，西文系统的cp1252文件也会被读成看似正常的汉字（如`Außenanlagen`变成`Au遝nanlagen`），因此只有解码后非ASCII字符中至少一半是常用汉字（GB2312）时才采用：本机95个真实中文Journal为0.83–1.0，上传的cp1252 Journal不超过0.003。同样，cp1252几乎能解码任何字节：内容像中文时，不会因为几个损坏字节改用cp1252，而是按gb18030退化解码并标记。同一批文件可以各自使用不同编码，报告、`browse --format json`和索引都记录每个文件实际使用的`encoding`、判定依据`encoding_basis`（如`language_code:DEU`、`chinese_text`）、请求值`encoding_requested`以及是否回退`encoding_fallback`；只有gb18030能解码但内容不像中文时标为“编码存疑”。

所有候选编码都无法完整解码时，`auto`不再让整个文件失败，而是按最后一个候选读取：无法解码的字节以`\xNN`原样显示，该行标记为`decode_degraded`，文件级给出退化行数`decode_degraded_line_count`和前20个行号，并在报告注意事项中说明。退化行上的识别结果可能不完整，可改用正确的编码重试。字节不会被静默替换或丢弃，SHA-256仍按原始字节计算。

显式指定单个编码（如`--encoding cp1252`）为严格模式：任何无法解码的字节都会使该文件读取失败，与旧版本一致。也可以给出逗号分隔的候选列表，例如`--encoding utf-8,gb18030,cp1252`，按顺序逐文件尝试（gb18030同样要通过常用汉字检查），全部失败时按最后一个候选退化解码。候选列表不使用Journal声明的界面语言。疑似二进制（含NUL）、超长行或扫描期间变化的文件仍记为读取失败。优先提供已停止写入的Journal副本。

首批上传的英/德文案例使用CP1252可以严格解码；其中不换行空格、角度符号和德文变音字符不符合UTF-8。扫描这批案例时显式使用`--encoding cp1252`，该选择不代表以后上传的所有文件都使用同一编码。

本机实测（中文Windows，cp936）：113个真实文件不传`--encoding`时全部可处理，其中96个按gb18030回退严格解码、8个为UTF-8，没有退化行；7个`.dmp`只计数，1个字节相同的`.copy.txt`与1个空`dump.0001.txt`不重复索引。

## Python接口

```python
from revit_journal_tools import ScanOptions, analyze, load_rules

report = analyze(
    "examples/synthetic.journal.txt",
    options=ScanOptions(context=3, min_severity="error"),
    rules=load_rules(),
)
for finding in report.findings:
    print(finding.line_number, finding.severity, finding.line.text)
result = report.to_dict()
```

接口调用失败时抛出异常；CLI负责批量任务的逐文件错误隔离。

## 项目结构

```text
src/revit_journal_tools/
  reader.py              逐文件编码判定、严格/退化解码、流式读取、哈希与源文件变化检查
  sessions.py            按文件名把主Journal、Worker、副本、dmp归入会话，副本按字节去重
  rules.py               规则加载、验证与匹配
  regex_safety.py        规则正则加载时的灾难性回溯（嵌套量词）检查
  data/builtin_rules.json 内置规则
  analyzer.py            上下文、计数、有限容量的明细与分组
  models.py              结构化报告
  reporting.py           文本、JSON、JSONL导出
  ticket.py              支持工单报告（Markdown/JSON）与个人信息替换
  cli.py                 批量扫描入口
  discovery.py           本机Revit Journals目录与索引缓存目录
  live.py                正在写入的Journal的快照读取
  summary.py             会话摘要、结束状态与会话级候选
  session_view.py        会话摘要的文本呈现
  sidecars.py            钢连接错误日志、CER日志与会话的关联
  categories.py          启动、Add-in、退出、错误、API和操作记录分类与会话阶段标记
  blocks.py              流式段落边界、跨行错误与操作分组
  loading.py             Add-in记录的种类、名称、厂商依据与来源筛选
  terminal.py            CLI颜色、折叠展开、文件切换与原文分页
  viewer_index.py        全量磁盘索引、全文检索（查询解析、命中行、全部文件搜索）、分页与上下文查询
  viewer_server.py       仅监听本机的查看器服务
  desktop.py             桌面窗口：pywebview内嵌WebView2，打开文件/文件夹并替换工作区
  i18n.py                查看器页面的英文文本（?lang=en）与错误提示翻译；默认中文
  web/                   本地界面（view与gui共用），无外部前端依赖
cases/                   案例说明、登记模板与接收目录
examples/                明确标注的人工样例
tests/                   读取、匹配、证据与命令行回归测试
docs/                    架构、规则扩展与参考来源
reports/                 本地输出目录
```

## 开发与后续

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 人工生成8MiB与64MiB日志，验证完整扫描、末尾命中和内存占用
PYTHONPATH=src python3 scripts/benchmark_streaming.py --mib 8 64
```

Windows下可用本机真实Journal做只读基准与等价性核对（结果只打印，不入库，不复制Journal）：

```powershell
$env:PYTHONPATH='src'
py -3 scripts/benchmark_real.py all --revit 2024          # 扫描、分类分段、单文件建索引、整目录browse
# 修改解析/规则后：与改动前的源码树逐行比对（旧树用git archive解压到仓库外）
py -3 scripts/verify_equivalence.py gates
py -3 scripts/verify_equivalence.py blocks --old-src <旧src目录>
py -3 scripts/verify_equivalence.py blocks --together --old-src <旧src目录>
# 偏移与全文两种索引方式逐页比较全部行、段落和查询结果
py -3 scripts/verify_equivalence.py stores --encoding auto
# 交互界面在原生控制台中的验收（会弹出一个窗口几秒钟）：conhost或Windows Terminal，逐键检查与退出恢复
py -3 scripts/windows_console_probe.py <journal.txt> [--host wt] [--ctrl-c]
```

本机实测（Windows 10、Python 3.12，2026-10-10）：6.47MB/85,387行真实Journal扫描约0.4s（约17MB/s），建索引约2.5s（约3.4万行/秒，索引为源文件的2.2倍；`--store text`约2.4s、3.6倍）；Revit 2024目录22个文件约11MB的`browse --print`约2.8s。

安装包后，Windows也可使用`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`运行同一套测试。桌面窗口的测试用替身代替pywebview，不打开窗口，没有安装pywebview时同样运行。

单文件分发：`py -3 scripts/build_zipapp.py`生成`dist/revit-journal.pyz`（约90KB，只含本包代码、内置规则和Web文件），目标机器只需Python 3.10+；其中的`gui`命令还需要该Python安装pywebview（`py -m pip install pywebview`）。当前版本未实现浏览器拖放导入、带Python运行时的安装包、AI分析、Journal回放或模型修复。

案例提交方式见[cases/README.md](cases/README.md)，规则扩展见[docs/rules.md](docs/rules.md)。Autodesk的[Journal阅读指导](https://www.autodesk.com/support/technical/article/How-to-read-the-journal-file-Revit/)建议结合具体操作、实际现象和近似时间排查；本项目按此保留上下文，其他参考见[docs/sources.md](docs/sources.md)。
