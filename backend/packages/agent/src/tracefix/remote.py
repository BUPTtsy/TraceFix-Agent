"""User-visible remote repository settings for an agent session."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit


REMOTE_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REMOTE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


def validate_repository(value: str) -> str:
    value = value.strip()
    if REMOTE_REPOSITORY.fullmatch(value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname == "github.com":
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if REMOTE_REPOSITORY.fullmatch(path):
            return path
    raise ValueError("远程仓库必须使用 owner/repository 或 GitHub HTTPS 地址")


def validate_branch(value: str) -> str:
    value = value.strip()
    if not value or not REMOTE_BRANCH.fullmatch(value) or value.startswith("/") or value.endswith("/"):
        raise ValueError("分支名称包含无效字符")
    if ".." in value or "//" in value or value.endswith("."):
        raise ValueError("分支名称不能包含连续点、连续斜杠或点结尾")
    return value


def default_path(data_root: Path | None = None) -> Path:
    return Path(data_root or os.getenv("TRACEFIX_DATA", ".tracefix")) / "remote-config.json"


class RemoteConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or default_path())

    def _read(self) -> dict:
        if not self.path.exists():
            return {"projects": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"远程仓库配置无法读取: {self.path}") from error
        if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
            raise ValueError("远程仓库配置格式无效")
        return {"projects": data["projects"]}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                               prefix=self.path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)

    def get(self, project_id: str) -> dict | None:
        value = self._read()["projects"].get(project_id)
        return dict(value) if isinstance(value, dict) else None

    def set(self, project_id: str, value: dict) -> dict:
        data = self._read()
        data["projects"][project_id] = dict(value)
        self._write(data)
        return dict(value)

    def clear(self, project_id: str) -> None:
        data = self._read()
        data["projects"].pop(project_id, None)
        self._write(data)


def effective(project: dict | None, session: dict | None) -> dict | None:
    if not project and not session:
        return None
    merged = dict(project or {})
    merged.update(session or {})
    return merged


def prepare_checkout(settings: dict, destination: Path) -> dict:
    """Clone a configured repair repository into an isolated local project."""
    repository = validate_repository(settings['repository'])
    base = settings.get('base') or 'main'
    validate_branch(base)
    branch = settings.get('branch')
    if branch:
        validate_branch(branch)
    destination = destination.expanduser().resolve()
    if destination.exists():
        if (destination / '.git').is_dir():
            return {'destination': str(destination), 'repository': repository, 'base': base,
                    'branch': branch, 'action': 'existing'}
        if any(destination.iterdir()):
            raise ValueError('远程仓库目标目录非空且不是 Git 工作区')
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ['git', 'clone', '--origin', 'origin', '--branch', base,
               '--single-branch', f'https://github.com/{repository}.git', str(destination)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8', timeout=120)
        if branch and branch != base:
            subprocess.run(['git', '-C', str(destination), 'switch', '--create', branch],
                           check=True, capture_output=True, text=True, encoding='utf-8', timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        detail = getattr(error, 'stderr', '') or str(error)
        raise RuntimeError(f'远程仓库拉取失败: {detail[-500:]}') from error
    return {'destination': str(destination), 'repository': repository, 'base': base,
            'branch': branch, 'action': 'cloned'}
