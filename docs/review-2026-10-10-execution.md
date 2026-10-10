# 审查修改计划执行记录（2026-10-10）

依据：`docs/review-2026-10-10.md`第4节修改计划，本次执行阶段0、阶段1、阶段2（J18、J10、J1、J2、J4、J5、J6、J7、J8，J9可选）。

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
