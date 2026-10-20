"""H3/E1 正式对照入口：冻结配置 + 容器内 Agent adapter + 评测方私有 Oracle。

本入口不放宽 SessionAdapter 对 held-out 的拒绝。调用方必须提供在 Docker 容器内运行 Agent 的
adapter 工厂（``module:function``），其返回对象需实现：
  - ``__call__``/``run``：与 FourCellRunner 的 adapter 约定一致；
  - ``boundary()``：返回最近一次运行的 ``DockerAgentBoundary``（含真实 container_ids、
    clean 镜像 digest 与 network/cache/retrieval 证据）。
隔离证明由 ``DockerAgentBoundary.verify`` 对真实容器 inspect 得出，缺任何一项都会得到
``isolation_error``，行不会进入 mode_gate 的 held-out 判定。
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from evals.config import EvaluationConfig
from evals.oracle_bridge import OracleBridge
from evals.run_matrix import run_matrix
from evals.runner import FourCellRunner


class _LatestBoundary:
    """把 verify 委托给 adapter 当前运行的容器清单（容器在每组运行时才创建）。"""

    def __init__(self, adapter):
        self.adapter = adapter

    def verify(self, private_root, private_files):
        return self.adapter.boundary().verify(private_root, private_files)


def _load_factory(spec):
    module, _, name = spec.partition(':')
    if not module or not name:
        raise ValueError('adapter 工厂格式为 module:function')
    return getattr(importlib.import_module(module), name)


def build_runner_factory(adapter_factory, private_root, oracle_script, *, timeout_seconds=600):
    private_root = Path(private_root).resolve()
    oracle_script = Path(oracle_script).resolve()

    def factory(config, output):
        if not config.held_out:
            raise ValueError('H3/E1 正式对照要求 held_out=true 的冻结配置')
        adapter = adapter_factory(config, output)
        if not callable(getattr(adapter, 'boundary', None)):
            raise TypeError('adapter 必须提供 boundary()，返回 DockerAgentBoundary')
        oracle = OracleBridge(
            private_root=private_root,
            command=('node', str(oracle_script)),
            oracle_script=oracle_script,
            boundary=_LatestBoundary(adapter),
            timeout_seconds=timeout_seconds,
        )
        return FourCellRunner(config, output, adapter=adapter, oracle=oracle)

    return factory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, action='append', required=True, help='已冻结的 EvaluationConfig JSON')
    parser.add_argument('--adapter', required=True, help='容器内 Agent adapter 工厂 module:function(config, output)')
    parser.add_argument('--private-root', type=Path, required=True, help='评测方私有目录（须已存在，Agent 不可见）')
    parser.add_argument('--oracle-script', type=Path, required=True, help='私有目录内的 Oracle 脚本')
    parser.add_argument('--arms', default='pipeline,hybrid')
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--oracle-timeout', type=float, default=600)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    configs = [EvaluationConfig.load(path) for path in args.config]
    factory = build_runner_factory(_load_factory(args.adapter), args.private_root, args.oracle_script,
                                   timeout_seconds=args.oracle_timeout)
    report = run_matrix(configs, args.output, arms=tuple(args.arms.split(',')), repeat=args.repeat,
                        runner_factory=factory)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
