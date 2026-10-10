"""Small deterministic static-rule adapter.

The full code-intelligence service can later replace this adapter with
tree-sitter/Semgrep execution. The contract already accepts either ``pattern``
or ``regex`` and always returns source locations with bounded evidence.
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Iterable

from .models import Finding, Rule, RuleEvaluation


def evaluate_static(rule: Rule, files: Iterable[tuple[str, str]], *, job_id: str,
                    run_id: str | None = None, evidence_ref: str | None = None,
                    scope_id: str | None = None, project_id: str | None = None,
                    repo_id: str | None = None) -> list[Finding]:
    if rule.detection.type != "static":
        return []
    config = rule.detection.static or {}
    expression = config.get("regex") or config.get("pattern")
    if not isinstance(expression, str) or not expression.strip():
        return []
    try:
        matcher = re.compile(expression, re.MULTILINE)
    except re.error as error:
        raise ValueError(f"静态规则正则无效：{error}") from error
    globs = config.get("path_globs", ["**"])
    title = str(config.get("message") or rule.name)
    result = []
    for path, content in files:
        if globs and not any(fnmatchcase(path, pattern) for pattern in globs):
            continue
        for match in list(matcher.finditer(content))[:100]:
            line = content.count("\n", 0, match.start()) + 1
            result.append(Finding.from_failure(
                job_id=job_id, run_id=run_id, rule=rule, title=title,
                scope_id=scope_id, project_id=project_id, repo_id=repo_id,
                expected=str(config.get("expected") or f"文件不得匹配规则 {rule.name}：{expression}"),
                actual=match.group(0)[:500],
                location={"path": path, "line": line}, evidence_refs=[evidence_ref] if evidence_ref else [],
                details={"match": match.group(0)[:500], "column": match.start() - content.rfind("\n", 0, match.start()),
                         "rule_context": rule.summary()},
                failure_signature=[path, line, match.group(0)[:200]],
            ))
    return result


def evaluate_static_result(rule: Rule, files: Iterable[tuple[str, str]], *, job_id: str,
                           run_id: str | None = None, evidence_ref: str | None = None,
                           scope_id: str | None = None, project_id: str | None = None,
                           repo_id: str | None = None) -> RuleEvaluation:
    if rule.detection.type != "static":
        return RuleEvaluation(status="not_applicable", reason="不是静态规则")
    config = rule.detection.static or {}
    if set(config) - {"regex", "pattern", "path_globs", "message", "expected"}:
        return RuleEvaluation(status="not_completed", reason="静态规则包含未实现的检测器或字段")
    expression = config.get("regex") or config.get("pattern")
    if not isinstance(expression, str) or not expression.strip():
        return RuleEvaluation(status="not_completed", reason="静态规则缺少可执行表达式")
    files = list(files)
    globs = config.get("path_globs", ["**"])
    if not any(not globs or any(fnmatchcase(path, pattern) for pattern in globs) for path, _ in files):
        return RuleEvaluation(status="not_applicable", reason="当前文件集合不匹配规则目标")
    try:
        findings = evaluate_static(rule, files, job_id=job_id, run_id=run_id, evidence_ref=evidence_ref,
                                   scope_id=scope_id, project_id=project_id, repo_id=repo_id)
    except ValueError as error:
        return RuleEvaluation(status="not_completed", reason=str(error))
    return RuleEvaluation(findings=findings, status="matched" if findings else "checked_pass")
