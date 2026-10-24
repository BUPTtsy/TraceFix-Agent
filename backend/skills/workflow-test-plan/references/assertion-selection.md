# 断言 condition 选择

运行时只按 role + name 精确匹配快照元素，并读取 [checked]、[disabled] 属性：

| condition | 通过条件 |
|---|---|
| visible | 恰好 1 个匹配 |
| absent | 0 个匹配 |
| checked | 恰好 1 个，且带 [checked]（[checked=mixed] 不算） |
| unchecked | 恰好 1 个，role 是 checkbox/radio/switch/menuitemcheckbox/menuitemradio，且不带 checked |
| disabled / enabled | 恰好 1 个，且带 / 不带 [disabled] |

除 absent 外，存在同名重复元素时断言一律失败。

| 用户目标 | 推荐 | 避免 |
|---|---|---|
| 勾选后刷新仍保持 | checkbox checked | visible |
| 取消勾选后保持 | checkbox unchecked | absent |
| 删除后不再出现 | 目标项 absent + 相邻锚点 visible | 只写 absent（页面白屏也会通过） |
| 新增后出现 | 新项（名称含输入值）visible | 只断言输入框 |
| 条件不满足时禁用 | disabled；满足后 enabled | visible |
| 错误或提示出现 | 带名称的 alert/status/heading visible | 没有名称的文本节点 |

`- paragraph: 文本` 中冒号后的内容不是 name（name 为空），不能用于断言。要断言文本，就找带引号名称的元素。
