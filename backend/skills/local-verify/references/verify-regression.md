# 验证矩阵

| kind | 证明 | 不能证明 | 失败先查 |
|-|-|-|-|
| static | 类型、lint 通过 | 行为正确 | 本次编辑引入的类型错误、未用导入 |
| unit | 现有单测通过 | 未覆盖的 UI 路径 | 补丁是否改变被测语义；不改测试 |
| build | 可构建 | 运行时行为 | 导入路径、缺失依赖（依赖只读） |
| health | 应用启动且健康端点可达 | 页面功能 | 启动期异常、模块顶层报错 |
| original | 冻结复现步骤下原断言成立 | 其他路径 | 未通过的 locator 与 condition；error_code 为 ACTION_POSTCONDITION_FAILED 或 ACTION_WAIT_FAILED 表示动作后置条件或等待失败 |
| regression | 相邻场景未退化 | 原问题已修 | 补丁副作用 |
| behavior | 每个检查点的状态转移成立 | 未列出的场景 | 首个失败的 scenario_step；刷新后失败是持久化反证，逆向操作失败是逆向退化 |
