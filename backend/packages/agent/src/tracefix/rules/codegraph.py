"""Persistent CodeGraph indexes, one database per configured repository."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from tracefix.execution.platforms import safe_relative
from tracefix.execution.repository import plain_root
from tracefix.runtime.contracts import digest


CODEGRAPH_VERSION = "1.6.2"
GRAPH_CHECKS = ("symbols", "context", "callers", "callees", "impact")
CODEGRAPH_SUPPORTED_SUFFIXES = {
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx",
    ".vue", ".svelte", ".astro", ".py", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".cs", ".php", ".rb", ".swift",
    ".scala", ".lua", ".luau", ".dart", ".erl", ".hrl", ".sol", ".nix",
    ".tf", ".r", ".R", ".m", ".mm", ".hpp", ".kts",
}
MAX_FILES = 20000
MAX_BYTES = 128 * 1024 * 1024
AGENT_ROOT = Path(__file__).resolve().parents[3]


def is_graph_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return (PurePosixPath(path).suffix in CODEGRAPH_SUPPORTED_SUFFIXES
            or name in {"package.json", "go.mod", "Cargo.toml", "composer.json", "Gemfile", "pom.xml"}
            or fnmatchcase(name, "tsconfig*.json") or fnmatchcase(name, "jsconfig*.json"))


class CodeGraphError(RuntimeError):
    pass


def command_prefix() -> list[str]:
    configured = os.getenv("TRACEFIX_CODEGRAPH_COMMAND")
    launchers = [Path(configured)] if configured else [
        root / "node_modules" / ".bin" / ("codegraph.cmd" if os.name == "nt" else "codegraph")
        for root in (AGENT_ROOT, AGENT_ROOT.parents[2])
    ]
    executable = configured or shutil.which("codegraph")
    if executable and not configured:
        launchers.append(Path(executable))
    if os.name == "nt":
        if os.environ.get("APPDATA") and not configured:
            launchers.append(Path(os.environ["APPDATA"]) / "npm" / "codegraph.cmd")
        arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        for launcher in launchers:
            for packages in (launcher.parent.parent / "@colbymchenry",
                             launcher.parent / "node_modules" / "@colbymchenry"):
                for bundle in (packages / f"codegraph-win32-{arch}",
                               packages / "codegraph" / "node_modules" / "@colbymchenry" / f"codegraph-win32-{arch}"):
                    entry = bundle / "lib" / "dist" / "bin" / "codegraph.js"
                    runtime = bundle / "node.exe"
                    if runtime.is_file() and entry.is_file():
                        return [str(runtime), "--liftoff-only", "--disable-warning=ExperimentalWarning", str(entry)]
        if executable and Path(executable).suffix.lower() == ".exe":
            return [executable]
        raise CodeGraphError("未找到 CodeGraph Windows runtime，请执行 npm ci --workspace @tracefix/agent --include=optional")
    if not configured:
        for launcher in launchers:
            if launcher.is_file():
                return [str(launcher)]
    if not executable:
        raise CodeGraphError("找不到 CodeGraph 运行组件，请执行 npm ci --workspace @tracefix/agent --include=optional")
    return [executable]


def run_cli(arguments: list[str], root: Path) -> str:
    try:
        timeout = min(max(int(os.getenv("TRACEFIX_CODEGRAPH_TIMEOUT", "120")), 1), 600)
    except ValueError as error:
        raise CodeGraphError("TRACEFIX_CODEGRAPH_TIMEOUT 必须为整数") from error
    environment = os.environ.copy()
    environment.update(CODEGRAPH_TELEMETRY="0", CODEGRAPH_NO_DOWNLOAD="1", NO_COLOR="1")
    try:
        completed = subprocess.run(command_prefix() + arguments, cwd=root, env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as error:
        raise CodeGraphError(f"CodeGraph {arguments[0]} 超时（{timeout}s）") from error
    except OSError as error:
        raise CodeGraphError("CodeGraph CLI 无法启动") from error
    if completed.returncode:
        raise CodeGraphError(f"CodeGraph {arguments[0]} 失败：" + (completed.stderr or completed.stdout)[-1000:])
    return completed.stdout.strip()


def json_output(stdout: str, expected: type):
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise CodeGraphError("CodeGraph 未返回有效 JSON") from error
    if not isinstance(value, expected):
        raise CodeGraphError("CodeGraph JSON 结构无效")
    return value


def store_root(data_root: Path | None = None) -> Path:
    configured = os.getenv("TRACEFIX_CODEGRAPH_ROOT")
    return plain_root(Path(configured)) if configured else plain_root(Path(data_root or os.getenv("TRACEFIX_DATA", ".tracefix"))) / "codegraph"


class GraphSession:
    """已同步索引上的只读查询；只在 query_session 持锁期间有效。"""

    def __init__(self, root: Path, sync: dict):
        self.root, self.sync = root, sync

    def symbols(self, selected: list[str], query: str = "") -> tuple[list[dict], bool]:
        filters = " ".join("path:" + json.dumps(path, ensure_ascii=False) for path in selected)
        if not filters:
            raise CodeGraphError("没有符合查询范围的授权源码")
        entries = json_output(run_cli(["query", "--json", "--limit", "501", "--path", str(self.root),
                                       "--", (query + " " + filters).strip()], self.root), list)
        items = []
        for entry in entries:
            node = entry.get("node") if isinstance(entry, dict) else None
            if not isinstance(node, dict) or not isinstance(node.get("filePath"), str):
                raise CodeGraphError("CodeGraph 符号结果结构无效")
            if node["filePath"] in selected:
                items.append({**node, "path": node["filePath"], "line": node.get("startLine"), "source": "codegraph"})
        return items, len(entries) >= 501

    def relation(self, check: str, query: str) -> dict:
        if check not in GRAPH_CHECKS or check == "symbols":
            raise CodeGraphError("不支持的 CodeGraph 检查：" + check)
        if not query or len(query) > 2000:
            raise CodeGraphError("图关系查询需要 1 至 2000 字符的 query")
        options = ["--format", "json", "--max-nodes", "50"] if check == "context" else ["--json"]
        return json_output(run_cli([check, *options, "--path", str(self.root), "--", query], self.root), dict)

    def callers(self, query: str) -> dict:
        return self.relation("callers", query)

    def callees(self, query: str) -> dict:
        return self.relation("callees", query)


class RepositoryCodeGraph:
    def __init__(self, project, data_root: Path | None = None):
        self.identity = {"repo_id": project.repo_id, "root": os.path.normcase(str(project.root.resolve()))}
        self.key = digest(self.identity)
        self.directory = plain_root(store_root(data_root) / self.key)
        self.root = plain_root(self.directory / "source")
        self.database = self.root / ".codegraph" / "codegraph.db"

    def _sources(self, files):
        sources, identities = {}, set()
        total = 0
        for path, content in files.items() if isinstance(files, dict) else files:
            safe_relative(path)
            if ".codegraph" in PurePosixPath(path).parts:
                raise CodeGraphError("源码不得覆盖 CodeGraph 索引目录")
            if not is_graph_file(path):
                continue
            data = content.encode("utf-8")
            total += len(data)
            if len(data) > 2 * 1024 * 1024 or total > MAX_BYTES or len(sources) >= MAX_FILES:
                raise CodeGraphError("CodeGraph 授权源码超过大小或文件数量上限")
            if path.casefold() in identities:
                raise CodeGraphError("CodeGraph 源码包含重复或大小写冲突路径")
            identities.add(path.casefold())
            sources[path] = data
        return sources

    def _sync(self, sources: dict[str, bytes], binding: dict) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        version = run_cli(["--version"], self.root)
        if version != CODEGRAPH_VERSION:
            raise CodeGraphError(f"CodeGraph 版本不匹配，需要 {CODEGRAPH_VERSION}，实际 {version}")
        manifest_path = self.directory / "manifest.json"
        previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        versions = {path: digest(data) for path, data in sources.items()}
        changed = False
        for candidate in self.root.rglob("*"):
            relative = candidate.relative_to(self.root).as_posix()
            if ".codegraph" in PurePosixPath(relative).parts:
                continue
            plain_root(candidate)
            if candidate.is_file() and relative not in sources:
                candidate.unlink()
                changed = True
        for path, data in sources.items():
            target = plain_root(self.root.joinpath(*PurePosixPath(path).parts))
            if not target.is_file() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                changed = True
        if not self.database.is_file():
            run_cli(["init", "--yes", str(self.root)], self.root)
        elif changed or previous.get("content_versions") != versions:
            run_cli(["sync", "--quiet", str(self.root)], self.root)
        status = json_output(run_cli(["status", "--json", str(self.root)], self.root), dict)
        index_status = status.get("index")
        pending = status.get("pendingChanges")
        if (status.get("initialized") is not True or not isinstance(index_status, dict)
                or index_status.get("state") != "complete" or index_status.get("reindexRecommended")
                or not isinstance(pending, dict) or any(pending.values())):
            raise CodeGraphError("CodeGraph 索引尚未完成或仍有未同步源码")
        manifest = {"repository": self.identity, "content_versions": versions,
                    "source_version": digest(versions), "binding": binding, "version": version}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"repo_key": self.key, "database_path": str(self.database), "source_version": manifest["source_version"],
                "binding": binding, "status": status, "version": version}

    @contextmanager
    def query_session(self, files, binding: dict | None = None):
        """同一把索引锁内只同步一次，随后可多次查询符号与调用关系。"""
        from tracefix.repository_registry import registry_lock
        sources = self._sources(files)
        with registry_lock(self.directory / "index", timeout=600):
            yield GraphSession(self.root, self._sync(sources, dict(binding or {})))

    def analyze(self, files, *, selected: list[str], check: str = "symbols", query: str = "",
                binding: dict | None = None) -> dict:
        result = {"status": "pass", "detector": check, "findings": [], "items": [], "errors": [],
                  "analyzed_files": selected, "supported_languages": [], "parser": "codegraph",
                  "codegraph_index": "repository", "fallback_required": False}
        try:
            with self.query_session(files, binding) as session:
                result["codegraph"] = session.sync
                result["supported_languages"] = session.sync["status"].get("languages", [])
                if check == "index":
                    return result
                if check in {"symbols", "elements", "syntax", "a11y_name", "event_binding"}:
                    items, result["truncated"] = session.symbols(selected, query)
                    result["items"] = items[:500]
                elif check in GRAPH_CHECKS:
                    result["graph"] = session.relation(check, query)
                else:
                    raise CodeGraphError("不支持的 CodeGraph 检查：" + check)
        except (CodeGraphError, OSError, ValueError, RuntimeError) as error:
            result["errors"].append({"detector": check, "message": str(error)})
            result.update(status="error", fallback_required=True)
        return result


def prepare_project_index(projects, scope_id: str, data_root: Path | None = None) -> dict:
    from tracefix.execution.repository import run_git
    from tracefix.knowledge.scope import ScopeResolver
    resolver = ScopeResolver(projects)
    context = resolver.context(scope_id)
    project = projects[scope_id]
    index = RepositoryCodeGraph(project, data_root)
    try:
        sources = []
        total_bytes = 0
        for record in run_git(project.root, "ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            metadata, encoded = record.split(b"\t", 1)
            if metadata.split()[0] not in {b"100644", b"100755"}:
                continue
            relative = encoded.decode("utf-8")
            if not is_graph_file(relative):
                continue
            try:
                path = resolver.path(context, relative)
            except PermissionError:
                continue
            if path.is_file():
                size = path.stat().st_size
                total_bytes += size
                if size > 2 * 1024 * 1024 or total_bytes > MAX_BYTES or len(sources) >= MAX_FILES:
                    raise CodeGraphError("CodeGraph 授权源码超过大小或文件数量上限")
                sources.append((relative, path.read_bytes().decode("utf-8")))
        result = index.analyze(sources, selected=[], check="index",
                               binding={"scope_id": scope_id, "view": "repository"})
    except (OSError, ValueError, RuntimeError) as error:
        result = {"status": "error", "fallback_required": True, "errors": [{"message": str(error)}]}
    index.directory.mkdir(parents=True, exist_ok=True)
    (index.directory / "health.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
