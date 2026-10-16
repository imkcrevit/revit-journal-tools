# 分类重组：Add-in独立、系统数据并入启动/退出/其他（2026-10-14）

## 1. 问题

用户反馈：“其他记录”里明确带有Add-in加载和退出标记的行，应当各自成为独立分类；“系统数据”和“其他记录”的大量内容只是启动、加载或退出过程的上下文，应按上下文并入启动、退出、其他三类。

本机105个Journal（458,256行）的实测分布（`bench.sqlite3`，旧分类）：

| 现分类 | 行数 | 段落数 | 主要内容 |
| --- | --- | --- | --- |
| 其他记录 | 250,999 | 71,812 | `[Jrn.RegisterExternalServer]`、`[Jrn.AddInManifest]`、`The requested assembly ... was loaded to the context`、`Third Party Updater ... registered/unregistered`、`ExternalDBApplication ... was not loaded`、`AddInJournalClient`、`[Jrn.AddInDynamicUpdate]`、`[Jrn.ExternalEventExecution]`、`Jrn.AddInEvent`、`Forcibly unregistering Updater`、`Unregistering all external services`、`N journal(s) aged out`、`Total session time spent reclaiming memory`、`CefSharp has been shutdown`，以及鼠标、`Candidates`、`Edit mode:`等交互期杂项 |
| 系统数据 | 44,098 | 22,678 | 绝大多数是贯穿整个会话的`Delta VM:`内存采样（20,362段），启动期的`Initial VM`、`OPERATING SYSTEM`、`SystemSettings:`、`Options::loadOptions`、`License mode`，会话中的`Connecting session ... Worksharing Log`、`File was saved in Autodesk Revit 20xx` |
| Add-in列表（段落筛选） | — | 5,041 | 仅`API_SUCCESS { Starting External (DB) Application ...}`块，段落分类为API活动，来源作为子标签 |

现有`阶段`筛选（启动/许可/Add-in加载/打开文档/交互操作/退出）已经按会话进程切分段落，但行分类与之无关：`Starting External Application`行被归为“启动”，其段落归为“API活动”，退出阶段的收尾记录全部是“其他记录”。

## 2. 目标分类

行分类（`CATEGORIES`，顺序即终端数字键顺序）：

| 键 | 中文 | 英文 | 内容 |
| --- | --- | --- | --- |
| `startup` | 启动 | Startup | 显式启动标记（`started recording journal file`、`Build:`、`Set Jrn =`等）；以及**第一次用户操作之前**（启动、许可、Add-in加载阶段）所有未被其他分类命中的行，包括原“系统数据”的环境信息与内存采样 |
| `addin` | Add-in | Add-in | 显式Add-in记录，不论阶段：`Starting/Loading External (DB) Application`、`Registered an external service/server`、`Unregistering all external services`、`[Jrn.AddInManifest]`、`[Jrn.RegisterExternalServer]`、`[Jrn.AddInDynamicUpdate]`、`[Jrn.ExternalEventExecution]`、`[Jrn.ExternalServerExecution]`、`[Jrn.ExternalResourceLoad]`、`Jrn.AddInEvent`、`The add-in folder ... was registered`、`Add-in manifest file ...`、`The requested assembly ... was loaded to the context`、`ExternalDBApplication ... was not loaded`、`AddInJournalClient`、`Third Party Updater`、`Forcibly unregistering Updater`、`Forcibly removing all update triggers`；`API_SUCCESS { Starting External Application` 的花括号续行同属此类 |
| `exit` | 退出 | Exit | 显式退出标记（`ExitManagedInstance`、`finished recording journal file`、`License shutdown starting`、`journal(s) aged out`、`Total session time spent reclaiming memory`、`CefSharp has been shutdown`）；以及退出阶段开始后所有未被其他分类命中的行 |
| `error`、`api_error`、`operation`、`warning`、`api`、`dialog` | 不变 | | |
| `other` | 其他记录 | Other | 会话中（第一次用户操作之后、退出之前）未命中任何分类的行，含`Delta VM`采样、空行 |

删除`system`。段落级“Add-in列表”筛选并入`addin`分类；来源子筛选`addin_autodesk`/`addin_third_party`/`addin_unknown`保留为段落视图下的子标签（按`addin_origin`过滤）。

优先级（行）：`API_ERROR`及其续行 > 对话框 > 错误规则 > 警告 > `Jrn.*`操作 > Add-in标记及其花括号续行 > `API_SUCCESS/API_INFO` > 启动标记 > 退出标记 > 按阶段默认（启动期→`startup`，退出期→`exit`，否则`other`）。空行始终为`other`。

阶段判定与`blocks.py`共用同一组标记（`phase_marker`移入`categories.py`，`blocks.py`导入）：第一次非被动`Jrn.*`命令或打开文档标记结束启动期；`ExitManagedInstance`、`finished recording journal file`、`journal exit`、`License shutdown starting`进入退出期，之后不再回退。

段落级：
- 段落初始分类取首行分类；`other`/`startup`/`exit`为弱分类，被段内第一个非弱分类行覆盖（原规则只覆盖`other`）；分类变化导致的分段也只看非弱分类。
- `API_SUCCESS { Starting External Application ...}`段落分类为`addin`；`API_ERROR {`开头的加载段落保持`api_error`并带`addin_origin`。
- `system_section`边界不再改写分类。
- `block_counts["addin"]`改为分类计数；`addin_autodesk`等来源计数不变。

索引：`BLOCK_VERSION`与`INDEX_VERSION`升版（`viewer-7`），已有缓存自动重建；表结构不变。

## 3. 修改计划

### 阶段A 引擎与测试（`categories.py`、`loading.py`、`blocks.py`、`viewer_index.py`、`summary.py`）

1. `categories.py`：`CATEGORIES`改为`startup, addin, exit, error, api_error, operation, warning, api, dialog, other`；新增`_ADDIN`/`_EXIT`正则及字面门控；删除`_SYSTEM`；`_STARTUP`去掉`Starting External`；`Categorizer`增加阶段状态（`startup`/`session`/`shutdown`）与Add-in花括号深度，缓存键包含阶段；`phase_marker`、`_COMMAND`、`_PASSIVE_JRN`、`_OPEN_COMMANDS`从`blocks.py`迁入。`loading.py`的`_START`、`START_GATE`、`loading_payload`、`is_addin_start`迁入`categories.py`并由`loading.py`原名再导出，避免循环导入。
2. `loading.py`：`ADDIN_FILTERS`只剩三个来源；`block_filter`/`matches_filter`对`addin`按分类过滤。
3. `blocks.py`：弱分类集合、API边界分类、去掉`system`改写、版本号。
4. `viewer_index.py`：`INDEX_VERSION`升版；`block_counts`不再单独累加`addin`。
5. 测试：更新断言中的`system`；`test_categories_gates.py`登记新正则与门控；新增正例与反例：每类Add-in标记归`addin`、退出标记归`exit`、首个命令前的`Delta VM`归`startup`、命令后的归`other`、退出标记后的杂项归`exit`、空行为`other`、加载段落分类`addin`、`API_ERROR`加载段落仍为`api_error`、`phase`与`stage`在标记行上一致。

### 阶段B 终端、Web、文档（`terminal.py`、`cli.py`、`web/*`、`i18n.py`、docs、README）

1. 终端：`COLORS`增加`addin`、`exit`，标题/列表用色改用独立键；Add-in摘要行按来源计数措辞；数字键顺序随`CATEGORIES`。
2. Web：分类标签表、`CATEGORY_KEYS`含`addin`；去掉独立的Add-in筛选片，来源子标签在`addin`分类下显示（仅段落视图）；行视图与全部文件搜索允许`addin`；CSS颜色变量。
3. 文档：README、`docs/viewer.md`分类表、`docs/terminal.md`按键与JSON说明、`docs/architecture.md`分类段落。

### 阶段C Add-in记录的名称、种类与来源（用户第二轮反馈）

反馈：列表里的Add-in记录（如`API_SUCCESS { Registered an external service: Name(Generic Repository Helper Service); VendorId(ADSK); ... }`）只显示原文，必须点开才知道是哪个Add-in；注册记录也应区分Autodesk与第三方；`Registered an external server`的API记录与紧随其后的`'E`空时间戳记录加`[Jrn.RegisterExternalServer]`（同一ServerId）应是一个段落。

实测：`Registered an external service` 3,966条全部`VendorId(ADSK)`；`Registered an external server` 4,305条中ADSK 3,704、`Autodesk` 169、`Autodesk Navisworks` 10、`USERADDIN` 384、`IFCX` 38；其中3,211条后面紧跟`'E ...; 0:<`与带相同ServerId的`[Jrn.RegisterExternalServer]`，另有186条是`[Jrn.RegisterExternalServer]`裸行在前、API记录在后（不合并）。

设计：

1. 段落新增`addin_kind`（`blocks`表新增列，迁移用`ALTER TABLE`，`INDEX_VERSION`升为`viewer-8`）。种类与提取规则放在`loading.py`的声明式表`ADDIN_RECORDS`（种类、正则、命名分组`name`/`vendor`/`class`）中，`describe_addin(text)`返回`(kind, name, vendor, class)`：

| `addin_kind` | 标签 | 记录 | 名称来源 | 来源依据 |
| --- | --- | --- | --- | --- |
| `load` | 加载 | `Starting/Loading External (DB) Application:`（现有`LoadingEvidence`） | 现有 | 现有`Vendor :` |
| `service` | 服务 | `Registered an external service: Name(X); VendorId(V)` | `Name(...)` | `VendorId(...)` |
| `server` | 服务器 | `Registered an external server: Name(X); VendorId(V); ServerId; ServiceId` | `Name(...)` | `VendorId(...)` |
| `manifest` | 清单 | `[Jrn.AddInManifest] ... Rvt.Attr.AddInName: X` | `AddInName` | 无→`unknown` |
| `event` | 外部事件 | `[Jrn.ExternalEventExecution] ... Rvt.Attr.AddInName: X` | `AddInName` | 无→`unknown` |
| `updater` | 更新器 | `[Jrn.AddInDynamicUpdate] ... AddInName: X`；`Third Party Updater 'X' registered/unregistered`；`Forcibly unregistering Updater with id:…`；`Forcibly removing all update triggers` | `AddInName`或引号内文字；无则空 | 无→`unknown` |
| `server_run` | 服务器执行 | `[Jrn.ExternalServerExecution]` | 空 | `unknown` |
| `resource` | 外部资源 | `[Jrn.ExternalResourceLoad]` | 空 | `unknown` |
| `folder` | 目录注册 | `The add-in folder 'path' was registered` | 路径最后一段 | `unknown` |
| `assembly` | 程序集 | `The requested assembly 'Name, Version=…' was loaded to the context` | 逗号前的程序集名 | `unknown` |
| `not_loaded` | 未加载 | `ExternalDBApplication Class (path) was not loaded`；`Add-in manifest file from: path, won't be loaded` | Class / 文件名 | `unknown` |
| `ui` | 窗口操作 | `Jrn.AddInEvent "AddInJournaling", "WpfWindow(X,…)…"` | `WpfWindow(`第一个参数 | `unknown` |
| `control` | 日志控件 | `AddInJournalClient …` | `=`前的控件名；无则空 | `unknown` |
| `unregister` | 注销全部服务 | `Unregistering all external services` | 空 | `unknown` |

2. `VendorId`的来源判定复用`vendor_origin`，并增加一条：VendorId正好是`Autodesk`或以`Autodesk `开头（Revit自身API记录的声明值）→`autodesk`，依据`vendor_id_autodesk`；其余非空→`third_party`；缺失→`unknown`。`addin_evidence`记录`VendorId(...)`原文与依据。`addin_origin`非空即为Add-in记录，所以来源子筛选与`--addin-prefix`覆盖全部种类。

3. 会话摘要（`summary.py`）的Add-in计数、第三方名单、加载失败只统计`addin_kind == "load"`，`session_view`文案不变。

4. 合并：当前段落是`server`种类的API记录且已闭合时，紧随其后的空`'E`/`'H`时间戳记录先暂存不开新段；若再下一行是`[Jrn.RegisterExternalServer]`且其`Rvt.Attr.ExternalServer`与该记录的`ServerId`相同（或该行不带ID），则两行并入当前段落后结束；否则按原逻辑先结束当前段落、再以暂存的时间戳行开新段。`BlockBuilder.finish()`要把暂存行按原逻辑收尾。

5. 展示：终端`block_summary`为`名称 [种类] [来源]`（无名称时`[种类]`开头），`addin`分类及来源子筛选下都按单行名称列出；Web列表行显示种类标记、来源标记与名称，详情头显示种类；`/api/blocks`、`/api/block`、`browse --format json`输出`addin_kind`；中英文标签表（`i18n.py`、`app.js`）加`addin_kind`。

6. 测试：每种记录的提取正例与一条反例；`VendorId(Autodesk)`→autodesk、`VendorId(USERADDIN)`→third_party、无VendorId→unknown；合并的正例（ID相同）、反例（ID不同、下一行不是注册记录、文件在空时间戳行结束）；摘要只计加载记录；JSON与HTTP字段。

### 阶段D Web页面：分类集中查看与列头筛选（用户第三轮反馈）

反馈截图（Web，约1040px宽）：分类片与来源行占两行且右侧被截断，要滚动才能看到其余分类与状态；希望按顶部标签集中查看，不必滚动；并且点击列名就能看到该列的各种取值，勾选后只显示选中的类型。

设计：

1. 布局：页面整体不滚动，只有记录列表与段落原文各自滚动；在1024×700窗口下，分类片、工具栏、列头与列表首行都应可见。工具栏（搜索、范围、阶段、折叠、跳行）允许换行；分类片在窄窗口下紧凑（更小内边距、计数灰色），来源子标签与分类片同一区域内换行，不出现水平滚动条。文件概览（标题、路径、四个统计卡）在窗口高度不足时收紧为一行。
2. 列头筛选菜单（无内联脚本，键盘可关闭，点击外部关闭）：
   - “分类”列头：列出各分类及计数（段落视图用`block_counts`，逐行视图用`counts`），单选，含“全部”；效果等同点击分类片。
   - “来源”列头（Add-in分类及来源子筛选下）：列出三种来源及计数，单选，含“全部”；效果等同来源子标签。
   - “Add-in名称”列头：列出14种记录种类（`ADDIN_KINDS`）及本文件计数，多选勾选框，附“全选”“清空”，确定后只显示选中种类；选中状态在列头上标出（如“Add-in名称 · 3种”）。
3. 接口：`/api/blocks`新增`kinds`参数（逗号分隔的`ADDIN_KINDS`子集，未知值400），只在`addin`、来源子筛选或名称前缀下生效；`block_counts`增加`kind_<kind>`计数（索引时统计，`INDEX_VERSION`升为`viewer-9`）。`browse`新增`--kind`（可重复）与JSON输出对应字段，终端界面不加交互。
4. 验收：单元测试覆盖`kinds`参数与计数；无头Edge在1024×700与1400×900各截一张“全部”“Add-in”视图截图，确认无水平滚动条、分类片全部可见、列头菜单可打开。

### 不做

- 不改阶段划分（`PHASES`），不把`ID_APP_EXIT`命令提前为退出期起点。
- 不改规则集、不改分词与搜索。
- `Jrn.AddInEvent`（Add-in窗口内的交互记录）归`addin`而非`operation`。

## 4. 验收

1. `PYTHONPATH=src python -m unittest discover -s tests`全部通过。
2. 本机全部Journal重建临时索引：无`system`；退出阶段段落里不再有`other`；`addin`行数不少于上表所列显式标记行数之和；`search --category addin`与`--category exit`可用；冷索引耗时与6.2s基线相比不明显退化。
3. 终端`1`–`9`、`c`、`i/a/t/u`切换无异常；Web分类片与来源子标签正常；`?lang=en`标签正确。

## 5. 执行记录

阶段A、B各由一个Opus 5.5代理编码，审查后合并到同一工作树；测试从234项增至258项，全部通过。

### 与计划的差异

- API记录（`API_SUCCESS`/`API_INFO`）的优先级提到启动、退出标记之前：`API_SUCCESS { Unregistering ApplicationInitialized event ...}`原本会因`ApplicationInitialized`被归为启动。
- 段落分类为`addin`的条件放宽为“加载起始行或首行分类为`addin`”，否则`API_SUCCESS { Registered an external server ...}`在行视图是Add-in、在段落视图却是API活动。
- `_EXIT`同时匹配`worker log(s) aged out`，它总与`journal(s) aged out`相邻。
- 引擎增加一次合并门控`_MARKER_GATE`（加载起始、Add-in、启动、退出四组字面量的并集），且只在启动期评估`_COMMAND`、进入退出期后不再评估阶段标记；单进程分类耗时2.61s→2.97s，冷索引整体在噪声范围内。
- `--addin-prefix`/`v`不再把分类强制为`addin`：前缀本身只列加载记录，而加载失败的记录分类为API错误，强制`addin`会把它们藏起来。
- `browse --format json`新增`addin_origin_counts`；`addin_count`改为Add-in分类的段落数。
- 终端列表标题改用加粗而非青色（青色现在表示Add-in）；页脚去掉`i`（仍可用，等同`2`）。
- Web中“来源/Add-in名称”列标题只在来源子筛选下显示，`addin`分类混有清单、服务等记录，按普通分类显示。

### 真实数据验收（本机105个Journal，458,265行，临时索引）

| 项目 | 旧 | 新 |
| --- | --- | --- |
| 行：其他记录 / 系统数据 | 251,002 / 44,104 | 240,087 / — |
| 行：启动 / Add-in / 退出 | 6,787 / — / — | 31,683 / 31,615 / 5,661 |
| 段落：Add-in / 退出 | — | 23,324 / 1,592 |
| 退出阶段段落 | 含1,536个其他记录、548个系统数据 | API 6,090、退出1,592、Add-in 524、警告21、错误2，无其他记录 |
| 带来源的加载段落 | API活动5,012、API错误29 | Add-in 5,012、API错误29 |
| 冷索引加`--print`全部会话 | 7.3s | 7.4–7.6s |

`search "aged out" --category exit`命中49行/25个文件；`search "Third Party Updater" --category addin`命中2,754行/96个文件，与grep一致；`browse --print --category addin`显示`当前日志Add-in: 455块 | 加载记录: Autodesk 108 / 第三方0 / 来源待定0`。

### 阶段C执行记录

一个Opus 5.5代理编码，审查后测试286项通过（新增28项）；冷索引7.1–7.5s，与阶段B持平。

与计划的差异：

- 每条Add-in记录单独成段（新边界`addin_record`），否则连续的清单、程序集、目录注册行会合成一段而只显示第一条的名称；Add-in段落由23,324增至28,037。
- `[Jrn.RegisterExternalServer]`裸行也归`server`种类（无名称、来源待定）；API记录后直接跟注册记录（Worker日志，无ID）的情况也合并。
- 清单、外部事件、更新器记录从`Rvt.Attr.(Command)VendorId`与说明文字取来源，判定方式与加载记录的`Vendor :`相同；缺失才为待定。
- `ADDIN_RECORDS`每行是（种类、字面门控、正则），门控登记进`test_categories_gates.py`；记录文本只读前4,096字符以限制正则扫描。
- 来源标签改为“Autodesk来源/第三方来源/来源待定”，因为来源现在覆盖所有种类而不只是加载。
- 无名称的非加载记录显示为`[种类] [来源] 原文`。

真实数据（105个文件，184,578段）：28,037个Add-in段落全部有种类与来源。服务器注册4,393段中3,259段已与`'E`加`[Jrn.RegisterExternalServer]`合并（含286段前面另有一条`'E`的四行段落），319段与无ID的注册行合并为两行，815段后面没有注册记录；独立的`[Jrn.RegisterExternalServer]`段只剩1个。无名称的Add-in段3,218个（11.5%），主要是外部资源、`Forcibly unregistering Updater`、注销全部服务与服务器执行这些原文本身不含名称的记录。用户截图中的第552行记录现为L552–554一段，摘要`AecAlignment Server [服务器] [Autodesk]`。

### 阶段D执行记录

一个Opus 5.5代理编码，审查后测试295项通过（新增9项）。代理用Playwright驱动本机Edge无头运行实际点击验证：1024×700下文档`scrollWidth`与`clientWidth`均为1024、无滚动偏移，默认视图11个分类片与Add-in视图15个片全部可见；勾选“服务”“服务器”后列表只剩这两种；切换文件、离开Add-in分类或切到逐行视图会清除种类。截图在scratchpad（`dark-addin-1024x700.png`、`dark-menu-kinds-1024x700.png`等），已目视核对。

与计划的差异：

- `.app`与`.main`改为`overflow: clip`，脚本的`scrollIntoView`也无法再滚动整页（用户截图里表头被滚出视野的推测原因，未复现）。
- `--kind`在交互终端里作为启动时的固定筛选生效，前缀行显示“种类: …”；不在Add-in视图时stderr提示。
- `kind_*`计数含加载失败的API错误段落，与来源计数一致，所以`addin`分类下“加载”的计数可能比可见行多。
- 全部勾选等于不筛选；一项都不勾时“确定”不可点；全部文件搜索时分类菜单不显示计数，也不使用种类但不清除。
- 宽度≤1500px时概览一行隐藏会话标签，放进“会话摘要”按钮提示；≤900px沿用原上下排列。

### 遗留

- 启动阶段以外仍有95行显式启动标记（`InitNativeInstance`等在会话中再次出现）保持“启动”分类，这是显式标记优先于阶段默认的结果。
- 启动期的空行（89个纯空行段落）保持“其他记录”。
- 索引版本升为`viewer-7`，已有缓存会在下次打开时重建。
- 未在Windows Terminal里手工走一遍数字键与来源子筛选，交互验收靠单元测试与`--print`；Web只核对了接口与首页截图，未点开Add-in分类。
