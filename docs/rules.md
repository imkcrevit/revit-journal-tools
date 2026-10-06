# 规则扩展

内置规则位于`src/revit_journal_tools/data/builtin_rules.json`。使用`--rules path/to/rules.json`追加自定义规则，重复ID会报错，不会静默覆盖。

```json
{
  "schema_version": 1,
  "rules": [
    {
      "id": "custom.connection_failure",
      "title": "示例连接失败标记",
      "severity": "error",
      "pattern": "CONNECTION_FAILED\\s*:",
      "exclude_patterns": ["expected synthetic failure"],
      "basis": "heuristic",
      "explanation": "这里只演示扩展格式，实际规则需要根据案例确定。"
    }
  ]
}
```

必填字段为`id`、`title`、`severity`、`pattern`和`explanation`。可选字段为`basis`、`source_url`、`exclude_patterns`。未知字段、错误级别、无效正则、空匹配与重复ID会在扫描前报错。

`pattern`和排除表达式使用Python正则，并统一忽略大小写。排除表达式只排除当前规则在这一行的命中，不会阻止同一行被其他规则识别。每条规则每行只记录第一次匹配，报告中的规则计数是命中行数，不是关键字出现次数。复杂或不受信任的正则不应直接装载；规则引擎尚无独立超时沙箱。

`severity`为`warning`、`error`或`fatal`，只用于候选排序和过滤。`--min-severity`会禁用低于该级别的规则；报告包含的规则集哈希覆盖完整加载规则集，具体过滤参数记录在`options`中。

`basis`含义：

| 值 | 含义 |
| --- | --- |
| `heuristic` | 通用文字或经验线索，仍需要案例验证 |
| `documented_marker` | 官方资料可核对的日志标记，不表示官方确认了当前文件的故障原因 |
| `case_verified` | 维护者已用真实案例验证，需要同时提交回归依据；程序不会自动授予此标签 |

规则验证顺序：先检查正常文本是否误报，再检查最小错误片段、相邻正常行、重复命中和日志末尾。不能把`0 errors`、菜单名称中的Error或异常类名称的普通提及当成已确认事故。

首批规则包含调试警告/错误、严重错误文字、显式错误标记、托管异常、原生异常事件/代码、断言失败、内存不足、文件访问、同步失败和缺失Schema线索。通用文字规则仍可能命中被引用的提示、已处理异常或否定语境；目前不做语义判定，应结合原文复核。
