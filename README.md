# Revit Journal Tools

本地Revit Journal查看器：按启动、系统数据、错误、API错误、操作内容等分类浏览大型日志，搜索关键词、直接跳转行号，并查看连续原文与命中依据。

当前版本包含可运行的本地浏览器界面、Python命令行工具和可复用解析模块。使用Python 3.10及以上版本和启用FTS5的SQLite，无运行时第三方依赖。界面和日志数据全部在本机处理，不加载外部脚本或字体。真实案例可以同步放入`cases/inbox/`，逐步校准规则。

## 快速开始

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
revit-journal view examples/synthetic.journal.txt
```

Windows PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\revit-journal.exe view examples\synthetic.journal.txt
```

不安装包也可以运行：

```bash
PYTHONPATH=src python3 -m revit_journal_tools scan examples/synthetic.journal.txt
```

## 本地查看器

```bash
# 查看第一批已上传的案例；首次在后台建立磁盘索引
revit-journal view cases/inbox --recursive --encoding cp1252

# 默认地址为http://127.0.0.1:8765；端口被占用时可调整
revit-journal view cases/inbox --recursive --encoding cp1252 --port 8766
```

浏览器打开命令显示的本地地址即可。左侧切换文件，上方切换分类；关键词搜索支持词语/词语前缀，列表每页最多100条，点击一条记录查看前后文，也可以直接跳到任意有效行号。分类包含启动、系统数据、错误、API错误、操作内容、警告、API活动和其他记录。

首次索引逐文件进行，已经就绪的文件可先浏览。索引缓存位于`.journal-cache/viewer.sqlite3`，再次启动会复用未变化文件的索引。缓存包含完整日志文本，不受CLI的500条明细限制，默认不进入Git。文件内容、编码或索引规则变化后会重建相关索引。

当前是本地浏览器应用，还未封装成Windows桌面安装包。详细用法和边界见[docs/viewer.md](docs/viewer.md)。

## 扫描与导出

```bash
# 单文件，保留每处命中前后各5行
revit-journal scan /path/to/journal.0001.txt --context 5

# 多文件或目录；目录选择.txt、.log、.journal和.txt.abbrev
revit-journal scan cases/inbox --recursive --format json -o reports/cases.json

# UTF-8以外且没有BOM时，显式指定编码
revit-journal scan journal.0001.txt --encoding gb18030

# 仅保留error及fatal规则的候选，增大明细容量
revit-journal scan journal.0001.txt --min-severity error --max-findings 2000

# 用自定义规则扩展内置规则；JSONL每个文件一条报告
revit-journal scan cases/inbox --recursive --rules examples/custom-rules.json --format jsonl

# 可供脚本使用：达到error或fatal时退出码为1
revit-journal scan journal.0001.txt --fail-on error

# 查看规则定义、级别和来源
revit-journal rules
```

`python -m revit_journal_tools`与`revit-journal`等价。输出文件以UTF-8原子替换，不能覆盖输入Journal或自定义规则文件。一次批量任务中的单个文件读取失败会单独记为`status: error`，其余文件继续处理。

退出码：`0`表示扫描成功；`1`表示成功扫描且达到显式指定的`--fail-on`级别；`2`表示参数、规则、输入或输出错误；`130`表示中断。默认发现错误候选不会导致非零退出码。

## 报告内容

- 来源：文件绝对路径、原始字节SHA-256、实际解码方式、文件大小和行数。
- 定位：从1开始的文本行号与字符列号、命中片段、前后文。CRLF、LF和单独的CR均作为换行；混合换行的文件可能与仅按LF计行的工具不同。
- 依据：规则ID、候选级别、解释、来源链接以及`heuristic`/`documented_marker`/`case_verified`标签。
- 统计：各级别的命中行数、各规则命中数、完整原文相同的重复组。
- 边界：扫描是否完成、保留明细数、省略明细数、未纳入分组的命中数。

一行命中多条规则时只产生一个候选，候选级别取当前启用规则中的最高级别。各规则计数的总和可能大于命中行数。规则级别是检查优先级，不能直接视为真实事故严重度。

`scan_complete: true`只表示已完整解码并扫描输入，并不表示所有错误都能识别。无命中表示现有规则未找到候选。所有命中仍为`candidate`，当前没有自动根因诊断。

## 大文件与编码

逐行读取，不将整个Journal加载到内存。默认保留最多500条候选明细和500个重复组；超过上限后仍继续扫描全文并累计候选数量。明细按`fatal > error > warning`优先保留，同级优先较早出现的候选，最终按行号展示；这样后出现的严重错误能替换已保存的普通警告。报告明确给出省略数量；可使用`--min-severity`减少警告干扰或提高`--max-findings`。分组只合并完全相同的解码行及规则集合，不模糊合并元素ID、路径或时间。

每个展示行最多保存4096个字符，超出时标记`text_truncated`。识别会检查整行，匹配点附近的独立片段与列号仍会保留。默认单行上限为1,048,576字符，超限返回明确错误，可通过`--max-line-chars`调整，不会静默跳过后半行。

`--encoding auto`识别UTF-8/UTF-16/UTF-32的BOM，没有BOM时使用严格UTF-8，不猜测其他代码页。UTF-8以外的旧文件可指定`gb18030`、`cp1252`、`utf-16-le`等。无法解码、疑似二进制或扫描期间变化的文件记为读取失败，防止产生貌似完整的报告。优先提供已停止写入的Journal副本。

首批上传的英/德文案例使用CP1252可以严格解码；其中不换行空格、角度符号和德文变音字符不符合UTF-8。扫描这批案例时显式使用`--encoding cp1252`，该选择不代表以后上传的所有文件都使用同一编码。

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
  reader.py              严格解码、流式读取、哈希与源文件变化检查
  rules.py               规则加载、验证与匹配
  data/builtin_rules.json 内置规则
  analyzer.py            上下文、计数、有限容量的明细与分组
  models.py              结构化报告
  reporting.py           文本、JSON、JSONL导出
  cli.py                 批量扫描入口
  categories.py          启动、系统、错误、API和操作记录分类
  viewer_index.py        全量磁盘索引、全文检索、分页与上下文查询
  viewer_server.py       仅监听本机的查看器服务
  web/                   本地界面，无外部前端依赖
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

安装包后，Windows也可使用`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`运行同一套测试。

下一步围绕实际案例增加会话时间线、完整操作阶段、插件/模块关联、堆栈合并和误报排除。当前版本未实现浏览器拖放导入、桌面安装包、AI分析、Journal回放或模型修复。

案例提交方式见[cases/README.md](cases/README.md)，规则扩展见[docs/rules.md](docs/rules.md)。Autodesk的[Journal阅读指导](https://www.autodesk.com/support/technical/article/How-to-read-the-journal-file-Revit/)建议结合具体操作、实际现象和近似时间排查；本项目按此保留上下文，其他参考见[docs/sources.md](docs/sources.md)。
