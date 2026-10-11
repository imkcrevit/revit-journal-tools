# 审查修改计划执行记录（2026-10-10）

依据：`docs/review-2026-10-10.md`第4节修改计划，已执行阶段0至阶段4（J1–J8、J10–J21、J25）；J22随J6处理（分类正则已预编译）；J23先靠门控降低规则正则的暴露面，后补了加载时的嵌套量词检查（见文末）；J9（可选的偏移模式）未实施；J24（Web会话视图）已在文末补做，见各阶段末“未完成与未验证”。阶段3单文件建索引低于35,000行/秒的偏差已在文末“阶段3速度偏差的修复”中处理。

环境：Windows 10 19045、Python 3.12.10、SQLite 3.49.1、系统代码页cp936、16个逻辑核。所有真实Journal只读使用，路径为`%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit <year>\Journals`，没有复制进仓库、测试或`cases/`。

基准命令（仓库根目录，`$env:PYTHONPATH='src'`）：

```powershell
py -3 scripts/benchmark_real.py scan  --encoding gb18030   # 默认取本机最大文本Journal
py -3 scripts/benchmark_real.py parse --encoding gb18030   # 只读/分类/分块，不写库
py -3 scripts/benchmark_real.py index --encoding gb18030   # 单文件建索引与索引体积
py -3 scripts/benchmark_real.py dir --revit 2024 --encoding gb18030   # browse --print整目录
```

单文件基准默认取`Autodesk Revit 2026/journal.0053.txt`（6,466,213字节、85,387行，即审查文档中的6.47MB文件）；目录基准为`Autodesk Revit 2024/Journals`（21个文本文件、11,041,479字节，另有2个`.dmp`不参与）。单文件取3次中最快一次，目录取2次中最快一次；每次在独立子进程中运行。

## 阶段0：基线与测试

### J18 Windows测试失败

- `tests/test_viewer.py`：HTTP测试改用`urllib.request.build_opener(urllib.request.ProxyHandler({}))`直连，避免系统代理（本机`127.0.0.1:10808`）改写`Host`；顺带关闭预期的`HTTPError`响应。
- `tests/test_loading.py`：旧数据库迁移测试改用`contextlib.closing(sqlite3.connect(...))`，测试结束前关闭连接，Windows可删除临时目录。
- 结果：改前107项中1失败1错误；改后107项全部通过。

### J10 基准脚本

- `scripts/benchmark_streaming.py`：Windows下用`ctypes`调用`psapi.GetProcessMemoryInfo`取`PeakWorkingSetSize`，不再返回`None`。合成8MiB峰值19.85MiB、32MiB峰值19.88MiB。
- 新增`scripts/benchmark_real.py`：读取本机`%LOCALAPPDATA%`下的Journal，提供`list/scan/parse/index/dir/all`入口和`--profile`；临时数据库放系统临时目录并删除，结果只打印，不入库。脚本为纯ASCII。

### 基线（改前，提交`60499be`代码）

| 场景 | 本次实测 | 审查文档2.3 |
| --- | --- | --- |
| scan，6.47MB/85,387行，gb18030 | 2.781s，2.32MB/s，50个命中，峰值21.1MiB | 2.79s，2.2MB/s，50个命中 |
| 只读解码 | 0.111s | 0.34s（剖析器下） |
| 分类＋分块（feed+classify，不写库） | 2.747s | 约4.8s（剖析器下） |
| 建索引，6.47MB | 5.283s，16,163行/秒，1.22MB/s；索引22.2MB＝3.44倍 | 5.59s，15,275行/秒，3.44倍 |
| `browse --print`，2024目录21文件，gb18030 | 10.54s；缓存38.4MB＝3.48倍 | 14s；38.4MB（3.5倍） |
| `browse --print`，2024目录，默认`auto` | 退出码2，21个文件中20个“无法解码” | — |
| 合成32MiB扫描 | 10.35s，3.2MB/s | 10.49s，3.1MB/s |

目录耗时比审查文档少约3.5s，可能与机器负载有关；后续对比统一以本次实测10.54s为基线。

偏差：无。

## 阶段1：读取正确性

### J1 逐文件编码判定与退化解码

- `reader.py`：新增`resolve_encoding`与`EncodingPolicy`。`auto`为BOM→严格UTF-8→系统ANSI代码页的严格超集（`locale.getencoding()`；cp936/GBK/GB2312映射为gb18030，cp1252保持，UTF-8/ASCII系统不追加）。每个文件先用增量解码器对整文件做严格预检，选第一个完整成功的候选，再按原流程逐行读取并计算SHA-256。
- 所有候选都失败时按最后一个候选退化解码：自定义错误处理器把坏字节暂存为U+DC00–U+DCFF孤立代理（不吞掉换行字节，行号不变；已用穷举确认gb18030的2/4字节序列不会产生代理），逐行替换为可见的`\xNN`并标记`JournalLine.degraded`。读取器提供`encoding_fallback`、`degraded_line_count`和前20个退化行号。没有采用`surrogateescape`：gb18030的不完整4字节序列错误区间含ASCII数字，`surrogateescape`会直接抛错。
- 显式单个编码保持严格模式（与旧行为一致）；新增逗号分隔候选链（如`utf-8,gb18030,cp1252`，`auto`可作为链中的一项）。
- 报告：`ScanReport`新增`encoding_requested`、`encoding_fallback`、`decode_degraded_line_count`、`decode_degraded_lines`，`SourceLine`新增`decode_degraded`；回退与退化都写入`warnings`，文本报告在编码处注明“自动回退”或“退化解码N行”，退化行带“[含无法解码字节，以\xNN显示]”。`schema_version`仍为1（只增字段）。
- 索引：`files`新增`encoding_request/encoding_requested/encoding_fallback/degraded_lines/degraded_sample`，`lines`新增`degraded`；旧库用`ALTER TABLE`补列。终端文件信息行、`--print`、JSON、Web上下文面板都显示实际编码与回退/退化状态。
- 原有4项以`auto`断言“整文件失败”的测试改为显式`--encoding utf-8`（严格语义仍被覆盖），另新增退化可见性测试。

验收：本机113个文件不传`--encoding`全部可处理——104个文本文件全部建索引成功（96个gb18030回退严格解码、8个UTF-8，0个退化行），7个`.dmp`计数、1个字节相同的副本与1个空文件不重复索引；2024目录`browse --print`默认参数退出码0（改前20/21失败）。

### J2 混合目录与缓存签名

- `files.signature`只含索引版本与规则集；编码策略单独存为`encoding_request`（`auto`解析为具体候选，如`auto:utf-8,gb18030`，系统代码页变化会触发重建），实际编码存`encoding`。
- 复用条件：请求策略相同，或显式单个编码等于上次严格使用的编码且无退化行。复用时保留原请求，避免把auto得到的结果误标为显式请求。
- `INDEX_VERSION`升为`viewer-3`，旧缓存首次打开会重建所选文件（已写入`docs/terminal.md`）。
- 测试：同一工作区内gb18030、cp1252、UTF-8三个文件用候选链一次索引，各自记录实际编码；`auto`下cp1252文件被标为退化而不是静默；缓存复用/重建规则。

偏差：计划写“签名应记录实际使用的编码”。实际编码只有建索引后才知道，注册时无法用于签名比较，因此改为“签名不含编码＋单独记录请求策略与实际编码＋按上述条件复用”，效果相同且不会因换写法重复建索引。

### J4 会话归组、副本去重、0字节文件

- 新增`sessions.py`：只按文件名与目录归组（`journal.NNNN.txt`、`.workerN.log`、`.NNNN.dmp`、`.txt.copy.txt`、`.txt.abbrev`），其他命名为“未识别命名”单文件会话并照常读取。大小相同的文本输入计算SHA-256，字节相同者只保留一个（优先主Journal），其余为`duplicate`；0字节为`empty`、`.dmp`为`binary`，都不入索引队列但在会话中列出。
- `cli._discover`在目录中额外收集`.dmp`作为会话旁注；`scan`对副本输出`status: duplicate`记录（含`duplicate_of`与SHA-256），不重复扫描。
- `ViewerIndex.register`接受规划结果，`catalog()`按会话→角色排序并附`session/role`，新增`sessions()`。终端文件列表按会话分组（会话标题、可选的已索引文件、不可选的旁注行），`--print`按会话输出，JSON新增`sessions`节；Web默认选中首个非空主Journal。
- 本机：57个会话（56个主Journal会话＋`dump.0001.txt`），`Autodesk Revit 2021/journal.0026.txt.copy.txt`与原文件字节相同，只索引一次。

偏差：会话的排序仍按目录与编号升序；“修改时间倒序、默认选中最新会话”属于阶段3的J11，本次未提前实现。0字节文件改为旁注而非可打开的空文件，原测试`test_empty_file_is_ready_with_no_events`相应改为断言旁注。

### 测试与基准

- 测试：130项全部通过（阶段0为107项；新增读取器编码10项、会话6项、CLI 2项、索引/终端5项）。
- 基准（同一文件/目录，改前→改后）：

| 场景 | 阶段0 | 阶段1 |
| --- | --- | --- |
| scan 6.47MB，`auto` | 失败（UTF-8严格） | 2.696s，2.40MB/s，50个命中，gb18030回退 |
| scan 6.47MB，`gb18030` | 2.781s | 2.657s |
| 建索引6.47MB，`auto` | 失败 | 5.569s，15,333行/秒，3.45倍 |
| `browse --print` 2024目录，`auto` | 退出码2，20/21失败 | 11.09s，0失败，3.50倍 |

预检使gb18030文件多一次约30ms的整文件解码；建索引比阶段0慢约0.3s，在多次运行的波动范围内（阶段0中位数5.47s、阶段1中位数5.63s），另有`lines.degraded`列的写入开销。

未完成项：无。

## 阶段2：速度

按计划顺序J5→J6→J7→J8实施，每步跑全部测试与基准。等价性用新增的`scripts/verify_equivalence.py`核对（纯ASCII，不含真实数据，只打印计数、文件标签、行号与字段名）：旧源码树用`git archive 4ac90a4 src`（阶段1提交）解压到仓库外，新旧两棵树分别在子进程中处理本机全部106个文本Journal（约45.5万行），逐行比对。测量期间本机有用户自己运行的Revit 2027在写入新Journal（`Autodesk Revit 2027/journal.0037.txt`），总行数在两次核对之间从455,249增至455,252；同一次核对中新旧两棵树读取一致，无差异。

### J5 规则字面量门控

- `rules.py`：`Rule`新增可选`gate`（加载时校验为非空字符串数组并折叠），`definition()`包含`gate`（规则集哈希因此变化，旧缓存会重建）；新增`fold()`与`match_rules()`，每行只折叠一次，门控子串都不在时跳过该规则正则；`Rule.match_pattern()`为不带门控的原始匹配。`analyzer`与`Categorizer`改用`match_rules`。
- 内置12条规则按附录B逐条核对正则后补齐`gate`（每个可选分支都含其中一个字面片段；`generic.file_access`的`access`同时覆盖`access denied`与`access is denied`）。
- 偏差：计划写“`lower()`后`in`检查”。穷举全部码位发现`re.IGNORECASE`还会把`İ`(U+0130)、`ı`(U+0131)匹配为i，`ſ`(U+017F)匹配为s（`K`(U+212A)的`lower()`已是k），单纯`lower()`会让“`Aſſertion failed:`”这类行在有门控时漏掉。因此`fold()`先替换这三个字符再`lower()`（纯ASCII行零开销）。测试中把`fold`换成`str.lower`，门控等价测试会失败，说明测试能发现该问题。
- 测试：`GateEquivalenceTests`在固定种子生成的6000行随机组合（含大小写变化与上述相似字符）、全部测试与示例文本上，证明有门控与无门控（`dataclasses.replace(rule, gate=())`）的命中列表逐项相同，且每条规则都被正向覆盖；另有门控校验与“自定义规则可不写gate”的测试。
- 真实数据：`verify_equivalence.py gates`在106个文件上0行差异，正则调用5,462,988次→6,485次；`verify_equivalence.py scan`（不设明细上限）新旧两棵树的全部候选、匹配列、上下文行号、分组、计数完全一致。

### J6 分类/分段/加载的Python侧重复工作

- 全部正则预编译（含原来每次调用才编译的`ExceptionCode`、`^\d+:<`、缩进判断和`API_ERROR`）。
- 每个忽略大小写的正则前加按`fold`检查的模块级字面量门控：`categories`的`_JOURNAL/_STARTUP/_SYSTEM`，`blocks`的`_API/_METRIC/_SECTION/_STRONG_ERROR/_NATIVE_*`，`loading`的`_START`（`application/addin/add-in`；计划写的区分大小写`"Application" in text`会漏掉小写写法，已改为折叠后检查）；区分大小写的`_EXCEPTION`用区分大小写的门控。
- `BlockBuilder.feed`每行只算一次形状判断（原来`is_addin_start`每行最多算两次、`loading_payload`每行至少一次），`payload`与`fold`结果传给`_add`和`LoadingEvidence.feed`；`LoadingEvidence`未激活时只检查加载前缀门控。`_POSSIBLE_ERROR`预筛选保留（它是分类语义的一部分），只把生成器换成`any(map(...))`。
- 偏差：没有新建跨三个类共享的“行形状结构体”，以免改动`classify(text)`、`feed(line, classification)`等被测试直接使用的接口；`Categorizer`自己的`lower()`在85k行上只占约24ms。目标已达到。
- 测试：新增`tests/test_categories_gates.py`，(1)每个正则在语料中的每次命中都含其门控；(2)把全部门控打补丁为`("",)`（等于总是运行正则）后，示例、测试文本和3份各4000行随机Journal的分类结果与块（起止、类别、级别、规则、标题、边界、闭合、Add-in字段）完全相同。删掉一个门控词、或把`fold`换成`str.lower`，测试都会失败。
- 真实数据：`verify_equivalence.py blocks`新旧两棵树0差异——106个文件记录、455,249行、179,523块。另与原始基线`60499be`比对，唯一差异是空文件`Autodesk Revit 2024/dump.0001.txt`（原来以0行文件入索引，J4后为旁注），其余行与块全部一致。

### J7 SQLite写入

- 每个文件一个事务：短事务清理旧行并标记`indexing`；`parse_into`按2万行一批写入不提交；文件读完后在同一事务内`INSERT INTO line_search(rowid, body) SELECT id, body FROM lines WHERE file_id=?`批量建立全文索引，再写统计、置`ready`。失败/停止整体回滚。
- 删除逐行触发器`lines_insert`与`lines_delete`（打开旧库时`DROP TRIGGER IF EXISTS`）；重建时用`INSERT INTO line_search(line_search, rowid, body) SELECT 'delete', ...`按文件删除FTS条目。“每一行都有FTS条目”的不变式在新旧库中都成立（旧库触发器与行同事务），因此迁移安全；`integrity-check`测试通过。
- 构建连接`cache_size`提到64MB；建立中的进度改为内存中的`ViewerIndex.progress`，`catalog()`合并显示。
- 隔离实验（同一文件85k行，只测写入）：逐行触发器＋每5000行提交1.996s；单事务＋批量FTS 0.404s（OFF）/0.493s（NORMAL）。
- 偏差：`synchronous`保持NORMAL，没有改为OFF。完整流程实测OFF只快0.15s（2.20s→2.05s，约7%），而WAL＋OFF在断电/系统崩溃时可能损坏整个数据库文件；计划所说的“失败即重建”只覆盖未完成文件，数据库文件损坏时程序打开即报错，需要用户手动删除缓存。目标在NORMAL下已达到。

### J8 按文件并行

- 至少2个文件且总量不小于2MB时，用`multiprocessing.get_context("spawn")`进程池（默认`min(8, 逻辑核数-1)`，`ViewerIndex(workers=...)`可调，1为顺序）。子进程把整个文件解析进系统临时目录中的私有临时库（无索引、`journal_mode=OFF`）并返回元数据；主进程按完成顺序`ATTACH`后`INSERT ... SELECT`复制，FTS与状态在同一事务。只有主进程写共享索引，子进程不使用主进程的连接。任务按文件大小降序分配以缩短总耗时，小文件仍会较早完成并先可用。子进程忽略Ctrl+C，停止时终止进程池，未复制的文件保持`queued`；无法启动进程池（`OSError`）时退回顺序构建。`__main__.py`加了`if __name__ == "__main__"`保护与`multiprocessing.freeze_support()`（为以后J21打包准备，未打包时无作用）。
- 偏差：计划写“子进程产出行批与块批、主进程串行写库”。改为子进程写临时库：226MB级文件不必把全部行对象经管道序列化、内存有界，主进程只做C层面的`INSERT ... SELECT`。
- 进程数（2024目录，仅建索引）：1个4.93s、2个2.89s、4个2.32s、8个2.16s、12个2.18s；8个以上收益消失，下限由最大文件（3.7MB）的解析决定。
- 测试：并行与顺序构建的`lines`/`blocks`/统计完全相同且无法解码的文件单独失败；并行构建后重建单个文件FTS一致；停止事件使文件保持`queued`。真实数据：`verify_equivalence.py blocks --together`（全部文件一次注册，新代码走进程池）与旧树0差异，104个可索引文件旧树35.4s、新代码7.1s。

### J9 偏移模式（可选）：未实施

原因：(1)当前读取器基于`TextIOWrapper`的通用换行，逐行拿字节偏移需要改为二进制分行读取（还要兼容独立CR与退化解码），等于重写读取层；(2)正文按需从源文件解码要求源文件不变，而J3（正在写入的Journal、快照模式）尚未实施，本机测量期间就有一个正在增长的Journal；(3)contentless FTS无法再用`substr(body)`做预览，查询层需要同步改。建议在J3快照模式完成后，与临时副本一起实施。

### 测试与基准

测试：140项全部通过（阶段1为130项；新增J5规则5项、J6门控2项、J8并行3项）。

| 场景（同一文件/目录） | 阶段0基线 | 阶段1 | J5后 | J6后 | J7后 | J8后（最终） |
| --- | --- | --- | --- | --- | --- | --- |
| scan 6.47MB，`auto` | 失败 | 2.696s | 0.527s | 0.497s | — | 0.558s（11.6MB/s） |
| scan 6.47MB，`gb18030` | 2.781s（2.32MB/s） | 2.657s | 0.463s | — | — | 0.488s（13.3MB/s） |
| 分类＋分块（feed+classify） | 2.747s | — | 2.624s | 1.023s | — | 1.047s |
| 建索引6.47MB | 5.283s，16,163行/秒 | 5.569s | 5.536s | 3.796s，22,494行/秒 | 2.222s，38,427行/秒 | 2.224s，38,401行/秒 |
| 单文件索引体积 | 3.44倍 | 3.45倍 | 3.45倍 | 3.45倍 | 3.28倍 | 3.28倍 |
| `browse --print` 2024目录，`auto` | 失败 | 11.09s | — | — | 5.27s | 2.58s（4.28MB/s；加入串行回退后复测2.79s） |
| `browse --print` 2024目录，`gb18030` | 10.54s | — | — | — | — | 2.61s |
| 目录缓存体积 | 3.48倍 | 3.50倍 | — | — | 3.60倍 | 3.57倍 |
| 合成32MiB扫描 | 10.35s | — | — | — | — | 1.385s（24.2MB/s） |

阶段2目标核对：J5 scan≤0.7s（0.49–0.56s）达成；J6 feed+classify≤1.5s（1.02–1.05s）达成；J7建索引≤2.5s且≥35,000行/秒（2.22s，38,401行/秒）达成；J8目录≤5s（2.58–2.79s）、相对基线10.54s为3.8–4.1倍，达成；总目标“单文件扫描≥8MB/s、建索引≥35,000行/秒、目录并行≥3倍”全部达成。

其他观察：
- 已建索引后的查询延迟与原始基线相同：分类1.5ms、FTS 3–5ms、按行定位1.4ms（同一6.47MB文件）。
- 建索引峰值内存45MB→59MB（构建连接64MB页缓存与单事务）；扫描峰值21–23MB不变。
- 目录缓存体积略增（3.48→3.57倍），单文件减小（3.44→3.28倍）：每个文件批量建FTS会各自产生段，小文件多时段数多。可在批量建立后执行FTS5 `optimize`合并，但会重写整个FTS索引，本次未做。

未完成项与遗留：J9未实施（见上）；CLI未增加`--jobs`参数（只有`ViewerIndex(workers=)`）；正在写入的Journal在建索引时仍会因“源文件发生变化”进入失败状态，需J3处理。

## 阶段3：Revit工作逻辑（含J3）

范围：J3、J11、J12、J13、J14、J15、J16。J3原在阶段1的发现清单中（P1），实时Journal是“Revit正在卡，看它在做什么”场景的前提，因此与阶段3一起实施。环境与基准命令同上；测量期间本机没有运行Revit。

### J3 正在写入的Journal（`live.py`）

- 探测：Windows上用`CreateFileW(GENERIC_READ, FILE_SHARE_READ)`只读打开，被其他进程以写方式打开时返回共享冲突（32），即“正在写入”；不加锁、不修改，句柄立即关闭。非Windows恒为否。
- 快照：读取当时的文件长度，截到最后一个完整换行（UTF-16/32按BOM与码元对齐），写入私有临时目录后再读取、哈希和建索引，结束即删除。`--snapshot auto`（默认）对正在写入或读取中变化（`SourceChangedError`）的文件读快照；`always`总是；`never`保持原行为。
- 报告/索引：`ScanReport.live/snapshot`、`files.live/snapshot`；警告写明快照时间、字节数与“SHA-256针对快照字节”；列表与TUI标注“正在写入/实时快照”。正在写入的会话结束状态为`live`，不生成`session.no_clean_exit`。
- 验证：单元测试覆盖截断半行、UTF-16对齐、`always/never`、临时目录清理；Windows上用Python以追加方式持有文件模拟Revit，确认探测为真、扫描与索引走快照、会话为`live`。真实Revit持有的Journal本次未测（测量时未运行Revit）。

### J11 自动发现与“最新优先”

- `discovery.py`：枚举`%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit [LT ]<year>\Journals`，新版本在前。`scan/browse/summary/view`不传路径时使用；`--revit YEAR`（可重复、逗号分隔）、`--latest [N]`；显式路径加`--revit`时按所在年份目录过滤。新增`--jobs`。
- 排序：会话按其最新成员的修改时间倒序，会话内主Journal→精简→worker→钢连接日志→未识别→副本→dmp。`ViewerIndex.preferred_id`为最新主Journal：TUI与Web默认打开它，顺序构建时第一个处理，并行构建时第一个提交。
- 真实数据：不带参数的`summary`列出本机7个版本56个会话，首项为`Autodesk Revit 2027/journal.0037`（最新）。112个输入冷启动7.45s（8进程），缓存命中0.67s。

### J12 会话层与摘要

- `summary.py`的`SessionCollector`在建索引的同一遍读取中收集：Build/Branch/Release、`Session<pid>_P0MainQueue`进程号、起止时间与时长（`started recording`起算）、`finished recording journal file`行号、`ExitManagedInstance/Journal Exit`、许可记录与`revit.license_error`、`openFromModelPath [路径]`与打开失败、Add-in加载来源计数与第三方名称、按程序集聚合的版本冲突、对话框、阶段首行、最后20行、最后10个操作/对话框块。全部有上限，存为`files.summary`（JSON）。
- `session_findings`：`session.no_clean_exit/license_error/model_open_failed/addin_conflict/steel_localdb/crash_dump`，`status: candidate`，每项带证据行号。
- 输出：新命令`summary`（文本/JSON，`--tail`）、TUI`s`面板、`browse --format json`的`sessions`节。
- 验收：本机55个主Journal中18个没有结束标记；独立对原始字节`grep "finished recording journal file"`得到相同的18个文件，工具全部标为“未正常结束”。审查文档写的“19/56”与“11个”不一致，按本次复核为准：56个会话＝55个主Journal＋空文件`dump.0001.txt`（无结束状态）。其余37个“正常结束”。会话级发现：`addin_conflict`47、`no_clean_exit`18、`license_error`7、`model_open_failed`7、`crash_dump`4、`steel_localdb`2。抽查2024 `journal.0011`：Build、进程号124036、1分55秒、结束标记第1639行、打开失败3条（第1340/1341/1343行）与原文一致。

### J13 阶段模型

- 块新增`phase`（startup/licensing/addin_loading/document_open/interactive/shutdown），由标记推进；空的`'H/'E`时间戳头随其引出的命令取命令所在阶段。`--phase`、TUI`P`键；`e`视图与`tail_blocks()`给出结束前最后N个操作与对话框（排除`Jrn.Directive`设置记录）。
- 偏差（实测后修正）：最初以任意`Jrn.*`命令作为“交互操作”开始，真实文件中启动阶段的`'H`头记录`Jrn.Data "JournalDefaultTemplate"`（第101行）和自动的`Jrn.RibbonEvent "TabActivated:Architecture"`都会把阶段提前推进到交互，导致Add-in加载阶段消失。改为`Jrn.Data`、`Jrn.Directive`、`Jrn.AddInEvent`和`TabActivated`不算用户动作。修正后本机分布：30个会话为启动→许可→Add-in加载→交互→打开文档→退出，7个无打开文档，7个无退出（未正常结束），8个停在启动/许可（启动即中断的会话）。
- 以`_`续行的`Jrn.*`块，标题接上续行内容（最多300字符），原来的标题只有`Jrn.Data  _`，在“结束前操作”中没有信息量。`BLOCK_VERSION`升为`blocks-5-phase-fold`，`INDEX_VERSION`为`viewer-4`。

### J14 规则与分类

- 新规则：`revit.license_error`、`revit.model_open_failed`、`revit.steel_localdb`、`revit.addin_conflict`、`revit.ui_warning`（`tags: ["ui_warning"]`）；规则新增可选字段`tags`。`generic.error_marker`排除层级前缀后的界面提示与`Adlsdk Error`。每条规则在`tests/test_revit_logic.py`有构造的正负样例，并加入门控等价性语料。
- 新分类“对话框”：`TaskDialog "..."`与`Jrn.Data "TaskDialogResult"`（含续行；续行才出现标记时整块改为对话框）。分类门控`_DIALOG_GATE`加入`test_categories_gates.py`。
- 同一`API_ERROR`记录上的其他规则（Add-in冲突、钢连接）与`revit.api_error`一起保留。
- 验收：本机全部Journal中带层级前缀的`ERROR:`/`错误:`行（“Can't create Railings”“不满足约束”“无法创建栏杆扶手”等）全部是建模提示，“错误”分类中此类行为0，`revit.ui_warning`94行；`addin_conflict`549行、`model_open_failed`23行、`license_error`7行、`steel_localdb`2行；对话框块519个。
- 偏差：计划写“`generic.error_marker`对UI提示降为warning”。规则级别是规则属性，不能按行变化，因此拆为独立规则`revit.ui_warning`并在通用规则中排除，效果相同且可单独统计。

### J15 侧车文件

- `sidecars.py`：钢连接`DatabaseConnectionErrors.log`（UTF-16 BOM，`[n][HH:MM:SS]`条目）按同一年份与时间挂到会话并作为可打开的文件索引；CER`cer.log`按产品RVT、release与进程号（无进程号时按时间重叠）列出；worker/dmp/副本按文件名归组。摘要中每个侧车文件写明关联依据。
- 偏差（实测后修正）：本机日志条目时间为15:51:10，文件修改时间却是前一天21:51:10，原实现用修改日期拼出条目时间，2024 `journal.0011`（10-05 15:49:53–15:51:48）无法关联。改为三级依据：条目时刻按修改日期落在会话内 > 修改时间落在会话内 > 仅条目时刻落在会话日期范围内（标注“关联较弱”）；多个候选时取依据最强、再取离修改时间最近的会话。修正后该日志只关联到2024 `journal.0011`，摘要列出两条LocalDB错误并生成`session.steel_localdb`。本机3个CER日志按进程号关联。
- 另修：钢连接日志总按`auto`（BOM）读取，不受Journal的`--encoding`影响。最初显式`--encoding gb18030`时该日志严格解码失败，2024目录退出码变为2；修正后为0，并有测试覆盖。

### J16 折叠

- 每块计算指纹：类别＋去掉时间戳、层级前缀、耗时前缀与`- N times`后的逐行内容（blake2b 8字节）。建完文件时汇总重复指纹到`block_folds`。列表默认在首次出现处显示`×N`，`--print`另注末次行号；阶段筛选下用窗口函数在筛选内折叠；搜索不折叠；`fold_members()`列出各处起始行；`g`跳行时关闭折叠。`z`/`--no-fold`切换。
- 真实数据：本机177,002块默认列为126,581项（减少28%），最大折叠为`[Jrn.ESSchemaOpenRoster]`2,349次。Add-in冲突块因程序集名不同，单文件最大折叠20次；按程序集的聚合计数由会话摘要给出（如2024 `journal.0011`为28个程序集28条）。

### 其他修正

- 并行建索引的挂起（J8遗留）：工作进程在启动时退出（例如spawn无法重新导入主模块：从标准输入运行的脚本）时，`multiprocessing.Pool`会不断重建进程，构建一直等待（本次测量中挂起超过600s）。改用`ProcessPoolExecutor`，出现`BrokenProcessPool`时终止进程池，剩余文件在主进程中顺序建立；停止请求同样终止工作进程。测试用`os._exit`作为工作进程初始化函数模拟，结果与顺序构建逐行相同。
- 规则预筛选移入`match_rules`：全部规则都有`gate`时，先把门控并集（去掉含更短门控的冗余项，ASCII行不查中文项，18项）作为一次预筛选，扫描与分类共用；任一规则无门控时停用。原分类器的`_POSSIBLE_ERROR`预筛选随之删除。
- 指纹归一化在行内容不可能以时间戳、耗时或多余`<`开头时直接复用已剥离的内容，跳过一次正则；与完整归一化的等价性有4000行随机语料测试。

### 测试与基准

测试：167项全部通过（阶段2为140项；新增`tests/test_revit_logic.py`26项、并行回退1项；`test_blocks`中续行标题的断言随J13更新）。

| 场景（同一文件/目录） | 阶段2最终 | 阶段3 |
| --- | --- | --- |
| scan 6.47MB，`gb18030` | 0.488s（13.3MB/s） | 0.390s（16.6MB/s） |
| scan 6.47MB，`auto` | 0.558s | 0.412s |
| 分类＋分块（feed+classify） | 1.047s | 1.459s |
| 建索引6.47MB | 2.224s，38,401行/秒 | 2.869s，29,763行/秒 |
| 单文件索引体积 | 3.28倍 | 3.63倍 |
| 建索引峰值内存 | 59MB | 68MB |
| `browse --print` 2024目录，`auto` | 2.58–2.79s | 3.31s（含钢连接日志共22个文件） |
| `browse --print` 2024目录，`gb18030` | 2.61s | 3.36s |
| 目录缓存体积 | 3.57倍 | 3.90倍 |

同日用`git archive`取阶段2提交复测：建索引2.065s（41,342行/秒），feed+classify 1.02s。

偏差：单文件建索引不再满足阶段2的“≥35,000行/秒”（现29,763行/秒）。逐项关闭测得每项约0.07–0.1s：会话收集、指纹、阶段推进、折叠与统计汇总；分类约+0.1s（对话框判定，规则在门控命中的所有行上运行而不只在“可能错误”行上）。体积增加来自续行标题（+1.4MB）与`phase/fingerprint`列（+0.8MB）。扫描反而更快；目录建索引仍≤5s。若需要找回速度，候选方向是把指纹改为整数列、会话收集只在时间戳/特定前缀行上运行，未在本次实施。（已另行修复，方法不同，见文末“阶段3速度偏差的修复”。）

### 未完成与未验证

- 交互式TUI的新按键（`s/e/P/z/9`）只在`Browser`单元测试中驱动并渲染，仍未在原生Windows终端人工验收（J20）。
- 真实Revit持有的Journal未测（见J3）。
- Web查看器只显示“正在写入/实时快照”和最新优先的默认选择，没有会话摘要与阶段视图（J24，已在文末补做）。
- 阶段4（J17报告、J19缓存位置、J20、J21、J25）与J9未实施。J3已完成，J9的前提条件已具备。

## 阶段4：交付

### J17 支持工单报告（`report`，`ticket.py`）

- `revit-journal report [路径…]`输出UTF-8 Markdown（`--format json`输出同内容的结构化版本）；不传路径时为本机最新会话（`--latest 1`），可与`--revit`、`--latest N`组合；`-o`以原子替换写出，不能覆盖输入或索引。
- 结构对齐RevitLocalDbFix的`ReportBuilder`：顶部要点列表（生成时间、工具版本与规则集SHA-256、系统、个人信息处理、范围），每个会话一节：两列事实表（目录、主Journal、版本、进程号、起止、时长、结束状态、会话级发现数）、文件表（SHA-256、实际编码、行数、字节、读取状态）、会话摘要代码块（与`summary`同一渲染）、规则候选；末尾“未验证声明”。
- 规则候选取自对会话内主Journal、精简Journal、worker日志的完整扫描：按严重级别、相同行（精确重复组）只列一次并注明次数与末次行号；同一规则组合最多3例并注明其余行数（真实2024会话中33条不同的程序集冲突原本占满30条名额）；`revit.ui_warning`只计数不列明细。每例带原始行号、前后文、规则ID、依据和说明；代码块围栏长于原文中最长的反引号串。
- 个人信息：默认把Windows用户名、域名、计算机名替换为`<user>`、`<domain>`、`<machine>`，规则移植自RevitLocalDbFix的`ReportRedactor`（整词、`DOMAIN\user`与`\Users\name`上下文、JSON双反斜杠、与报告常用词同名时只在限定上下文替换）；`--include-personal-info`保留。本机2024全部会话报告中用户名出现0次（保留时89次）。SHA-256始终针对原始字节，报告中注明替换处摘录与原文不同。
- 只有侧车文件的会话（如空`dump.0001.txt`）单列为“只有未读取的侧车文件”，不再显示误导的“主Journal尚未完成索引”。
- 实测：本机2024最新会话（2个文件、29,655行）报告2.2s（全新索引数据库，含建索引）；2024目录全部11个会话1,791行Markdown，`journal.0011`一节列出钢连接错误日志、关联依据与`session.steel_localdb`候选。

### J19 缓存位置

- 默认索引数据库移出当前目录：Windows为`%LOCALAPPDATA%\revit-journal-tools\cache\`，其他系统为`$XDG_CACHE_HOME/revit-journal-tools`（缺省`~/.cache`）；环境变量`REVIT_JOURNAL_CACHE`或`--database`可改。`browse`/`summary`/`report`共用`terminal.sqlite3`，`view`用`viewer.sqlite3`。
- 新增`revit-journal cache`显示位置、文件数和大小；`--clear`只删除本工具的两个数据库及其`-wal/-shm`，目录中其他文件不动。旧的`.journal-cache/`不自动删除（`.gitignore`保留该项）。

### J20 原生Windows控制台验收

- 新增`scripts/windows_console_probe.py`：在新的conhost窗口或Windows Terminal窗口中启动`browse`，附加到该控制台，用`WriteConsoleInputW`按键、`ReadConsoleOutputCharacterW`读取屏幕，逐键检查可见效果，再检查退出码、控制台模式恢复和备用屏退出。
- 本机结果（Windows 10 19045 conhost、Windows Terminal Preview，2024 `journal.0014`真实Journal，120×30）：`s`摘要、`e`结束前操作、`P`阶段、`z`折叠切换、↓与Enter展开、Esc、`f`文件列表、`g`行号跳转、`/`搜索全部通过；`q`退出码0，Ctrl+C退出码130；conhost中VT标志在运行时开启、退出后恢复为原值`0x3`，两种宿主都离开了备用屏。中文与全角宽度显示正常。
- 控制台无法进入交互模式时（不是控制台，或`SetConsoleMode`无法开启VT处理的旧版conhost）不再报错退出：stderr提示原因，等待建索引完成后输出一页段落摘要（等同`--print`，不带颜色），退出码0。该回退路径由单元测试模拟；本机没有不支持VT的旧控制台，未实测。
- 探针运行时发现：从本工具外壳启动的进程继承了“忽略Ctrl+C”，探针在子进程中先恢复Ctrl+C处理；用户自己打开的控制台不受影响。

### J21 分发形态

- `scripts/build_zipapp.py`生成`dist/revit-journal.pyz`（约90KB），只含本包代码、内置规则和Web文件，目标机器只需Python 3.10+，`py dist\revit-journal.pyz <命令>`。入口与包的`__main__`一样受`__name__`保护，Windows spawn的建索引工作进程可以从zip中导入：2024目录`summary --jobs 4`为3.45s，`--jobs 1`为6.6s，说明并行确实生效而不是回退。
- 没有做PyInstaller（带运行时的单文件exe）：会引入构建依赖和较大的二进制，且需要另外处理杀毒软件误报；有需要时再加。

### J25 文档

- README快速开始改为Windows优先：zipapp与虚拟环境两种方式、自动发现的Journals路径、中文系统无需`--encoding`、Windows Terminal与conhost说明；CLI示例去掉`--encoding cp1252`（只在英/德文首批案例和混合目录示例中保留）；新增“支持工单报告”一节；缓存位置与清理；项目结构补齐阶段3、4的模块。
- `docs/terminal.md`补充缓存目录、`cache`命令、报告和Windows控制台说明；`docs/validation.md`记录本阶段验收。

### 测试

177项全部通过（阶段3为167项；新增`tests/test_delivery.py`10项：报告的哈希/行号/规则/声明/界面提示计数/围栏、个人信息替换与保留、JSON与输出保护、同规则组合限例、`Redactor`整词与JSON、围栏长度、各平台缓存目录、命令默认使用缓存目录与`cache --clear`只删本工具文件、控制台回退、zipapp在无源码树时运行并加载内置规则）。

### 未完成与未验证

- 不支持VT的旧版控制台（Windows 10 1511之前）只由单元测试模拟回退路径。
- Web查看器仍没有段落与会话视图（J24，已在文末补做）；J9偏移模式未实施。
- 报告中的规则候选与会话级发现仍是候选；报告未与RevitLocalDbFix合并为单个文件，只是格式对齐。

## 阶段3速度偏差的修复：单文件建索引恢复到≥35,000行/秒

剖析（同一6.47MB/85,387行文件，`gb18030`）：分类约5.9µs/行、分块约11.7µs/行，每行十余次`any(map(...))`门控（共约109万次）；写库中提交时的WAL检查点约0.27s。真实Journal中约一半的行与前面某行文字完全相同（该文件46,206种文字，Revit 2024 `journal.0006`为28,649行中13,000种）。

- 分类结果缓存：在API_ERROR花括号之外，`classify`的结果和下一状态只取决于（文字，操作续行，对话框续行），因此按这三者缓存（`categories._CACHE_LIMIT`=16,384条，满了清空）。该文件命中约3.7万次。花括号内的续行仍逐行计算。
- 分块的行特征缓存：`blocks._shape(text)`一次算出`feed`需要的所有纯文字特征（正文、指纹用的规范化字节、时间戳与空记录、`Jrn.`命令、API记录、各形状判断、阶段标记、续行符、花括号差），按文字缓存（同样16,384条）；`feed`中依赖状态的逻辑不变。特征计算先用一次合并门控`_SHAPE_GATE`（由各形状门控求并集，测试检查覆盖关系），大多数行一次排除全部形状正则。堆栈与缩进判断只在错误块之后计算。没有加载前缀且未处于加载事件时不调用`LoadingEvidence.feed`；未激活的`LoadingEvidence`在块之间复用。
- 写库：没有Add-in证据的块不再调用`json.dumps`；新建的索引数据库使用`page_size=8192`（完整建索引约快4%，体积相同；已有缓存保持原页大小，不需要重建）。

等价性：`verify_equivalence.py blocks`增加了阶段、折叠指纹和会话摘要的比较（两棵树都有这些字段时才比较）。与改动前的提交逐行比对：本机104个文本Journal共626,169行记录（`--together`，走进程池）0差异；`cases/inbox/`中上传案例18个文件共562,672行记录（`--encoding auto`）0差异。新增测试3项：合并门控覆盖每个形状门控；缓存、极小上限（4条，反复清空）与不缓存三种方式的分类和分块结果相同；缓存有上限。变异检查：把分类缓存的键改为只用文字时7组样例中有3组结果不同，把空记录标记置为假时有5组不同，说明这些样例能发现缓存错误。

| 场景（同一时段，最优值） | 修复前 | 修复后 |
| --- | --- | --- |
| 建索引6.47MB | 2.85–2.90s，约29,700行/秒 | 2.36–2.37s，约36,200行/秒 |
| 单独分类 | 0.505s | 0.347s |
| 单独分块（不含分类） | 0.789s | 0.708–0.746s |
| 单文件索引体积 | 23.5MB | 23.2MB |
| 建索引峰值工作集（`auto`） | 71MB | 76MB |
| 61.7MB/333,331行德文Journal建索引（`auto`） | 22.1s | 11.1s（峰值工作集135→136MB） |
| `browse --print` Revit 2024目录（22个文件） | 3.41–3.46s | 2.74–2.77s |
| scan 6.47MB，`gb18030` | 0.39s | 0.38s（未改动） |

偏差：没有采用阶段3所列的“指纹改为整数列”和“会话收集只在特定行上运行”。前者要改变已有缓存的列类型（SQLite的`ALTER TABLE`不能改类型，需要迁移或重建）；后者只占约0.28s，且已只做廉价的子串判断。提交时的WAL检查点（约0.27s）未处理，`synchronous`仍为NORMAL，理由同阶段2。

## J23 规则正则的加载时检查（`regex_safety.py`）

- 加载规则时，用标准库的正则解析器（Python 3.11+为`re._parser`，3.10为`sre_parse`）检查`pattern`和每个排除表达式：在可重复的组里（上限大于1），贪婪或懒惰的无界量词只有满足两个条件才接受：(1)重复的是单个字符类；(2)它后面可能出现的每个字符（组内的下一部分，或下一次重复的开头）都不在这个字符类中。字符类是否相交，用约600个测试字符（Latin-1与拉丁扩展、IGNORECASE会映射到ASCII的`İ ı ſ K`、Unicode空白与数字、中日文、一个辅助平面字符）分别按忽略大小写编译后判断。无法证明安全的（分支、前瞻、多字符的内层重复等）一律报错；原子组和占有量词不回溯，接受。
- 报错信息指出是正则还是哪个排除表达式，并给出改写方向。内置19条规则全部通过（`generic.exception`的`(?:[A-Za-z_][A-Za-z0-9_]*\.)*`属于可证明安全的写法）。首次加载规则约13ms（之前约3ms），之后约3ms。
- 实测证据（CPython 3.12）：被拒绝的写法在22–23个不匹配字符上耗时0.3–158秒；被接受的写法在1–2万字符上为0.5–3.2秒，按行长的平方增长，属于检查不覆盖的多项式级写法。
- 测试2项：8种危险写法（含排除表达式中的一种、前瞻内的一种、忽略大小写才相交的一种）被拒绝；7种安全写法（Python 3.11+再加原子组与占有量词2种）被接受。
- 偏差与局限：计划写“拒绝明显的嵌套量词模式”。实现按上述条件证明安全，比单纯“星高大于1”更窄，否则会误拒内置`generic.exception`。重叠分支（`(a|ab)*`）和多项式回溯不检查，`docs/rules.md`写明了这一点；使用的是标准库的私有解析模块，Python升级时可能需要调整；本机只有Python 3.12，3.10的`sre_parse`路径未实测。

## J24 Web查看器的段落与会话视图

- 服务端只读接口：`/api/blocks`（`ViewerIndex.blocks`，含阶段与折叠参数）、`/api/block`（`block_lines`，附与`/api/context`相同的哈希与编码字段）、`/api/block_at`、`/api/fold`（`fold_members`）、`/api/session`（`session_view.summary_lines`加`main_tail`）；`/api/files`另返回段落筛选与阶段名称。段落的列表文字用CLI的`block_label`/`block_summary`在服务端生成。`main_tail`从`terminal.py`移到`session_view.py`，CLI与Web共用。
- 界面：默认为段落视图（与`browse`一致：阶段标签、“×N”折叠、阶段下拉框、折叠开关、Add-in来源筛选只显示存在的来源），可切换为原有逐行视图；标题下显示所属会话，“会话摘要”在右侧面板显示与`summary`相同的文字，含“第N行”或行号列的行可点击，定位到主Journal（必要时先切换文件）。长段落每次加载200行并可继续加载；“跳至行”在段落视图中打开该行所在段落，从该行前20行开始显示并选中该行。页面加了空favicon，避免每次加载产生一个404。
- 测试1项（`IndexViewTests`）：经HTTP取得的段落列表去掉标签后与`ViewerIndex.blocks`逐项相同，`summary`字段与`block_summary`相同；折叠成员、阶段筛选、段落原文与来源哈希；会话摘要与`summary_lines(session, main_tail(...))`完全相同；非法`fold`、未知阶段、空指纹、未知文件返回400/404。
- 浏览器实测：本机headless Chrome通过DevTools协议，在两个合成会话（含折叠、崩溃会话）和一个超过200行的段落上逐步操作：段落列表、折叠段落详情（“相同内容共3段……起始行43、44、45”）、会话摘要（崩溃会话列出异常前的命令、异常代码及含义、未处理异常）、从摘要跳转、阶段筛选、切换逐行、长段落三次继续加载、跳至第400行（从第380行显示并选中）、1440与1100像素宽度。全过程没有脚本异常、控制台错误或失败的请求。
- 偏差与局限：Web端没有CLI中的折叠展开（逐个查看折叠成员的原文）、Add-in名称前缀筛选和“结束前操作”单独视图（结束前操作已包含在会话摘要中）；没有为前端写自动化测试（仓库无前端测试依赖），浏览器检查脚本只在本次运行，未加入仓库。
