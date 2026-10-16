# CLI段落查看器

```bash
revit-journal browse                      # 自动查找本机各版本Revit的Journals目录
revit-journal browse --revit 2024 --latest
revit-journal summary --latest            # 最新一次会话：怎么结束的、最后在做什么
revit-journal browse cases/inbox --recursive
revit-journal search "Command Jrn" --latest   # 在全部文件中搜索，按会话列出命中行
```

只在终端中运行，无需浏览器或HTTP端口。命令读取Journal并在本机建立SQLite索引。日志内容不会被执行或回放。

## 如何分组

| 输入结构 | 展示单元 |
| --- | --- |
| 普通连续文本 | 以空白分隔的完整段落 |
| 时间戳记录 | 新事件；空的时间戳头附在后续操作或API段前 |
| Build/Branch/Release等启动信息 | 保留在同一启动段中 |
| SystemSettings、Log Summary、许可证属性等 | 标题及连续属性形成一块 |
| VM/RAM与紧随其后的GUI Resource Usage | 一组资源采样 |
| `Jrn.*`与以`_`连接的续行 | 一条完整操作 |
| `API_SUCCESS/INFO/ERROR { ... }` | 花括号包围的完整事件，包括多行错误内容 |
| 错误消息、紧随的异常类型与堆栈 | 同一错误块 |
| `Exception occurred`及后续异常代码/地址 | 同一原生异常块，即使代码行带时间戳 |
| 新的独立错误标记或独立异常 | 新块，不与上一个错误合并 |

每行恰好属于一个块，保留连续的原始行号，空白行也不丢弃。读取器继续按CRLF、LF、独立CR识别换行。段落边界主要来自空白、时间戳、显式记录、括号、续行符和类别变化；这些是启发式规则，不表示所有版本、插件和自定义日志格式均已正确识别。

未闭合的API花括号段或操作续行在遇到新的显式事件或文件结束时标记“未闭合”。不会为了固定行数强行截断段落。异常堆栈支持常见的`at`、`--->`和`Caused by`等前缀；没有可识别关联特征的错误文本可能仍形成独立块，后续通过案例校准。

### 分类

每行只有一个分类，顺序即数字键顺序：启动、Add-in、退出、错误、API错误、操作内容、警告、API活动、对话框、其他记录。显式标记按优先级判定：`API_ERROR`及其续行、对话框、错误规则、警告、`Jrn.*`操作、Add-in记录、启动标记、退出标记、`API_SUCCESS/API_INFO`。

- Add-in：任何阶段的显式Add-in记录，包括`Starting External (DB) Application`加载记录及其花括号续行、`[Jrn.AddInManifest]`、`[Jrn.RegisterExternalServer]`、`Jrn.AddInEvent`、外部服务注册与注销、`The requested assembly ... was loaded to the context`、`Third Party Updater`等。加载失败（`API_ERROR { Starting External Application ...}`）仍属API错误，同时带来源。
- 没有命中任何标记的行按所在阶段归类：第一次用户操作之前为启动（含`Initial VM`、系统与许可信息等），退出标记之后为退出，其间为其他记录（含会话中的`Delta VM`内存采样）；空行总是其他记录。阶段标记与段落的`phase`相同（见“阶段”），进入退出后不再回退。
- 段落取首行分类；启动、退出、其他记录只说明位置，会被段内第一个其他分类覆盖，分类变化导致的分段也只看其他分类。

## 交互

| 按键 | 行为 |
| --- | --- |
| ↑/↓或k/j | 选择块；展开时滚动原文；全部文件结果中选择命中行 |
| Enter或空格 | 展开/折叠选中块；有搜索时展开到第一个命中行（其上保留3行）；全部文件结果中打开该命中行 |
| PgUp/PgDn | 在当前列表或展开原文中快速移动 |
| ←/→ | 横向滚动长行，保留行号 |
| n/p | 下一页/上一页段落；全部文件结果只有一页，不响应 |
| 0 | 全部分类 |
| 1至9 | 启动、Add-in、退出、错误、API错误、操作、警告、API活动、对话框 |
| i | Add-in分类（同`2`） |
| a/t/u | Autodesk来源/第三方来源/来源待定（各种Add-in记录，含加载失败） |
| v | 按名称或Class前缀只列Add-in记录（含加载失败），不改变当前分类；清空后Enter移除前缀条件 |
| c | 循环切换分类：全部→`1`至`9`→其他记录→三个加载来源 |
| s | 当前会话摘要（再按返回段落列表）；上下键滚动 |
| e | 结束前最后N个操作与对话框（再按返回） |
| P | 循环切换阶段筛选：全部→启动→许可→Add-in加载→打开文档→交互操作→退出 |
| z | 折叠/展开内容相同的段落（默认折叠，列表显示×N） |
| / | 在当前文件中搜索关键词（语法见“关键词搜索”）；Enter确认，Esc取消；清空后确认移除筛选；只含标点时提示错误并保留原搜索 |
| ? | 在全部已索引文件中搜索关键词；结果按文件列出命中行，Enter打开并定位 |
| h/H | 在选中段落内跳到下一个/上一个命中行；未展开时先展开 |
| g | 输入原始行号，定位并展开所属段落；清除分类、阶段、搜索和前缀筛选，并关闭折叠 |
| f | 文件列表；上下选择，Enter打开，Esc返回 |
| [和] | 上一个/下一个文件 |
| +和- | 增加/减少普通段落预览行数；Add-in原文仍只在展开后显示 |
| Esc | 折叠、取消输入，或从全部文件结果返回段落列表 |
| q或Ctrl+C | 退出并恢复终端 |

字体缩放由终端自身提供，具体快捷键取决于终端设置。程序不修改终端字体配置；窗口改变尺寸后重新计算可见行和列。原文过长时可横向滚动，单行预览上限为16,384个字符，超限附截断提示；索引仍保留全文。

默认配色：启动与API活动为蓝色，退出为亮蓝色，Add-in为青色，普通错误为红色，API错误为紫色，操作为绿色，警告为黄色，对话框为亮青色，其他为灰色；文件列表的会话标题和全部文件结果的文件标题为粗体。界面同时显示文字标签，不依靠颜色单独表达类型。支持`--color auto|always|never`，`NO_COLOR`环境变量关闭自动颜色；显式`always`优先。

## 关键词搜索

`/`（当前文件）、`?`（全部文件）、`browse --query`和`search`子命令使用同一种语法。匹配基于FTS5的unicode61分词：连续的字母和数字成为一个词，空白和标点是分隔符，英文字母不区分大小写。它不是任意子串或正则表达式搜索。

- 以空白分隔的各个词必须出现在同一行中，顺序不限（与）：`Command Jrn`和`Jrn Command`结果相同。
- 双引号括起的部分是一个短语，其中的词必须相邻并按顺序出现：`"open command"`只匹配`open`后紧跟`command`的行。词内带标点时也按短语匹配，例如`Jrn.Command`匹配`Jrn`后紧跟`Command`的行。
- 最后一个未加引号的词同时按前缀匹配，便于边输入边查找：`DBG_ERR`能找到`DBG_ERROR`。其他词和引号内的短语按完整词匹配，子串（如`rror`）不会命中。
- 不含任何字母或数字的词（如`}`、`->`）不参与分词，会被忽略；整个搜索只剩这类词时会报错“搜索词不含可检索的字母或数字；标点和符号不参与分词”，不会静默返回空结果。交互界面会保留原来的搜索和列表；`search`子命令以退出码2结束，并且不读取任何文件。
- 不支持NOT/OR语法和正则表达式。中文、日文等连续文字整段作为一个词，只能从这一段的开头匹配（`不满足约束`可以命中，`满足`不能）；查询含这类文字时会在消息行提示。Journal正文为英文，这类文字通常只出现在项目文件名中。

“命中N行”统计的是满足条件的行数，不是段落数。`/`搜索的列表按段落显示，同一段落只出现一次；状态行的“搜索: 词 命中N行”是当前文件的命中行总数。选择了行分类（`1`至`9`，或用`c`切换到的其他记录）时只统计该分类的行，阶段、Add-in来源和前缀筛选不影响这个数字。

### 段内命中

有搜索时，列表中的每个段落都记录其中第一个命中行。按Enter展开时直接滚动到该行，上方保留3行原文。展开后，含任一搜索词（不区分大小写）的行以反色显示并在行号后标`*`，当前命中行标`>`，无颜色终端也能分辨。`h`/`H`跳到本段内的下一个/上一个命中行（每段最多统计前200个），消息行显示“段内第k/N个命中”；段落未展开时先展开。高亮按搜索词做文本比较，只是阅读辅助；是否命中以索引的分词匹配为准，两者在标点处可能略有差异。

### 全部文件搜索（`?`）

按`?`输入关键词，可在当前工作区所有已完成索引的文件中搜索。结果顶部显示“全部文件命中N行 / M个文件”；还有文件未完成或失败时注明“跳过K个未就绪”。每个有命中的文件占一行标题，显示Revit版本目录、会话、文件名和该文件的命中行数；标题下最多列出前3个命中行（`L行号`和原文）。

上下键在命中行之间移动。按Enter会打开对应文件，并像`g`一样定位：清除分类、阶段和前缀筛选，关闭折叠，展开所在段落，并把该行放在顶部。同时，这次的关键词会成为当前文件的搜索条件，因此可以继续使用高亮和`h`/`H`。按Esc返回段落列表；再按`?`后回车可回到全部文件结果，关键词不变时保留原来的选中位置。

结果是按下Enter时的快照，之后才完成索引的文件需要重新搜索。在结果列表中按其他段落列表按键（如`/`、`f`、数字分类）时，会先回到段落列表再执行；`n`/`p`和`h`/`H`不响应。

### `search`子命令

```bash
revit-journal search "<关键词>" [路径…] [--category 分类|all] [--per-file N] [--format text|json]
revit-journal search "Command Jrn" C:\cases --recursive
revit-journal search ExceptionCode --revit 2024 --format json --per-file 10
```

另外支持与`browse`相同的`--recursive`、`--encoding`、`--revit`、`--latest`、`--snapshot`、`--jobs`、`--database`和`--store`；不传路径时自动查找本机各版本的Journals目录。关键词必须写在路径之前，含空格时要加引号。在PowerShell 5.1中，关键词里的双引号需要写成`'\"open command\" Ribbon'`才能传给程序。命令先建立或复用索引（与`browse`、`summary`共用`terminal.sqlite3`），等所有文件处理完再搜索。

- `--category`只统计并列出某个行分类的命中行（`startup`、`addin`、`exit`、`error`、`api_error`、`operation`、`warning`、`api`、`dialog`、`other`），默认`all`。加载来源`addin_autodesk`等是`browse`的段落筛选，不是行分类。
- `--per-file N`设置每个文件最多列出的命中行数，范围1–20，默认3；它不影响命中总数。
- 文本输出按会话分组：先输出`== 会话标题`，再为每个文件输出一行`路径 | 命中N行`（只列出部分时注明“列出前N行”），其下每行为`  行号: 原文`。原文超过约200字符时截断，并标`[截断]`。末行为`共命中N行 / M个文件`；有跳过的文件时，再输出`跳过K个未就绪或失败的文件`和每个失败文件的原因。
- `--format json`输出`{"schema_version": 1, "query", "terms", "hint", "total_lines", "file_count", "skipped", "files", "errors"}`，其中`hint`仅在查询含中文等连续文字时出现。`files`中的每项包含`id`、`name`、`path`、`role`、`session`、`session_label`、`hit_lines`和`items`；`items`与Web查看器`/api/events`的行结构相同（`line_number`、`category`、`text`，前700字符，以及`text_length`等）。`errors`列出索引失败的文件及原因。
- 退出码：正常时为0，没有命中也是0；有文件索引失败时为2（与`browse --print`一致），结果仍会输出。关键词为空、只含标点或参数超出范围时，在标准错误中输出原因并以2退出。

## 会话、摘要与排查顺序

排查通常从“最新一次会话”开始：先看是否正常结束、结束前在做什么，再看许可、Add-in和对话框。

- 不传路径时枚举`%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit <year>\Journals`（含LT），找不到时提示传入路径。`--revit 2024`（可重复或逗号分隔）只看指定版本；`--latest [N]`只保留最新N个会话（默认1）。显式路径与`--revit`同时使用时按所在`Autodesk Revit <year>`目录过滤。
- 会话按其中最新文件的修改时间倒序排列，会话内主Journal在前。默认打开最新的主Journal，索引队列也先处理它；并行建索引时它最先开始。
- `summary`命令和TUI的`s`面板显示：Build/Branch/Release与进程号、起止时间与时长、结束标记行号、阶段首次出现的行号、许可记录与许可错误、打开过的文档（`openFromModelPath`）与打开失败、Add-in加载记录（Autodesk/第三方/待定）与第三方名称、按程序集聚合的版本冲突、对话框、最后8行、侧车文件和会话级发现。每一项都带原始行号。
- 结束状态：有`finished recording journal file`为“正常结束”；没有为“未正常结束”（崩溃、被强制结束、断电或仍未写完），并生成候选`session.no_clean_exit`；Revit仍在写入的文件标“正在写入”，不判为未正常结束。
- 会话级发现（`session_findings`）：`session.no_clean_exit`、`session.license_error`、`session.model_open_failed`、`session.addin_conflict`、`session.steel_localdb`、`session.crash_dump`。它们和规则命中一样是候选，需要结合原文复核。
- `summary --format json`每个会话输出一项，含`summary`、`sidecar_logs`、`session_findings`、`end_state`和结束前操作`tail`；`--tail N`（1–200，默认10）调整结束前操作数量。

### 阶段

块带有`phase`，由明确标记推进：启动→许可（`License initialization`、`LicenseUpd(`、`Adlsdk`）→Add-in加载（第一个`Starting External ...`）→打开文档（`openFromModelPath`或打开类命令ID）→交互操作（第一个用户动作）→退出（`ExitManagedInstance`、`finished recording journal file`等）。Revit自己写入的`Jrn.Data`（如`'H`头记录`JournalDefaultTemplate`、对话框答复）、`Jrn.Directive`、`Jrn.AddInEvent`和启动时自动的`TabActivated`不算用户动作。阶段是阅读辅助，不是诊断；`--phase`和`P`键按阶段筛选。

`e`视图列出结束前最后N个操作与对话框（不含Revit自动写入的`Jrn.Directive`设置记录），对应“结合具体操作与时间阅读”的做法。以`_`续行的`Jrn.*`记录，标题会接上续行内容（最多300字符），例如`Jrn.Data "TaskDialogResult" , "..." , "Close" , "IDCLOSE"`。

### 对话框

`TaskDialog "..."`是用户看到的提示，`Jrn.Data "TaskDialogResult"`及其续行是用户的选择，二者归入“对话框”分类（`9`键）并单独成块。摘要按出现顺序保留最后10个对话框文字。

### 折叠重复段落

类别相同、内容除时间戳、层级前缀、耗时前缀和`- N times`计数外完全相同的块有相同指纹。列表默认只在首次出现处显示一次并标`×N`（`--print`另注末次行号），展开后列出各处起始行，`g`跳到某行会关闭折叠以显示原位置。`z`或`--no-fold`关闭折叠；关键词搜索从不折叠。

### 正在写入的Journal

Revit运行时持续追加当前会话的Journal。`--snapshot auto`（默认）在Windows上以只读、不共享写入的方式探测文件：被其他进程以写方式打开时，先把当时已完整的行（到最后一个换行为止）复制到私有临时目录再读取、哈希和索引；读取期间文件变化时也自动改读快照。`always`总是读快照，`never`保持原行为（变化即读取失败）。原文件只读，不加锁、不修改、不保持打开。报告和列表标注“正在写入/实时快照”和快照时间，SHA-256针对快照字节；之后写入的内容需要重新打开读取。

### 侧车日志

- `journal.NNNN.workerN.log`、`journal.NNNN.NNNN.dmp`、副本按文件名归入会话；dmp只计数和显示大小，不解析。
- 钢结构连接：`%ProgramData%\Autodesk\Revit Steel Connections <year>\<lang>\DatabaseConnectionErrors.log`（UTF-16，条目`[n][HH:MM:SS] 文字`）。LocalDB故障时Journal中可能没有任何警告，证据只在此日志。该日志按同一Revit年份和时间关联到会话，依据依次为：条目时刻（按文件修改日期）落在会话时间内、文件修改时间落在会话内、仅条目时刻落在会话日期的时间范围内（日志不记日期，关联较弱）。关联依据显示在摘要中；它是候选关联，不是证明。修复可使用独立工具RevitLocalDbFix（不是Revit插件）。该日志总按BOM判定编码，`--encoding`只作用于Journal和worker日志。
- CER：`%LOCALAPPDATA%\Autodesk\CER\<id>\cer.log`中产品为RVT且release与年份相同、进程号与Journal中`Session<pid>_P0MainQueue`一致时列出；Journal没有进程号时按时间重叠关联。

### 并行与回退

多文件建索引默认使用`min(8, 逻辑核数-1)`个进程，`--jobs N`调整，`--jobs 1`为顺序。工作进程无法启动或中途退出（例如嵌入式环境或从标准输入运行的脚本，spawn无法重新导入主模块）时，剩余文件自动改为在主进程中顺序建立，不会一直等待。

## 支持工单报告

`revit-journal report`把会话摘要与规则候选整理成可贴进Autodesk支持工单的Markdown（`--format json`为结构化版本），格式与RevitLocalDbFix报告对齐。不传路径时为本机最新会话；默认替换Windows用户名、域名和计算机名，`--include-personal-info`保留。每个文件有SHA-256与实际编码；规则候选带原始行号、前后文和规则ID，相同行只列一次，同一规则组合最多3例，Revit界面提示只计数；末尾为未验证声明。用法与字段见README“支持工单报告”。

## Add-in加载来源筛选

```bash
revit-journal browse journal.txt --encoding cp1252 --category addin
revit-journal browse journal.txt --encoding cp1252 --addin-prefix ATFTranslationServer
revit-journal browse journal.txt --encoding cp1252 --category addin --kind service --kind server
revit-journal browse journal.txt --encoding cp1252 --category addin_autodesk
revit-journal browse journal.txt --encoding cp1252 --category addin_third_party
revit-journal browse journal.txt --encoding cp1252 --category addin_unknown
revit-journal browse examples/addin-loading.journal.txt
```

`Starting External Application:`和`Starting External DB Application:`等加载前缀必须出现在记录正文开头；程序先去除Journal时间戳、层级标记和`API_SUCCESS/ERROR/INFO`包装，再提取名称、Class和同一块内的Vendor。普通消息中间提到这些词、服务注册、DLL引用或带有Autodesk字样的路径不会单独被当成Add-in加载；服务注册、清单等其他显式Add-in记录也属于Add-in分类，按下文的种类表识别名称和来源，但不是加载记录。

例如`Starting External DB Application: ATF Translation Server, Class: ATFTranslationServer.ATFTranslationServerRegister, ...`会提取名称`ATF Translation Server`和Class`ATFTranslationServer.ATFTranslationServerRegister`。按`i`（或`2`）查看Add-in分类：每条Add-in记录一行，显示为`名称 [种类] [来源]`和行号，加载失败等错误另加`[API错误]`，例如`Generic Repository Helper Service [服务] [Autodesk]`；没有名称时以`[种类] [来源]`开头，加载记录不显示原文，其他种类在标签后接原文标题。种类、Class和原始整行在Enter展开后查看。即使已按`+`提高其他段落的预览级别，Add-in分类、来源筛选和前缀下的折叠行也不会读取或展示原始数据。

| 种类 | 记录 | 名称 | 来源依据 |
| --- | --- | --- | --- |
| 加载 | `Starting/Loading External (DB) Application:` | 前缀后的名称 | `Vendor :`（见下文来源表） |
| 服务 | `Registered an external service: Name(X); VendorId(V)` | `Name(...)` | `VendorId(...)` |
| 服务器 | `Registered an external server: Name(X); VendorId(V); ServerId(...)`；单独的`[Jrn.RegisterExternalServer]` | `Name(...)`；后者无 | `VendorId(...)`；后者无 |
| 清单 | `[Jrn.AddInManifest]` | `AddInName` | `CommandVendorId`与`CommandVendorDescription` |
| 外部事件 | `[Jrn.ExternalEventExecution]` | `AddInName` | `VendorId`与`VendorDescription` |
| 更新器 | `[Jrn.AddInDynamicUpdate]`、`Third Party Updater 'X' ...`、`Forcibly unregistering Updater`、`Forcibly removing all update triggers` | `AddInName`或引号内文字，否则无 | `[Jrn.AddInDynamicUpdate]`的`VendorId`，否则无 |
| 服务器执行 | `[Jrn.ExternalServerExecution]` | 无 | 无 |
| 外部资源 | `[Jrn.ExternalResourceLoad]` | 无 | 无 |
| 目录注册 | `The add-in folder 'path' was registered` | 路径最后一段 | 无 |
| 程序集 | `The requested assembly 'Name, Version=...' was loaded to the context` | 逗号前的程序集名 | 无 |
| 未加载 | `ExternalDBApplication Class (path) was not loaded`；`Add-in manifest file from: path, won't be loaded` | Class；`.addin`文件名 | 无 |
| 窗口操作 | `Jrn.AddInEvent "AddInJournaling", "WpfWindow(X,...)..."` | `WpfWindow(`的第一个参数，否则无 | 无 |
| 日志控件 | `AddInJournalClient ...` | `control: X =`中的控件名，否则无 | 无 |
| 注销全部服务 | `Unregistering all external services` | 无 | 无 |

名称只取记录本身的文字，不从DLL路径推断；目录注册取目录名，未加载的清单取`.addin`文件名。带厂商ID的记录：`ADSK`或指向`autodesk.com`的说明归Autodesk；VendorId正好是`Autodesk`或以`Autodesk `开头（Revit自身API记录的声明值，如`Autodesk Navisworks`）也归Autodesk，依据为`vendor_id_autodesk`；其他非空ID（如`USERADDIN`、`IFCX`）归第三方；没有厂商字段的记录为来源待定（`vendor_missing`）。清单、外部事件和更新器的厂商ID与说明按`ID(说明)`组合，与加载记录的Vendor同样判断。每条记录自成一个段落，其后未被其他分类认领的行（如`The add-in ID ... is registered in the context`）留在同一段落。

`API_SUCCESS { Registered an external server: ...; ServerId(X); ... }`之后紧跟空的`'E`/`'H`时间戳行和`[Jrn.RegisterExternalServer] Rvt.Attr.ExternalServer: X`时，三行合为一个服务器段落（边界仍为API记录，相同注册照常折叠）；Worker日志中API记录后直接跟不带ID的`[Jrn.RegisterExternalServer]`时两行合并。ID不同、下一行不是该记录或文件在空时间戳行结束时，时间戳行照常开始下一段落；`[Jrn.RegisterExternalServer]`在API记录之前出现时不合并，单独列为没有名称的服务器记录。

按`v`输入`ATFTranslationServer`可按Class前缀找到该项；输入`ATF Translation`也可按名称前缀找到。前缀从字段开头匹配，英文字母不区分大小写，`%`和`_`按普通字符处理，长度最多256字符；它可以与`a/t/u`来源分类及`/`全文关键词同时使用。前缀只列有来源的Add-in记录（各种类），不改变当前分类：在“全部”下包含`API_ERROR`加载失败，在Add-in分类下不含加载失败（属于API错误）。前缀不用于推断官方/第三方来源。`0`恢复全部分类，但保留搜索和前缀；可分别用`/`与`v`清空，或用`g`定位原文并清除全部条件。

`--kind KIND`（可重复）只列这些种类的Add-in记录，取值为下文JSON中`addin_kind`的键，如`--kind service --kind server`；它与Add-in分类、`--category addin_*`来源筛选或`--addin-prefix`同时生效，在其他分类下不使用，并在标准错误中提示。交互界面中它在整个会话里保持，前缀行显示为`名称/Class前缀: 不限 | 种类: 服务、服务器`；没有修改种类的按键，Web查看器用“Add-in名称”列头的菜单勾选种类。

Add-in分类、来源筛选或前缀生效时，列表上方显示`当前日志Add-in: N块 | 来源: Autodesk a / 第三方t / 来源待定u`：N是当前文件Add-in分类的块数，来源计数包含当前文件所有带来源的Add-in记录（各种类，含加载失败），两者有重叠，不能相加；筛选后的当前页数量显示在底部；同一插件反复加载会保留每次记录，便于逐次展开核对，不能把记录条数直接当作已安装插件数量。缺少名称时显示Class，再没有则以种类与来源标签开头，不用Vendor或DLL名称补造插件名称。会话摘要（`s`、`summary`）的Add-in计数仍只统计加载记录。

| Vendor依据 | 来源 |
| --- | --- |
| `ADSK()`、`ADSK(Autodesk, www.autodesk.com)` | Autodesk |
| `RPCA(Autodesk, www.autodesk.com)`或指向`autodesk.com`及其子域名的网站 | Autodesk |
| 其他明确厂商ID/名称，或其他网站；允许没有网址 | 第三方 |
| 厂商缺失、`<None>`、不同厂商字段相互冲突、厂商描述不完整 | 来源待定 |

网站依据按实际域名判断；`autodesk.com.example.org`、其他网站查询参数中的`autodesk.com`都不属于Autodesk域名。厂商字段不跨加载事件继承，错误堆栈或程序集路径中的名称也不用于猜测来源。这是日志声明的厂商归类，不证明默认安装来源、数字签名或组件可信度。

来源标签独立于原有API/错误分类。例如，第三方`API_ERROR`加载事件会同时出现在`t`和`5`筛选中，保留错误颜色、严重级别及完整原文。列表显示`名称 [加载] [第三方] [API错误]`，展开后的标题为`API错误 / 第三方来源`；加载记录出现也不等于加载成功。来源统计与原有类别存在重叠，不能相加当作总块数。本次不增加许可证初始化日志分类，原有Web列表仍按原来的逐行类别展示。

人工样例共有18行、7块：第2–6行为Autodesk加载，第7–9行为第三方加载，第10–12行为第三方加载失败，第13–14行为来源待定；第15行是服务注册`Synthetic Service [服务] [Autodesk]`，第16–18行是服务器注册与其Journal记录合并的`Synthetic Server [服务器] [第三方]`。可按`g`跳到这些行展开核对，按`t`后搜索`failure`应只返回第10–12行。

## 分页输出与程序调用

```bash
revit-journal browse journal.txt --print --category error --page-size 20
revit-journal browse journal.txt --format json --query GeomStepList
# 使用上一页返回的next_after续页；这里的100仅是示例
revit-journal browse journal.txt --format json --after 100
```

重定向或管道环境自动使用非交互输出。每个文件最多返回一页，默认100块，上限200块；结果明确给出是否还有下一页。JSON包含原始字节SHA-256、编码、文件总行数/总块数、块的行号范围、分类、规则ID、分组依据和边界完整状态。

JSON中的`addin_origin`为`autodesk`、`third_party`、`unknown`，非Add-in记录为空字符串；`addin_kind`为上文种类表的键（`load`、`service`、`server`、`manifest`、`event`、`updater`、`server_run`、`resource`、`folder`、`assembly`、`not_loaded`、`ui`、`control`、`unregister`），非Add-in记录为空字符串；`addin_evidence`包含厂商原文摘要、`line_number`和`basis`（`vendor_id_adsk`、`vendor_id_autodesk`、`autodesk_domain`、`other_vendor`、`vendor_missing`、`vendor_incomplete`）。每块最多保留8条依据，每条摘要最多400字符，完整原文仍在索引中。单个跨行Vendor字段超过4096字符时保留为来源待定，不因截断信息而猜测厂商。

`addin_name`与`addin_class`保存提取的名称和Class，各最多512字符；超出时`addin_identity_truncated`显式标记，完整值仍可展开查看。文件层的`addin_count`是全文件Add-in分类的块数，`addin_origin_counts`是各来源（`autodesk`、`third_party`、`unknown`）的Add-in记录数（各种类，含加载失败），`addin_prefix`说明本次前缀条件，`kinds`为本次`--kind`的种类列表（未指定为空列表；在Add-in分类、来源筛选或前缀以外不使用）。文件层的计数不受种类筛选影响。`--print`输出名称摘要，`--format json`提供这些结构化字段。

`--query`的语法与交互搜索相同（见“关键词搜索”）：段落中的任意行命中即返回整个段落，同一块只出现一次；JSON中的每个段落带`hit_line`（段内第一个命中行），文件层带`terms`和`hit_lines`（命中行数，不是段落数）。已知行号可用`g`直接定位。

## 索引与边界

索引数据库默认在用户缓存目录：Windows为`%LOCALAPPDATA%\revit-journal-tools\cache\`，其他系统为`$XDG_CACHE_HOME/revit-journal-tools`（缺省`~/.cache`）。`browse`、`summary`、`search`、`report`共用`terminal.sqlite3`，Web查看器用`viewer.sqlite3`；环境变量`REVIT_JOURNAL_CACHE`或`--database`可改变位置。索引默认只保存每行在源Journal中的字节位置（`--store offsets`），查看时按位置从源文件读取原文，因此源文件要保持原位、不被修改；读取前核对大小、修改时间和文件标识，不一致时明确提示重新打开以重建，不会显示错位的文字。`--store text`把全文存入索引（实测为偏移方式的1.7–2.3倍），之后源文件移走也能查看。正在写入的Journal（读快照）和UTF-16等无法按字节定位行的文件总是存全文。`revit-journal cache`显示位置与大小，`revit-journal cache --clear`只删除这两个数据库及其`-wal/-shm`文件。早期版本写在当前目录的`.journal-cache/`不会自动删除，可手工删除。首次建立磁盘索引需要时间与空间；在交互模式下可先查看已完成的文件，再切换到后续文件。输入发生变化时提示退出重开；下次启动按文件状态、编码和索引版本重建相关文件。

Add-in来源筛选升级了索引版本。重新打开时会自动为本次选择的旧缓存文件重建索引，大目录可能需要较长时间；无需手动删除缓存或修改原始Journal。

阶段、对话框、折叠指纹与会话摘要（2026-10-10，阶段3）升级为`viewer-4`，首次打开会重建所选文件。

分类重组（2026-10-14：Add-in与退出成为独立分类，原“系统数据”按阶段并入启动、退出和其他记录）升级为`viewer-7`，首次打开会重建所选文件。

Add-in记录的种类、名称与来源（2026-10-14：`blocks.addin_kind`，每条Add-in记录自成段落，服务器注册与其Journal记录合并）升级为`viewer-8`，旧数据库自动补充字段，首次打开会重建所选文件。

Add-in种类计数（2026-10-14：`block_counts`增加`kind_<种类>`，供`--kind`与Web“Add-in名称”列头菜单显示本文件各种类的段落数，含加载失败的API错误段落）升级为`viewer-9`，表结构不变，首次打开会重建所选文件。

逐文件编码判定（2026-10-10）再次升级了索引版本（`viewer-3`），并把编码从缓存签名中拆出：`files`表记录请求的编码策略（`auto`会解析为具体候选，如`auto:utf-8,gb18030`）和每个文件实际使用的编码。相同请求或“显式编码等于上次严格使用的编码”时复用缓存；更换编码请求、系统代码页变化或升级后首次打开都会重建所选文件。

文件列表按会话分组（最新在前）：会话标题显示目录（如`Autodesk Revit 2024`）、开始时间、时长、结束状态、各角色文件数和dmp数量/大小；文件行显示修改时间与“正在写入”标记；只有已索引的文本文件可以选择，副本、空文件和dmp作为说明行显示。顶部文件信息行显示实际编码，回退时标“自动回退”，退化时标出退化行数；展开原文时退化行带“含无法解码字节”标记。

分组器只持有当前块的行号、有限标题和规则集合；完整内容在原文表中。展开只读取当前可见页，大段落不整体加载到内存。无法完整解码或索引失败的文件显示失败状态，不能作为完整结果打开。

Linux终端已通过真实PTY验证。Windows 10的conhost与Windows Terminal已用`scripts/windows_console_probe.py`验收（按键、Ctrl+C、退出后恢复控制台模式与离开备用屏，见`docs/validation.md`）。控制台无法进入交互模式（不是控制台，或旧版conhost不支持VT控制序列）时，提示原因并改为输出一页段落摘要，等同`--print`。终端中的控制字符会转为可见转义文字，以免日志中的ESC/OSC内容改变终端状态。

目前不调用AI API：已知Journal结构先由本地规则处理。若后续增加AI分类，仅考虑规则无法判定的少量段落，先做重复内容合并、长度限制和结果缓存；不会让AI通读整份日志。本地首次顺序读取用于严格解码、哈希和全文索引，之后复用磁盘缓存。
