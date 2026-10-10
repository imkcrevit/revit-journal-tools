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
