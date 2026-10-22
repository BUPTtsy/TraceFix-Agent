"""Shared local tools with explicit delegation and container shell boundaries."""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from tracefix.execution.platforms import bind_mount, container_user, is_link
from tracefix.execution.workspace import CandidateRejected
from tracefix.execution.runner import DockerRunner, process
from tracefix.runtime.contracts import Contract, Phase, RunState, digest, new_id
from tracefix.tools.core import ToolRejected, ToolResult
from tracefix.tools.effects import EffectBoundaryError, apply_effect_receipt, run_effect
from tracefix.tools.fingerprints import diff_fingerprint, record_tool_action
from tracefix.workers.locks import WorkspaceMutex


class ReadInput(Contract):
    file_path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=2000)
    pages: list[int] = Field(default_factory=list, max_length=20)
    expected_content_version: str | None = None


class WriteInput(Contract):
    file_path: str = Field(min_length=1)
    content: str


class EditBlock(Contract):
    old_string: str = Field(min_length=1)
    new_string: str


class EditInput(Contract):
    file_path: str = Field(min_length=1)
    old_string: str | None = Field(default=None, min_length=1)
    new_string: str = ''
    replace_all: bool = False
    edits: list[EditBlock] = Field(default_factory=list, max_length=8)
    expected_overlay_revision: int | None = Field(default=None, ge=0)
    expected_overlay_hash: str | None = Field(default=None, min_length=1)

    @model_validator(mode='after')
    def valid_shape(self):
        if self.edits:
            if not self.file_path or self.old_string is not None or self.replace_all:
                raise ValueError('局部批量编辑必须提供 file_path、edits，且禁止 replace_all')
        elif not self.file_path or self.old_string is None:
            raise ValueError('Edit 必须提供 file_path/old_string，或提供局部 edits')
        if self.local_mode and self.replace_all:
            raise ValueError('版本绑定局部编辑禁止 replace_all')
        return self

    @property
    def local_mode(self):
        return bool(self.edits) or self.expected_overlay_hash is not None or self.expected_overlay_revision is not None


class GlobInput(Contract):
    pattern: str = Field(min_length=1)
    path: str | None = None


class GrepInput(Contract):
    pattern: str = Field(min_length=1, max_length=2000)
    path: str | None = None
    glob: str = '**/*'
    type: str | None = None
    multiline: bool = False
    case_sensitive: bool = True
    output_mode: Literal['content', 'files_with_matches', 'count'] = 'files_with_matches'
    head_limit: int = Field(default=100, ge=1, le=2000)


class NotebookEditInput(Contract):
    notebook_path: str = Field(min_length=1)
    cell_number: int = Field(ge=0)
    new_source: str = ''
    cell_type: Literal['code', 'markdown', 'raw'] = 'code'
    edit_mode: Literal['replace', 'insert', 'delete'] = 'replace'


class BashInput(Contract):
    command: str = Field(min_length=1, max_length=40000)
    timeout: int = Field(default=30, ge=1, le=120)
    description: str = Field(min_length=1, max_length=1000)


LOCAL_TOOL_ALIASES = {
    'Read': {'Read', 'file.read', 'code.read'},
    'Glob': {'Glob', 'file.read', 'code.read'},
    'Grep': {'Grep', 'file.read', 'code.read'},
    'Write': {'Write', 'file.write', 'code.write'},
    'Edit': {'Edit', 'file.write', 'code.write'},
    'NotebookEdit': {'NotebookEdit', 'file.write', 'code.write'},
    'Bash': {'Bash', 'shell', 'shell.readonly', 'shell.patch'},
    'RunTests': {'RunTests'},
}


def local_tool_context(engine, context):
    """归一化委派权限；显式空工具集合拒绝全部，GUI 权限以父引擎授权为准。"""
    context = dict(context or {})
    if getattr(engine, 'subagent_depth', 0) == 1:
        context.update(worker_depth=1,
            worker_write_enabled=getattr(engine, 'worker_write_enabled', False),
            worker_allowed_files=getattr(engine, 'worker_allowed_files', []),
            worker_writable_files=getattr(engine, 'worker_writable_files', []),
            worker_allowed_tools=getattr(engine, 'worker_allowed_tools', []),
            worker_shell_mode=getattr(engine, 'worker_shell_mode', 'disabled'))
    if context.get('worker_depth') == 1:
        grants = [set(context[name]) for name in ('allowed_tools', 'worker_allowed_tools')
                  if name in context]
        tools = [name for name, aliases in LOCAL_TOOL_ALIASES.items()
                 if grants and all(grant & aliases for grant in grants)]
        shell_mode = context.get('worker_shell_mode', 'disabled')
        if shell_mode not in {'readonly', 'patch'} or 'Bash' not in tools:
            shell_mode = 'disabled'
            tools = [name for name in tools if name != 'Bash']
        elif any('shell.readonly' in grant for grant in grants):
            # 只读授权是能力上限，不能在别名归一化时升级成 patch。
            shell_mode = 'readonly'
        context['worker_allowed_tools'] = tools
        context['worker_shell_mode'] = shell_mode
    return context


class LocalTools:
    def __init__(self, engine, context, state=None):
        context = local_tool_context(engine, context)
        self.engine = engine
        self.workspace = engine.workspace
        self.root = self.workspace.root.resolve()
        self.state = state
        self.worker = context.get('worker_depth') == 1
        self.allowed = {str(path).replace('\\', '/') for path in context.get('worker_allowed_files', [])}
        self.writable = ({str(path).replace('\\', '/') for path in context.get('worker_writable_files', [])}
                         if self.worker else None)
        self.worker_allowed_tools = set(context.get('worker_allowed_tools', [])) if self.worker else None
        self.worker_shell_mode = context.get('worker_shell_mode', 'disabled') if self.worker else 'disabled'
        self.write_enabled = (isinstance(state, RunState) and state.mode == 'repair'
                              and state.phase in {Phase.DIAGNOSE, Phase.PATCH}
                              and (not self.worker or context.get('worker_write_enabled') is True))
        self.shell_write_enabled = (self.write_enabled
                                    and (not self.worker or self.worker_shell_mode == 'patch'))
        if self.worker and self.writable - self.allowed:
            raise ToolRejected('子 Agent 可写路径必须属于允许文件集合')
        if not hasattr(self.workspace, '_local_tool_mutex'):
            self.workspace._local_tool_mutex = WorkspaceMutex(self.root)
        self.mutex = self.workspace._local_tool_mutex

    def staged_content(self, relative):
        return self.workspace.staged_content(relative)

    def require_tool(self, name):
        if self.worker and name not in self.worker_allowed_tools:
            raise ToolRejected('Supervisor 未授予该工具权限')

    def require_managed(self):
        store = getattr(self.engine, 'store', None)
        if (not isinstance(self.state, RunState) or store is None
                or any(not callable(getattr(store, name, None))
                       for name in ('begin', 'finish', 'mark_unknown', 'operation_guard'))
                or any(not callable(getattr(self.workspace, name, None))
                       for name in ('staged_content', 'stage', 'stage_many', 'require_repository_snapshot'))):
            raise ToolRejected('本地副作用工具需要有效 RunState、store 和受管 overlay')
        snapshot = self.workspace.require_repository_snapshot()
        if snapshot['scope_id'] != self.state.scope_id:
            raise ToolRejected('工作区快照与 Run scope 不一致')

    def store_content(self, path, content):
        """仅在受管资源 fence 内暂存候选补丁，禁止直接改写磁盘。"""
        self.require_managed()
        with apply_effect_receipt():
            return self.workspace.stage(path.relative_to(self.root).as_posix(), content)

    def path(self, value, *, write=False):
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ToolRejected('文件工具必须使用绝对路径')
        try:
            relative = candidate.relative_to(self.root).as_posix()
        except ValueError as error:
            raise ToolRejected('路径必须位于当前授权工作区') from error
        if self.worker and relative not in self.allowed:
            raise ToolRejected('文件不在 Supervisor 委派范围内')
        if write and (not self.write_enabled or self.worker and relative not in self.writable):
            raise ToolRejected('Supervisor 未授予该文件写权限')
        if write:
            self.require_managed()
            if self.state.mode != 'repair' or self.state.phase not in {Phase.DIAGNOSE, Phase.PATCH}:
                raise ToolRejected('当前 Run 模式或阶段不允许本地写入')
        return self.workspace.path(relative, write=True) if write else self.workspace.path(relative)

    def files(self, directory=None):
        base = Path(directory) if directory else self.root
        if not base.is_absolute() or not base.resolve().is_relative_to(self.root):
            raise ToolRejected('检索目录必须是工作区内的绝对路径')
        candidates = [base] if base.is_file() else base.rglob('*')
        seen = set()
        for candidate in candidates:
            if candidate.is_file():
                try:
                    authorized = self.path(str(candidate))
                    seen.add(authorized)
                    yield authorized
                except PermissionError:
                    continue
                except ToolRejected:
                    continue
        for relative in self.workspace._staged_files:
            candidate = self.root / relative
            if candidate not in seen and (candidate == base or candidate.is_relative_to(base)):
                try:
                    yield self.path(str(candidate))
                except (PermissionError, ToolRejected):
                    continue

    def read(self, arguments):
        self.require_tool('Read')
        path = self.path(arguments.file_path)
        relative = path.relative_to(self.root).as_posix()
        with self.mutex.read_lock(path):
            disk_version = path.stat() if path.exists() else None
            staged = self.staged_content(relative)
            if disk_version is None and staged is None:
                raise FileNotFoundError(str(path))
            baseline = path.read_bytes() if disk_version is not None else b''
            disk_after = path.stat() if path.exists() else None
            version = lambda stat: ((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                                    if stat is not None else None)
            if version(disk_version) != version(disk_after):
                raise ToolRejected('读取期间磁盘文件发生变化，拒绝使用过期源码')
            data = staged if staged is not None else baseline
            if staged != self.staged_content(relative):
                raise ToolRejected('读取期间 overlay 发生变化，拒绝使用过期源码')
        content_version = digest(data)
        if arguments.expected_content_version is not None and arguments.expected_content_version != content_version:
            raise ToolRejected('源码 content_version 已变化，请重新读取')
        source_revision = getattr(self.state, 'source_manifest', '') or ''
        metadata = {'path': str(path), 'relative_path': relative,
                    'before_hash': digest(baseline), 'content_version': content_version,
                    'source_revision': source_revision, 'overlay': staged is not None,
                    'truncated': False, 'offset': arguments.offset}
        if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
            return {**metadata, 'type': 'image', 'mime_type': mimetypes.guess_type(path)[0],
                    'base64': base64.b64encode(data).decode('ascii')}
        if path.suffix.lower() == '.pdf':
            import pypdf
            document = pypdf.PdfReader(io.BytesIO(data))
            pages = arguments.pages or list(range(1, min(len(document.pages), 20) + 1))
            if any(page < 1 or page > len(document.pages) for page in pages):
                raise ToolRejected('PDF 页码超出范围')
            return {**metadata, 'type': 'pdf', 'total_pages': len(document.pages),
                    'pages': [{'page': page, 'text': document.pages[page - 1].extract_text()}
                              for page in pages]}
        text = data.decode('utf-8')
        if path.suffix == '.ipynb':
            notebook = json.loads(text)
            cells = notebook.get('cells', [])
            return {**metadata, 'type': 'notebook', 'total_cells': len(cells),
                    'cells': cells[arguments.offset - 1:arguments.offset - 1 + arguments.limit]}
        lines = text.splitlines()
        end_line = min(len(lines), arguments.offset + arguments.limit - 1)
        metadata.update(total_lines=len(lines), start_line=arguments.offset,
                        end_line=end_line, truncated=arguments.offset > 1 or end_line < len(lines),
                        next_offset=end_line + 1 if end_line < len(lines) else None)
        metadata['content'] = '\n'.join(
            f'{index + 1}\t{line}' for index, line in enumerate(lines)
            if arguments.offset <= index + 1 < arguments.offset + arguments.limit)
        relative = path.relative_to(self.root).as_posix()
        metadata.update(path=str(path), before_hash=digest(baseline),
                disk_before_hash=digest(baseline), overlay_hash=digest(data),
                overlay_revision=self.workspace.file_overlay_revision(relative),
                line_ending='CRLF' if b'\r\n' in data else 'LF',
                raw_content=''.join(text.splitlines(keepends=True)[
                    arguments.offset - 1:arguments.offset - 1 + arguments.limit]),
                offset=arguments.offset)
        return metadata

    def write(self, arguments):
        self.require_tool('Write')
        path = self.path(arguments.file_path, write=True)
        with self.mutex.write_lock(path):
            return self.store_content(path, arguments.content)

    async def write_effect(self, arguments, tool_name):
        if tool_name not in {'Write', 'Edit', 'NotebookEdit'}:
            raise ToolRejected('未知的本地写请求')
        self.require_tool(tool_name)
        path_value = arguments.file_path if tool_name != 'NotebookEdit' else arguments.notebook_path
        path = self.path(path_value, write=True)
        relative = path.relative_to(self.root).as_posix()
        with self.mutex.write_lock(path):
            if tool_name == 'Edit' and arguments.local_mode:
                with apply_effect_receipt():
                    return self._local_edit(relative, arguments)
            staged = self.staged_content(relative)
            if not path.exists() and staged is None and tool_name != 'Write':
                raise ToolRejected('编辑不存在文件前应先使用 Write 创建 overlay')
            baseline = path.read_bytes() if path.exists() else None
            content = staged if staged is not None else (baseline or b'')
            with apply_effect_receipt():
                pass
            prepared = await run_effect('local.prepare', {
                'tool': tool_name,
                'content': content.decode('utf-8'),
                'arguments': arguments.model_dump(mode='json'),
                'cell_id': new_id('cell')}, timeout_s=45)
            if digest(content) != prepared['before_hash']:
                raise ToolRejected('本地写入基线在执行期间变化，拒绝回写')
            with apply_effect_receipt():
                current = self.staged_content(relative)
                path = self.path(path_value, write=True)
                disk = path.read_bytes() if path.exists() else None
                if current != staged or disk != baseline:
                    raise ToolRejected('本地写入基线在执行期间变化，拒绝回写')
                return self.workspace.stage(relative, prepared['content'])

    def edit(self, arguments):
        self.require_tool('Edit')
        path = self.path(arguments.file_path, write=True)
        with self.mutex.write_lock(path):
            relative = path.relative_to(self.root).as_posix()
            staged = self.staged_content(relative)
            text = (staged if staged is not None else path.read_bytes()).decode('utf-8')
            if arguments.local_mode:
                with apply_effect_receipt():
                    return self._local_edit(relative, arguments)
            count = text.count(arguments.old_string)
            if not count or count != 1 and not arguments.replace_all:
                raise ToolRejected('old_string 必须存在且唯一；多处替换需指定 replace_all')
            return self.store_content(path, text.replace(arguments.old_string, arguments.new_string,
                                                        -1 if arguments.replace_all else 1))

    def _local_edit(self, relative, arguments):
        edits = ([(block.old_string, block.new_string) for block in arguments.edits]
                 if arguments.edits else [(arguments.old_string, arguments.new_string)])
        result = self.workspace.stage_local_edit(relative, edits,
            expected_overlay_revision=arguments.expected_overlay_revision,
            expected_overlay_hash=arguments.expected_overlay_hash)
        result = self.workspace.bind_staged_candidate(result, self.state, self.engine.artifacts)
        self.engine.store.save(self.state)
        return result

    def glob(self, arguments):
        self.require_tool('Glob')
        base = Path(arguments.path) if arguments.path else self.root
        paths = [path for path in self.files(str(base))
                 if path.relative_to(base).match(arguments.pattern)
                 or arguments.pattern.startswith('**/') and path.relative_to(base).match(arguments.pattern[3:])]
        paths.sort(key=lambda path: (-path.stat().st_mtime_ns if path.exists() else 0, str(path)))
        return {'files': [str(path) for path in paths]}

    def grep(self, arguments):
        self.require_tool('Grep')
        extensions = {'ts': {'.ts', '.tsx'}, 'js': {'.js', '.jsx', '.mjs'},
                      'py': {'.py'}, 'json': {'.json', '.ipynb'}}
        executable = shutil.which('rg')
        paths = [path for path in self.files(arguments.path)
                 if (path.match(arguments.glob) or arguments.glob == '**/*')
                 and (not arguments.type or path.suffix in
                      extensions.get(arguments.type, {'.' + arguments.type}))]
        if not paths:
            return {'matches': [], 'truncated': False, 'metadata': {},
                    'source_revision': getattr(self.state, 'source_manifest', '') or ''}
        records = []
        disk_versions = {}
        with self.mutex.workspace_read():
            for path in paths:
                relative = path.relative_to(self.root).as_posix()
                disk_version = path.stat() if path.exists() else None
                baseline = path.read_bytes() if disk_version is not None else b''
                staged = self.staged_content(relative)
                data = staged if staged is not None else baseline
                disk_after = path.stat() if path.exists() else None
                version = ((disk_version.st_size, disk_version.st_mtime_ns, disk_version.st_ctime_ns)
                           if disk_version is not None else None)
                after_version = ((disk_after.st_size, disk_after.st_mtime_ns, disk_after.st_ctime_ns)
                                 if disk_after is not None else None)
                if version != after_version:
                    raise ToolRejected('Grep 扫描前磁盘文件发生变化，拒绝使用过期源码')
                disk_versions[relative] = version
                records.append((path, relative, baseline, data, staged is not None))
        with tempfile.TemporaryDirectory(prefix='tracefix-grep-') as staging_dir:
            staging_root = Path(staging_dir)
            path_map = {}
            for path, relative, baseline, data, overlay in records:
                staged_path = staging_root / relative
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_bytes(data)
                path_map[str(staged_path.resolve())] = (path, relative, baseline, data, overlay)
            if executable:
                command = [executable, '--json', '--no-follow']
                if arguments.output_mode != 'count':
                    command.extend(['--max-count', str(arguments.head_limit + 1)])
                if arguments.multiline:
                    command.extend(['--multiline', '--multiline-dotall'])
                if not arguments.case_sensitive:
                    command.append('--ignore-case')
                command.extend(['--', arguments.pattern, *[str(staging_root / relative) for _, relative, *_ in records]])
            else:
                command = [sys.executable, '-c', 'pass']
            with self.mutex.workspace_read():
                response = subprocess.run(command, capture_output=True, timeout=30,
                                          encoding='utf-8', errors='replace')
                for path, relative, baseline, data, overlay in records:
                    current_path = self.path(str(path))
                    disk_after = current_path.stat() if current_path.exists() else None
                    version = ((disk_after.st_size, disk_after.st_mtime_ns, disk_after.st_ctime_ns)
                               if disk_after is not None else None)
                    disk = current_path.read_bytes() if current_path.exists() else b''
                    if (version != disk_versions[relative] or disk != baseline
                            or self.staged_content(relative) != (data if overlay else None)):
                        raise ToolRejected('Grep 扫描期间源码发生变化，拒绝使用过期命中')
        if response.returncode not in {0, 1}:
            raise ToolRejected(response.stderr[:2000])
        if executable:
            scan_lines = response.stdout.splitlines()
        else:
            flags = 0 if arguments.case_sensitive else re.IGNORECASE
            if arguments.multiline:
                flags |= re.MULTILINE | re.DOTALL
            try:
                matcher = re.compile(arguments.pattern, flags)
            except re.error as error:
                raise ToolRejected(f'Grep 模式无效：{error}') from error
            scan_lines = []
            for path, relative, baseline, data, overlay in records:
                staged_path = staging_root / relative
                text = data.decode('utf-8', errors='replace')
                if arguments.multiline:
                    matches = list(matcher.finditer(text))
                    if arguments.output_mode != 'count':
                        matches = matches[:arguments.head_limit + 1]
                    for match in matches:
                        line_number = text.count('\n', 0, match.start()) + 1
                        match_text = match.group(0)
                        scan_lines.append(json.dumps({'type': 'match', 'data': {
                            'path': {'text': str(staged_path)}, 'line_number': line_number,
                            'lines': {'text': match_text}}}, ensure_ascii=False))
                else:
                    matched = 0
                    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
                        if not matcher.search(line):
                            continue
                        matched += 1
                        scan_lines.append(json.dumps({'type': 'match', 'data': {
                            'path': {'text': str(staged_path)}, 'line_number': line_number,
                            'lines': {'text': line}}}, ensure_ascii=False))
                        if arguments.output_mode != 'count' and matched > arguments.head_limit:
                            break
        results = []
        counts = {}
        ranges = {}
        source_revision = getattr(self.state, 'source_manifest', '') or ''
        for line in scan_lines:
            entry = json.loads(line)
            if entry['type'] != 'match':
                continue
            match = entry['data']
            path = match['path'].get('text')
            if not path:
                continue
            record = path_map.get(str(Path(path).resolve()))
            if record is None:
                continue
            original, relative, baseline, data, overlay = record
            path = str(original)
            content_version = digest(data)
            counts[path] = counts.get(path, 0) + 1
            line_number = match['line_number']
            match_text = match['lines'].get('text', '').rstrip('\r\n')
            end_line = line_number + max(0, match_text.count('\n'))
            current_range = ranges.setdefault(path, [line_number, end_line])
            current_range[0] = min(current_range[0], line_number)
            current_range[1] = max(current_range[1], end_line)
            if arguments.output_mode == 'files_with_matches':
                if path not in results:
                    results.append(path)
            elif arguments.output_mode == 'content':
                results.append({'path': path, 'line': match['line_number'],
                                'content': match_text, 'relative_path': relative,
                                'start_line': line_number, 'end_line': end_line,
                                'content_version': content_version,
                                'source_revision': source_revision, 'overlay': overlay,
                                'truncated': False})
        metadata = {}
        for path, relative, baseline, data, overlay in records:
            path = str(path)
            if path in counts:
                metadata[path] = {'relative_path': relative, 'start_line': ranges[path][0],
                                  'end_line': ranges[path][1], 'content_version': digest(data),
                                  'source_revision': source_revision, 'overlay': overlay,
                                  'before_hash': digest(baseline)}
        if arguments.output_mode == 'count':
            results = [{'path': path, 'count': count, **metadata[path], 'truncated': False}
                       for path, count in counts.items()]
        return {'matches': results[:arguments.head_limit], 'truncated': len(results) > arguments.head_limit,
                'metadata': metadata, 'source_revision': source_revision}

    def notebook_edit(self, arguments):
        self.require_tool('NotebookEdit')
        path = self.path(arguments.notebook_path, write=True)
        with self.mutex.write_lock(path):
            staged = self.staged_content(path.relative_to(self.root).as_posix())
            notebook = json.loads(staged if staged is not None else path.read_bytes())
            cells = notebook['cells']
            index = arguments.cell_number
            if index > len(cells) or index == len(cells) and arguments.edit_mode != 'insert':
                raise ToolRejected('cell_number 超出范围')
            if arguments.edit_mode == 'delete':
                cells.pop(index)
            else:
                cell = {'cell_type': arguments.cell_type, 'metadata': {},
                        'source': arguments.new_source.splitlines(keepends=True)}
                if arguments.cell_type == 'code':
                    cell.update(outputs=[], execution_count=None)
                if arguments.edit_mode == 'insert':
                    cell['id'] = new_id('cell')
                    cells.insert(index, cell)
                else:
                    cell['metadata'] = cells[index].get('metadata', {})
                    if 'id' in cells[index]:
                        cell['id'] = cells[index]['id']
                    cells[index] = cell
            return self.store_content(path, json.dumps(notebook, ensure_ascii=False, indent=1) + '\n')

    async def bash(self, arguments, call_id=None):
        self.require_tool('Bash')
        if self.worker and self.worker_shell_mode == 'disabled':
            raise ToolRejected('Supervisor 未授予 Bash 权限')
        self.require_managed()
        with apply_effect_receipt():
            pass
        result = await self.sandbox_run(arguments.command, writable=self.shell_write_enabled,
                                        timeout=arguments.timeout)
        if result['passed']:
            record_tool_action(self.engine, self.state, 'Bash', arguments, result)
        return result

    async def sandbox_run(self, argv_or_command, *, writable, timeout, extra_mounts=(),
                          output_limit=100_000, complete_source=False):
        """在当前 overlay 的临时副本执行命令；绝不回退到宿主执行。"""
        profile = getattr(self.engine, 'profile', None)
        if profile is None:
            raise ToolRejected('命令执行需要配置 Docker sandbox 镜像')
        runner = getattr(self.engine, 'runner', None)
        ready = isinstance(runner, DockerRunner) and runner._started
        mounts = list(extra_mounts)
        excluded = set()
        if ready:
            mounts.append(f'type=volume,src={runner.dependency_volume},dst=/workspace/node_modules,readonly')
            excluded.update({'node_modules', 'dist'})
        for mount_spec in mounts:
            fields = next(csv.reader([mount_spec]))
            destination = next((field.split('=', 1)[1] for field in fields
                                if field.startswith(('dst=', 'destination=', 'target='))), '')
            if destination.startswith('/workspace/'):
                excluded.add(destination.removeprefix('/workspace/').strip('/'))

        def mounted(relative):
            return any(relative == item or relative.startswith(item + '/') for item in excluded)

        name = (runner.name + '-shell-' + new_id('shell').removeprefix('shell_')[:12]
                if ready else new_id('tf-shell'))
        with tempfile.TemporaryDirectory(prefix='tracefix-shell-') as folder:
            staging = Path(folder)
            staging.chmod(0o777 if writable else 0o755)
            originals = {}
            baselines = {}
            overlays = {}
            with self.mutex.workspace_write():
                view = {'overlay_revision': self.workspace.overlay_revision,
                        'diff_fingerprint': diff_fingerprint(self.workspace, self.state)}
                if self.state.repair_mode == 'hybrid':
                    from tracefix.runtime.repair_progress import normalized_diff_fingerprint
                    view['normalized_diff_fingerprint'] = normalized_diff_fingerprint(self.workspace, self.state)
                sources = {source.relative_to(self.root).as_posix(): source for source in self.files()}
                if complete_source:
                    if (not getattr(self.engine, 'worker_attempt', False) or writable
                            or not isinstance(argv_or_command, list)
                            or argv_or_command not in [profile.commands.get(target)
                                                       for target in ('unit', 'static', 'build')]):
                        raise ToolRejected('完整 Worker 测试源码仅供 Profile 固定只读自测使用')
                    sources = {relative: self.workspace.path(relative)
                               for relative in self.workspace.require_repository_snapshot()['files']}
                created = {relative for relative, entry in self.workspace._created_entries.items()
                           if entry['kind'] == 'file'}
                for relative in set(self.workspace._staged_files) | created:
                    if relative not in sources:
                        try:
                            sources[relative] = (self.workspace.path(relative) if complete_source
                                                 else self.path(str(self.root / relative)))
                        except (PermissionError, ToolRejected):
                            continue
                for relative, source in sources.items():
                    if mounted(relative):
                        continue
                    staged = self.staged_content(relative)
                    overlays[relative] = staged
                    originals[relative] = staged if staged is not None else source.read_bytes()
                    baselines[relative] = source.read_bytes() if source.exists() else None
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(originals[relative])
                    target.chmod(0o666)
            for relative in excluded:
                (staging / relative).mkdir(parents=True, exist_ok=True)
            if writable:
                for directory in staging.rglob('*'):
                    if directory.is_dir():
                        directory.chmod(0o777)
            mount = io.StringIO()
            fields = next(csv.reader([bind_mount(staging)]))
            fields = [field for field in fields if field != 'readonly']
            fields[fields.index('dst=/app')] = 'dst=/workspace'
            if not writable:
                fields.append('readonly')
            csv.writer(mount, lineterminator='').writerow(fields)
            command = ['docker', 'run', '--rm', '--name', name, '--network=none',
                       '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                       '--pids-limit=128', '--memory=1g', '--cpus=2', '--user', container_user(),
                       '--tmpfs', '/tmp:rw,nosuid,size=128m,mode=1777', '--mount', mount.getvalue()]
            for dependency_mount in mounts:
                command += ['--mount', dependency_mount]
            if ready:
                command += ['--tmpfs', '/workspace/dist:rw,nosuid,size=128m,mode=1777']
            image = (runner.resolved_image_ids or {}).get('app', profile.image) if ready else profile.image
            if isinstance(argv_or_command, str):
                command += ['--workdir', '/workspace', '--entrypoint', '/bin/bash', image,
                            '--noprofile', '--norc', '-c', argv_or_command]
            else:
                if not argv_or_command or any(not isinstance(value, str) or not value
                                              for value in argv_or_command):
                    raise ToolRejected('沙箱命令必须是非空 argv')
                command += ['--workdir', '/workspace', '--entrypoint', argv_or_command[0], image,
                            *argv_or_command[1:]]
            try:
                if output_limit == 100_000:
                    result = await process(command, timeout)
                else:
                    result = await process(command, timeout, output_limit=output_limit)
                result = {**result, **view}
            finally:
                await process(['docker', 'rm', '-f', name], 15)
            updated = {}
            for target in staging.rglob('*'):
                relative = target.relative_to(staging).as_posix()
                if mounted(relative):
                    continue
                if is_link(target):
                    raise ToolRejected('Bash 输出不能包含符号链接')
                if target.is_file():
                    updated[relative] = target.read_bytes()
            removed = set(originals) - set(updated)
            if removed:
                raise ToolRejected('Bash 删除文件不会回写工作区；请使用文件编辑工具')
            changes = {relative: data for relative, data in updated.items()
                       if originals.get(relative) != data}
            if not result['passed']:
                return {**result, 'files_changed': [], 'discarded_changes': list(changes),
                        'cwd': '/workspace'}
            if not writable:
                return {**result, 'files_changed': [], 'discarded_changes': list(changes),
                        'cwd': '/workspace'}
            targets = {relative: self.path(str(self.root / relative), write=True) for relative in changes}
            with self.mutex.workspace_write():
                for relative, target in targets.items():
                    current = target.read_bytes() if target.exists() else None
                    if (current != baselines.get(relative)
                            or self.staged_content(relative) != overlays.get(relative)):
                        raise ToolRejected('Bash 执行期间文件已变化，拒绝覆盖并发修改')
                    changes[relative].decode('utf-8')
                if changes:
                    with apply_effect_receipt():
                        self.require_managed()
                        self.workspace.stage_many({relative: data.decode('utf-8')
                                                   for relative, data in changes.items()})
            return {**result, 'files_changed': list(changes), 'cwd': '/workspace', 'staged': True}


def register_local_tools(engine, state, context, bind, *, track_repair_progress=False):
    tools = LocalTools(engine, context, state)
    definitions = [
        ('Read', ReadInput, tools.read, False,
         '读取当前授权工作区内的绝对路径。文本默认最多 2000 行，可用 offset/limit 分页；支持图片、PDF 页文本、Jupyter cell。倾向并行读取多个文件。'),
        ('Glob', GlobInput, tools.glob, False, '按文件名 glob（如 **/*.ts）列出授权文件的绝对路径，按修改时间降序排序；path 为绝对目录，省略时从当前工作区根搜索。可定位页面组件供 Read 核对。'),
        ('Grep', GrepInput, tools.grep, False, '正则内容搜索；支持 glob/type 过滤、multiline 和 content/files_with_matches/count 三种输出。path 为工作区内绝对路径，省略时搜索当前工作区；可搜索页面文案、可访问名称和事件绑定辅助识别可操作元素。'),
        ('Write', WriteInput, tools.write, True, '创建或覆盖授权绝对路径的文件，仅暂存到 overlay；精确替换优先 Edit。'),
        ('Edit', EditInput, tools.edit, True, '旧模式支持单块精确替换；局部模式用 edits 在同一共同基线提交多个唯一、不重叠块，带 expected_overlay_revision/hash，失败返回可纠正错误，不自动模糊匹配或 replace_all。'),
        ('NotebookEdit', NotebookEditInput, tools.notebook_edit, True, '按零起始 cell_number 替换、插入或删除 Jupyter cell；保留 notebook 元数据，代码修改清空旧输出。'),
    ]
    for name, model, callback, writing, description in definitions:
        if tools.worker and tools.worker_allowed_tools is not None and name not in tools.worker_allowed_tools:
            continue
        if writing and not tools.write_enabled:
            continue
        async def handler(arguments, call_id, callback=callback, name=name, writing=writing):
            previous_revision = tools.workspace.overlay_revision
            write_completed = False

            def require_unchanged_overlay(error):
                if writing and tools.workspace.overlay_revision != previous_revision:
                    raise EffectBoundaryError('overlay 已改变，后续处理失败，需核对操作结果') from error

            try:
                if writing:
                    result = await tools.write_effect(arguments, name)
                    write_completed = True
                    record_tool_action(engine, state, name, arguments, result)
                    return result
                result = await asyncio.to_thread(callback, arguments)
                if name == 'Read' and track_repair_progress and state.repair_mode == 'hybrid':
                    from tracefix.runtime.repair_progress import observe_repair
                    observe_repair(engine, state, 'read', path=result['relative_path'])
                return result
            except ToolRejected as error:
                # prepare 检测到其他写入者的版本变化时，当前操作仍是确定未执行。
                if write_completed:
                    raise EffectBoundaryError('写入已完成，后续处理失败，需核对操作结果') from error
                raise
            except CandidateRejected as error:
                require_unchanged_overlay(error)
                return ToolResult(call_id=call_id, name=name, isError=True,
                    result=error.details, error={**error.details, 'message': str(error), 'executed': False})
            except PermissionError as error:
                require_unchanged_overlay(error)
                if getattr(error, 'error_code', None) != 'protected_path':
                    raise ToolRejected(str(error)) from error
                details = {'error_code': error.error_code, **error.details}
                return ToolResult(call_id=call_id, name=name, isError=True,
                    result=details, error={**details, 'message': str(error), 'executed': False})
            except (ValueError, FileNotFoundError) as error:
                require_unchanged_overlay(error)
                raise ToolRejected(str(error)) from error
        bind(name, description, model, handler, phases=set(Phase),
             side_effect='write' if writing else 'read', parallel_safe=not writing)
    if tools.worker and tools.worker_allowed_tools is not None and not ({'Bash', 'shell', 'shell.readonly', 'shell.patch'} & tools.worker_allowed_tools):
        return
    if tools.worker and tools.worker_shell_mode == 'disabled':
        return
    async def bash_handler(arguments, call_id):
        try:
            return await tools.bash(arguments, call_id)
        except PermissionError as error:
            if getattr(error, 'error_code', None) != 'protected_path':
                raise
            details = {'error_code': error.error_code, **error.details}
            return ToolResult(call_id=call_id, name='Bash', isError=True,
                result=details, error={**details, 'message': str(error), 'executed': False})

    bind('Bash', '在独立 Docker Linux sandbox 执行 Bash，工作目录 /workspace；只复制授权文件，禁网、禁宿主访问。'
         '提供 command/description，timeout 为秒（最多 120）。优先用 Read/Glob/Grep/Edit 操作文件。'
         '子 Agent 默认只读，只有 Supervisor 显式启用写权限才回写 writable_files；删除、越权或并发 hash 冲突不回写。'
         '不要 push、后台运行、索取凭据或宣称失败命令成功；检查 exit_code/output。',
         BashInput, bash_handler, phases=set(Phase), side_effect='write', parallel_safe=False,
         timeout_s=150)
