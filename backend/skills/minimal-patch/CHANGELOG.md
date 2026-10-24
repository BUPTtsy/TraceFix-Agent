# CHANGELOG

- 1.4.0：改为 SDK coder 原生 Edit 流程，删除 propose_patch/overlay/staged 旧机制；纠错表按 candidate_export 拒绝原因重写。
- 1.4.1：前提补充 code_map 用法：只作 Read 起点、修改依据以源码行为准、outside 文件不改、unavailable 时回退 Grep/Glob。
