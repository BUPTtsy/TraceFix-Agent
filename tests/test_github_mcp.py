import asyncio
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent, Tool

import tracefix.connectors.github_mcp as github
from tracefix.connectors import (
    GitHubMCPConfig,
    GitHubMCPConnector,
    GitHubMCPError,
    GitHubReadRequest,
)
from tracefix.knowledge.assembler import ContextAssembler, TokenCounter
from tracefix.runtime.contracts import digest
from tracefix.storage.artifacts import Artifacts


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
SECRET = "deployment-private-token-12345"


def config(**fields):
    return GitHubMCPConfig(
        command=("configured-github-mcp",), repo_id="alpha", owner="owner", repo="app",
        ref="main", commit=COMMIT, credential_ref="deployment/alpha",
        allowed_files=("src/**",), **fields,
    )


def tool(name, properties, required=None, **extra):
    return Tool(name=name, inputSchema={
        "type": "object", "properties": properties,
        "required": required or list(properties), "additionalProperties": False,
    }, **extra)


def tools():
    repository = {"owner": {"type": "string"}, "repo": {"type": "string"}}
    return [
        tool("get_commit", {**repository, "sha": {"type": "string"}}),
        tool("get_file_contents", {**repository, "path": {"type": "string"}, "ref": {"type": "string"}}),
        tool("get_repository", repository),
        tool("issue_read", {**repository, "issue_number": {"type": "integer"},
                            "method": {"type": "string", "enum": ["get", "get_comments"]}}),
        tool("pull_request_read", {**repository, "pullNumber": {"type": "integer"},
                                   "method": {"type": "string", "enum": ["get", "get_diff"]}}),
        tool("create_pull_request", repository),
        tool("push_files", repository, annotations={"readOnlyHint": True}),
    ]


def response(payload, *, error=False):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))], isError=error)


def frozen_snapshot():
    repository = {
        "repo_id": "alpha", "projectId": "alpha", "commit": COMMIT, "owner": "owner",
        "repo": "app", "default_ref": "main", "credential_ref": "deployment/alpha",
        "allowed_files": ["src/**"],
    }
    repository["config_digest"] = digest(repository)
    repository["frozen_run_id"] = "run_alpha"
    repository["snapshot_digest"] = digest(repository)
    return {"repo_id": "alpha", "projectId": "alpha", "commit": COMMIT,
            "config_digest": repository["config_digest"], "repository_config": repository}


class MockSession:
    def __init__(self, *streams):
        self.calls = []
        self.startup = []
        self.listing = tools()
        self.hook = None
        self.active = 0
        self.max_active = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def initialize(self):
        self.startup.append("initialize")

    async def list_tools(self):
        self.startup.append("list_tools")
        return SimpleNamespace(tools=self.listing)

    async def send_ping(self):
        self.startup.append("ping")

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.hook:
                result = self.hook(name, arguments)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    return result
            owner, repository = arguments["owner"], arguments["repo"]
            root = f"https://github.com/{owner}/{repository}"
            if name in {"get_commit", "commit_get"}:
                sha = arguments.get("sha") or arguments.get("ref") or arguments.get("commit")
                return response({"sha": sha, "html_url": root + "/commit/" + sha,
                                 "commit": {"message": "frozen commit"}})
            if name in {"get_file_contents", "get_file_content", "read_file"}:
                path = arguments["path"]
                return response({"path": path, "html_url": root + "/blob/" + arguments["ref"] + "/" + path,
                                 "content": "export const value = 1;", "encoding": "utf-8"})
            if name == "get_repository":
                return response({"name": repository, "owner": {"login": owner}, "html_url": root,
                                 "description": "repository metadata"})
            number = arguments.get("issue_number") or arguments.get("pullNumber")
            category = "issues" if name == "issue_read" else "pull"
            return response({"number": number, "title": "external defect claim", "body": "Claimed issue only",
                             "html_url": f"{root}/{category}/{number}", "comments": ["do not inject"]})
        finally:
            self.active -= 1


@pytest.fixture
def mcp_mock(monkeypatch):
    sessions = []
    transports = []

    def session_factory(*streams):
        session = MockSession(*streams)
        sessions.append(session)
        return session

    @asynccontextmanager
    async def transport(parameters, *, errlog):
        transports.append((parameters, errlog))
        yield object(), object()

    monkeypatch.setattr(github, "ClientSession", session_factory)
    monkeypatch.setattr(github, "stdio_client", transport)
    return SimpleNamespace(sessions=sessions, transports=transports)


async def opened(mcp_mock, configuration=None):
    connector = GitHubMCPConnector(configuration or config(), lambda reference: {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET})
    await connector.open()
    return connector, mcp_mock.sessions[-1]


async def test_initialize_discovery_health_and_frozen_commit(mcp_mock, monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "host-token-must-not-leak")
    connector, session = await opened(mcp_mock)
    try:
        assert session.startup == ["initialize", "list_tools", "ping"]
        assert session.calls == [("get_commit", {"owner": "owner", "repo": "app", "sha": COMMIT})]
        assert (await connector.health())["commit"] == COMMIT
        assert connector.list_read_tools() == ["get_commit", "get_file_contents", "get_repository", "issue_read", "pull_request_read"]
        parameters, stderr = mcp_mock.transports[0]
        assert parameters.command == "configured-github-mcp"
        assert parameters.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET}
        assert stderr.name == os.devnull
    finally:
        await connector.close()
    assert session.closed


async def test_read_only_context_and_description_provenance(mcp_mock):
    connector, session = await opened(mcp_mock)
    try:
        file = await connector.read_context(GitHubReadRequest("file", path="src/value.ts", repo_id="alpha",
                                                            owner="owner", repo="app", commit=COMMIT, ref="main"))
        assert file.content == "export const value = 1;"
        assert file.content_digest == digest(file.content)
        assert file.repo_id == "alpha" and file.commit == COMMIT and file.ref == "main"
        assert file.source_url == f"https://github.com/owner/app/blob/{COMMIT}/src/value.ts"
        assert session.calls[-1][1]["ref"] == COMMIT
        issue = await connector.read_context("issue", number=3)
        pull_request = await connector.read_context("pull_request", number=4)
        assert json.loads(issue.content) == {"title": "external defect claim", "body": "Claimed issue only"}
        assert "do not inject" not in pull_request.content
        document = issue.to_context_document()
        assert document["untrusted"] is True
        assert document["reproduction_status"] == "not_evidence_of_reproduction"
        assert document["source_refs"] == ["https://github.com/owner/app/issues/3"]
        assert document["provenance"]["source_kind"] == "issue"
        assert document["provenance"]["retrieved_at"].endswith("+00:00")
    finally:
        await connector.close()


@pytest.mark.parametrize("source_kind", ["push_files", "create_pull_request", "comment", "merge", "get_comments"])
async def test_reject_write_and_unknown_operations(mcp_mock, source_kind):
    connector, session = await opened(mcp_mock)
    try:
        count = len(session.calls)
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context(source_kind)
        assert failure.value.code == "invalid_request"
        assert len(session.calls) == count
    finally:
        await connector.close()


@pytest.mark.parametrize("fields", [
    {"owner": "other"}, {"repo": "another"}, {"repo_id": "beta"}, {"commit": OTHER_COMMIT}, {"ref": "other"},
])
async def test_cross_repository_and_ref_override_are_rejected(mcp_mock, fields):
    connector, session = await opened(mcp_mock)
    try:
        count = len(session.calls)
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context(GitHubReadRequest("file", path="src/value.ts", **fields))
        assert failure.value.code == "binding_mismatch"
        assert len(session.calls) == count
    finally:
        await connector.close()


@pytest.mark.parametrize("path", ["../src/value.ts", "/src/value.ts", "C:/value.ts", "src\\value.ts", "server/private.ts"])
async def test_unauthorized_paths_are_rejected_before_tool_call(mcp_mock, path):
    connector, session = await opened(mcp_mock)
    try:
        count = len(session.calls)
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("file", path=path)
        assert failure.value.code == "path_denied"
        assert len(session.calls) == count
    finally:
        await connector.close()


@pytest.mark.parametrize("kind", ["issue", "pull_request"])
def test_issue_and_pr_need_explicit_positive_number_and_get(kind):
    with pytest.raises(ValueError):
        GitHubReadRequest(kind)
    with pytest.raises(ValueError):
        GitHubReadRequest(kind, number=2, method="get_comments")


async def test_binding_mismatch_cannot_be_hidden_in_untrusted_body(mcp_mock):
    connector, session = await opened(mcp_mock)
    try:
        session.hook = lambda name, arguments: response({
            "path": "src/value.ts", "content": "owner app " + COMMIT,
            "html_url": f"https://github.com/other/repository/blob/{COMMIT}/src/value.ts",
        }) if name == "get_file_contents" else None
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("file", path="src/value.ts")
        assert failure.value.code == "binding_mismatch"
    finally:
        await connector.close()


@pytest.mark.parametrize("payload", [
    {"sha": OTHER_COMMIT, "html_url": f"https://github.com/owner/app/commit/{OTHER_COMMIT}"},
    {"sha": COMMIT},
    {"sha": COMMIT, "html_url": f"https://example.test/owner/app/commit/{COMMIT}"},
])
async def test_health_rejects_missing_or_wrong_server_binding(mcp_mock, payload):
    connector, session = await opened(mcp_mock)
    try:
        session.hook = lambda name, arguments: response(payload)
        with pytest.raises(GitHubMCPError) as failure:
            await connector.health()
        assert failure.value.code == "binding_mismatch"
    finally:
        await connector.close()


async def test_schema_validation_and_actual_discovered_name(mcp_mock):
    connector, session = await opened(mcp_mock)
    try:
        session.listing = tools()
        session.listing[0].name = "commit_get"
        session.listing[1].name = "read_file"
        connector._discover(SimpleNamespace(tools=session.listing))
        await connector.read_context("file", path="src/value.ts")
        assert [name for name, arguments in session.calls][-2:] == ["commit_get", "read_file"]
        connector._tools["read_file"]["required"].append("unexpected_required")
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("file", path="src/value.ts")
        assert failure.value.code == "schema_error"
    finally:
        await connector.close()


@pytest.mark.parametrize("schema", [{"type": 7}, {"type": "object", "properties": {"path": {"type": "string"}}}])
async def test_discovery_rejects_invalid_schema(mcp_mock, schema):
    connector, session = await opened(mcp_mock)
    try:
        with pytest.raises(GitHubMCPError) as failure:
            connector._discover(SimpleNamespace(tools=[SimpleNamespace(name="get_commit", inputSchema=schema)]))
        assert failure.value.code == "schema_error"
    finally:
        await connector.close()


async def test_timeout_and_disconnect_are_stable_and_not_retried(mcp_mock):
    connector, session = await opened(mcp_mock, config(timeout_seconds=0.1))
    async def timeout(name, arguments):
        await asyncio.sleep(1)
    session.hook = timeout
    try:
        count = len(session.calls)
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("commit")
        assert failure.value.code == "timeout"
        assert len(session.calls) == count + 1
        with pytest.raises(GitHubMCPError):
            await connector.read_context("commit")
        assert len(session.calls) == count + 1
    finally:
        await connector.close()
    connector, session = await opened(mcp_mock)
    def disconnected(name, arguments):
        raise ConnectionError("disconnected " + SECRET)
    session.hook = disconnected
    try:
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("commit")
        assert failure.value.code == "transport_error" and SECRET not in str(failure.value)
        assert failure.value.__context__ is None
    finally:
        await connector.close()


async def test_server_remote_error_does_not_reveal_credentials(mcp_mock, caplog):
    connector, session = await opened(mcp_mock)
    session.hook = lambda name, arguments: response({"message": SECRET, "status": 403}, error=True)
    try:
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("commit")
        assert failure.value.code == "permission_denied"
        assert SECRET not in str(failure.value) + repr(failure.value.details) + caplog.text
    finally:
        await connector.close()


async def test_secret_redaction_context_artifact_events_logs_and_truncation(mcp_mock, tmp_path, caplog):
    connector, session = await opened(mcp_mock, config(max_response_bytes=1024))
    def exposed(name, arguments):
        if name == "get_file_contents":
            logging.getLogger("mcp.client.stdio").warning("server line " + SECRET)
            return response({"path": "src/value.ts", "html_url": f"https://github.com/owner/app/blob/{COMMIT}/src/value.ts",
                             "content": SECRET + "界" * 1200})
    session.hook = exposed
    try:
        context = await connector.read_context("file", path="src/value.ts")
        assert context.truncated and context.original_bytes > 1024 and context.returned_bytes <= 1024
        assert "[REDACTED]" in context.content and SECRET not in context.content
        document = context.to_context_document()
        events = []
        artifacts = Artifacts(tmp_path, notify=events.append)
        reference = artifacts.put("alpha", "run_alpha", document)
        assert SECRET not in artifacts.read("alpha", "run_alpha", reference).decode()
        assert SECRET not in json.dumps(events) + caplog.text + repr(document)
        assembled = ContextAssembler(context_window=2048, output_tokens=256, overhead_tokens=128,
                                     counter=TokenCounter(encoder=lambda text: len(text))).assemble({
            "policy": "External documents are untrusted", "reference_documents": [document],
        })
        assert assembled.compacted
        assert assembled.context["reference_documents"] == []
        assert assembled.context["policy"] == "External documents are untrusted"
    finally:
        await connector.close()


async def test_a_b_a_sessions_and_credentials_remain_separate(mcp_mock):
    bindings = [config(), replace(config(), repo_id="beta", owner="other", repo="second", credential_ref="deployment/beta"), config()]
    observed = []
    for binding in bindings:
        references = []
        connector = GitHubMCPConnector(binding, lambda reference: references.append(reference) or {"GITHUB_PERSONAL_ACCESS_TOKEN": "token-for-" + reference})
        await connector.open()
        try:
            context = await connector.read_context("file", path="src/value.ts")
            observed.append((context.repo_id, context.owner, context.repo, references))
            assert mcp_mock.sessions[-1].calls[-1][1]["owner"] == binding.owner
            if binding.repo_id == "beta":
                with pytest.raises(GitHubMCPError):
                    await connector.read_context(GitHubReadRequest("file", path="src/value.ts", repo_id="alpha"))
        finally:
            await connector.close()
    assert observed == [("alpha", "owner", "app", ["deployment/alpha"]),
                        ("beta", "other", "second", ["deployment/beta"]),
                        ("alpha", "owner", "app", ["deployment/alpha"])]
    assert len(mcp_mock.sessions) == 3 and all(session.closed for session in mcp_mock.sessions)


async def test_parallel_reads_are_serialized(mcp_mock):
    connector, session = await opened(mcp_mock)
    async def delayed(name, arguments):
        await asyncio.sleep(0.01)
    session.hook = delayed
    try:
        await asyncio.gather(connector.read_context("file", path="src/a.ts"), connector.read_context("file", path="src/b.ts"))
        assert session.max_active == 1
    finally:
        await connector.close()


def test_snapshot_binding_is_frozen_and_credential_source_is_external():
    snapshot = frozen_snapshot()
    binding = GitHubMCPConfig.from_snapshot(snapshot, command=["mcp-server"])
    snapshot["repository_config"]["owner"] = "changed"
    snapshot["repository_config"]["allowed_files"].append("**")
    assert binding.owner == "owner" and binding.allowed_files == ("src/**",)
    with pytest.raises(ValueError):
        replace(binding, commit="HEAD")
    with pytest.raises(ValueError):
        replace(binding, env_keys=("GITHUB_TOKEN",))


async def test_base64_file_credentials_are_redacted_after_decoding(mcp_mock):
    connector, session = await opened(mcp_mock)
    session.hook = lambda name, arguments: response({
        "path": "src/value.ts", "html_url": f"https://github.com/owner/app/blob/{COMMIT}/src/value.ts",
        "encoding": "base64", "content": base64.b64encode(SECRET.encode()).decode(),
    }) if name == "get_file_contents" else None
    try:
        context = await connector.read_context("file", path="src/value.ts")
        assert context.content == "[REDACTED]"
        assert SECRET not in repr(context.to_context_document())
    finally:
        await connector.close()


async def test_malformed_source_url_returns_stable_error(mcp_mock):
    connector, session = await opened(mcp_mock)
    session.hook = lambda name, arguments: response({
        "sha": COMMIT, "html_url": f"https://github.com:{SECRET}/owner/app/commit/{COMMIT}",
    })
    try:
        with pytest.raises(GitHubMCPError) as failure:
            await connector.health()
        assert failure.value.code == "binding_mismatch"
        assert SECRET not in str(failure.value) + repr(failure.value.details)
    finally:
        await connector.close()


async def test_pull_request_accepts_bound_github_api_url(mcp_mock):
    connector, session = await opened(mcp_mock)
    session.hook = lambda name, arguments: response({
        "number": 3, "url": "https://api.github.com/repos/owner/app/pulls/3",
        "body": "explicit caller description", "title": "PR",
    }) if name == "pull_request_read" else None
    try:
        context = await connector.read_context("pull_request", number=3)
        assert context.source_url == "https://api.github.com/repos/owner/app/pulls/3"
    finally:
        await connector.close()


async def test_exact_file_pattern_does_not_expand_to_children(mcp_mock):
    connector, session = await opened(mcp_mock, replace(config(), allowed_files=("src/value.ts",)))
    try:
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("file", path="src/value.ts/child")
        assert failure.value.code == "path_denied"
    finally:
        await connector.close()


async def test_missing_metadata_capability_does_not_fabricate_context(mcp_mock):
    connector, session = await opened(mcp_mock)
    connector._semantic.pop("metadata")
    try:
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context("metadata")
        assert failure.value.code == "tool_unavailable"
    finally:
        await connector.close()


@pytest.mark.parametrize("read_request", [None, {}, [], 42])
async def test_invalid_requests_return_structured_error(mcp_mock, read_request):
    connector, session = await opened(mcp_mock)
    try:
        with pytest.raises(GitHubMCPError) as failure:
            await connector.read_context(read_request)
        assert failure.value.code == "invalid_request"
    finally:
        await connector.close()


@pytest.mark.parametrize("field,value", [
    ("repo_id", "beta"), ("projectId", "beta"), ("owner", "other"), ("repo", "other"),
    ("allowed_files", ["**"]), ("credential_ref", "deployment/beta"),
    ("commit", OTHER_COMMIT), ("config_digest", "tampered"), ("snapshot_digest", "tampered"),
])
def test_snapshot_rejects_binding_or_digest_tampering(field, value):
    snapshot = frozen_snapshot()
    snapshot["repository_config"][field] = value
    with pytest.raises(ValueError):
        GitHubMCPConfig.from_snapshot(snapshot, command=["server"])


@pytest.mark.parametrize("field,value", [("repo_id", "beta"), ("projectId", "beta"),
                                         ("owner", "other"), ("allowed_files", ["**"])])
def test_snapshot_rejects_outer_binding_conflict(field, value):
    snapshot = frozen_snapshot()
    snapshot[field] = value
    with pytest.raises(ValueError):
        GitHubMCPConfig.from_snapshot(snapshot, command=["server"])


async def test_discovery_rejects_remote_schema_reference(mcp_mock):
    connector, session = await opened(mcp_mock)
    try:
        schema = {"type": "object", "properties": {"owner": {"$ref": "http://127.0.0.1/private"}}}
        with pytest.raises(GitHubMCPError) as failure:
            connector._discover(SimpleNamespace(tools=[SimpleNamespace(name="get_commit", inputSchema=schema)]))
        assert failure.value.code == "schema_error"
    finally:
        await connector.close()


async def test_unverified_source_url_does_not_override_provenance(mcp_mock):
    connector, session = await opened(mcp_mock)
    session.hook = lambda name, arguments: response({
        "path": "src/value.ts", "url": f"https://github.com/owner/app/blob/{COMMIT}/src/value.ts",
        "source_url": "https://example.test/fabricated", "content": "value",
    }) if name == "get_file_contents" else None
    try:
        context = await connector.read_context("file", path="src/value.ts")
        assert context.source_url == f"https://github.com/owner/app/blob/{COMMIT}/src/value.ts"
    finally:
        await connector.close()


async def test_async_credential_resolver_has_timeout(mcp_mock):
    async def hanging(reference):
        await asyncio.sleep(10)
    connector = GitHubMCPConnector(config(timeout_seconds=0.1), hanging)
    with pytest.raises(GitHubMCPError) as failure:
        await asyncio.wait_for(connector.open(), 0.5)
    assert failure.value.code == "timeout"
    assert not mcp_mock.transports


async def test_credential_resolver_error_is_sanitized(mcp_mock):
    def unavailable(reference):
        raise RuntimeError(SECRET)
    connector = GitHubMCPConnector(config(), unavailable)
    with pytest.raises(GitHubMCPError) as failure:
        await connector.open()
    assert failure.value.code == "credential_unavailable" and SECRET not in str(failure.value)
    assert failure.value.__suppress_context__ is True
    assert not mcp_mock.transports


async def test_real_github_mcp_read_only_when_explicitly_configured():
    deployment_file = os.environ.get("TRACEFIX_GITHUB_MCP_ACCEPTANCE_CONFIG")
    if not deployment_file:
        pytest.skip("未配置独立 GitHub MCP 验收 command/repo/commit/credential_ref")
    with open(deployment_file, encoding="utf-8") as handle:
        deployment = json.load(handle)
    secret_env = deployment.pop("acceptance_credential_env", None)
    if not secret_env or not os.environ.get(secret_env):
        pytest.skip("GitHub MCP 验收 credential_ref 的部署解析环境未提供")
    binding = GitHubMCPConfig(**deployment)
    expected_ref = binding.credential_ref
    def resolver(reference):
        assert reference == expected_ref
        return {"GITHUB_PERSONAL_ACCESS_TOKEN": os.environ[secret_env]}
    async with GitHubMCPConnector(binding, resolver) as connector:
        context = await connector.read_context("commit")
        assert context.commit == binding.commit and context.source_url
