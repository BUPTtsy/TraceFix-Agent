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
    repository_config: dict | None = None

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
        if p.repository_config:
            from tracefix.repository_registry import validate_stored_config
            validate_stored_config(p)
            if data.get('frozen_run_id') and p.repository_config.get('frozen_run_id') != data['frozen_run_id']:
                raise ValueError('冻结注册表的 run_id 与仓库快照不一致')
        if p.id in projects:
            raise ValueError("项目 ID 重复")
        projects[p.id] = p
    return projects


def load_profile(path: Path) -> Profile:
    return Profile(**yaml.safe_load(path.read_text(encoding='utf-8')))


def create_project(path: Path, project_id: str, root: str, repo_id: str,
                   allowed_files: list[str] | None = None) -> Project:
    """Create a local product project in the user-owned registry."""
    from tracefix.repository_registry import RepositoryRegistry
    return RepositoryRegistry(path).create_legacy(project_id, root, repo_id, allowed_files)


def save_profile(path: Path, fields: dict) -> Profile:
    """Validate and persist a complete project execution profile."""
    from tracefix.repository_registry import RepositoryRegistry
    registry = path.parent / 'projects.yaml'
    if registry.exists():
        return RepositoryRegistry(registry).configure_legacy(fields['project'], fields, path)
    from tracefix.repository_registry import atomic_yaml, registry_lock
    profile = Profile(**dict(fields))
    with registry_lock(path):
        atomic_yaml(path, profile.model_dump(mode='json'))
    return profile
