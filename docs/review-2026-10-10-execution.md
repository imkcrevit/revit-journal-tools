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
