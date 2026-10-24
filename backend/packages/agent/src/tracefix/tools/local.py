"""宿主检测与验收使用的只读本地工具；源码修改只由 Claude Agent SDK 完成。"""
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

from pydantic import Field

from tracefix.execution.platforms import bind_mount, container_user
from tracefix.execution.runner import DockerRunner, process
from tracefix.runtime.contracts import Contract, Phase, RunState, digest, new_id
from tracefix.tools.core import ToolRejected, ToolResult
from tracefix.tools.fingerprints import diff_fingerprint, record_tool_action
from tracefix.workers.locks import WorkspaceMutex


CONTAINER_WORKSPACE = '/workspace'


class ReadInput(Contract):
    file_path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=2000)
    pages: list[int] = Field(default_factory=list, max_length=20)
    expected_content_version: str | None = None


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


class BashInput(Contract):
    command: str = Field(min_length=1, max_length=40000)
    timeout: int = Field(default=30, ge=1, le=120)
    description: str = Field(min_length=1, max_length=1000)


LOCAL_TOOL_ALIASES = {
    'Read': {'Read', 'file.read', 'code.read'},
    'Glob': {'Glob', 'file.read', 'code.read'},
    'Grep': {'Grep', 'file.read', 'code.read'},
    'Bash': {'Bash', 'shell', 'shell.readonly'},
}


def local_tool_context(engine, context):
    """归一化委派权限；显式空工具集合拒绝全部，GUI 权限以父引擎授权为准。"""
    context = dict(context or {})
    if getattr(engine, 'subagent_depth', 0) == 1:
        context.update(worker_depth=1,
            worker_allowed_files=getattr(engine, 'worker_allowed_files', []),
            worker_allowed_tools=getattr(engine, 'worker_allowed_tools', []),
            worker_shell_mode=getattr(engine, 'worker_shell_mode', 'disabled'))
    if context.get('worker_depth') == 1:
        grants = [set(context[name]) for name in ('allowed_tools', 'worker_allowed_tools')
                  if name in context]
        tools = [name for name, aliases in LOCAL_TOOL_ALIASES.items()
                 if grants and all(grant & aliases for grant in grants)]
        shell_mode = context.get('worker_shell_mode', 'disabled')
        if shell_mode != 'readonly' or 'Bash' not in tools:
            shell_mode = 'disabled'
            tools = [name for name in tools if name != 'Bash']
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
        self.worker_allowed_tools = set(context.get('worker_allowed_tools', [])) if self.worker else None
        self.worker_shell_mode = context.get('worker_shell_mode', 'disabled') if self.worker else 'disabled'
        if not hasattr(self.workspace, '_local_tool_mutex'):
            self.workspace._local_tool_mutex = WorkspaceMutex(self.root)
        self.mutex = self.workspace._local_tool_mutex

    def require_tool(self, name):
        if self.worker and name not in self.worker_allowed_tools:
            raise ToolRejected('Supervisor 未授予该工具权限')

    def require_managed(self):
        store = getattr(self.engine, 'store', None)
        if (not isinstance(self.state, RunState) or store is None
                or any(not callable(getattr(store, name, None))
                       for name in ('begin', 'finish', 'mark_unknown', 'operation_guard'))
                or not callable(getattr(self.workspace, 'require_repository_snapshot', None))):
            raise ToolRejected('沙箱命令需要有效 RunState、store 和受管工作区')
        snapshot = self.workspace.require_repository_snapshot()
        if snapshot['scope_id'] != self.state.scope_id:
            raise ToolRejected('工作区快照与 Run scope 不一致')

    def container_path(self, value):
        """把沙箱内的 /workspace/... 映射到宿主工作区根。

        模型在容器里看到的是 /workspace，Windows 上 Path('/workspace/x') 不是绝对路径，
        直接拒绝会让合法的容器路径被误判。映射后的路径仍须通过后续的工作区边界校验。"""
        text = str(value).replace('\\', '/')
        if text == CONTAINER_WORKSPACE or text.startswith(CONTAINER_WORKSPACE + '/'):
            parts = [part for part in text[len(CONTAINER_WORKSPACE):].split('/') if part]
            if '..' in parts:
                raise ToolRejected('路径必须位于当前授权工作区')
            return self.root.joinpath(*parts)
        return Path(value)

    def path(self, value):
        candidate = self.container_path(value)
        if not candidate.is_absolute():
            raise ToolRejected('文件工具必须使用绝对路径')
        try:
            relative = candidate.relative_to(self.root).as_posix()
        except ValueError as error:
            raise ToolRejected('路径必须位于当前授权工作区') from error
        if self.worker and relative not in self.allowed:
            raise ToolRejected('文件不在 Supervisor 委派范围内')
        return self.workspace.path(relative)

    def files(self, directory=None):
        base = self.container_path(directory) if directory else self.root
        if not base.is_absolute() or not base.resolve().is_relative_to(self.root):
            raise ToolRejected('检索目录必须是工作区内的绝对路径')
        candidates = [base] if base.is_file() else base.rglob('*')
        for candidate in candidates:
            if candidate.is_file():
                try:
                    yield self.path(str(candidate))
                except (PermissionError, ToolRejected):
                    continue

    def read(self, arguments):
        self.require_tool('Read')
        path = self.path(arguments.file_path)
        relative = path.relative_to(self.root).as_posix()
        with self.mutex.read_lock(path):
            if not path.exists():
                raise FileNotFoundError(str(path))
            disk_version = path.stat()
            data = path.read_bytes()
            disk_after = path.stat()
            version = lambda stat: (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if version(disk_version) != version(disk_after):
                raise ToolRejected('读取期间磁盘文件发生变化，拒绝使用过期源码')
        content_version = digest(data)
        if arguments.expected_content_version is not None and arguments.expected_content_version != content_version:
            raise ToolRejected('源码 content_version 已变化，请重新读取')
        source_revision = getattr(self.state, 'source_manifest', '') or ''
        metadata = {'path': str(path), 'relative_path': relative,
                    'before_hash': content_version, 'content_version': content_version,
                    'source_revision': source_revision, 'truncated': False, 'offset': arguments.offset}
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
        metadata.update(line_ending='CRLF' if b'\r\n' in data else 'LF',
                raw_content=''.join(text.splitlines(keepends=True)[
                    arguments.offset - 1:arguments.offset - 1 + arguments.limit]),
                offset=arguments.offset)
        return metadata

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
                data = baseline
                disk_after = path.stat() if path.exists() else None
                version = ((disk_version.st_size, disk_version.st_mtime_ns, disk_version.st_ctime_ns)
                           if disk_version is not None else None)
                after_version = ((disk_after.st_size, disk_after.st_mtime_ns, disk_after.st_ctime_ns)
                                 if disk_after is not None else None)
                if version != after_version:
                    raise ToolRejected('Grep 扫描前磁盘文件发生变化，拒绝使用过期源码')
                disk_versions[relative] = version
                records.append((path, relative, baseline, data, False))
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
                    if version != disk_versions[relative] or disk != baseline:
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

    async def bash(self, arguments, call_id=None):
        self.require_tool('Bash')
        if self.worker and self.worker_shell_mode == 'disabled':
            raise ToolRejected('Supervisor 未授予 Bash 权限')
        self.require_managed()
        result = await self.sandbox_run(arguments.command, timeout=arguments.timeout)
        if result['passed']:
            record_tool_action(self.engine, self.state, 'Bash', arguments, result)
        return result

    async def sandbox_run(self, argv_or_command, *, timeout, output_limit=100_000):
        """在授权源码的只读临时副本执行命令；绝不回退到宿主执行，也不回写工作区。"""
        profile = getattr(self.engine, 'profile', None)
        if profile is None:
            raise ToolRejected('命令执行需要配置 Docker sandbox 镜像')
        runner = getattr(self.engine, 'runner', None)
        ready = isinstance(runner, DockerRunner) and runner._started
        mounts = []
        excluded = set()
        if ready:
            mounts.append(f'type=volume,src={runner.dependency_volume},dst=/workspace/node_modules,readonly')
            excluded.update({'node_modules', 'dist'})

        def mounted(relative):
            return any(relative == item or relative.startswith(item + '/') for item in excluded)

        name = (runner.name + '-shell-' + new_id('shell').removeprefix('shell_')[:12]
                if ready else new_id('tf-shell'))
        with tempfile.TemporaryDirectory(prefix='tracefix-shell-') as folder:
            staging = Path(folder)
            staging.chmod(0o755)
            with self.mutex.workspace_read():
                view = {'diff_fingerprint': diff_fingerprint(self.workspace, self.state)}
                for source in self.files():
                    relative = source.relative_to(self.root).as_posix()
                    if mounted(relative):
                        continue
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    target.chmod(0o644)
            for relative in excluded:
                (staging / relative).mkdir(parents=True, exist_ok=True)
            mount = io.StringIO()
            fields = next(csv.reader([bind_mount(staging)]))
            fields = [field for field in fields if field != 'readonly']
            fields[fields.index('dst=/app')] = 'dst=/workspace'
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
            return {**result, 'files_changed': [], 'cwd': '/workspace'}


def register_local_tools(engine, state, context, bind):
    tools = LocalTools(engine, context, state)
    definitions = [
        ('Read', ReadInput, tools.read,
         '读取当前授权工作区内的绝对路径。文本默认最多 2000 行，可用 offset/limit 分页；支持图片、PDF 页文本、Jupyter cell。倾向并行读取多个文件。'),
        ('Glob', GlobInput, tools.glob, '按文件名 glob（如 **/*.ts）列出授权文件的绝对路径，按修改时间降序排序；path 为绝对目录，省略时从当前工作区根搜索。可定位页面组件供 Read 核对。'),
        ('Grep', GrepInput, tools.grep, '正则内容搜索；支持 glob/type 过滤、multiline 和 content/files_with_matches/count 三种输出。path 为工作区内绝对路径，省略时搜索当前工作区；可搜索页面文案、可访问名称和事件绑定辅助识别可操作元素。'),
    ]
    for name, model, callback, description in definitions:
        if tools.worker and tools.worker_allowed_tools is not None and name not in tools.worker_allowed_tools:
            continue
        async def handler(arguments, call_id, callback=callback):
            try:
                return await asyncio.to_thread(callback, arguments)
            except ToolRejected:
                raise
            except PermissionError as error:
                if getattr(error, 'error_code', None) != 'protected_path':
                    raise ToolRejected(str(error)) from error
                details = {'error_code': error.error_code, **error.details}
                return ToolResult(call_id=call_id, name=name, isError=True,
                    result=details, error={**details, 'message': str(error), 'executed': False})
            except (ValueError, FileNotFoundError) as error:
                raise ToolRejected(str(error)) from error
        bind(name, description, model, handler, phases=set(Phase), side_effect='read', parallel_safe=True)
    if tools.worker and tools.worker_allowed_tools is not None and not ({'Bash', 'shell', 'shell.readonly'} & tools.worker_allowed_tools):
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

    bind('Bash', '在独立 Docker Linux sandbox 只读执行 Bash，工作目录 /workspace；只复制授权文件，禁网、禁宿主访问。'
         '提供 command/description，timeout 为秒（最多 120）。优先用 Read/Glob/Grep 读取文件。'
         '命令对副本的改动一律丢弃，不回写工作区；源码修改只由 Claude Agent SDK 完成。'
         '不要 push、后台运行、索取凭据或宣称失败命令成功；检查 exit_code/output。',
         BashInput, bash_handler, phases=set(Phase), side_effect='write', parallel_safe=False,
         timeout_s=150)
