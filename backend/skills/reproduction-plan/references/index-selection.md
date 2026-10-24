# 索引选择示例（占位数据）

exploration_actions：
```
0 navigate <RUN_URL>
1 click  checkbox "Complete 任务A"
2 observe
3 click  checkbox "Complete 任务A"   ← 重复点击，状态被切回
4 click  checkbox "Complete 任务A"
5 navigate <RUN_URL>                 ← 刷新读回
6 click  link "设置"                 ← 无关探查
```
合格输出：`{"action_indices": [4, 5], "summary": "保留4号勾选任务A和5号刷新读回；剔除0号（重放已含首屏导航）、1/3号（重复切换）、2号observe、6号无关导航；末尾按TestSpec断言检查任务A为checked"}`

注意：只有 1、3、4 三次点击合起来的净效果等于一次勾选时，才能只保留 4。如果目标是"勾选再取消"，就保留 [1, 3, 5]，或者交互次数与目标一致的其他组合。

| 错误输出 | 运行时结果 |
|---|---|
| [5, 4] | 非递增，被拒绝 |
| [4] | 交互后缺少 navigate，被拒绝 |
| [0, 2, 5] | 缺少交互，被拒绝 |
| [4, 5, 7] | 越界，被拒绝 |
| [1, 4, 5] | 合法，但 1 和 4 互相抵消，导致复现语义错误 |
