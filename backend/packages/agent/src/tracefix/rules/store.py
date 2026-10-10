"""SQLite persistence for rules, immutable versions, run snapshots and Findings."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tracefix.runtime.contracts import digest

from .models import Finding, Rule, RuleCoverage, RuleRef, RuleSnapshot


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class _JSONNumber(str):
    pass


def _stored_body_hash(body: str) -> str:
    def canonical(value):
        if isinstance(value, _JSONNumber):
            return str(value)
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + canonical(value[key]) for key in sorted(value)) + "}"
        if isinstance(value, list):
            return "[" + ",".join(canonical(item) for item in value) + "]"
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    parsed = json.loads(body, parse_int=_JSONNumber, parse_float=_JSONNumber)
    return hashlib.sha256(canonical(parsed).encode("utf-8")).hexdigest()


class RuleConflictError(ValueError):
    pass


class RuleLibrary:
    """Transactional local rule store.

    The schema mirrors the Postgres design from the product manual while using
    JSON bodies so the single-user Windows distribution needs no migrations.
    """

    def __init__(self, path: str | Path = ".tracefix/console.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS rules (
              id TEXT PRIMARY KEY, org_id TEXT NOT NULL, scope_level TEXT NOT NULL,
              current_version INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_versions (
              rule_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
              body_hash TEXT NOT NULL, change_note TEXT, author TEXT, created_at TEXT NOT NULL,
              PRIMARY KEY (rule_id, version)
            );
            CREATE TABLE IF NOT EXISTS rule_run_snapshots (
              run_id TEXT PRIMARY KEY, parent_run_id TEXT, snapshot_hash TEXT NOT NULL,
              refs TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_assignments (
              run_id TEXT NOT NULL, rule_id TEXT NOT NULL, version INTEGER NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY (run_id, rule_id)
            );
            CREATE TABLE IF NOT EXISTS findings (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL, run_id TEXT, rule_id TEXT,
              rule_version INTEGER, source TEXT NOT NULL, severity TEXT NOT NULL,
              status TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS findings_rule_idx ON findings(rule_id, created_at);
            CREATE TABLE IF NOT EXISTS rule_sets (
              id TEXT PRIMARY KEY, org_id TEXT, name TEXT NOT NULL, parent_id TEXT,
              version INTEGER NOT NULL, items TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rule_coverage (
              run_id TEXT NOT NULL, rule_id TEXT NOT NULL, version INTEGER NOT NULL,
              phase TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(run_id, rule_id, version, phase)
            );
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(rule_run_snapshots)")}
            if "snapshot_data" not in columns:
                connection.execute("ALTER TABLE rule_run_snapshots ADD COLUMN snapshot_data TEXT")
        self.seed_builtins()

    def seed_builtins(self) -> list[Rule]:
        """Insert shipped demonstration rules once, without overwriting edits."""
        from .builtins import builtin_rules
        inserted = []
        for rule in builtin_rules():
            try:
                self.rule(rule.id)
            except FileNotFoundError:
                inserted.append(self.save_rule(rule, author="system", change_note="内置演示规则"))
        return inserted

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _body(self, row) -> Rule:
        rule = Rule.model_validate(json.loads(row["body"]))
        if row["body_hash"] not in {rule.body_hash, _stored_body_hash(row["body"])}:
            raise ValueError(f"规则版本正文 hash 不匹配：{rule.id}@{rule.version}")
        if rule.id != row["rule_id"] or rule.version != row["version"]:
            raise ValueError("规则版本正文身份不匹配")
        return rule

    def rules(self, *, project_id: str | None = None, include_archived: bool = False,
              status: str | None = None, query: str = "") -> list[Rule]:
        clauses, params = [], []
        if not include_archived:
            clauses.append("status != 'archived'")
        if status:
            clauses.append("status = ?"); params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(f"SELECT id, current_version FROM rules{where} ORDER BY updated_at DESC", params).fetchall()
        result = []
        for row in rows:
            rule = self.version(row["id"], row["current_version"])
            scope = rule.scope
            if project_id and scope.project_ids and project_id not in scope.project_ids:
                continue
            if query and query.casefold() not in (rule.id + " " + rule.name + " " + " ".join(rule.tags)).casefold():
                continue
            result.append(rule)
        return result

    def rule(self, rule_id: str) -> Rule:
        with self.connect() as connection:
            row = connection.execute("SELECT current_version FROM rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("规则不存在")
        return self.version(rule_id, row["current_version"])

    def version(self, rule_id: str, version: int) -> Rule:
        with self.connect() as connection:
            row = connection.execute("SELECT rule_id, version, body, body_hash FROM rule_versions WHERE rule_id=? AND version=?", (rule_id, version)).fetchone()
        if row is None:
            raise FileNotFoundError("规则版本不存在")
        return self._body(row)

    def versions(self, rule_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT rule_id, version, body_hash, change_note, author, created_at FROM rule_versions WHERE rule_id=? ORDER BY version DESC", (rule_id,)).fetchall()
        return [dict(row) for row in rows]

    def save_rule(self, fields: Rule | dict, rule_id: str | None = None, *, expected_version: int | None = None,
                  author: str = "local", change_note: str = "") -> Rule:
        data = fields.model_dump(mode="json") if isinstance(fields, Rule) else dict(fields)
        org_id = data.pop("org_id", "local")
        data.pop("expected_version", None)
        data.pop("author", None)
        data.pop("change_note", None)
        if rule_id:
            data["id"] = rule_id
        existing = None
        if rule_id:
            try: existing = self.rule(rule_id)
            except FileNotFoundError: pass
        if existing:
            if expected_version is not None and expected_version != existing.version:
                raise RuleConflictError("规则已被其他窗口更新，请重新打开后编辑")
            data["version"] = existing.version + 1
            data.setdefault("status", existing.status)
            data.setdefault("created_at", existing.created_at)
        data.setdefault("version", 1)
        now = _timestamp()
        data["created_at"] = data.get("created_at") or now
        data["updated_at"] = now
        rule = Rule.model_validate(data)
        with self.connect() as connection:
            if not existing:
                if connection.execute("SELECT 1 FROM rules WHERE id=?", (rule.id,)).fetchone():
                    raise RuleConflictError("规则 id 已存在")
                connection.execute("INSERT INTO rules VALUES (?,?,?,?,?,?,?,?)",
                                   (rule.id, org_id, rule.scope.level, rule.version,
                                    rule.status, rule.owner, rule.created_at, now))
            else:
                connection.execute("UPDATE rules SET scope_level=?, current_version=?, status=?, owner=?, updated_at=? WHERE id=?",
                                   (rule.scope.level, rule.version, rule.status, rule.owner, now, rule.id))
            connection.execute("INSERT INTO rule_versions VALUES (?,?,?,?,?,?,?)",
                               (rule.id, rule.version, rule.model_dump_json(), rule.body_hash,
                                change_note, author, now))
        return rule

    def set_status(self, rule_id: str, status: str, *, expected_version: int | None = None,
                   author: str = "local", change_note: str = "状态变更") -> Rule:
        rule = self.rule(rule_id)
        if expected_version is not None and expected_version != rule.version:
            raise RuleConflictError("规则版本已过期")
        return self.save_rule(rule.model_copy(update={"status": status}), rule_id,
                              expected_version=rule.version, author=author, change_note=change_note)

    def rollback(self, rule_id: str, version: int, *, author: str = "local", change_note: str = "回滚") -> Rule:
        target = self.version(rule_id, version)
        current = self.rule(rule_id)
        return self.save_rule(target.model_copy(update={"version": current.version + 1, "status": current.status}),
                              rule_id, expected_version=current.version, author=author, change_note=change_note)

    def delete_rule(self, rule_id: str) -> None:
        with self.connect() as connection:
            referenced = connection.execute(
                "SELECT 1 FROM rule_run_snapshots WHERE refs LIKE ? LIMIT 1", (f'%"id": "{rule_id}"%',)
            ).fetchone() or connection.execute("SELECT 1 FROM findings WHERE rule_id=? LIMIT 1", (rule_id,)).fetchone()
            if referenced:
                raise RuleConflictError("已被历史 Run 或 Finding 引用的规则只能归档")
            if connection.execute("SELECT 1 FROM rules WHERE id=?", (rule_id,)).fetchone() is None:
                raise FileNotFoundError("规则不存在")
            connection.execute("DELETE FROM rule_versions WHERE rule_id=?", (rule_id,))
            connection.execute("DELETE FROM rules WHERE id=?", (rule_id,))

    def save_snapshot(self, snapshot: RuleSnapshot) -> RuleSnapshot:
        from .resolver import RuleResolver
        snapshot = RuleSnapshot.model_validate(snapshot.model_dump(mode="json"))
        RuleResolver(self).load_snapshot_rules(snapshot, project_id=snapshot.project_id,
                                               scope_id=snapshot.scope_id, repo_id=snapshot.repo_id)
        with self.connect() as connection:
            old = connection.execute("SELECT snapshot_hash FROM rule_run_snapshots WHERE run_id=?", (snapshot.run_id,)).fetchone()
            if old:
                if old["snapshot_hash"] != snapshot.hash:
                    raise RuleConflictError("活动 Run 的规则快照不可替换")
                return snapshot
            connection.execute("INSERT INTO rule_run_snapshots (run_id,parent_run_id,snapshot_hash,refs,created_at,snapshot_data) VALUES (?,?,?,?,?,?)",
                               (snapshot.run_id, snapshot.parent_run_id, snapshot.hash,
                                json.dumps([ref.model_dump(mode="json") for ref in snapshot.refs], ensure_ascii=False),
                                snapshot.created_at or _timestamp(), snapshot.model_dump_json()))
            for ref in snapshot.refs:
                connection.execute("INSERT OR REPLACE INTO rule_assignments VALUES (?,?,?,?)",
                                   (snapshot.run_id, ref.id, ref.version, snapshot.created_at or _timestamp()))
        return snapshot

    def snapshot(self, run_id: str) -> RuleSnapshot | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM rule_run_snapshots WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        if row["snapshot_data"]:
            fields = json.loads(row["snapshot_data"])
            fields.update(run_id=row["run_id"], parent_run_id=row["parent_run_id"], hash=row["snapshot_hash"],
                          refs=json.loads(row["refs"]))
            return RuleSnapshot.model_validate(fields)
        refs = [RuleRef.model_validate(ref) for ref in json.loads(row["refs"])]
        return RuleSnapshot(run_id=run_id, parent_run_id=row["parent_run_id"], format_version=1, refs=refs,
                            hash=row["snapshot_hash"], created_at=row["created_at"])

    def run_rules(self, run_id: str) -> list[RuleRef]:
        with self.connect() as connection:
            rows = connection.execute("SELECT rule_id, version FROM rule_assignments WHERE run_id=? ORDER BY rule_id", (run_id,)).fetchall()
        return [RuleRef(id=row["rule_id"], version=row["version"]) for row in rows]

    def save_finding(self, finding: Finding) -> Finding:
        now = _timestamp()
        finding = Finding.model_validate(finding.model_dump(mode="json"))
        key = digest([finding.scope_id, finding.project_id, finding.repo_id, finding.run_id, finding.fingerprint])
        with self.connect() as connection:
            row = connection.execute("SELECT id, data FROM findings WHERE id=? OR fingerprint=?", (finding.id, key)).fetchone()
            if row:
                old = Finding.model_validate(json.loads(row["data"]))
                if (old.run_id, old.scope_id, old.project_id, old.repo_id) != (finding.run_id, finding.scope_id, finding.project_id, finding.repo_id):
                    raise RuleConflictError("Finding 的 Run 与项目仓库归属不可替换")
                immutable = ("job_id", "run_id", "scope_id", "project_id", "repo_id", "rule_id",
                             "rule_version", "source", "fingerprint", "source_base_commit", "source_manifest")
                if any(getattr(old, field) != getattr(finding, field) for field in immutable):
                    raise RuleConflictError("Finding 来源身份和指纹不可换绑")
                if finding.status == "fixed" and old.status != "fixed":
                    raise RuleConflictError("Finding 只有确定性验证入口可以标记 fixed")
                if old.status == "fixed" and finding.status == "fixed" and (
                        old.repair_run_id, old.patch_hash, old.verification_refs) != (
                        finding.repair_run_id, finding.patch_hash, finding.verification_refs):
                    raise RuleConflictError("已验证 Finding 的修复来源不可替换")
                if old.status == "fixed" and finding.status != "fixed":
                    finding.status = old.status
                    finding.repair_run_id = old.repair_run_id
                    finding.patch_hash = old.patch_hash
                    finding.verification_refs = old.verification_refs
                updates = finding.model_dump(mode="python", exclude={"id", "evidence_refs", "details"})
                updates.update(evidence_refs=list(dict.fromkeys(old.evidence_refs + finding.evidence_refs)),
                               details={**old.details, **finding.details})
                old = old.model_copy(update=updates)
                connection.execute("UPDATE findings SET status=?, data=?, updated_at=? WHERE id=?",
                                   (old.status, old.model_dump_json(), now, old.id))
                return old
            if finding.status == "fixed":
                raise RuleConflictError("Finding 只有确定性验证入口可以标记 fixed")
            connection.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                               (finding.id, finding.job_id, finding.run_id, finding.rule_id, finding.rule_version,
                                finding.source, finding.severity, finding.status, key,
                                finding.model_dump_json(), now, now))
        return finding

    def save_verified_finding(self, finding: Finding) -> Finding:
        if finding.status != "fixed" or not finding.repair_run_id or not finding.patch_hash or not finding.verification_refs:
            raise RuleConflictError("标记 fixed 必须提供修复 Run、patch hash 和验证证据")
        existing = self.finding(finding.id)
        if existing.status == "fixed" and (existing.repair_run_id, existing.patch_hash, existing.verification_refs) == (
                finding.repair_run_id, finding.patch_hash, finding.verification_refs):
            return existing
        if existing.status == "fixed":
            raise RuleConflictError("已验证 Finding 的修复来源不可替换")
        immutable = ("job_id", "run_id", "scope_id", "project_id", "repo_id", "rule_id",
                     "rule_version", "source", "fingerprint", "source_base_commit", "source_manifest")
        if any(getattr(existing, field) != getattr(finding, field) for field in immutable):
            raise RuleConflictError("Finding 来源身份和指纹不可换绑")
        with self.connect() as connection:
            connection.execute("UPDATE findings SET status=?, data=?, updated_at=? WHERE id=?",
                ("fixed", finding.model_dump_json(), _timestamp(), finding.id))
        return finding

    def finding(self, finding_id: str) -> Finding:
        with self.connect() as connection:
            row = connection.execute("SELECT data FROM findings WHERE id=?", (finding_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("Finding 不存在")
        return Finding.model_validate(json.loads(row["data"]))

    def findings(self, *, rule_id: str | None = None, run_id: str | None = None,
                 status: str | None = None, limit: int = 100, scope_id: str | None = None,
                 project_id: str | None = None, repo_id: str | None = None) -> list[Finding]:
        clauses, params = [], []
        for key, value in (("rule_id", rule_id), ("run_id", run_id), ("status", status)):
            if value is not None: clauses.append(key + "=?"); params.append(value)
        for key, value in (("scope_id", scope_id), ("project_id", project_id), ("repo_id", repo_id)):
            if value is not None:
                clauses.append(f"json_extract(data, '$.{key}')=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(f"SELECT data FROM findings{where} ORDER BY created_at DESC LIMIT ?", [*params, max(1, min(limit, 1000))]).fetchall()
        return [Finding.model_validate(json.loads(row["data"])) for row in rows]

    def insights(self, *, from_time: str | None = None, to_time: str | None = None,
                 scope_id: str | None = None, project_id: str | None = None,
                 repo_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if from_time: clauses.append("created_at >= ?"); params.append(from_time)
        if to_time: clauses.append("created_at <= ?"); params.append(to_time)
        for key, value in (("scope_id", scope_id), ("project_id", project_id), ("repo_id", repo_id)):
            if value is not None:
                clauses.append(f"json_extract(data, '$.{key}')=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(f"SELECT rule_id, status, json_extract(data, '$.scope_id') AS scope_id, json_extract(data, '$.project_id') AS project_id, json_extract(data, '$.repo_id') AS repo_id, COUNT(*) AS count FROM findings{where} GROUP BY scope_id, project_id, repo_id, rule_id, status", params).fetchall()
        grouped: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = (row["scope_id"], row["project_id"], row["repo_id"], row["rule_id"])
            item = grouped.setdefault(key, {"rule_id": row["rule_id"], "scope_id": row["scope_id"],
                "project_id": row["project_id"], "repo_id": row["repo_id"], "total": 0,
                "reproduced": 0, "false_positive": 0, "fixed": 0})
            item["total"] += row["count"]; item[row["status"]] = row["count"]
        for item in grouped.values():
            total = item["total"] or 1
            item["reproduction_rate"] = item.get("reproduced", 0) / total
            item["false_positive_rate"] = item.get("false_positive", 0) / total
            item["fix_rate"] = item.get("fixed", 0) / total
        return list(grouped.values())

    def save_coverage(self, coverage: RuleCoverage | dict) -> RuleCoverage:
        coverage = RuleCoverage.model_validate(coverage)
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO rule_coverage VALUES (?,?,?,?,?,?)",
                (coverage.run_id, coverage.rule_id, coverage.rule_version, coverage.phase,
                 coverage.model_dump_json(), _timestamp()))
        return coverage

    def coverage(self, run_id: str, *, scope_id: str | None = None,
                 project_id: str | None = None, repo_id: str | None = None) -> list[RuleCoverage]:
        with self.connect() as connection:
            rows = connection.execute("SELECT data FROM rule_coverage WHERE run_id=? ORDER BY rule_id, phase", (run_id,)).fetchall()
        records = [RuleCoverage.model_validate(json.loads(row["data"])) for row in rows]
        filters = (("scope_id", scope_id), ("project_id", project_id), ("repo_id", repo_id))
        return [record for record in records if all(value is None or getattr(record, key) == value for key, value in filters)]

    def save_rule_set(self, fields: dict[str, Any], set_id: str | None = None) -> dict[str, Any]:
        now = _timestamp(); set_id = set_id or str(uuid.uuid4())
        with self.connect() as connection:
            old = connection.execute("SELECT version FROM rule_sets WHERE id=?", (set_id,)).fetchone()
            version = (old["version"] + 1) if old else 1
            record = {**fields, "id": set_id, "version": version, "updated_at": now, "created_at": fields.get("created_at", now)}
            connection.execute("INSERT OR REPLACE INTO rule_sets VALUES (?,?,?,?,?,?,?,?)",
                               (set_id, record.get("org_id"), record.get("name", ""), record.get("parent_id"), version,
                                json.dumps(record.get("items", []), ensure_ascii=False), record["created_at"], now))
        return record

    def rule_sets(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM rule_sets ORDER BY updated_at DESC").fetchall()
        return [{**dict(row), "items": json.loads(row["items"])} for row in rows]
