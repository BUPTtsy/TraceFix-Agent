# 验证引用

验证顺序包含原始失败、刷新或重新进入、取消完成和相关正常业务。结果必须来自独立验证 artifact；模型自报成功、截图和 LSP 结果不能替代业务断言。

逐项核对 original/regression/behavior/static/unit/build/health 的 artifact refs 与当前 patch、环境、源码和 TestSpec。刷新失败支持持久化反证；取消完成失败说明逆向行为退化。过期 pass 不可复用，不读取最终 held-out Oracle 结果来修补。
