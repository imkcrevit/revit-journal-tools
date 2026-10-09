# CLI段落查看器

```bash
revit-journal browse cases/inbox --recursive --encoding cp1252
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

## 交互

| 按键 | 行为 |
| --- | --- |
| ↑/↓或k/j | 选择块；展开时滚动原文 |
| Enter或空格 | 展开/折叠选中块 |
| PgUp/PgDn | 在当前列表或展开原文中快速移动 |
| ←/→ | 横向滚动长行，保留行号 |
| n/p | 下一页/上一页段落 |
| 0 | 全部分类 |
| 1至8 | 启动、系统、错误、API错误、操作、警告、API、其他 |
| i | 当前日志的Add-in加载列表 |
| a/t/u | Autodesk加载/第三方加载/来源待定加载 |
| v | 按名称或Class前缀筛选；清空后Enter移除前缀条件 |
| c | 循环切换分类 |
| / | 输入关键词；Enter确认，Esc取消；清空后确认移除筛选 |
| g | 输入原始行号，定位并展开所属段落；清除分类、搜索和前缀筛选 |
| f | 文件列表；上下选择，Enter打开，Esc返回 |
| [和] | 上一个/下一个文件 |
| +和- | 增加/减少普通段落预览行数；Add-in原文仍只在展开后显示 |
| Esc | 折叠或取消输入 |
| q或Ctrl+C | 退出并恢复终端 |

字体缩放由终端自身提供，具体快捷键取决于终端设置。程序不修改终端字体配置；窗口改变尺寸后重新计算可见行和列。原文过长时可横向滚动，单行预览上限为16,384个字符，超限附截断提示；索引仍保留全文。

默认配色：启动/API为蓝色，系统为青色，普通错误为红色，API错误为紫色，操作为绿色，警告为黄色，其他为灰色。界面同时显示文字标签，不依靠颜色单独表达类型。支持`--color auto|always|never`，`NO_COLOR`环境变量关闭自动颜色；显式`always`优先。

## Add-in加载来源筛选

```bash
revit-journal browse journal.txt --encoding cp1252 --category addin
revit-journal browse journal.txt --encoding cp1252 --addin-prefix ATFTranslationServer
revit-journal browse journal.txt --encoding cp1252 --category addin_autodesk
revit-journal browse journal.txt --encoding cp1252 --category addin_third_party
revit-journal browse journal.txt --encoding cp1252 --category addin_unknown
revit-journal browse examples/addin-loading.journal.txt
```

`Starting External Application:`和`Starting External DB Application:`等加载前缀必须出现在记录正文开头；程序先去除Journal时间戳、层级标记和`API_SUCCESS/ERROR/INFO`包装，再提取名称、Class和同一块内的Vendor。普通消息中间提到这些词、服务注册、DLL引用或带有Autodesk字样的路径不会单独被当成Add-in加载。

例如`Starting External DB Application: ATF Translation Server, Class: ATFTranslationServer.ATFTranslationServerRegister, ...`会提取名称`ATF Translation Server`和Class`ATFTranslationServer.ATFTranslationServerRegister`。按`i`查看加载列表，标题优先显示名称、来源、错误标记和行号；Class和原始整行在Enter展开后查看。即使已按`+`提高其他段落的预览级别，Add-in折叠行也不会读取或展示原始数据。

按`v`输入`ATFTranslationServer`可按Class前缀找到该项；输入`ATF Translation`也可按名称前缀找到。前缀从字段开头匹配，英文字母不区分大小写，`%`和`_`按普通字符处理，长度最多256字符；它可以与`a/t/u`来源分类及`/`全文关键词同时使用。前缀不用于推断官方/第三方来源。`0`恢复全部分类，但保留搜索和前缀；可分别用`/`与`v`清空，或用`g`定位原文并清除全部条件。

列表上方显示当前文件的加载记录总数和来源分布。数字包含当前文件所有加载事件，筛选后的当前页数量显示在底部；同一插件反复加载会保留每次记录，便于逐次展开核对，不能把记录条数直接当作已安装插件数量。缺少名称时显示Class或明确的缺失提示，不用Vendor或DLL名称补造插件名称。

| Vendor依据 | 来源 |
| --- | --- |
| `ADSK()`、`ADSK(Autodesk, www.autodesk.com)` | Autodesk加载 |
| `RPCA(Autodesk, www.autodesk.com)`或指向`autodesk.com`及其子域名的网站 | Autodesk加载 |
| 其他明确厂商ID/名称，或其他网站；允许没有网址 | 第三方加载 |
| 厂商缺失、`<None>`、不同厂商字段相互冲突、厂商描述不完整 | 来源待定加载 |

网站依据按实际域名判断；`autodesk.com.example.org`、其他网站查询参数中的`autodesk.com`都不属于Autodesk域名。厂商字段不跨加载事件继承，错误堆栈或程序集路径中的名称也不用于猜测来源。这是日志声明的厂商归类，不证明默认安装来源、数字签名或组件可信度。

来源标签独立于原有API/错误分类。例如，第三方`API_ERROR`加载事件会同时出现在`t`和`4`筛选中，保留错误颜色、严重级别及完整原文。列表显示`API错误 / 第三方加载`，加载记录出现也不等于加载成功。来源统计与原有类别存在重叠，不能相加当作总块数。本次不增加许可证初始化日志分类，原有Web列表仍按原来的逐行类别展示。

人工样例共有6块：第2–6行为Autodesk加载，第7–9行为第三方加载，第10–12行为第三方加载失败，第13–14行为来源待定。第15行是普通服务注册，不应出现在来源筛选中。可按`g`跳到这些行展开核对，按`t`后搜索`failure`应只返回第10–12行。

## 分页输出与程序调用

```bash
revit-journal browse journal.txt --print --category error --page-size 20
revit-journal browse journal.txt --format json --query GeomStepList
# 使用上一页返回的next_after续页；这里的100仅是示例
revit-journal browse journal.txt --format json --after 100
```

重定向或管道环境自动使用非交互输出。每个文件最多返回一页，默认100块，上限200块；结果明确给出是否还有下一页。JSON包含原始字节SHA-256、编码、文件总行数/总块数、块的行号范围、分类、规则ID、分组依据和边界完整状态。

JSON中的`addin_origin`为`autodesk`、`third_party`、`unknown`，非加载事件为空字符串；`addin_evidence`包含厂商原文摘要、`line_number`和`basis`。每块最多保留8条依据，每条摘要最多400字符，完整原文仍在索引中。单个跨行Vendor字段超过4096字符时保留为来源待定，不因截断信息而猜测厂商。

`addin_name`与`addin_class`保存提取的名称和Class，各最多512字符；超出时`addin_identity_truncated`显式标记，完整值仍可展开查看。文件层的`addin_count`是全文件加载记录数，`addin_prefix`说明本次前缀条件。`--print`输出名称摘要，`--format json`提供这些结构化字段。

关键词搜索使用FTS5的词语或最后一个词的前缀匹配，不是任意子串或正则表达式。命中段落中的任意行即返回整个段落，同一块只出现一次。展开后滚动查看命中所在行；已知行号可用`g`直接定位。

## 索引与边界

CLI数据库默认`.journal-cache/terminal.sqlite3`，通过`--database`调整。旧Web数据库默认位于另一个文件。首次建立磁盘索引需要时间与空间；在交互模式下可先查看已完成的文件，再切换到后续文件。输入发生变化时提示退出重开；下次启动按文件状态、编码和索引版本重建相关文件。

Add-in来源筛选升级了索引版本。重新打开时会自动为本次选择的旧缓存文件重建索引，大目录可能需要较长时间；无需手动删除缓存或修改原始Journal。

分组器只持有当前块的行号、有限标题和规则集合；完整内容在原文表中。展开只读取当前可见页，大段落不整体加载到内存。无法完整解码或索引失败的文件显示失败状态，不能作为完整结果打开。

Linux终端已通过真实PTY验证。代码包含Windows控制台适配，尚未在原生Windows机器验收；推荐使用支持UTF-8与ANSI的终端。终端中的控制字符会转为可见转义文字，以免日志中的ESC/OSC内容改变终端状态。

目前不调用AI API：已知Journal结构先由本地规则处理。若后续增加AI分类，仅考虑规则无法判定的少量段落，先做重复内容合并、长度限制和结果缓存；不会让AI通读整份日志。本地首次顺序读取用于严格解码、哈希和全文索引，之后复用磁盘缓存。
