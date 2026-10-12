"""部署阶段、真实验收和凭据边界的回归检查。"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.connectors.github_mcp import GitHubMCPError


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("github_mcp_deployment", ROOT / "scripts/github/deploy_github_mcp.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)
SECRET = "deployment-test-secret-do-not-persist"
BINDING = {
    "repo_id": "tracefix-agent", "owner": "BUPTtsy", "repo": "TraceFix-Agent",
    "ref": "main", "commit": "a" * 40, "credential_ref": "github/tracefix-agent",
    "allowed_files": ["README.md"],
}


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    template = json.loads(deployment.TEMPLATE.read_text(encoding="utf-8"))
    config_path = tmp_path / "acceptance.json"
    report_path = tmp_path / "report.json"
    deployment.write_json(config_path, {**template, **BINDING})
    monkeypatch.setattr(deployment, "command", lambda arguments: "linux")
    monkeypatch.setattr(deployment.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    async def probe(config):
        return {"status": "passed", "authentication_tested": False}
    monkeypatch.setattr(deployment, "probe_protocol", probe)
    return config_path, report_path


def run(prepared, mode):
    config_path, report_path = prepared
    result = deployment.main([mode, "--config", str(config_path), "--report", str(report_path)])
    return result, json.loads(report_path.read_text(encoding="utf-8"))


def test_prepare_does_not_resolve_credentials_or_claim_authentication(prepared, monkeypatch):
    async def unexpected_verify(*args):
        pytest.fail("协议检查不应读取凭据")
    monkeypatch.setattr(deployment, "verify_read", unexpected_verify)
    result, report = run(prepared, "--prepare")
    assert result == 0
    assert report["deployment"]["status"] == "passed"
    assert report["deployment"]["authentication_tested"] is False
    assert report["real_read"] == {"status": "not_run"}


@pytest.mark.parametrize("error,expected_status,expected_code", [
    (GitHubMCPError("credential_unavailable", SECRET), "blocked", "credential_unavailable"),
    (GitHubMCPError("permission_denied", SECRET), "failed", "permission_denied"),
    (ExceptionGroup("remote response", [RuntimeError(SECRET)]), "failed", "real_read_failed"),
])
def test_real_failure_preserves_protocol_result_and_redacts_exception(prepared, monkeypatch, capsys,
                                                                     error, expected_status, expected_code):
    async def failing_verify(*args):
        raise error
    monkeypatch.setattr(deployment, "verify_read", failing_verify)
    result, report = run(prepared, "--verify")
    captured = capsys.readouterr()
    assert result == 2
    assert report["deployment"]["status"] == "passed"
    assert report["real_read"] == {"status": expected_status, "code": expected_code}
    assert SECRET not in captured.out + captured.err + prepared[1].read_text(encoding="utf-8")


def test_protocol_task_group_failure_is_reported_without_traceback(prepared, monkeypatch, capsys):
    async def failing_probe(*args):
        raise ExceptionGroup("initialization", [RuntimeError(SECRET)])
    monkeypatch.setattr(deployment, "probe_protocol", failing_probe)
    result, report = run(prepared, "--prepare")
    captured = capsys.readouterr()
    assert result == 2
    assert report["deployment"] == {"status": "failed", "code": "deployment_failed"}
    assert report["real_read"] == {"status": "not_run"}
    assert SECRET not in captured.out + captured.err


def test_configured_image_drift_is_rejected_before_start(prepared, monkeypatch):
    path, _ = prepared
    original = json.loads(path.read_text(encoding="utf-8"))
    original["args"][original["args"].index("--read-only")] = "--privileged"
    deployment.write_json(path, original)
    async def unexpected_probe(*args):
        pytest.fail("偏离模板的命令不应启动")
    monkeypatch.setattr(deployment, "probe_protocol", unexpected_probe)
    result, report = run(prepared, "--prepare")
    assert result == 2
    assert report["deployment"]["status"] == "failed"
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_missing_credential_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "ROOT", tmp_path)
    monkeypatch.delenv("TRACEFIX_GITHUB_TOKEN", raising=False)
    template = json.loads((ROOT / "profiles/github-mcp.deployment.json").read_text(encoding="utf-8"))
    config_path = tmp_path / "acceptance.json"
    deployment.write_json(config_path, {**template, **BINDING})
    config, secret_env = deployment.load_configuration(config_path)
    with pytest.raises(GitHubMCPError) as failure:
        deployment.credential_resolver(config, secret_env)
    assert failure.value.code == "credential_unavailable"
    literal_secret = "literal-${TRACEFIX_DEPLOYMENT_PLACEHOLDER}"
    (tmp_path / ".env").write_text(f"TRACEFIX_GITHUB_TOKEN={literal_secret}\n", encoding="utf-8")
    monkeypatch.setenv("TRACEFIX_DEPLOYMENT_PLACEHOLDER", "expanded")
    resolver = deployment.credential_resolver(config, secret_env)
    assert resolver(config.credential_ref) == {"GITHUB_PERSONAL_ACCESS_TOKEN": literal_secret}
    monkeypatch.setenv(secret_env, SECRET)
    resolver = deployment.credential_resolver(config, secret_env)
    assert resolver(config.credential_ref) == {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET}
    with pytest.raises(GitHubMCPError) as failure:
        resolver("github/other-repository")
    assert failure.value.code == "binding_mismatch"
    assert SECRET not in config_path.read_text(encoding="utf-8")
