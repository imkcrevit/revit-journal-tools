# 规则与读取的参考来源

核对日期：2026-10-06。仅使用Autodesk公开资料作为Journal基础行为和部分标记的依据，未将这些资料当成用户项目的实际运行证据。

| 来源 | 本项目使用范围 |
| --- | --- |
| [How to read the journal file from Revit](https://www.autodesk.com/support/technical/article/How-to-read-the-journal-file-Revit/) | 排查需要具体操作、预期与实际表现、近似时间；因此保留上下文并提供案例登记模板 |
| [Location of Revit journal files](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/Location-of-journal-files.html) | Journal为文本，通常每次启动产生一个文件；二进制dump与Journal文本分开处理 |
| [Autodesk Journal分析附件](https://www.autodesk.com/akn-aknsite-article-attachments/ea657414-ceec-4517-b029-99c43a7d4b69.pdf) | 第20、30页中的调试标记、异常事件、异常代码和断言失败，作为候选入口 |
| [Missing ESSchema与Revit 2024.3问题记录](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/A-serious-error-has-occurred-Please-open-the-model-with-Audit-error-with-Revit-2024-3.html) | 特定标记的官方记录；不把某版本的处理办法自动泛化到所有文件 |
| [同步时的File Not Saved记录](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/File-not-saved-during-synchronize-with-central.html) | 同步/保存错误文字的参考；具体原因仍依赖案例上下文 |

其余英文和中文通用匹配标记为`heuristic`。所有演示文本均为人工构造，不是下载的真实案例。当前未实现修复建议、自动模型修改或Journal执行。
