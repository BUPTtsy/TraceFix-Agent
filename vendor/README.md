# Windows Python 依赖

`wheels-win_amd64/` 包含 CPython 3.12、Windows x64 的 70 个锁定依赖 wheel。
启动器在 Windows 自动使用 `--no-index --find-links` 从该目录安装，随后从本地源码安装 TraceFix。
不包含 Python、Git、Docker Desktop、容器镜像或训练模型；这些仍需单独准备。

版本来自根目录 `requirements.lock`，文件 SHA-256 和 Windows 平台依赖闭包核对结果见 `artifacts/windows-dependencies.json`。
原始第三方版权与许可保留在各 wheel 的 `.dist-info` 元数据及 licenses 文件中。
这些是第三方原始分发包，没有修改其二进制内容。

已核对平台标签、版本约束和 Windows 条件依赖；当前 Linux 主机无法验证 Windows 二进制加载。
ARM64、32 位 Python、其他 Python 小版本不在本 wheel 集合的目标范围内。
