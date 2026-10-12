"""部署官方 Docker stdio GitHub MCP，并分别验收协议与真实读取。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/packages/agent/src"))

from dotenv import dotenv_values
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tracefix.connectors.github_mcp import GitHubMCPConfig, GitHubMCPConnector, GitHubMCPError


TEMPLATE = ROOT / "profiles/github-mcp.deployment.json"
DEFAULT_CONFIG = ROOT / ".tracefix/github-mcp/acceptance.json"
DEFAULT_REPORT = ROOT / ".tracefix/github-mcp/deployment-report.json"
READ_TOOLS = {"get_commit", "get_file_contents", "issue_read", "pull_request_read"}


def command(arguments):
    result = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    if result.returncode:
        raise RuntimeError(f"部署命令失败：{arguments[0]}（退出码 {result.returncode}）")
    return result.stdout.strip()


def local_binding():
    remote = command(["git", "remote", "get-url", "origin"])
    if remote.startswith("git@github.com:"):
        repository = remote.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(remote)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
            raise ValueError("验收仓库 origin 必须是 github.com HTTPS 或 SSH 地址")
        repository = parsed.path.lstrip("/")
    repository = repository.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("验收仓库地址无效")
    owner, repo = repository.split("/")
    commit = command(["git", "rev-parse", "HEAD"])
    return {"repo_id": "tracefix-agent", "owner": owner, "repo": repo,
            "ref": command(["git", "branch", "--show-current"]) or commit,
            "commit": commit, "credential_ref": "github/tracefix-agent",
            "allowed_files": ["README.md", "backend/**", "frontend/**", "tests/**", "profiles/**", "scripts/**"]}


def load_configuration(path):
    deployment = json.loads(path.read_text(encoding="utf-8-sig"))
    secret_env = deployment.pop("acceptance_credential_env", None)
    if not isinstance(secret_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", secret_env):
        raise ValueError("acceptance_credential_env 必须是凭据环境变量名")
    return GitHubMCPConfig(**deployment), secret_env


def credential_resolver(config, secret_env):
    values = dotenv_values(ROOT / ".env", encoding="utf-8-sig", interpolate=False)
    secret = os.getenv(secret_env) or values.get(secret_env)
    if not secret or not secret.strip():
        raise GitHubMCPError("credential_unavailable", f"请在本地 .env 或环境变量配置 {secret_env}")

    def resolve(reference):
        if reference != config.credential_ref:
            raise GitHubMCPError("binding_mismatch", "部署凭据引用与验收仓库不一致")
        return {"GITHUB_PERSONAL_ACCESS_TOKEN": secret}

    return resolve


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def probe_protocol(config):
    environment = {key: os.environ[key] for key in config.env_keys if key in os.environ}
    environment["GITHUB_PERSONAL_ACCESS_TOKEN"] = "protocol-probe-without-api-access"
    parameters = StdioServerParameters(command=config.command[0],
        args=list(config.command[1:] + config.args), env=environment)
    with open(os.devnull, "w", encoding="utf-8") as errors:
        async with asyncio.timeout(config.timeout_seconds):
            async with stdio_client(parameters, errlog=errors) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    listing = await session.list_tools()
                    connector = GitHubMCPConnector(config, lambda reference: {})
                    connector._discover(listing)
                    names = {tool.name for tool in listing.tools}
                    if names != READ_TOOLS or any(not tool.annotations or not tool.annotations.readOnlyHint
                                                  for tool in listing.tools):
                        raise RuntimeError("部署 server 工具集不符合四项只读白名单")
                    await session.send_ping()
                    return {"status": "passed", "server": initialized.serverInfo.name,
                            "version": initialized.serverInfo.version, "tools": sorted(names),
                            "authentication_tested": False}


async def verify_read(config, secret_env):
    resolver = credential_resolver(config, secret_env)
    async with GitHubMCPConnector(config, resolver) as connector:
        health = await connector.health()
        commit = await connector.read_context("commit")
        file = await connector.read_context("file", path="README.md")
        if not file.content.strip():
            raise RuntimeError("真实读取的 README.md 内容为空")
        return {"status": "passed", "health": health, "commit_source": commit.source_url,
                "file_source": file.source_url, "file_digest": file.content_digest,
                "file_bytes": file.returned_bytes, "truncated": file.truncated}


def main(argv=None):
    parser = argparse.ArgumentParser(description="部署与验收只读 GitHub MCP Docker stdio server")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="准备镜像、配置并验收 MCP 协议")
    mode.add_argument("--verify", action="store_true", help="使用本地凭据验收绑定 commit 和 README.md 真实读取")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    report = {"deployment": {"status": "pending"}, "real_read": {"status": "not_run"},
              "runtime_integration": "pending"}
    phase = "deployment"
    try:
        if command(["docker", "info", "--format", "{{.OSType}}"]) != "linux":
            raise RuntimeError("需要 Docker Desktop Linux 容器引擎")
        template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        image = next(value for value in template["args"] if value.startswith("ghcr.io/github/github-mcp-server@sha256:"))
        if not args.config.exists():
            if args.verify:
                raise ValueError("部署配置不存在，请先运行 --prepare")
            write_json(args.config, {**template, **local_binding()})
        config, secret_env = load_configuration(args.config)
        if config.command != tuple(template["command"]) or config.args != tuple(template["args"]):
            raise ValueError("部署配置的启动命令必须与锁定镜像模板一致")
        inspected = subprocess.run(["docker", "image", "inspect", image], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if inspected.returncode:
            command(["docker", "pull", image])
        report["deployment"] = asyncio.run(probe_protocol(config))
        report["deployment"].update({"image": image, "repo": f"{config.owner}/{config.repo}", "commit": config.commit})
        if not args.prepare:
            phase = "real_read"
            report["real_read"] = asyncio.run(verify_read(config, secret_env))
        return 0
    except GitHubMCPError as error:
        report[phase] = {"status": "blocked" if error.code == "credential_unavailable" else "failed",
                         "code": error.code}
        print(f"GitHub MCP {'真实读取' if phase == 'real_read' else '部署'}未完成（{error.code}）。", file=sys.stderr)
        return 2
    except Exception:
        report[phase] = {"status": "failed", "code": f"{phase}_failed"}
        print("GitHub MCP 真实读取未完成，请检查凭据、仓库权限与网络。" if phase == "real_read"
              else "GitHub MCP 部署未完成，请检查 Docker、镜像和部署配置。", file=sys.stderr)
        return 2
    finally:
        write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
