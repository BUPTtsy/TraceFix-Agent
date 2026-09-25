"""Clone a GitHub repository into an isolated TraceFix run directory.

This script only prepares a local checkout. It never pushes, creates a pull
request, or reads a token; those operations belong to an explicitly enabled
GitHub MCP adapter.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from tracefix.messages import ChineseArgumentParser, error_message


REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def run(*arguments: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = '\n'.join(part.strip() for part in (getattr(error, 'stdout', ''), getattr(error, 'stderr', '')) if part and part.strip())
        raise RuntimeError(f'Git 工作区准备失败：{error_message(error)}'
                           + (f'\nGit 原始输出：\n{detail}' if detail else '')) from error
    return result.stdout.strip()


def repository_url(repository: str) -> str:
    if not REMOTE_PATTERN.fullmatch(repository):
        raise ValueError("仓库必须使用 owner/repository 格式")
    return f"https://github.com/{repository}.git"


def validate_destination(destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"目标目录必须为空：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    parser = ChineseArgumentParser(description="准备 GitHub 分支开发的 TraceFix 本地工作区")
    repository_group = parser.add_mutually_exclusive_group()
    repository_group.add_argument("--repository", help="源仓库或已有 fork，格式为 owner/repository")
    repository_group.add_argument("--fork", help="兼容旧参数：已有 fork，格式为 owner/repository")
    parser.add_argument("--destination", type=Path, required=True, help="本次 Run 的独立 clone 目录")
    parser.add_argument("--base", default=os.getenv("TRACEFIX_GITHUB_BASE", "main"), help="要检出的基线分支")
    parser.add_argument("--branch", required=True, help="必须创建并检出的本地工作分支")
    args = parser.parse_args()
    repository = args.repository or args.fork or os.getenv("TRACEFIX_GITHUB_REPOSITORY") or os.getenv("TRACEFIX_GITHUB_FORK")
    if not repository:
        parser.error("必须提供 --repository（或兼容参数 --fork）")
    validate_destination(args.destination)
    url = repository_url(repository)
    run("clone", "--origin", "origin", "--branch", args.base, "--single-branch", url, str(args.destination))
    run("switch", "--create", args.branch, cwd=args.destination)
    print(f"已准备隔离的 GitHub 工作区：{args.destination}")
    print(f"远程仓库：{repository}；基线：{args.base}；分支：{args.branch}")


if __name__ == "__main__":
    main()
