"""持久化用户引导、收窄策略与二次确认后的改目标流程。"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from tracefix.runtime.contracts import Guidance, GuidanceConstraints, Phase, RunStatus, new_id
from tracefix.storage.artifacts import redact


class GuidanceRejected(PermissionError):
    def __init__(self, message, *, code='guidance_constraint', details=None):
        super().__init__(message)
        self.details = {'code': code, 'message': message, **(details or {})}


def active_guidance(state, *, logical_call=None):
    # retarget 经独立确认处理；其余引导按 run、phase 或 once 有效期注入。
    result = []
    for entry in state.guidance:
        if entry.level == 'retarget' or entry.status in {'rejected', 'superseded'}:
            continue
        if entry.expires == 'phase' and entry.created_phase != state.phase:
            continue
        if entry.expires == 'once' and entry.applied_call is not None and entry.applied_call != logical_call:
            continue
        result.append(entry)
    return result


def parse_constraints(text):
    # 优先解析显式 JSON；自然语言只识别可以由运行时强制执行的限制。
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raw = {}
        excluded = re.search(r'(?:不要改|禁止修改|不修改|exclude)\s*[:：]?\s*([\w./*?\\-]+)', text, re.I)
        def count(value):
            if value.isdigit():
                return int(value)
            numerals = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3,
                        '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
                        '十': 10, '百': 100}
            if value in numerals:
                return numerals[value]
            total, current = 0, 0
            for character in value:
                number = numerals.get(character)
                if number is None:
                    raise ValueError('无法识别约束中的数量')
                if number in {10, 100}:
                    current = max(current, 1) * number
                    total += current
                    current = 0
                else:
                    current = current * 10 + number
            return total + current

        lines = re.search(r'(?:不超过|最多|<=)\s*(?:修改|变更|更改)?\s*([0-9零〇一二两三四五六七八九十百]+)\s*行', text)
        files = re.search(r'(?:不超过|最多|<=)\s*(?:修改|变更|更改)?\s*([0-9零〇一二两三四五六七八九十百]+)\s*(?:个)?\s*文件', text)
        if excluded:
            pattern = excluded.group(1).replace('\\', '/').rstrip('/')
            raw['exclude_paths'] = [pattern if any(character in pattern for character in '*?') or Path(pattern).suffix else pattern + '/**']
        if lines:
            raw['max_lines_changed'] = count(lines.group(1))
        if files:
            raw['max_files_changed'] = count(files.group(1))
    if not isinstance(raw, dict):
        raise ValueError('约束必须是 JSON 对象或可识别的路径/文件数/行数限制')
    return GuidanceConstraints.model_validate(raw)


def classify_guidance(text):
    if re.search(r'(?:改目标|更改目标|(?:改为|改成|换成)(?:检查|检测|测试|修复)|不是.+(?:而是|是)|其实问题在|retarget)', text, re.I):
        return 'retarget'
    try:
        parse_constraints(text)
    except (ValueError, TypeError):
        return 'hint'
    return 'constraint'


def forbidden_request(text):
    # 先排除“不得绕过”类否定语句，避免把禁止越权的提示误判成越权请求。
    text = re.sub(r'(?:不要|不得|不能|禁止)(?:跳过|绕过|关闭|禁用|删除|取消|扩大|提升|忽略)', '', text)
    return bool(re.search(r'(?:跳过|绕过|关闭|禁用|删除|取消).{0,12}(?:验证|门禁|测试|权限|审批)|'
                          r'(?:扩大|提升|忽略).{0,12}(?:权限|授权)|'
                          r'(?:skip|bypass|disable).{0,20}(?:validation|verification|tests?|approval|policy)', text, re.I))


class GuidanceLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS run_guidance '
                               '(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope_id TEXT NOT NULL, data TEXT NOT NULL)')

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def submit(self, run_id, scope_id, text, *, level=None, author='user', phase=None,
               expires='run', constraints=None):
        text = str(redact(text)).strip()
        entry = Guidance(run_id=run_id, scope_id=scope_id, text=text, level=level or classify_guidance(text),
                         author=author, created_phase=phase, expires=expires)
        if forbidden_request(text):
            entry.status, entry.rejection_reason = 'rejected', '引导不能扩大权限或跳过验证门禁'
        elif entry.level == 'constraint':
            try:
                # 强制约束持续到 Run 结束，防止约束过期后隐式恢复更大权限。
                if expires != 'run':
                    raise ValueError('强制约束在当前 Run 结束前持续生效，不能通过过期扩大权限')
                entry.constraints = GuidanceConstraints.model_validate(constraints) if constraints is not None else parse_constraints(text)
            except (ValueError, TypeError) as error:
                entry.status, entry.rejection_reason = 'rejected', str(error)
        # 拒绝也要落盘，保留提交内容、拒绝原因和完整引导生命周期。
        self.save(entry)
        return entry

    def save(self, entry):
        with self.connection() as connection:
            connection.execute('INSERT INTO run_guidance VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                               (entry.id, entry.run_id, entry.scope_id, entry.model_dump_json()))

    def list(self, run_id, scope_id):
        with self.connection() as connection:
            rows = connection.execute('SELECT data FROM run_guidance WHERE run_id=? AND scope_id=? ORDER BY rowid',
                                      (run_id, scope_id)).fetchall()
        return [Guidance.model_validate_json(row[0]) for row in rows]

    def confirm(self, entry_id, run_id, scope_id):
        # 提前取得写锁，确保同一改目标不会被并发请求重复确认。
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT data FROM run_guidance WHERE id=? AND run_id=? AND scope_id=?',
                                     (entry_id, run_id, scope_id)).fetchone()
            if not row:
                raise ValueError('当前 Run 中没有此引导')
            entry = Guidance.model_validate_json(row[0])
            if entry.level != 'retarget' or entry.status != 'queued' or entry.confirmed_at:
                raise ValueError('只有尚未确认的改目标引导可以确认')
            entry.confirmed_at = time.time()
            connection.execute('UPDATE run_guidance SET data=? WHERE id=?', (entry.model_dump_json(), entry.id))
        return entry


def retarget_state(previous, entry):
    if entry.level != 'retarget' or not entry.confirmed_at or entry.status != 'queued':
        raise ValueError('改目标必须先由用户二次确认')
    if previous.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                              RunStatus.FAILED, RunStatus.SUPERSEDED}:
        raise ValueError('结束的 Run 不能在运行中改目标')
    from tracefix.runtime.continuation import derive_run
    child = derive_run(previous, goal=entry.text)
    # 改目标开启新的预算与模型轨迹，避免把两个目标的消耗混在一起。
    child.budget = type(previous.budget)()
    child.model_exchange_refs = []
    child.reasoning_refs = []
    child.guidance = []
    # 旧证据仅作为可追溯输入继承，后续结论仍须在子 Run 中重新验证。
    child.inherited_evidence_refs = list(previous.evidence_refs)
    child.continuation_instruction = None
    child.continuation_count = 0
    child.continuation_markers = []
    child.abnormal_termination = False
    return child
