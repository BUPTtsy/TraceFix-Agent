"""Deterministic, side-effect-free Oracle evaluators."""
from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from typing import Any

from tracefix.execution.browser import elements

from .models import Finding, Rule


def _payload_lines(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    text = str(value)
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            result.extend(parsed if isinstance(parsed, list) else [parsed])
        except (TypeError, ValueError):
            result.append(line)
    return result


def _url(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("url") or item.get("request", {}).get("url") or "")
    match = re.search(r'https?://[^\s\]]+', str(item))
    return match[0] if match else ""


def _status(item: Any) -> int | None:
    if isinstance(item, dict):
        value = item.get("status", item.get("statusCode", item.get("response", {}).get("status")))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    match = re.search(r"=>\s*\[([1-5]\d{2})\]", str(item)) or re.search(r"\b([1-5]\d{2})\b", str(item))
    return int(match[1]) if match else None


def _locator_matches(snapshot: str, locator: dict[str, Any] | None) -> list[dict]:
    if not locator:
        return []
    role, name = locator.get("role"), locator.get("name")
    return [item for item in elements(snapshot) if item["role"] == role and (name is None or item["name"] == name)]


def _finding(rule: Rule, *, job_id: str, run_id: str | None, title: str,
             location: dict, evidence_ref: str | None, details: dict) -> Finding:
    return Finding.from_failure(job_id=job_id, run_id=run_id, rule=rule, title=title,
                                location=location,
                                evidence_refs=[evidence_ref] if evidence_ref else [],
                                details=details, failure_signature=details)


def _console(rule: Rule, config: dict, observation: dict, *, job_id: str, run_id: str | None,
             evidence_ref: str | None) -> list[Finding]:
    ignored = [re.compile(pattern) for pattern in config.get("ignore", [])]
    failures = []
    for entry in _payload_lines(observation.get("console")):
        level = str(entry.get("level", entry.get("type", ""))).lower() if isinstance(entry, dict) else ""
        message = str(entry.get("message", entry.get("text", ""))) if isinstance(entry, dict) else str(entry)
        if level not in {"error", "exception", "uncaught", "fatal"} and not re.search(r"\b(error|exception)\b", message, re.I):
            continue
        if any(pattern.search(message) for pattern in ignored):
            continue
        failures.append(_finding(rule, job_id=job_id, run_id=run_id,
                                 title="页面 console 出现错误",
                                 location={"url": observation.get("url"), "channel": "console"},
                                 evidence_ref=evidence_ref,
                                 details={"entry": entry, "message": message}))
    return failures


def _network(rule: Rule, config: dict, observation: dict, *, job_id: str, run_id: str | None,
             evidence_ref: str | None) -> list[Finding]:
    patterns = config.get("url_patterns", config.get("urls", ["/**"]))
    statuses = {int(value) for value in config.get("forbidden_statuses", [500, 501, 502, 503, 504, 505, 506, 507, 508, 509, 510, 511])}
    failures = []
    for entry in _payload_lines(observation.get("network")):
        url, status = _url(entry), _status(entry)
        path = url.split('://', 1)[-1]
        path = '/' + path.split('/', 1)[1] if '/' in path else '/'
        if status is None or status not in statuses or not any(
                fnmatchcase(url, pattern) or fnmatchcase(path, pattern) for pattern in patterns):
            continue
        failures.append(_finding(rule, job_id=job_id, run_id=run_id,
                                 title=f"接口返回禁止状态码 {status}",
                                 location={"url": url, "status": status}, evidence_ref=evidence_ref,
                                 details={"request": entry}))
    return failures


def _dom(rule: Rule, config: dict, observation: dict, *, job_id: str, run_id: str | None,
         evidence_ref: str | None, previous_observation: dict | None = None) -> list[Finding]:
    snapshot = str(observation.get("snapshot", ""))
    locator = config.get("locator")
    matches = _locator_matches(snapshot, locator)
    condition = config.get("condition", "visible")
    passed = bool(matches)
    if condition == "absent":
        passed = not matches
    elif condition in {"checked", "disabled", "enabled"} and matches:
        passed = (f"[{condition}]" in matches[0]["attrs"] if condition != "enabled"
                  else "[disabled]" not in matches[0]["attrs"])
    if "text_matches" in config and matches:
        passed = any(re.search(str(config["text_matches"]), item["name"]) for item in matches)
    if "count" in config:
        passed = len(matches) == int(config["count"])
    if "attribute" in config and matches:
        attribute = config["attribute"]
        attr_name = attribute.get("name") if isinstance(attribute, dict) else str(attribute)
        attr_value = attribute.get("value") if isinstance(attribute, dict) else None
        passed = bool(re.search(rf"\[{re.escape(attr_name)}(?:=([^\]]+))?\]", matches[0]["attrs"]))
        if passed and attr_value is not None:
            passed = str(attr_value) in matches[0]["attrs"]
    if passed:
        return []
    return [_finding(rule, job_id=job_id, run_id=run_id, title="页面元素不符合检测规则",
                     location={"url": observation.get("url"), "locator": locator}, evidence_ref=evidence_ref,
                     details={"condition": condition, "matches": len(matches), "config": config})]


def evaluate_oracle(rule: Rule, observation: dict, *, job_id: str, run_id: str | None = None,
                    evidence_ref: str | None = None, previous_observation: dict | None = None,
                    action: dict | None = None) -> list[Finding]:
    """Evaluate one Oracle rule against an observation and return Findings.

    Unsupported capabilities (axe and screenshot comparison) remain explicit:
    they can only produce a Finding when an internal runner supplies a checked
    failure payload, and never from model-authored text.
    """
    if rule.detection.type != "oracle":
        return []
    config = rule.detection.oracle or {}
    kind = config.get("kind")
    if kind == "console_no_error":
        return _console(rule, config, observation, job_id=job_id, run_id=run_id, evidence_ref=evidence_ref)
    if kind == "network_status":
        return _network(rule, config, observation, job_id=job_id, run_id=run_id, evidence_ref=evidence_ref)
    if kind == "dom_assertion":
        return _dom(rule, config, observation, job_id=job_id, run_id=run_id, evidence_ref=evidence_ref)
    if kind == "dom_after_action":
        if not action:
            return []
        trigger = config.get("trigger", {})
        if action and trigger and action.get("kind") != trigger.get("action"):
            return []
        trigger_locator = trigger.get("locator") if isinstance(trigger, dict) else None
        if action and trigger_locator:
            actual = action.get("locator") or {}
            if trigger_locator.get("role") and actual.get("role") != trigger_locator.get("role"):
                return []
            name_regex = trigger_locator.get("name_regex")
            if name_regex:
                try:
                    matched = re.search(str(name_regex), str(actual.get("name", "")))
                except re.error:
                    return []
                if not matched:
                    return []
        expect_any = config.get("expect_any", [])
        failures = []
        for expected in expect_any:
            current = _dom(rule, expected, observation, job_id=job_id, run_id=run_id, evidence_ref=evidence_ref,
                           previous_observation=previous_observation)
            if not current:
                return []
            failures.extend(current)
        return failures
    internal_failure = observation.get("oracle_failures", {}).get(kind) if isinstance(observation.get("oracle_failures"), dict) else None
    if internal_failure:
        return [_finding(rule, job_id=job_id, run_id=run_id, title="Oracle 检查未通过",
                         location={"url": observation.get("url")}, evidence_ref=evidence_ref,
                         details={"kind": kind, "failure": internal_failure})]
    return []
