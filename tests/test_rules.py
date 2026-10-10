from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path
import pytest

from tracefix.rules import (Finding, Rule, RuleResolver, RuleLibrary, RuleSnapshot, RuleCoverage,
                            evaluate_oracle, evaluate_oracle_result, evaluate_static_result, render_rule_context)
from tracefix.runtime.continuation import derive_run
from tracefix.runtime.contracts import RunState
from tracefix.runtime.contracts import digest


def oracle_rule(rule_id="console-errors", **overrides):
    return Rule(id=rule_id, name="Console 不得报错", status="enabled",
                detection={"type": "oracle", "oracle": {"kind": "console_no_error", **overrides}})


def test_oracle_console_and_network_produce_deduplicable_findings():
    rule = oracle_rule()
    findings = evaluate_oracle(rule, {"url": "http://app", "console": "ERROR failed"}, job_id="job", run_id="run")
    assert len(findings) == 1 and findings[0].rule_id == rule.id
    network = oracle_rule("network-errors", kind="network_status", url_patterns=["*/api/*"])
    assert evaluate_oracle(network, {"url": "http://app", "network": '[{"url":"http://app/api/x","status":500}]'}, job_id="job")


def test_rule_snapshot_is_inherited_and_child_can_add_rule():
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        library = RuleLibrary(Path(directory) / "rules.sqlite")
        library.save_rule(oracle_rule("parent"))
        library.save_rule(oracle_rule("child"))
        resolver = RuleResolver(library)
        parent, _ = resolver.resolve_snapshot(run_id="parent-run", project_id="project", phase="*", additional=["parent"])
        child, rules = resolver.resolve_snapshot(run_id="child-run", project_id="project", phase="*", parent=parent, additional=["child"])
        assert {"child", "parent"}.issubset({item.id for item in child.refs})
        assert {"parent", "child"}.issubset({rule.id for rule in rules})


def test_derived_run_keeps_parent_rule_refs_and_adds_requested_ids():
    parent = RunState(scope_id="project", goal="检查页面", url="http://app", rule_refs=[{"id": "base", "version": 1}], rule_snapshot_hash="hash")
    child = derive_run(parent, additional_rule_ids=["extra"])
    assert child.parent_run_id == parent.run_id
    assert child.rule_refs == parent.rule_refs
    assert child.additional_rule_ids == ["extra"]


def test_rule_context_keeps_ids_when_detail_budget_is_exhausted():
    rules = [oracle_rule(f"r-{index}") for index in range(10)]
    context = render_rule_context(rules, max_tokens=128)
    assert context["rule_ids"] == [rule.id for rule in rules]
    assert len(context["items"]) == len(rules)


def test_explicit_selection_appends_required_rules_and_freezes_body_and_scope(tmp_path):
    library = RuleLibrary(tmp_path / "rules.sqlite")
    original = library.save_rule(oracle_rule("selected"))
    library.save_rule(oracle_rule("selected_two"))
    resolver = RuleResolver(library)
    snapshot, rules = resolver.resolve_snapshot(run_id="test-run", project_id="project", repo_id="repo",
        scope_id="scope", phase="*", rule_ids=["selected", "selected_two"])
    library.save_snapshot(snapshot)
    assert snapshot.format_version == 2
    assert snapshot.rules == rules and snapshot.scope_id == "scope" and snapshot.repo_id == "repo"
    assert {"selected", "selected_two", "builtin_api_no_5xx"} <= {reference.id for reference in snapshot.refs}
    assert snapshot.body_hashes["selected@1"] == original.body_hash
    library.save_rule(original.model_copy(update={"name": "修改要求", "status": "disabled"}), original.id)
    frozen = resolver.load_snapshot_rules(library.snapshot("test-run"), project_id="project", repo_id="repo", scope_id="scope")
    assert next(rule for rule in frozen if rule.id == original.id).name == original.name
    changed, _ = resolver.resolve_snapshot(run_id="test-run", project_id="project", phase="*")
    with pytest.raises(ValueError, match="不可替换"):
        library.save_snapshot(changed)
    with pytest.raises(ValueError, match="不属于当前"):
        resolver.load_snapshot_rules(snapshot, repo_id="other")
    fields = snapshot.model_dump(mode="json")
    fields["hash"] = "bad"
    with pytest.raises(ValueError, match="hash"):
        RuleSnapshot.model_validate(fields)
    fields["hash"] = ""
    fields["body_hashes"]["selected@1"] = "bad"
    with pytest.raises(ValueError, match="body hash"):
        RuleSnapshot.model_validate(fields)
    with library.connect() as connection:
        connection.execute("DELETE FROM rule_versions WHERE rule_id=? AND version=1", (original.id,))
    with pytest.raises(FileNotFoundError, match="版本不存在"):
        resolver.load_snapshot_rules(snapshot)


@pytest.mark.parametrize("status", ["draft", "disabled", "archived"])
def test_selected_rules_must_be_enabled_and_project_bound(status):
    rule = oracle_rule("chosen").model_copy(update={"status": status})
    with pytest.raises(ValueError, match="停用"):
        RuleResolver([rule]).resolve(project_id="project", phase="*", rule_ids=[rule.id])
    foreign = Rule.model_validate({**oracle_rule("foreign").model_dump(), "scope": {"project_ids": ["other"]}})
    with pytest.raises(ValueError, match="不属于当前"):
        RuleResolver([foreign]).resolve(project_id="project", phase="*", rule_ids=[foreign.id])


def test_legacy_snapshot_and_corrupt_history_fail_explicitly(tmp_path):
    library = RuleLibrary(tmp_path / "rules.sqlite")
    rule = library.save_rule(oracle_rule("historical"))
    refs = [{"id": rule.id, "version": rule.version}]
    legacy = RuleSnapshot.model_validate({"run_id": "legacy", "refs": refs, "hash": digest(refs)})
    assert legacy.format_version == 1
    assert RuleResolver(library).load_snapshot_rules(legacy) == [rule]
    with library.connect() as connection:
        connection.execute("UPDATE rule_versions SET body_hash='bad' WHERE rule_id=?", (rule.id,))
    with pytest.raises(ValueError, match="hash"):
        library.version(rule.id, rule.version)
    with pytest.raises(ValueError, match="hash"):
        library.rules()
    with pytest.raises(ValueError, match="hash"):
        RuleResolver(library).load_snapshot_rules(legacy)


def test_scoped_findings_preserve_run_identity_and_repair_provenance(tmp_path):
    library = RuleLibrary(tmp_path / "rules.sqlite")
    rule = oracle_rule()
    findings = []
    for scope, repo, run in [("one", "repo-one", "first"), ("two", "repo-two", "second"), ("one", "repo-one", "third")]:
        finding = Finding.from_failure(job_id="job", run_id=run, rule=rule, title="同一个问题",
            location={"url": "http://app"}, scope_id=scope, project_id=scope, repo_id=repo,
            expected="预期", actual="实际", evidence_refs=["evidence.json"])
        findings.append(library.save_finding(finding))
        assert library.save_finding(finding.model_copy(update={"id": "duplicate-" + run})).id == finding.id
    assert len({finding.id for finding in findings}) == 3
    assert len(library.findings(repo_id="repo-one")) == 2
    assert len(library.insights()) == 2
    fixed = findings[0].model_copy(update={"status": "fixed", "repair_run_id": "repair", "patch_hash": "hash",
        "verification_refs": ["validation.json"]})
    library.save_finding(fixed)
    assert library.finding(fixed.id).verification_refs == ["validation.json"]
    assert library.insights(scope_id="one")[0]["fixed"] == 1
    with pytest.raises(ValueError, match="归属"):
        library.save_finding(fixed.model_copy(update={"run_id": "foreign"}))


def test_evaluation_coverage_distinguishes_absent_inputs_and_unsupported_checks(tmp_path):
    rule = oracle_rule()
    assert evaluate_oracle_result(rule, {}, job_id="job").status == "not_completed"
    assert evaluate_oracle_result(rule, {"console": []}, job_id="job").status == "checked_pass"
    matched = evaluate_oracle_result(rule, {"console": "ERROR failed"}, job_id="job", run_id="run",
        scope_id="project", project_id="project", repo_id="repo", evidence_ref="evidence.json")
    assert matched.status == "matched"
    assert matched.findings[0].expected and matched.findings[0].actual
    assert matched.findings[0].repo_id == "repo" and matched.findings[0].rule_version == 1
    assert evaluate_oracle_result(oracle_rule("axe", kind="a11y_axe"), {}, job_id="job").status == "not_completed"
    static = Rule(id="static", name="禁止 console.log", status="enabled", detection={"type": "static", "static": {"regex": "console\\.log"}})
    assert evaluate_static_result(static, [], job_id="job").status == "not_applicable"
    result = evaluate_static_result(static, [("src/main.ts", "console.log('x')")], job_id="job", run_id="run",
        repo_id="repo", scope_id="project", evidence_ref="source.json")
    assert result.status == "matched" and result.findings[0].expected and result.findings[0].actual
    library = RuleLibrary(tmp_path / "coverage.sqlite")
    record = RuleCoverage(run_id="run", rule_id=rule.id, rule_version=1, phase="EXPLORE",
        status=matched.status, scope_id="project", repo_id="repo", evidence_refs=["evidence.json"],
        finding_ids=[matched.findings[0].id])
    library.save_coverage(record)
    assert library.coverage("run", scope_id="project") == [record]
    assert library.coverage("run", repo_id="other") == []


def test_repair_rule_coverage_requires_patch_and_verify():
    rule = oracle_rule("explore-only").model_copy(update={"phases": ["EXPLORE", "DIAGNOSE"]})
    resolver = RuleResolver([rule])
    snapshot, _ = resolver.resolve_snapshot(run_id="run", project_id="project", phase="*")
    with pytest.raises(ValueError, match="覆盖不足"):
        resolver.require_repair_coverage(snapshot, [rule.id])
    complete = rule.model_copy(update={"phases": ["EXPLORE", "DIAGNOSE", "PATCH", "VERIFY"]})
    resolver = RuleResolver([complete])
    snapshot, _ = resolver.resolve_snapshot(run_id="run", project_id="project", phase="*")
    resolver.require_repair_coverage(snapshot, [rule.id])
