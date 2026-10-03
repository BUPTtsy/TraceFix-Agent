# Windows Python 依赖

`wheels-win_amd64/` 包含 CPython 3.12、Windows x64 的 71 个锁定依赖 wheel，其中包括 `pypdf==6.1.1`。
启动器在 Windows 自动使用 `--no-index --find-links` 从该目录安装，随后从本地源码安装 TraceFix。
不包含 Python、Git、Docker Desktop、容器镜像或训练模型；这些仍需单独准备。

版本以根目录 `requirements.lock` 为准。`tests/test_web_startup.py` 的离线包完整性检查核对每个 Windows 锁定依赖的版本约束和兼容平台标签。
原始第三方版权与许可保留在各 wheel 的 `.dist-info` 元数据及 licenses 文件中。
这些是第三方原始分发包，没有修改其二进制内容。

已在 Windows x64、CPython 3.12 的全新虚拟环境中验证纯离线安装与 `--smoke` 启动。
ARM64、32 位 Python、其他 Python 小版本不在本 wheel 集合的目标范围内。

## 更新离线依赖

修改锁文件后，在 Windows x64、CPython 3.12 环境中同步依赖包并运行完整性检查：

```powershell
.\.venv\Scripts\python.exe -m pip download --only-binary=:all: --dest vendor/wheels-win_amd64 -r requirements.lock
.\.venv\Scripts\python.exe -m pytest -q tests/test_web_startup.py -k wheelhouse
```

`vendor/` 被 `.gitignore` 忽略，新增 wheel 必须通过 `git add -f -- vendor/wheels-win_amd64/文件名.whl` 显式纳入版本控制，才能随 GitHub 检出分发。
