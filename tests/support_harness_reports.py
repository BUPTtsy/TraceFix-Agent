"""报告集成测试使用短工作目录，避免 Windows Git 候选引用超过路径限制。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def report_workspace():
    parent = Path(__file__).resolve().parents[1] / '.tracefix'
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='rp-', dir=parent) as directory:
        yield Path(directory)
