"""Install pinned CodeGraph and prepare every configured repository index."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/packages/agent/src"))

from tracefix.config import load_projects
from tracefix.rules.codegraph import CODEGRAPH_VERSION, prepare_project_index, run_cli


def install():
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm 不可用，请先安装 Node.js 或使用 CodeGraph 官方独立安装器")
    command = [npm]
    if os.name == "nt":
        node = shutil.which("node")
        entry = Path(npm).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if not node or not entry.is_file():
            raise RuntimeError("无法定位 npm-cli.js，请执行 npm ci --workspace @tracefix/agent --include=optional")
        command = [node, str(entry)]
    subprocess.run([*command, "ci", "--workspace", "@tracefix/agent", "--include=optional", "--omit=dev"],
                   cwd=ROOT, check=True, timeout=300)


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装 CodeGraph 并为每个已配置仓库构建独立持久图数据库")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--check", action="store_true", help="仅校验 Agent 运行依赖及锁定版本，不读取项目注册表或构建索引")
    parser.add_argument("--projects", type=Path, default=ROOT / "profiles/projects.yaml")
    parser.add_argument("--data", type=Path, default=Path(os.getenv("TRACEFIX_DATA", str(ROOT / ".tracefix"))))
    parser.add_argument("--repo", action="append", help="只准备指定项目 ID，可重复")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report_path = args.report or args.data / "codegraph/deployment-report.json"
    report = {"component": "colbymchenry/codegraph", "version": CODEGRAPH_VERSION, "repositories": {}}
    try:
        if args.install:
            install()
        version = run_cli(["--version"], ROOT)
        if version != CODEGRAPH_VERSION:
            raise RuntimeError(f"CodeGraph 版本不匹配，需要 {CODEGRAPH_VERSION}，实际 {version}")
        report["runtime"] = {"status": "pass", "version": version}
        if not args.check:
            projects = load_projects(args.projects)
            selected = args.repo or list(projects)
            unknown = set(selected) - projects.keys()
            if unknown:
                raise ValueError("未配置的项目 ID：" + ", ".join(sorted(unknown)))
            for identifier in selected:
                report["repositories"][identifier] = prepare_project_index(projects, identifier, args.data)
        report["status"] = "passed" if all(value["status"] == "pass" for value in report["repositories"].values()) else "failed"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        report.update(status="failed", error=str(error))
    if not args.check:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
