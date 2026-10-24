# CHANGELOG（仅维护者，不注入）

## 1.1.0 (2026-10-24)
- 重写为场景配方：适用/不适用、根因判定表、修复优先级、反模式、宿主验证清单。
- phases 去掉 VERIFY（确定性阶段不消费正文），保留 PATCH 以满足 tests/test_skills.py 选择约束。
- tools_hint 改为 SDK coder 原生工具名；体积控制在 DIAGNOSE 预算 2000 字符内。
