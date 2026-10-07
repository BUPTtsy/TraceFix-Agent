"""Run the opt-in real-model, Docker and Playwright MCP acceptance flow.

This checker never substitutes Fake components.  Without the required real
environment it reports SKIPPED locally or FAILED in required CI mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
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
    if not os.getenv("TRACEFIX_API_KEY", "").strip():
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


def validate_spec(contents: bytes, *, case: str):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend/packages/agent/src"))
    from tracefix.runtime.contracts import TestSpec

    specification = TestSpec.model_validate_json(contents.decode("utf-8-sig"))
    if case == "B01":
        task_name = "Complete Write project brief"

        def task_assertion(assertions, condition):
            return any(assertion.locator.role == "checkbox" and assertion.locator.name == task_name
                       and assertion.condition == condition for assertion in assertions)

        required = [("click", "checkbox", task_name, "checked"),
                    ("navigate", None, None, "checked"),
                    ("click", "checkbox", task_name, "unchecked"),
                    ("navigate", None, None, "unchecked"),
                    ("click", "button", "Todo", "unchecked"),
                    ("click", "button", "Done", "absent")]
        covered = False
        for scenario in specification.behavior_scenarios:
            for offset in range(len(scenario.steps) - len(required) + 1):
                matched = True
                for step, (kind, role, name, condition) in zip(scenario.steps[offset:], required):
                    action = step.action
                    if (action.kind != kind or not task_assertion(step.assertions, condition)
                            or (role is not None and (action.locator is None
                                or action.locator.role != role or action.locator.name != name))):
                        matched = False
                        break
                if matched:
                    covered = True
                    break
        if not task_assertion(specification.assertions, "checked") or not covered:
            raise ValueError("B01 冻结规范必须包含原问题 checked 断言和完成/刷新/取消完成/刷新/Todo/Done 双向业务场景")
    return specification


def freeze_spec(run_root: Path, source: Path, *, case: str = "B01") -> tuple[Path, str]:
    contents = source.read_bytes()
    validate_spec(contents, case=case)
    digest = hashlib.sha256(contents).hexdigest()
    destination = run_root / "frozen-spec.json"
    run_root.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        output.write(contents)
    return destination, digest


def verify_evidence(state, artifacts, frozen_spec: Path, *, case: str) -> set[str]:
    from tracefix.runtime.contracts import TestSpec, Validation, verification_gate

    def artifact_exists(reference):
        return artifacts.exists(state.scope_id, state.run_id, reference)

    def artifact_read(reference):
        return artifacts.json(state.scope_id, state.run_id, reference)

    expected = validate_spec(frozen_spec.read_bytes(), case=case)
    if not state.test_spec_ref or not artifact_exists(state.test_spec_ref):
        raise RuntimeError("真实 E2E 缺少冻结规范证据")
    actual = TestSpec.model_validate(artifact_read(state.test_spec_ref))
    if actual != expected:
        raise RuntimeError("真实 E2E 的 Run 规范与源码修改前冻结的规范不一致")
    validations = [Validation.model_validate(artifact_read(reference))
                   for reference in state.validation_refs]
    if not verification_gate(state, validations, artifact_exists, artifact_read=artifact_read,
                             artifact_read_bytes=lambda reference: artifacts.read(
                                 state.scope_id, state.run_id, reference)):
        raise RuntimeError("真实 E2E 独立验证门禁拒绝：验证项、业务检查点或源码/补丁/规范绑定不完整")
    return {validation.kind for validation in validations if validation.passed}


def agent_command(registry: Path, data_root: Path, spec: Path) -> list:
    return [str(PYTHON), "tools/bootstrap/launch.py", "--plain", "--project", "bugboard",
            "--projects", registry, "--profile", ROOT / "profiles/bugboard.yaml",
            "--data", data_root, "--spec", spec, "--run", "--goal",
            "把 Write project brief 标记为完成，刷新页面并验证状态持久化；失败则修复。",
            "--mode", "repair", "--execution-mode", "batch"]


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
    run_root = ROOT / ".tracefix" / "real-e2e" / f"{stamp}-{uuid.uuid4().hex[:12]}"
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
    })
    result = {"status": "failed", "started_at": stamp, "case": args.case,
              "run_root": str(run_root.relative_to(ROOT))}
    compose_started = False
    try:
        frozen_spec, spec_hash = freeze_spec(run_root, args.spec, case=args.case)
        result.update({"frozen_spec": str(frozen_spec.relative_to(ROOT)),
                       "frozen_spec_sha256": spec_hash})
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
        agent_result = command(agent_command(registry, data_root, frozen_spec),
                               env=env, check=False, timeout=3600)
        if hashlib.sha256(frozen_spec.read_bytes()).hexdigest() != spec_hash:
            raise RuntimeError("真实 E2E 的冻结规范在执行期间发生变化")
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
        kinds = verify_evidence(state, artifacts, frozen_spec, case=args.case)
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
    parser.add_argument("--preflight-only", action="store_true", help="仅检查前置条件，不执行真实修复")
    parser.add_argument("--spec", type=Path, default=ROOT / "profiles/persistence.spec.json",
                        help="在初始化和修复源码前保存快照的验收规范")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/real-e2e/report.json")
    args = parser.parse_args(argv)
    load_local_env()
    missing = prerequisites()
    if missing:
        status = "failed" if args.required else "skipped"
        report = {"status": status, "case": args.case, "missing": missing,
                  "message": "未使用 Fake 替代真实环境；请补齐前置条件后重试。"}
        if "TRACEFIX_API_KEY" in missing:
            report["message"] += (
                " TRACEFIX_API_KEY 为空：GitHub Actions 请在仓库 Settings > Secrets and variables"
                " > Actions 中添加名为 TRACEFIX_API_KEY 的 Repository secret，值为有效的模型 API 密钥。"
                "若使用 Organization secret，请授权当前仓库；若使用 Environment secret，"
                "请为 workflow job 配置对应 environment。本地运行请设置环境变量或项目根目录 .env。"
            )
        write_json(args.output, report)
        print(f"REAL_E2E: {status.upper()} — " + "；".join(missing))
        print(report["message"])
        return 2 if args.required else 0
    if args.preflight_only:
        write_json(args.output, {"status": "ready", "case": args.case,
                                 "message": "前置条件已就绪；尚未执行真实 E2E。"})
        print("REAL_E2E: READY — 前置条件已就绪；尚未执行真实 E2E")
        return 0
    print("REAL_E2E: RUNNING — 使用真实模型、Docker 应用和 Playwright MCP")
    return run_flow(args, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
