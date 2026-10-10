"""GitHub MCP 的受控、只读 stdio 连接器。

Git checkout 仍然负责源码物化；本模块只读取 GitHub 元数据和外部描述。
连接器不会把发现到的 MCP 工具当作任意工具执行，而是先绑定到有限的
语义操作，再按服务器实际返回的 JSON Schema 构造参数。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import base64
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import unquote, urlsplit

import jsonschema

from tracefix.runtime.contracts import digest
from tracefix.storage.artifacts import redact

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = StdioServerParameters = stdio_client = None


_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REPO_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SEMANTIC_NAMES = {
    "metadata": ("get_repository", "repository_get", "get_repo"),
    "file": ("get_file_contents", "get_file_content", "read_file"),
    "commit": ("get_commit", "commit_get"),
    "issue": ("issue_read",),
    "pull_request": ("pull_request_read", "pr_read"),
}
_READ_KINDS = frozenset(_SEMANTIC_NAMES)
_ISSUE_KINDS = frozenset({"issue", "pull_request"})
_SAFE_ENV = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
    "HOME", "USERPROFILE", "LANG", "LC_ALL",
})


class GitHubMCPError(RuntimeError):
    """不会暴露 MCP、命令行或凭据原文的稳定连接器错误。"""

    def __init__(self, code: str, message: str = "GitHub MCP 读取失败", *, details: Mapping[str, Any] | None = None):
        self.code = code
        self.details = {"code": code, **(dict(details or {}))}
        super().__init__(message)


@dataclass(frozen=True)
class GitHubMCPConfig:
    """部署侧 connector 配置；模型不能修改其中任何字段。"""

    command: tuple[str, ...]
    args: tuple[str, ...] = ()
    repo_id: str = ""
    owner: str = ""
    repo: str = ""
    ref: str = ""
    commit: str = ""
    credential_ref: str = ""
    allowed_files: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    max_response_bytes: int = 1_048_576
    env_keys: tuple[str, ...] = ()

    def __post_init__(self):
        if isinstance(self.command, str) or isinstance(self.args, str):
            raise ValueError("GitHub MCP command/args 必须是参数数组")
        command = tuple(str(item) for item in self.command)
        args = tuple(str(item) for item in self.args)
        if not command or not command[0].strip() or any(not item for item in command + args):
            raise ValueError("GitHub MCP command/args 无效")
        if not _REPO_ID.fullmatch(self.repo_id or ""):
            raise ValueError("repo_id 无效")
        if not _NAME.fullmatch(self.owner or "") or not _NAME.fullmatch(self.repo or ""):
            raise ValueError("GitHub owner/repo 无效")
        if not self.ref or any(char in self.ref for char in "\r\n\x00"):
            raise ValueError("GitHub ref 无效")
        if not _SHA.fullmatch(self.commit or ""):
            raise ValueError("GitHub commit 必须是具体 SHA")
        if not self.credential_ref or any(char in self.credential_ref for char in "\r\n\x00"):
            raise ValueError("credential_ref 无效")
        if (isinstance(self.timeout_seconds, bool) or not math.isfinite(float(self.timeout_seconds))
                or not 0.1 <= float(self.timeout_seconds) <= 300):
            raise ValueError("GitHub MCP timeout 必须在 0.1-300 秒内")
        if not isinstance(self.max_response_bytes, int) or not 1024 <= self.max_response_bytes <= 10_485_760:
            raise ValueError("GitHub MCP 最大响应必须在 1 KiB-10 MiB 内")
        for pattern in self.allowed_files:
            if not isinstance(pattern, str) or not pattern or pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
                raise ValueError("allowed_files 必须是仓库内相对模式")
        if any(key not in _SAFE_ENV for key in self.env_keys):
            raise ValueError("env_keys 包含未批准的继承变量")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "args", args)
        object.__setattr__(self, "allowed_files", tuple(self.allowed_files))
        object.__setattr__(self, "env_keys", tuple(self.env_keys))
        object.__setattr__(self, "commit", self.commit.lower())

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any], *, command: list[str] | tuple[str, ...],
                      args: list[str] | tuple[str, ...] = (), timeout_seconds: float = 30,
                      max_response_bytes: int = 1_048_576,
                      env_keys: list[str] | tuple[str, ...] = ()):
        """从 B1 冻结快照建立绑定，不读取任意当前项目配置。"""
        if not isinstance(snapshot, Mapping):
            raise ValueError("repository snapshot 无效")
        repository = snapshot.get("repository_config")
        if not isinstance(repository, Mapping):
            raise ValueError("repository snapshot 无效")
        commit = str(snapshot.get("commit") or "")
        configured_commit = str(repository.get("commit") or "")
        repo_id = snapshot.get("repo_id")
        if (not isinstance(repo_id, str) or snapshot.get("projectId", repo_id) != repo_id
                or repository.get("repo_id") != repo_id or repository.get("projectId") != repo_id
                or not configured_commit or configured_commit != commit):
            raise ValueError("repository snapshot commit 不一致")
        for key in ("owner", "repo", "allowed_files", "credential_ref", "default_ref"):
            if key in snapshot and snapshot[key] != repository.get(key):
                raise ValueError("repository snapshot 配置绑定不一致")
        expected_digest = digest({key: value for key, value in repository.items()
                                  if key not in {"config_digest", "frozen_run_id", "snapshot_digest"}})
        if repository.get("config_digest") != expected_digest or snapshot.get("config_digest") != expected_digest:
            raise ValueError("repository snapshot 配置摘要不一致")
        if not repository.get("frozen_run_id") or repository.get("snapshot_digest") != digest({
                key: value for key, value in repository.items() if key != "snapshot_digest"}):
            raise ValueError("repository snapshot 冻结摘要不一致")
        return cls(
            command=tuple(command), args=tuple(args),
            repo_id=repo_id,
            owner=str(repository.get("owner") or ""), repo=str(repository.get("repo") or repository.get("name") or ""),
            ref=str(repository.get("default_ref") or repository.get("ref") or snapshot.get("ref") or ""),
            commit=commit,
            credential_ref=str(repository.get("credential_ref") or ""),
            allowed_files=tuple(repository.get("allowed_files") or ()),
            timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes, env_keys=tuple(env_keys),
        )


@dataclass(frozen=True)
class GitHubReadRequest:
    """B4 入口的调用请求；绑定字段只能由部署侧填充。"""

    source_kind: str
    path: str | None = None
    number: int | None = None
    method: str = "get"
    ref: str | None = None
    commit: str | None = None
    repo_id: str | None = None
    owner: str | None = None
    repo: str | None = None

    def __post_init__(self):
        if self.source_kind not in _READ_KINDS:
            raise ValueError("GitHub source_kind 不在只读白名单")
        if self.source_kind == "file" and (not self.path or self.number is not None):
            raise ValueError("file 读取需要 path")
        if self.source_kind in _ISSUE_KINDS and (isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1):
            raise ValueError("Issue/PR 读取需要正整数编号")
        if self.source_kind not in _ISSUE_KINDS and self.number is not None:
            raise ValueError("该读取类型不接受 number")
        if self.source_kind in _ISSUE_KINDS and self.method != "get":
            raise ValueError("Issue/PR 只允许 method=get")


@dataclass(frozen=True)
class GitHubContext:
    repo_id: str
    owner: str
    repo: str
    commit: str
    ref: str
    source_kind: str
    source_id: str
    content: str
    content_digest: str
    retrieved_at: str
    truncated: bool = False
    original_bytes: int = 0
    returned_bytes: int = 0
    source_url: str | None = None

    def to_context_document(self) -> dict[str, Any]:
        """适配 ContextAssembler 的 reference_documents 可选区块。"""
        provenance = {
            "repo_id": self.repo_id, "owner": self.owner, "repo": self.repo,
            "commit": self.commit, "ref": self.ref, "source_kind": self.source_kind,
            "source_id": self.source_id, "content_digest": self.content_digest,
            "retrieved_at": self.retrieved_at, "truncated": self.truncated,
            "original_bytes": self.original_bytes, "returned_bytes": self.returned_bytes,
        }
        if self.source_url:
            provenance["source_url"] = self.source_url
        return {
            "kind": "github_context", "content": self.content,
            "provenance": provenance, "source_refs": [self.source_url] if self.source_url else [],
            "untrusted": True, "reproduction_status": "not_evidence_of_reproduction",
        }


@dataclass
class _Pending:
    name: str
    arguments: dict[str, Any]
    future: asyncio.Future


CredentialResolver = Callable[[str], Mapping[str, str] | Awaitable[Mapping[str, str]]]


class GitHubMCPConnector:
    """单个 Run 独占一个 GitHub MCP 会话；所有请求串行执行。"""

    def __init__(self, config: GitHubMCPConfig, credential_resolver: CredentialResolver):
        if not callable(credential_resolver):
            raise ValueError("必须提供部署侧 credential resolver")
        self.config = config
        self._resolve_credential = credential_resolver
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._worker: asyncio.Task | None = None
        self._queue: asyncio.Queue[_Pending | None] = asyncio.Queue(maxsize=1)
        self._ready: asyncio.Future | None = None
        self._tools: dict[str, dict[str, Any]] = {}
        self._semantic: dict[str, str] = {}
        self._state = "closed"
        self._lock = asyncio.Lock()
        self._secrets: tuple[str, ...] = ()

    async def open(self) -> dict[str, Any]:
        if self._worker and not self._worker.done():
            raise GitHubMCPError("already_open", "GitHub MCP 已打开")
        try:
            resolved = self._resolve_credential(self.config.credential_ref)
            if inspect.isawaitable(resolved):
                resolved = await asyncio.wait_for(resolved, self.config.timeout_seconds)
            if not isinstance(resolved, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in resolved.items()):
                raise ValueError
            environment = {str(k): str(v) for k, v in resolved.items()}
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\x00" in value for key, value in environment.items()):
                raise ValueError
            self._secrets = tuple(value for value in environment.values() if value)
            environment = {**{key: os.environ[key] for key in self.config.env_keys if key in os.environ}, **environment}
        except asyncio.TimeoutError:
            raise GitHubMCPError("timeout", "GitHub MCP credential 解析超时") from None
        except Exception:
            raise GitHubMCPError("credential_unavailable", "GitHub MCP credential 不可用") from None
        self._ready = asyncio.get_running_loop().create_future()
        self._worker = asyncio.create_task(self._serve(environment))
        try:
            await asyncio.wait_for(self._ready, self.config.timeout_seconds)
        except asyncio.TimeoutError:
            await self.close()
            raise GitHubMCPError("timeout", "GitHub MCP 初始化超时") from None
        except Exception:
            await self.close()
            if isinstance(self._ready.exception(), GitHubMCPError):
                raise self._ready.exception()
            raise GitHubMCPError("connect_failed", "GitHub MCP 连接失败") from None
        try:
            return await self.health()
        except Exception:
            await self.close()
            raise

    async def _serve(self, environment: Mapping[str, str]):
        if stdio_client is None or ClientSession is None:
            error = GitHubMCPError("mcp_unavailable", "GitHub MCP SDK 不可用")
            if self._ready and not self._ready.done():
                self._ready.set_exception(error)
            return
        stderr = open(os.devnull, "w", encoding="utf-8")
        log_filter = _DiscardMCPLogs()
        loggers = [logging.getLogger(name) for name in ("mcp.client.stdio", "mcp.client.session", "mcp.shared.session")]
        for logger in loggers:
            logger.addFilter(log_filter)
        try:
            params = StdioServerParameters(command=self.config.command[0], args=list(self.config.command[1:] + self.config.args),
                                           env=dict(environment))
            async with AsyncExitStack() as stack:
                transport = await stack.enter_async_context(stdio_client(params, errlog=stderr))
                session = await stack.enter_async_context(ClientSession(*transport))
                self._stack, self._session = stack, session
                await asyncio.wait_for(session.initialize(), self.config.timeout_seconds)
                listing = await asyncio.wait_for(session.list_tools(), self.config.timeout_seconds)
                self._discover(listing)
                await asyncio.wait_for(session.send_ping(), self.config.timeout_seconds)
                self._state = "ready"
                if not self._semantic.get("commit"):
                    raise GitHubMCPError("tool_unavailable", "GitHub MCP 缺少 commit 只读能力")
                if self._ready and not self._ready.done():
                    self._ready.set_result(True)
                while True:
                    request = await self._queue.get()
                    if request is None:
                        break
                    if request.future.cancelled():
                        continue
                    try:
                        result = await asyncio.wait_for(session.call_tool(request.name, request.arguments), self.config.timeout_seconds)
                        if not request.future.done():
                            request.future.set_result(result)
                    except asyncio.TimeoutError:
                        if not request.future.done():
                            request.future.set_exception(GitHubMCPError("timeout", "GitHub MCP 工具调用超时"))
                        self._state = "failed"
                        break
                    except Exception:
                        if not request.future.done():
                            request.future.set_exception(GitHubMCPError("transport_error", "GitHub MCP 传输失败"))
                        self._state = "failed"
                        break
        except GitHubMCPError as error:
            self._state = "failed"
            if self._ready and not self._ready.done():
                self._ready.set_exception(error)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._state = "failed"
            if self._ready and not self._ready.done():
                self._ready.set_exception(GitHubMCPError("timeout", "GitHub MCP 初始化超时"))
        except Exception:
            self._state = "failed"
            if self._ready and not self._ready.done():
                self._ready.set_exception(GitHubMCPError("connect_failed", "GitHub MCP 连接失败"))
        finally:
            stderr.close()
            for logger in loggers:
                logger.removeFilter(log_filter)
            self._session = None
            self._stack = None
            if self._state == "ready":
                self._state = "closed"

    def _discover(self, listing: Any):
        tools = getattr(listing, "tools", None)
        if not isinstance(tools, list):
            raise GitHubMCPError("schema_error", "GitHub MCP 工具发现结果无效")
        discovered: dict[str, dict[str, Any]] = {}
        semantic: dict[str, str] = {}
        for tool in tools:
            name = getattr(tool, "name", None)
            schema = getattr(tool, "inputSchema", None)
            if not isinstance(name, str) or not isinstance(schema, Mapping):
                raise GitHubMCPError("schema_error", "GitHub MCP 工具 schema 无效")
            if _has_schema_reference(schema):
                raise GitHubMCPError("schema_error", "GitHub MCP 工具 schema 不允许引用")
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError:
                raise GitHubMCPError("schema_error", "GitHub MCP 工具 schema 无效") from None
            discovered[name] = dict(schema)
            for kind, aliases in _SEMANTIC_NAMES.items():
                if name in aliases:
                    if kind in semantic:
                        raise GitHubMCPError("ambiguous_tool", "GitHub MCP 只读工具名称不唯一")
                    semantic[kind] = name
        self._tools, self._semantic = discovered, semantic
        for kind, name in semantic.items():
            self._validate_semantic_schema(kind, self._tools[name])

    @staticmethod
    def _validate_semantic_schema(kind: str, schema: Mapping[str, Any]):
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or ())
        if not isinstance(properties, Mapping) or not isinstance(required, set):
            raise GitHubMCPError("schema_error", "GitHub MCP 工具 schema 无效")
        required_names = {str(item) for item in required}
        names = set(str(item) for item in properties)
        if not {"owner", "repo"}.issubset(names):
            raise GitHubMCPError("schema_error", "GitHub MCP 工具缺少仓库绑定参数")
        if kind == "file" and not {"path", "ref"}.issubset(names):
            raise GitHubMCPError("schema_error", "GitHub MCP 文件工具缺少 path/ref")
        if kind == "commit" and not ({"sha", "commit", "ref"} & names):
            raise GitHubMCPError("schema_error", "GitHub MCP commit 工具缺少 SHA 参数")
        if kind in _ISSUE_KINDS and ("method" not in names or not {"issue_number", "pullNumber", "number"} & names):
            raise GitHubMCPError("schema_error", "GitHub MCP Issue/PR 工具 schema 不受支持")
        if required_names - names:
            raise GitHubMCPError("schema_error", "GitHub MCP 工具必填参数无效")

    async def close(self):
        worker = self._worker
        if worker and not worker.done():
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                worker.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(worker), min(self.config.timeout_seconds + 2, 8))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                worker.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 2)
                except asyncio.TimeoutError:
                    pass
        self._worker = None
        self._session = None
        self._state = "closed"
        self._tools.clear()
        self._semantic.clear()
        self._queue = asyncio.Queue(maxsize=1)
        self._secrets = ()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    def list_read_tools(self) -> list[str]:
        return sorted(self._semantic.values())

    async def health(self) -> dict[str, Any]:
        if self._state != "ready" or not self._session:
            raise GitHubMCPError("not_ready", "GitHub MCP 尚未就绪")
        try:
            result = await self._enqueue("commit", self._arguments("commit", {}))
            payload = self._payload(result)
            self._verify_payload(payload, "commit")
        except GitHubMCPError:
            raise
        except Exception:
            raise GitHubMCPError("health_failed", "GitHub MCP 健康检查失败") from None
        return {"status": "ready", "repo_id": self.config.repo_id, "owner": self.config.owner,
                "repo": self.config.repo, "ref": self.config.ref, "commit": self.config.commit,
                "tools": self.list_read_tools()}

    async def read_context(self, request: GitHubReadRequest | str, *, path: str | None = None,
                           number: int | None = None, method: str = "get", ref: str | None = None,
                           commit: str | None = None) -> GitHubContext:
        if isinstance(request, str):
            try:
                request = GitHubReadRequest(request, path=path, number=number, method=method, ref=ref, commit=commit)
            except (ValueError, TypeError):
                raise GitHubMCPError("invalid_request", "GitHub MCP 请求格式无效") from None
        if not isinstance(request, GitHubReadRequest):
            raise GitHubMCPError("invalid_request", "GitHub MCP 请求格式无效")
        if (request.ref not in (None, self.config.ref) or request.commit not in (None, self.config.commit)
                or request.repo_id not in (None, self.config.repo_id) or request.owner not in (None, self.config.owner)
                or request.repo not in (None, self.config.repo)):
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 请求绑定与 Run 快照不一致")
        if request.source_kind == "file":
            self._check_path(request.path or "")
        if self._state != "ready":
            raise GitHubMCPError("not_ready", "GitHub MCP 尚未就绪")
        async with self._lock:
            await self.health()
            kind = request.source_kind
            arguments = self._arguments(kind, {"path": request.path, "number": request.number, "method": request.method})
            result = await self._enqueue(kind, arguments)
            payload = self._payload(result)
            self._verify_payload(payload, kind, request)
        payload = self._redact(payload)
        content, source_id, source_url = self._content(payload, request)
        content = self._redact(content)
        source_id = self._redact(source_id)
        source_url = self._redact(source_url) if source_url else None
        original = len(content.encode("utf-8"))
        content, truncated = _truncate_utf8(content, self.config.max_response_bytes)
        return GitHubContext(repo_id=self.config.repo_id, owner=self.config.owner, repo=self.config.repo,
                             commit=self.config.commit, ref=self.config.ref, source_kind=request.source_kind,
                             source_id=source_id, content=content, content_digest=digest(content),
                             retrieved_at=datetime.now(timezone.utc).isoformat(), truncated=truncated,
                             original_bytes=original, returned_bytes=len(content.encode("utf-8")), source_url=source_url)

    def _check_path(self, path: str):
        if not path or "\x00" in path or path.startswith("/") or "\\" in path or ":" in path:
            raise GitHubMCPError("path_denied", "GitHub MCP 文件路径不在允许范围")
        pure = PurePosixPath(path)
        if (".." in pure.parts or any(part in {"", "."} for part in pure.parts)
                or any(part.casefold() in {".git", ".env"} for part in pure.parts)):
            raise GitHubMCPError("path_denied", "GitHub MCP 文件路径不在允许范围")
        if not self.config.allowed_files or not any(_glob(path, pattern) for pattern in self.config.allowed_files):
            raise GitHubMCPError("path_denied", "GitHub MCP 文件路径不在允许范围")

    def _arguments(self, kind: str, values: Mapping[str, Any]) -> dict[str, Any]:
        name = self._semantic.get(kind)
        if not name:
            raise GitHubMCPError("tool_unavailable", "请求的 GitHub MCP 只读能力未发现")
        schema = self._tools[name]
        if _has_schema_reference(schema):
            raise GitHubMCPError("schema_error", "GitHub MCP 工具 schema 不允许引用")
        properties = schema.get("properties") or {}
        aliases = {"sha": self.config.commit, "commit": self.config.commit, "ref": self.config.commit,
                   "owner": self.config.owner, "repo": self.config.repo, "path": values.get("path"),
                   "issue_number": values.get("number"), "pullNumber": values.get("number"),
                   "number": values.get("number"), "method": "get"}
        arguments = {key: value for key, value in aliases.items() if key in properties and value is not None}
        missing = [key for key in schema.get("required", []) if key not in arguments]
        if missing:
            raise GitHubMCPError("schema_error", "GitHub MCP 请求 schema 不支持绑定参数")
        try:
            jsonschema.validate(arguments, schema)
        except jsonschema.ValidationError:
            raise GitHubMCPError("schema_error", "GitHub MCP 请求参数校验失败") from None
        return arguments

    async def _enqueue(self, kind: str, arguments: dict[str, Any]):
        name = self._semantic.get(kind)
        if not name or not self._worker or self._worker.done():
            raise GitHubMCPError("transport_error", "GitHub MCP 会话不可用")
        future = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(self._queue.put(_Pending(name, arguments, future)), self.config.timeout_seconds)
            return await asyncio.wait_for(future, self.config.timeout_seconds + 1)
        except asyncio.TimeoutError:
            self._state = "failed"
            raise GitHubMCPError("timeout", "GitHub MCP 工具调用超时") from None

    def _payload(self, result: Any) -> Any:
        if getattr(result, "isError", False):
            structured = getattr(result, "structuredContent", None)
            if isinstance(structured, Mapping) and structured.get("status") in {401, 403}:
                raise GitHubMCPError("permission_denied", "GitHub MCP 无权读取仓库")
            texts = "\n".join(str(getattr(block, "text", "")) for block in getattr(result, "content", ()) or ())
            try:
                failure = json.loads(texts)
            except (TypeError, ValueError):
                failure = None
            if isinstance(failure, Mapping) and failure.get("status") in {401, 403}:
                raise GitHubMCPError("permission_denied", "GitHub MCP 无权读取仓库")
            raise GitHubMCPError("remote_error", "GitHub MCP 只读工具返回错误")
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            if len(json.dumps(structured, ensure_ascii=False, default=str).encode("utf-8")) > self.config.max_response_bytes * 16:
                raise GitHubMCPError("response_too_large", "GitHub MCP 响应超过传输解析上限")
            return structured
        blocks = getattr(result, "content", None) or []
        texts = [getattr(block, "text", "") for block in blocks if getattr(block, "type", "text") == "text"]
        raw = "\n".join(texts)
        if len(raw.encode("utf-8")) > self.config.max_response_bytes * 16:
            raise GitHubMCPError("response_too_large", "GitHub MCP 响应超过传输解析上限")
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"content": raw}

    def _verify_payload(self, payload: Any, kind: str, request: GitHubReadRequest | None = None):
        if not isinstance(payload, Mapping):
            raise GitHubMCPError("schema_error", "GitHub MCP 响应必须是对象")
        if payload.get("repo_id", self.config.repo_id) != self.config.repo_id:
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 响应仓库与 Run 快照不一致")
        if payload.get("ref", self.config.commit) != self.config.commit:
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 响应 ref 与 Run 快照不一致")
        if payload.get("commit_sha", self.config.commit) != self.config.commit:
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 响应 commit 与 Run 快照不一致")
        urls = [payload.get(key) for key in ("html_url", "url", "download_url") if payload.get(key)]
        paths = [self._bound_url(value) for value in urls]
        if not paths:
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 响应缺少可验证仓库来源")
        if kind == "commit":
            if str(payload.get("sha") or payload.get("oid") or "").lower() != self.config.commit:
                raise GitHubMCPError("binding_mismatch", "GitHub MCP 响应 commit 与 Run 快照不一致")
            if not any(parts[:2] == ["commit", self.config.commit] or parts[:2] == ["commits", self.config.commit] for parts in paths):
                raise GitHubMCPError("binding_mismatch", "GitHub MCP 响应 commit 来源不一致")
        if kind == "file":
            if request is None or payload.get("path") != request.path:
                raise GitHubMCPError("binding_mismatch", "GitHub MCP 文件响应路径不一致")
            if not any(parts[:2] == ["blob", self.config.commit] and "/".join(parts[2:]) == request.path
                       or parts[:1] == [self.config.commit] and "/".join(parts[1:]) == request.path for parts in paths):
                raise GitHubMCPError("binding_mismatch", "GitHub MCP 文件响应 commit 与 Run 快照不一致")
        if kind in _ISSUE_KINDS:
            category = "issues" if kind == "issue" else "pull"
            if request is None or payload.get("number") != request.number or not any(parts[:2] == [category, str(request.number)] for parts in paths):
                raise GitHubMCPError("binding_mismatch", "GitHub MCP Issue/PR 响应编号不一致")

    def _bound_url(self, value: Any) -> list[str]:
        if not isinstance(value, str):
            raise GitHubMCPError("schema_error", "GitHub MCP 来源 URL 无效")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 来源 URL 无效") from None
        if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 来源 URL 无效")
        parts = [unquote(item) for item in parsed.path.strip("/").split("/")]
        if parsed.hostname == "api.github.com" and parts[:1] == ["repos"]:
            parts = parts[1:]
        elif parsed.hostname not in {"github.com", "raw.githubusercontent.com"}:
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 来源不是批准的 GitHub 地址")
        if len(parts) < 2 or parts[0].casefold() != self.config.owner.casefold() or parts[1].casefold() != self.config.repo.casefold():
            raise GitHubMCPError("binding_mismatch", "GitHub MCP 来源仓库与 Run 快照不一致")
        return parts[2:]

    def _redact(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {self._redact(str(key)): self._redact(item) for key, item in redact(value).items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return redact(value)
        return value

    @staticmethod
    def _content(payload: Any, request: GitHubReadRequest) -> tuple[str, str, str | None]:
        if isinstance(payload, Mapping):
            source_id = str(payload.get("path") or payload.get("number") or payload.get("issue_number")
                            or payload.get("pullNumber") or request.path or request.number or request.source_kind)
            source_url = next((str(payload[key]) for key in ("html_url", "url", "download_url") if payload.get(key)), None)
            if request.source_kind in _ISSUE_KINDS:
                content = {key: payload.get(key) for key in ("title", "body")}
                return json.dumps(content, ensure_ascii=False, sort_keys=True), source_id, source_url
            for key in ("content", "body", "text", "description", "message"):
                if isinstance(payload.get(key), str):
                    if key == "content" and payload.get("encoding") == "base64":
                        try:
                            return base64.b64decode(payload[key]).decode("utf-8"), source_id, source_url
                        except (ValueError, UnicodeDecodeError):
                            raise GitHubMCPError("schema_error", "GitHub MCP 文件内容编码无效") from None
                    return payload[key], source_id, source_url
        source_id = str(request.path or request.number or request.source_kind)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), source_id, None


class _DiscardMCPLogs(logging.Filter):
    def filter(self, record):
        return False


def _has_schema_reference(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(key in {"$ref", "$dynamicRef", "$recursiveRef"} or _has_schema_reference(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_has_schema_reference(item) for item in value)
    return False


def _glob(path: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(path, pattern)


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", errors="ignore"), True
