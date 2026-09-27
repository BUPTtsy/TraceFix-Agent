"""Small deterministic static-rule adapter.

The full code-intelligence service can later replace this adapter with
tree-sitter/Semgrep execution. The contract already accepts either ``pattern``
or ``regex`` and always returns source locations with bounded evidence.
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Iterable

from .models import Finding, Rule


def evaluate_static(rule: Rule, files: Iterable[tuple[str, str]], *, job_id: str,
                    run_id: str | None = None, evidence_ref: str | None = None) -> list[Finding]:
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
                location={"path": path, "line": line}, evidence_refs=[evidence_ref] if evidence_ref else [],
                details={"match": match.group(0)[:500], "column": match.start() - content.rfind("\n", 0, match.start())},
                failure_signature=[path, line, match.group(0)[:200]],
            ))
    return result
