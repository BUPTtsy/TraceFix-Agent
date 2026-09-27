from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import Field, model_validator

from tracefix.runtime.contracts import Contract, digest


class Project(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    parent_id: str | None = None
    repo_id: str
    root: Path
    memory_revision: int = 1
    access_epoch: int = 1
    allowed_files: list[str] = Field(default_factory=lambda: ["src/**", "server/**"])
    shared_paths: list[str] = Field(default_factory=list)
    agent_instructions: str = "AGENTS.md"

    @model_validator(mode="after")
    def validate_agent_instructions(self):
        path = Path(self.agent_instructions)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError("agent_instructions 必须是项目内的相对路径")
        return self


class Profile(Contract):
    project: str
    source_commit: str
    url: str = "http://app:3000"
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://app:3000"])
    image: str = "tracefix-bugboard:1.0"
    browser_image: str = "tracefix-browser:1.0"
    commands: dict[str, list[str]]
    health_path: str = "/health"
    version_path: str = "/version"
    port: int = 3000
    timeout_seconds: int = Field(default=120, ge=1, le=600)
    screenshot_redaction: str = "public_demo"

    @model_validator(mode="after")
    def audited_templates(self):
        required = {"start", "reset", "static", "unit", "build"}
        if not required <= self.commands.keys():
            raise ValueError(f"缺少已批准的命令：{required - self.commands.keys()}")
        for key, command in self.commands.items():
            if not command or not all(isinstance(a, str) and a for a in command):
                raise ValueError(f"命令无效：{key}")
        for origin in self.allowed_origins:
            u = urlsplit(origin)
            if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.path not in {"", "/"}:
                raise ValueError("origin 必须是精确的 http(s) 来源")
        return self

    @property
    def environment_digest(self):
        return digest(self)


def load_projects(path: Path) -> dict[str, Project]:
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    projects = {}
    for item in data["projects"]:
        item["root"] = (path.parent / item["root"]).resolve()
        p = Project(**item)
        if p.id in projects:
            raise ValueError("项目 ID 重复")
        projects[p.id] = p
    return projects


def load_profile(path: Path) -> Profile:
    return Profile(**yaml.safe_load(path.read_text(encoding='utf-8')))


def create_project(path: Path, project_id: str, root: str, repo_id: str,
                   allowed_files: list[str] | None = None) -> Project:
    """Create a local product project in the user-owned registry."""
    raw = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {'projects': []}
    raw = raw or {'projects': []}
    entries = raw.get('projects', [])
    if any(item.get('id') == project_id for item in entries):
        raise ValueError('项目 ID 已存在')
    item = {'id': project_id, 'repo_id': repo_id, 'root': root,
            'allowed_files': allowed_files or ['src/**', 'server/**'],
            'memory_revision': 1, 'access_epoch': 1, 'agent_instructions': 'AGENTS.md'}
    project = Project(**{**item, 'root': (path.parent / root).resolve() if not Path(root).is_absolute() else Path(root)})
    entries.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({'projects': entries}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    project.root.mkdir(parents=True, exist_ok=True)
    return project


def save_profile(path: Path, fields: dict) -> Profile:
    """Validate and persist a complete project execution profile."""
    payload = dict(fields)
    profile = Profile(**payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile.model_dump(mode='json'), allow_unicode=True, sort_keys=False), encoding='utf-8')
    return profile
