# 编辑引用

补丁保持 exact anchor、当前 before_hash 和允许路径。只提交最小局部变更；语法检查通过不等于业务验证通过，失败后应重新读取当前版本再修正。

- ambiguous：读取有限匹配行，选包含唯一上下文的小 anchor。
- not-found、stale、disk-drift：重读当前源码及 overlay revision，不覆盖新的磁盘内容。
- overlap、no-change：重新划分不重叠变更或移除无效候选。
- syntax-error：读取新诊断行并修正同一暂存候选，确认真实 diff 后再提交引用。

disk before_hash、overlay revision 与候选 patch_hash 各有语义，不可互换。权限和 apply/UNKNOWN 由运行时决定。
