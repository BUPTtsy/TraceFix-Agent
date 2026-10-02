"""Run the opt-in real-model, Docker and Playwright MCP acceptance flow.

This checker never substitutes Fake components.  Without the required real
environment it reports SKIPPED locally or FAILED in required CI mode.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
REQUIRED_VALIDATIONS = {"static", "unit", "build", "health", "original", "regression"}
DEFAULT_POSTGRES_PASSWORD = "tracefix"


def safe_output(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+|api[_-]?key\s*[=:]\s*)[^\s,]+", r"\1[REDACTED]", value)
    return value[-2500:]


def load_local_env():
    """Read simple KEY=VALUE entries without overriding CI-injected values."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip("'\"")


def command(argv, *, env=None, check=True, timeout=1800):
    result = subprocess.run(
        [str(item) for item in argv], cwd=ROOT, env=env, text=True,
        encoding="utf-8", errors="replace", capture_output=True, timeout=timeout,
    )
    if check and result.returncode:
        detail = safe_output((result.stdout or "") + "\n" + (result.stderr or ""))
        raise RuntimeError(f"命令失败（退出码 {result.returncode}）：{argv[0]}\n{detail}")
    return result


def postgres_connection_config() -> tuple[str, str]:
    password = os.getenv("POSTGRES_PASSWORD") or DEFAULT_POSTGRES_PASSWORD
    if "\r" in password or "\n" in password:
        raise ValueError("POSTGRES_PASSWORD 不能包含换行符")
    dsn = f"postgresql://tracefix:{quote(password, safe='')}@127.0.0.1:55432/tracefix"
    compose_env = f"POSTGRES_PASSWORD={password}\n"
    return dsn, compose_env


def prerequisites() -> list[str]:
    missing = []
    if not os.getenv("TRACEFIX_API_KEY"):
        missing.append("TRACEFIX_API_KEY")
    if not os.getenv("TRACEFIX_BASE_URL"):
        missing.append("TRACEFIX_BASE_URL")
    if not os.getenv("TRACEFIX_TEXT_MODEL"):
        missing.append("TRACEFIX_TEXT_MODEL")
    docker = shutil.which("docker")
    if not docker:
        missing.append("docker 命令")
    else:
        info = command([docker, "info", "--format", "{{.OSType}}"], check=False, timeout=30)
        if info.returncode or info.stdout.strip() != "linux":
            detail = safe_output((info.stderr or info.stdout).strip()).replace("\n", " ")
            missing.append("Docker Linux 引擎（docker info 未就绪" + (f"：{detail}" if detail else "") + "）")
        compose = command([docker, "compose", "version", "--short"], check=False, timeout=30)
        if compose.returncode:
            missing.append("Docker Compose v2")
    return missing


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_state(data_root: Path, dsn: str, run_id: str):
    sys.path.insert(0, str(ROOT / "backend/packages/agent/src"))
    from tracefix.storage.store import PostgresStore

    store = PostgresStore(dsn)
    try:
        return store.load(run_id, "bugboard")
    finally:
        store.close()


def run_flow(args, output_path: Path) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = ROOT / ".tracefix" / "real-e2e" / stamp
    data_root = run_root / "data"
    demo_root = run_root / "bugboard-target"
    registry = run_root / "projects.json"
    compose_env = run_root / "compose.env"
    dsn, compose_env_contents = postgres_connection_config()
    python = str(PYTHON)
    docker = shutil.which("docker") or "docker"
    env = os.environ.copy()
    env.update({
        "TRACEFIX_REAL_E2E": "1",
        "TRACEFIX_DATABASE_URL": dsn,
        "TRACEFIX_DATA": str(data_root),
        "TRACEFIX_PROJECTS": str(registry),
        "TRACEFIX_TOOL_MODE": "native",
    })
    result = {"status": "failed", "started_at": stamp, "case": args.case,
              "run_root": str(run_root.relative_to(ROOT))}
    compose_started = False
    try:
        command([python, "bugboard/scripts/init_demo.py", "--case", args.case,
                 "--destination", demo_root], env=env)
        registry.write_text(json.dumps({"projects": [{
            "id": "bugboard", "repo_id": "bugboard-real-e2e", "root": str(demo_root),
            "allowed_files": ["src/**", "server/**"], "memory_revision": 1,
            "access_epoch": 1, "agent_instructions": "AGENTS.md",
        }]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        compose_env.write_text(compose_env_contents, encoding="utf-8")
        existing_postgres = command([docker, "compose", "--env-file", compose_env,
                                     "ps", "--status", "running", "--quiet", "postgres"], timeout=30)
        command([docker, "compose", "--env-file", compose_env, "up", "-d", "--wait", "postgres"], timeout=600)
        compose_started = not bool(existing_postgres.stdout.strip())
        images = {
            "tracefix-bugboard:1.0": [docker, "build", "-f", "bugboard/docker/Dockerfile",
                                      "-t", "tracefix-bugboard:1.0", "bugboard/target"],
            "tracefix-browser:1.0": [docker, "build", "-f", "Dockerfile.browser",
                                      "-t", "tracefix-browser:1.0", "."],
        }
        for image, build in images.items():
            inspected = command([docker, "image", "inspect", image], check=False, timeout=60)
            if inspected.returncode:
                command(build, env=env, timeout=3600)
        common = [python, "tools/bootstrap/launch.py", "--plain", "--project", "bugboard",
                  "--projects", registry, "--profile", ROOT / "profiles/bugboard.yaml",
                  "--data", data_root, "--spec", ROOT / "profiles/persistence.spec.json"]
        agent_result = command([*common, "--run", "--goal",
                 "把 Write project brief 标记为完成，刷新页面并验证状态持久化；失败则修复。",
                 "--mode", "repair", "--execution-mode", "batch"], env=env, check=False, timeout=3600)
        artifact_root = data_root / "artifacts" / "bugboard"
        run_dirs = sorted((item for item in artifact_root.glob("run_*") if item.is_dir()),
                          key=lambda item: item.stat().st_mtime)
        if not run_dirs:
            detail = safe_output((agent_result.stdout or "") + "\n" + (agent_result.stderr or ""))
            raise RuntimeError(f"真实 E2E 未生成 Run 证据目录（Agent 退出码 {agent_result.returncode}）：{detail}")
        run_id = run_dirs[-1].name
        state = load_state(data_root, dsn, run_id)
        sys.path.insert(0, str(ROOT / "backend/packages/agent/src"))
        from tracefix.storage.artifacts import Artifacts

        artifacts = Artifacts(data_root / "artifacts")
        report = artifacts.json("bugboard", run_id, state.report_ref) if state.report_ref else {}
        result.update({"run_id": run_id, "run_status": str(state.run_status),
                       "outcome": str(state.outcome) if state.outcome else None,
                       "agent_exit_code": agent_result.returncode,
                       "report_ref": state.report_ref, "report_summary": report.get("result_summary"),
                       "patch_diff_ref": report.get("patch_diff_ref"),
                       "patch_available": report.get("patch_available", False),
                       "patch_verification": report.get("patch_verification", "none"),
                       "error_details": report.get("error_details")})
        if str(state.run_status) != "COMPLETED" or str(state.outcome) != "FIX_VERIFIED":
            raise RuntimeError(f"真实 E2E 未完成确定性验收：status={state.run_status}, outcome={state.outcome}；原因={state.error or report.get('result_summary') or '未生成最终报告'}")
        if agent_result.returncode:
            raise RuntimeError(f"Agent 已生成结果，但进程退出码异常：{agent_result.returncode}")
        if not report.get("patch_available") or report.get("patch_verification") != "verified":
            raise RuntimeError("真实 E2E 未导出已通过验证的有效补丁")
        if not state.validation_refs:
            raise RuntimeError("真实 E2E 未生成验证引用")
        kinds = set()
        for reference in state.validation_refs:
            validation = artifacts.json("bugboard", run_id, reference)
            if validation.get("passed"):
                kinds.add(validation.get("kind"))
        if REQUIRED_VALIDATIONS - kinds:
            raise RuntimeError(f"真实 E2E 验证项不完整：缺少 {sorted(REQUIRED_VALIDATIONS - kinds)}")
        result.update({"status": "passed", "run_id": run_id, "outcome": str(state.outcome),
                       "validation_kinds": sorted(kinds), "report_ref": state.report_ref,
                       "report_summary": report.get("result_summary")})
        return 0
    except Exception as error:
        result["error"] = safe_output(str(error))
        raise
    finally:
        if compose_started:
            command([docker, "compose", "--env-file", compose_env, "down"], check=False, timeout=300)
        write_json(output_path, result)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TraceFix 真实模型 + Docker + MCP E2E 验收")
    parser.add_argument("--case", default="B01", choices=[f"B{i:02d}" for i in range(1, 13)])
    parser.add_argument("--required", action="store_true", help="缺少真实环境时以失败退出（CI 使用）")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/real-e2e/report.json")
    args = parser.parse_args(argv)
    load_local_env()
    missing = prerequisites()
    if missing:
        status = "failed" if args.required else "skipped"
        report = {"status": status, "case": args.case, "missing": missing,
                  "message": "未使用 Fake 替代真实环境；请补齐前置条件后重试。"}
        write_json(args.output, report)
        print(f"REAL_E2E: {status.upper()} — " + "；".join(missing))
        return 2 if args.required else 0
    print("REAL_E2E: RUNNING — 使用真实模型、Docker 应用和 Playwright MCP")
    return run_flow(args, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
