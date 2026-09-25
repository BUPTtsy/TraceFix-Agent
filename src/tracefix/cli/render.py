import json
import os
import re
import time

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from tracefix.storage.artifacts import redact, sanitize
from tracefix.storage.presentation import label, readable


ACCENT = '#da7756'
PHASES = ('PREPARE', 'EXPLORE', 'REPRODUCE', 'DIAGNOSE', 'PATCH', 'VERIFY', 'REVIEW', 'FINALIZE')


def compact(value):
    text = clean(value)
    return re.sub(r'\b[a-f0-9]{64}\b(?!\.[a-z])', lambda match: match.group()[:12]+'…', text)


def clean(value):
    return ''.join(character for character in sanitize(str(value)) if not 128 <= ord(character) <= 159)


class Renderer:
    def __init__(self, plain=False):
        self.plain = plain or 'NO_COLOR' in os.environ or os.getenv('TERM') == 'dumb'
        self.console = Console(no_color=self.plain, markup=False, highlight=False)
        self.model = ''
        self.artifacts = None
        self.scope = None
        self.reset()

    def text(self, text):
        value = Text(clean(text))
        width = getattr(self.console, '_width', None) or self.console.width
        if self.console.is_terminal and width:
            wrapped = []
            for line in value.plain.splitlines() or ['']:
                wrapped.extend(part.plain for part in Text(line).wrap(
                    self.console, width, overflow='fold', justify='left'))
            value = Text('\n'.join(wrapped))
        self.console.print(value, soft_wrap=False)

    def panel(self, title, text):
        if self.plain or not self.console.is_terminal or (getattr(self.console, '_width', self.console.width) or self.console.width) < 80:
            self.text(f'{title}\n{text}')
            return
        self.console.print(Panel(Text(clean(text)), title=Text(clean(title)),
                                 title_align='left', border_style=ACCENT, padding=(1, 2)))

    def table(self, title, columns, rows):
        if not rows:
            self.text(f'{title}：暂无记录。')
            return
        if self.plain or not self.console.is_terminal or (getattr(self.console, '_width', self.console.width) or self.console.width) < 80:
            self.text(title)
            for row in rows:
                self.text('\n'.join(f'{column}：{value}' for column, value in zip(columns, row)) + '\n')
            return
        table = Table(title=Text(clean(title)), box=box.SIMPLE, show_lines=True)
        for column in columns:
            table.add_column(clean(column), overflow='fold')
        for row in rows:
            table.add_row(*(Text(clean(value)) for value in row))
        self.console.print(table)

    def stream(self, text):
        self.console.print(Text(clean(text)), end='', soft_wrap=True)

    def welcome(self, scope, mode, model='', *, preview=False):
        self.model = model
        title = Text('*  TraceFix' if self.plain else '✳  TraceFix', style=f'bold {ACCENT}')
        title.append('   Agent · 0.1.1', style='dim')
        body = Text('证据驱动的 GUI 测试与修复\n', style='dim')
        body.append(f'项目 {clean(scope)}   ·   {clean(label(mode))}模式', style='default')
        if model:
            body.append(f'   ·   {clean(model)}')
        body.append('\n\n描述测试目标，然后输入 /run。输入 / 查看带说明的命令。')
        body.append('\n/projects 项目 · /runs 历史 · /knowledge 文档 · /mode chat 流式咨询', style='dim')
        body.append('\nEnter 发送 · Alt+Enter 换行 · Ctrl+C 取消 · Ctrl+D 退出', style='dim')
        if preview:
            body.append('\n\n演示数据 · 离线界面预览，未调用模型、工具或外部服务。', style=ACCENT)
        self.console.print()
        if self.plain or not self.console.is_terminal or (getattr(self.console, '_width', self.console.width) or self.console.width) < 80:
            self.text(title.plain)
            self.text(body.plain)
        else:
            self.console.print(Panel(body, title=title, title_align='left', border_style=ACCENT,
                                     box=box.ROUNDED, padding=(1, 2)))
        self.console.print()

    def reset(self):
        self.phase = None
        self.run_status = 'WAITING_INPUT'
        self.activity = ''
        self.started_at = None
        self.finished_at = None
        self.tokens = 0
        self.model_calls = 0
        self.run_id = None
        self.seen_phases = set()
        self._last_status = None

    def elapsed(self):
        if self.started_at is None:
            return '0s'
        seconds = max(0, int((self.finished_at or time.monotonic()) - self.started_at))
        return f'{seconds // 60}m {seconds % 60:02d}s' if seconds >= 60 else f'{seconds}s'

    def stop(self, status=None):
        self.activity = ''
        if status is not None:
            self.run_status = str(status)
        self.finished_at = self.finished_at or time.monotonic()

    def toolbar(self, scope, mode):
        busy = self.run_status == 'RUNNING'
        marker = ('◐', '◓', '◑', '◒')[int(time.monotonic()*4) % 4] if busy else '●'
        status = self.activity or label(self.run_status)
        segments = [sanitize(scope), label(mode), label(self.phase) if self.phase else '就绪', status]
        if self.started_at is not None:
            segments.append(self.elapsed())
        if self.model:
            segments.append(self.model)
        if self.tokens:
            segments.append(f'{self.tokens:,} tokens')
        return [('class:accent', f' {marker} '), ('class:status', compact(' · '.join(segments)))]

    def timeline(self, phase):
        if phase == self.phase:
            return
        self.phase = phase
        self.seen_phases.add(phase)
        if self.plain or not self.console.is_terminal:
            self.text(f'── {label(phase)} ──')
            return
        timeline = Text()
        for index, item in enumerate(PHASES):
            if index:
                timeline.append(' › ', style='dim')
            style = f'bold {ACCENT}' if item == phase else ('default' if item in self.seen_phases else 'dim')
            timeline.append(label(item), style=style)
        self.console.print()
        self.console.print(timeline)

    def status(self, state):
        if state.run_id != self.run_id:
            self.reset()
            self.run_id = state.run_id
            self.started_at = time.monotonic()
        self.timeline(str(state.phase))
        self.run_status = str(state.run_status)
        self.tokens = state.budget.tokens
        self.model_calls = state.budget.model_calls
        if self.run_status != 'RUNNING':
            self.activity = ''
            self.finished_at = self.finished_at or time.monotonic()
        else:
            self.finished_at = None
        summary = (f'项目 {state.scope_id} · {label(state.mode)} · {label(state.run_status)} · 本次会话 {self.elapsed()}\n'
                   f'运行 {state.run_id}\n'
                   f'模型调用 {state.budget.model_calls}/{state.budget.max_model_calls}   '
                   f'浏览器动作 {state.budget.browser_actions}/{state.budget.max_browser_actions}   '
                   f'补丁 {state.budget.patches}/{state.budget.max_patches}\n'
                   f'Token 数 {state.budget.tokens:,}   预估成本 ${state.budget.cost_usd:.4f}')
        if state.outcome:
            summary = f'{label(state.outcome)}\n\n'+summary
        self.panel('运行结果' if state.outcome else '运行状态', summary)
        if self.run_status == 'WAITING_APPROVAL':
            self.approval(state.approval_ref, state.patch_hash)

    def input(self, value):
        self.text(f'> {value}')

    def approval(self, approval_ref, patch_hash):
        self.panel('需要审批', f'候选补丁 {compact(patch_hash)}\n'
                   f'批准此一次性动作：/approve {approval_ref}\n'
                   f'拒绝并保留候选产物：/reject {approval_ref}\n'
                   '输入 /diff 查看补丁；只有提交完整审批 ID 后才会执行对应决定。')

    def help(self, commands):
        if self.plain or not self.console.is_terminal or (getattr(self.console, '_width', self.console.width) or self.console.width) < 80:
            for command in commands:
                self.text(f'{command.usage}\n  {command.help}')
            return
        table = Table(box=None, show_header=False, padding=(0, 2))
        table.add_column(style=ACCENT, overflow='fold', ratio=3)
        table.add_column(overflow='fold', ratio=2)
        for command in commands:
            table.add_row(command.usage, command.help)
        self.console.print(table)

    def event(self, event, *, replay=False):
        kind = event['type']
        payload = redact(event.get('payload') or {})
        phase = str(event.get('phase', self.phase or 'PREPARE'))
        if not replay:
            if self.started_at is None:
                self.started_at = time.monotonic()
            self.timeline(phase)
            if kind == 'state.changed':
                self.run_status = str(payload.get('status', self.run_status))
                if self.run_status != 'RUNNING':
                    self.activity = ''
                    self.finished_at = time.monotonic()
                else:
                    self.finished_at = None
            elif kind == 'model.started':
                self.model = str(payload.get('model', self.model))
                self.model_calls += 1
                self.activity = '等待模型响应'
            elif kind == 'tool.started':
                self.activity = self.tool_name(payload)
            elif kind in {'model.called', 'model.error.persisted', 'tool.completed', 'run.error', 'run.finished'}:
                self.activity = ''
            if kind == 'model.usage':
                self.tokens += int(payload.get('usage', {}).get('total_tokens', 0))
            if kind in {'run.error', 'run.finished'}:
                if kind == 'run.error':
                    self.run_status = str(payload.get('status', 'FAILED'))
                elif self.run_status not in {'CANCELLED', 'FAILED', 'COMPLETED'}:
                    self.run_status = 'COMPLETED'
                self.finished_at = time.monotonic()
            elif kind == 'run.started':
                self.run_status = 'RUNNING'
        # Normal runtime output is intentionally minimal. Detailed event
        # payloads remain available through explicit diagnostic commands.
        if not replay:
            if kind == 'state.changed':
                status = str(payload.get('status', self.run_status))
                status_changed = status != self._last_status
                self._last_status = status
                if status_changed:
                    self.text(f'[{label(phase)}] {label(status)}')
                return
            if kind in {'model.response.persisted', 'model.called'}:
                if kind == 'model.response.persisted':
                    output = self._response_text(payload)
                    if output:
                        self.text(output)
                return
            if kind == 'tool.started':
                operation_id = str(payload.get('operation_id', ''))
                if operation_id.rsplit(':', 1)[-1] == 'sandbox.start':
                    self.text(f'启动沙箱：{operation_id}')
                return
            if kind == 'gate.decided':
                patch_hash = payload.get('patch_hash')
                evidence_ref = payload.get('evidence_ref')
                if not patch_hash and isinstance(evidence_ref, str) and evidence_ref.endswith('.json'):
                    candidate = evidence_ref[:-5]
                    if re.fullmatch(r'[a-f0-9]{64}', candidate):
                        patch_hash = candidate
                lines = [label(kind), f"是否通过：{'是' if payload.get('passed') else '否'}"]
                if patch_hash:
                    lines.append(f'补丁哈希：{compact(patch_hash)}')
                if evidence_ref:
                    lines.append(f'证据：{readable(evidence_ref)}')
                self.text('\n'.join(lines))
                return
            if kind in {'run.started', 'run.finished', 'run.error'}:
                status = ('RUNNING' if kind == 'run.started' else
                          str(payload.get('status', 'FAILED')) if kind == 'run.error' else 'COMPLETED')
                if status != self._last_status:
                    self._last_status = status
                    self.text(f'[{label(phase)}] {label(status)}')
                return
            return
        marker = '>' if self.plain else ('↳' if replay else '●')
        title = Text(clean(f'  {marker} {label(kind)}'), style=ACCENT)
        title.append(clean(f"  {label(phase)} · #{event.get('seq', '?')}"), style='dim')
        self.console.print(title)
        if kind in {'tool.started', 'tool.completed'}:
            details = {'工具': self.tool_name(payload)}
            data = payload.get('intent') if kind == 'tool.started' else payload.get('receipt')
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, (str, int, float, bool)) or key.endswith('_refs'):
                        details[label(key)] = readable(value)
            elif data is not None:
                details['摘要'] = readable(data)
        elif kind.startswith('model.'):
            selected = ('model', 'schema', 'logical_call', 'attempt', 'request_ref', 'response_ref',
                        'error_ref', 'model_revision', 'finish_reason', 'usage', 'summary', 'action', 'evidence_refs')
            details = {label(key): readable(payload[key]) for key in selected if key in payload}
        else:
            details = readable(payload)
        for key, value in details.items():
            shown = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            shown = compact(shown)
            if len(shown) > 1200 and not (str(key).endswith('_ref') or '文件' in str(key)):
                shown = shown[:1200]+'…（完整内容见证据文件）'
            self.console.print(Text(f'    {clean(key)}  {shown}'), soft_wrap=self.plain or not self.console.is_terminal)

    def _response_text(self, payload):
        value = payload.get('response')
        if value is None and self.artifacts and payload.get('response_ref') and self.scope and self.run_id:
            try:
                value = self.artifacts.json(self.scope, self.run_id, payload['response_ref'])
            except Exception:
                return ''
        body = value.get('body') if isinstance(value, dict) else value
        if isinstance(body, dict):
            choices = body.get('choices') or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get('message') or {}
                value = message.get('content')
        if isinstance(value, list):
            value = '\n'.join(str(item.get('text', '')) if isinstance(item, dict) else str(item) for item in value)
        if not isinstance(value, str):
            return ''
        return clean(value)

    @staticmethod
    def tool_name(payload):
        name = str(payload.get('tool') or payload.get('operation_id', '工具').rsplit(':', 1)[-1])
        return label(name)

    def diff(self, text):
        if self.plain or not self.console.is_terminal:
            self.text(text or '（无补丁）')
        else:
            self.console.print(Syntax(clean(text or '（无补丁）'), 'diff', word_wrap=True))
