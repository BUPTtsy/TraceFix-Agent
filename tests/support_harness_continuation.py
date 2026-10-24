"""Harness 恢复测试使用短目录，避免 Windows Git 引用路径超过限制。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def continuation_workspace():
    parent = Path(__file__).resolve().parents[1] / '.tracefix'
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='hc-', dir=parent) as directory:
        workspace = Path(directory).resolve()
        assert workspace.parent == parent.resolve()
        yield workspace
