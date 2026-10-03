"""Shared local tools with explicit delegation and container shell boundaries."""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from tracefix.execution.platforms import container_user, is_link
from tracefix.execution.runner import process
from tracefix.runtime.contracts import Contract, Phase, digest, new_id
from tracefix.runtime.tools import ToolRejected
from tracefix.runtime.effects import apply_effect_receipt, run_effect
from tracefix.workers.locks import WorkspaceMutex


class ReadInput(Contract):
    file_path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1, le=2000)
    pages: list[int] = Field(default_factory=list, max_length=20)


class WriteInput(Contract):
    file_path: str = Field(min_length=1)
    content: str


class EditInput(Contract):
    file_path: str = Field(min_length=1)
    old_string: str = Field(min_length=1)
    new_string: str
    replace_all: bool = False


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


class LocalTools:
    def __init__(self, engine, context, state=None):
        self.engine = engine
        self.workspace = engine.workspace
        self.root = self.workspace.root.resolve()
        self.state = state
        self.worker = context.get('worker_depth') == 1
        self.allowed = set(context.get('worker_allowed_files', []))
        self.writable = set(context.get('worker_writable_files', [])) if self.worker else None
        self.worker_allowed_tools = (set(context['worker_allowed_tools'])
                                     if self.worker and 'worker_allowed_tools' in context else None)
        self.worker_shell_mode = context.get('worker_shell_mode', 'disabled') if self.worker else 'disabled'
        phase = Phase(getattr(state, 'phase', context.get('phase', Phase.PREPARE)))
        mode = getattr(state, 'mode', context.get('mode', 'test'))
        self.write_enabled = (mode == 'repair' and phase in {Phase.DIAGNOSE, Phase.PATCH}
                              and (not self.worker or context.get('worker_write_enabled') is True))
        if self.worker and self.writable - self.allowed:
            raise ToolRejected('子 Agent 可写路径必须属于允许文件集合')
        if not hasattr(self.workspace, '_local_tool_mutex'):
            self.workspace._local_tool_mutex = WorkspaceMutex(self.root)
        self.mutex = self.workspace._local_tool_mutex

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
        return self.workspace.path(relative, write=True) if write else self.workspace.path(relative)

    def files(self, directory=None):
        base = Path(directory) if directory else self.root
        if not base.is_absolute() or not base.resolve().is_relative_to(self.root):
            raise ToolRejected('检索目录必须是工作区内的绝对路径')
        candidates = [base] if base.is_file() else base.rglob('*')
        for candidate in candidates:
            if candidate.is_file():
                try:
                    yield self.path(str(candidate))
                except PermissionError:
                    continue
                except ToolRejected:
                    continue

    def read(self, arguments):
        path = self.path(arguments.file_path)
        with self.mutex.read_lock(path):
            staged = self.workspace.staged_content(path.relative_to(self.root).as_posix())
            data = staged if staged is not None else path.read_bytes()
        if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
            return {'path': str(path), 'type': 'image', 'mime_type': mimetypes.guess_type(path)[0],
                    'base64': base64.b64encode(data).decode('ascii')}
        if path.suffix.lower() == '.pdf':
            import pypdf
            document = pypdf.PdfReader(io.BytesIO(data))
            pages = arguments.pages or list(range(1, min(len(document.pages), 20) + 1))
            if any(page < 1 or page > len(document.pages) for page in pages):
                raise ToolRejected('PDF 页码超出范围')
            return {'type': 'pdf', 'total_pages': len(document.pages),
                    'pages': [{'page': page, 'text': document.pages[page - 1].extract_text()}
                              for page in pages]}
        text = data.decode('utf-8')
        if path.suffix == '.ipynb':
            notebook = json.loads(text)
            cells = notebook.get('cells', [])
            return {'type': 'notebook', 'total_cells': len(cells),
                    'cells': cells[arguments.offset - 1:arguments.offset - 1 + arguments.limit]}
        lines = text.splitlines()
        baseline = path.read_bytes()
        return {'path': str(path), 'total_lines': len(lines), 'before_hash': digest(baseline),
                'offset': arguments.offset,
                'content': '\n'.join(f'{index + 1}\t{line}' for index, line in enumerate(lines)
                                     if arguments.offset <= index + 1 < arguments.offset + arguments.limit)}

    def save(self, path, content):
        data = content.encode('utf-8')
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tracefix-edit-')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {'path': str(path), 'after_hash': digest(data)}

    def write(self, arguments):
        path = self.path(arguments.file_path, write=True)
        with self.mutex.write_lock(path):
            if not path.exists():
                raise ToolRejected('本地编辑工具不支持创建新文件')
            return self.workspace.stage(path.relative_to(self.root).as_posix(), arguments.content)

    async def write_effect(self, arguments, tool_name):
        path_value = arguments.file_path if tool_name != 'NotebookEdit' else arguments.notebook_path
        path = self.path(path_value, write=True)
        relative = path.relative_to(self.root).as_posix()
        with self.mutex.write_lock(path):
            staged = self.workspace.staged_content(relative)
            baseline = path.read_bytes()
            content = staged if staged is not None else baseline
            prepared = await run_effect('local.prepare', {
                'tool': tool_name,
                'content': content.decode('utf-8'),
                'arguments': arguments.model_dump(mode='json'),
                'cell_id': new_id('cell')}, timeout_s=45)
            if digest(content) != prepared['before_hash']:
                raise ToolRejected('本地写入基线在执行期间变化，拒绝回写')
            with apply_effect_receipt():
                current = self.workspace.staged_content(relative)
                if current != staged or path.read_bytes() != baseline:
                    raise ToolRejected('本地写入基线在执行期间变化，拒绝回写')
                return self.workspace.stage(relative, prepared['content'])

    def edit(self, arguments):
        path = self.path(arguments.file_path, write=True)
        with self.mutex.write_lock(path):
            relative = path.relative_to(self.root).as_posix()
            staged = self.workspace.staged_content(relative)
            text = (staged if staged is not None else path.read_bytes()).decode('utf-8')
            count = text.count(arguments.old_string)
            if not count or count != 1 and not arguments.replace_all:
                raise ToolRejected('old_string 必须存在且唯一；多处替换需指定 replace_all')
            return self.workspace.stage(relative, text.replace(arguments.old_string, arguments.new_string,
                                                               -1 if arguments.replace_all else 1))

    def glob(self, arguments):
        base = Path(arguments.path) if arguments.path else self.root
        paths = [path for path in self.files(str(base))
                 if path.relative_to(base).match(arguments.pattern)
                 or arguments.pattern.startswith('**/') and path.relative_to(base).match(arguments.pattern[3:])]
        paths.sort(key=lambda path: (-path.stat().st_mtime_ns, str(path)))
        return {'files': [str(path) for path in paths]}

    def grep(self, arguments):
        extensions = {'ts': {'.ts', '.tsx'}, 'js': {'.js', '.jsx', '.mjs'},
                      'py': {'.py'}, 'json': {'.json', '.ipynb'}}
        executable = shutil.which('rg')
        if not executable:
            raise ToolRejected('Grep 需要安装 ripgrep（rg）')
        paths = [path for path in self.files(arguments.path)
                 if (path.match(arguments.glob) or arguments.glob == '**/*')
                 and (not arguments.type or path.suffix in
                      extensions.get(arguments.type, {'.' + arguments.type}))]
        if not paths:
            return {'matches': [], 'truncated': False}
        command = [executable, '--json', '--no-follow']
        if arguments.output_mode != 'count':
            command.extend(['--max-count', str(arguments.head_limit + 1)])
        if arguments.multiline:
            command.extend(['--multiline', '--multiline-dotall'])
        if not arguments.case_sensitive:
            command.append('--ignore-case')
        command.extend(['--', arguments.pattern, *[str(path) for path in paths]])
        with self.mutex.workspace_read():
            response = subprocess.run(command, capture_output=True, timeout=30,
                                      encoding='utf-8', errors='replace')
        if response.returncode not in {0, 1}:
            raise ToolRejected(response.stderr[:2000])
        results = []
        counts = {}
        for line in response.stdout.splitlines():
            entry = json.loads(line)
            if entry['type'] != 'match':
                continue
            match = entry['data']
            path = match['path'].get('text')
            if not path:
                continue
            path = str(self.path(path))
            counts[path] = counts.get(path, 0) + 1
            if arguments.output_mode == 'files_with_matches':
                if path not in results:
                    results.append(path)
            elif arguments.output_mode == 'content':
                results.append({'path': path, 'line': match['line_number'],
                                'content': match['lines'].get('text', '').rstrip('\r\n')})
        if arguments.output_mode == 'count':
            results = [{'path': path, 'count': count} for path, count in counts.items()]
        return {'matches': results[:arguments.head_limit], 'truncated': len(results) > arguments.head_limit}

    def notebook_edit(self, arguments):
        path = self.path(arguments.notebook_path, write=True)
        with self.mutex.write_lock(path):
            notebook = json.loads(path.read_bytes())
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
            return self.workspace.stage(path.relative_to(self.root).as_posix(),
                                       json.dumps(notebook, ensure_ascii=False, indent=1) + '\n')

    async def bash(self, arguments, call_id=None):
        profile = getattr(self.engine, 'profile', None)
        if profile is None:
            raise ToolRejected('Bash 需要配置 Docker sandbox 镜像')
        name = new_id('tf-shell')
        with tempfile.TemporaryDirectory(prefix='tracefix-shell-') as folder:
            staging = Path(folder)
            staging.chmod(0o777 if self.write_enabled else 0o755)
            originals = {}
            baselines = {}
            with self.mutex.workspace_read():
                for source in self.files():
                    relative = source.relative_to(self.root).as_posix()
                    staged = self.workspace.staged_content(relative)
                    originals[relative] = staged if staged is not None else source.read_bytes()
                    baselines[relative] = source.read_bytes()
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(originals[relative])
                    target.chmod(0o666)
            if self.write_enabled and (not self.worker or self.worker_shell_mode == 'patch'):
                for directory in staging.rglob('*'):
                    if directory.is_dir():
                        directory.chmod(0o777)
            mount = io.StringIO()
            fields = ['type=bind', 'src=' + str(staging), 'dst=/workspace']
            if not self.write_enabled or (self.worker and self.worker_shell_mode != 'patch'):
                fields.append('readonly')
            csv.writer(mount, lineterminator='').writerow(fields)
            command = ['docker', 'run', '--rm', '--name', name, '--network=none',
                       '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                       '--pids-limit=128', '--memory=1g', '--cpus=2', '--user', container_user(),
                       '--tmpfs', '/tmp:rw,nosuid,size=128m,mode=1777', '--mount', mount.getvalue(),
                       '--workdir', '/workspace', '--entrypoint', '/bin/bash', profile.image,
                       '--noprofile', '--norc', '-c', arguments.command]
            try:
                result = await process(command, arguments.timeout)
            finally:
                await process(['docker', 'rm', '-f', name], 15)
            updated = {}
            for target in staging.rglob('*'):
                if is_link(target):
                    raise ToolRejected('Bash 输出不能包含符号链接')
                if target.is_file():
                    updated[target.relative_to(staging).as_posix()] = target.read_bytes()
            removed = set(originals) - set(updated)
            if removed:
                raise ToolRejected('Bash 删除文件不会回写工作区；请使用文件编辑工具')
            changes = {relative: data for relative, data in updated.items()
                       if originals.get(relative) != data}
            if not result['passed']:
                return {**result, 'files_changed': [], 'discarded_changes': list(changes),
                        'cwd': '/workspace'}
            if not self.write_enabled or (self.worker and self.worker_shell_mode != 'patch'):
                return {**result, 'files_changed': [], 'discarded_changes': list(changes),
                        'cwd': '/workspace'}
            targets = {relative: self.path(str(self.root / relative), write=True) for relative in changes}
            with self.mutex.workspace_write():
                for relative, target in targets.items():
                    current = target.read_bytes() if target.exists() else None
                    if current != baselines.get(relative):
                        raise ToolRejected('Bash 执行期间文件已变化，拒绝覆盖并发修改')
                    changes[relative].decode('utf-8')
                for target in targets.values():
                    if not target.exists():
                        raise ToolRejected('Bash 不支持创建新文件')
                with apply_effect_receipt():
                    self.workspace.stage_many({relative: data.decode('utf-8')
                                               for relative, data in changes.items()})
            return {**result, 'files_changed': list(changes), 'cwd': '/workspace', 'staged': True}


def register_local_tools(engine, state, context, bind):
    tools = LocalTools(engine, context, state)
    definitions = [
        ('Read', ReadInput, tools.read, False,
         '读取当前授权工作区内的绝对路径。文本默认最多 2000 行，可用 offset/limit 分页；支持图片、PDF 页文本、Jupyter cell。倾向并行读取多个文件。'),
        ('Glob', GlobInput, tools.glob, False, '按文件名 glob（如 **/*.ts）列出授权文件，按修改时间降序排序；path 为绝对目录。'),
        ('Grep', GrepInput, tools.grep, False, '正则内容搜索；支持 glob/type 过滤、multiline 和 content/files_with_matches/count 三种输出。'),
        ('Write', WriteInput, tools.write, True, '写入或覆盖绝对路径文件；已有文件优先 Edit。用户没有要求时不要创建 Markdown 或 README。'),
        ('Edit', EditInput, tools.edit, True, '精确字符串替换；old_string 必须存在且唯一，replace_all=true 可替换全部匹配。'),
        ('NotebookEdit', NotebookEditInput, tools.notebook_edit, True, '按零起始 cell_number 替换、插入或删除 Jupyter cell；保留 notebook 元数据，代码修改清空旧输出。'),
    ]
    for name, model, callback, writing, description in definitions:
        if tools.worker and tools.worker_allowed_tools is not None and name not in tools.worker_allowed_tools:
            continue
        if writing and not tools.write_enabled:
            continue
        async def handler(arguments, call_id, callback=callback, name=name, writing=writing):
            if writing:
                return await tools.write_effect(arguments, name)
            return await asyncio.to_thread(callback, arguments)
        bind(name, description, model, handler, phases=set(Phase),
             side_effect='write' if writing else 'read', parallel_safe=not writing)
    if tools.worker and tools.worker_allowed_tools is not None and not ({'Bash', 'shell', 'shell.readonly', 'shell.patch'} & tools.worker_allowed_tools):
        return
    bind('Bash', '在独立 Docker Linux sandbox 执行 Bash，工作目录 /workspace；只复制授权文件，禁网、禁宿主访问。'
         '提供 command/description，timeout 为秒（最多 120）。优先用 Read/Glob/Grep/Edit 操作文件。'
         '子 Agent 默认只读，只有 Supervisor 显式启用写权限才回写 writable_files；删除、越权或并发 hash 冲突不回写。'
         '不要 push、后台运行、索取凭据或宣称失败命令成功；检查 exit_code/output。',
         BashInput, tools.bash, phases=set(Phase), side_effect='write', parallel_safe=False,
         timeout_s=150)
