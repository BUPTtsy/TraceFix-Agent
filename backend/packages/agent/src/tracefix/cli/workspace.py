"""Workspace commands use the same services and version checks as the Web API."""
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from tracefix.cli.registry import split_arguments
from tracefix.console import dispatch
from tracefix.config import create_project, load_projects, save_profile
from tracefix.knowledge.documents import ConflictError
from tracefix.messages import ChineseArgumentParser, argument_message, error_message
from tracefix.storage.presentation import label
from tracefix.remote import validate_branch, validate_repository


HELP = {
    'remote': ('/remote show\n'
               '/remote set --repository OWNER/REPOSITORY [--base BRANCH] [--branch BRANCH] '
               '[--destination PATH] [--scope session|project] [--remote-write]\n'
               '/remote clear [--scope session|project]'),
    'projects': '/projects list\n/projects show [PROJECT_ID]\n/projects use PROJECT_ID',
    'knowledge': ('/knowledge list [--query TEXT] [--kind testing|repair|experience]\n'
                  '/knowledge show ID\n/knowledge search QUERY\n'
                  '/knowledge import FILE [--title TITLE] [--kind TYPE] [--tags TAG1,TAG2] [--global] [--disabled]\n'
                  '/knowledge new TITLE [--file FILE] [--kind TYPE] [--tags TAG1,TAG2] [--global] [--disabled]\n'
                  '/knowledge edit ID [--file FILE --version N] [--title TITLE] [--kind TYPE] [--tags TAG1,TAG2] [--scope current|global]\n'
                  '/knowledge enable ID\n/knowledge disable ID\n/knowledge export ID FILE\n'
                  '未提供正文文件的新建 / 编辑会打开 TRACEFIX_EDITOR、VISUAL 或 EDITOR。带空格的路径请用引号包裹。'),
    'runs': ('/runs list [--all] [--status STATUS] [--query TEXT] [--limit N]\n'
             '/runs show ID\n/runs logs ID\n/runs sources ID\n'
             '/runs export ID ARTIFACT_REF FILE\n/runs remember ID [--title TITLE] [--file FILE]\n'
             '支持完整的控制台记录 ID 或 Agent Run ID。经验草稿默认停用，导出不会覆盖已有文件。'),
}


HELP['projects'] += ('\n/projects create PROJECT_ID ROOT [--repo-id REPOSITORY]\n'
                     '/projects configure [PROJECT_ID] --start CMD --reset CMD --static CMD --unit CMD --build CMD')
HELP['runs'] += '\n/runs continue RUN_ID INSTRUCTION\n/runs trace RUN_ID'


class CommandParser(ChineseArgumentParser):
    def error(self, message):
        raise ValueError(f'{self.prog}：{argument_message(message)}')


def command_parser(name):
    return CommandParser(prog='/' + name, add_help=False)


def read_document_file(filename):
    path = Path(filename).expanduser()
    if path.suffix.lower() not in {'.md', '.markdown', '.txt'} or not path.is_file():
        raise ValueError('请选择 .md、.markdown 或 .txt 文本文件')
    with path.open('rb') as handle:
        raw = handle.read(262145)
    if len(raw) > 262144:
        raise ValueError('文档不能超过 256 KiB')
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise ValueError('文档必须使用 UTF-8 编码') from error
    if not content.strip() or '\x00' in content:
        raise ValueError('文档必须是非空文本')
    return content


def export_text(filename, content):
    path = Path(filename).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='') as handle:
        handle.write(content)
    return path


def edit_document(content, directory):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix='.md', delete=False) as handle:
        path = Path(handle.name)
        handle.write(content.encode('utf-8'))
    editor = os.getenv('TRACEFIX_EDITOR') or os.getenv('VISUAL') or os.getenv('EDITOR') or ('notepad.exe' if os.name == 'nt' else 'vi')
    try:
        subprocess.run([*split_arguments(editor), str(path)], check=True)
        updated = read_document_file(path)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise ValueError(f'编辑未完成，草稿保留在 {path}：{error_message(error)}') from error
    return updated, path


class WorkspaceCommands:
    def __init__(self, session):
        self.session = session

    def call(self, operation, fields=None):
        return dispatch(operation, fields or {}, library=self.session.documents,
                        projects_path=self.session.args.projects, data_root=self.session.args.data)

    def show(self, title, record):
        self.session.render.panel(title, json.dumps(record, ensure_ascii=False, indent=2))

    def document(self, document_id):
        record = self.call('document', {'id': document_id})
        if record['projectId'] not in {None, self.session.scope}:
            raise PermissionError('请先切换到此文档所属项目')
        return record

    def run(self, run_id):
        records = self.call('runs', {'projectId': self.session.scope})
        record = next((record for record in records if run_id in {record['id'], record.get('agentRunId')}), None)
        if record is None:
            raise ValueError('当前项目没有此运行记录；可先输入 /runs list')
        return self.call('run', {'id': record['id']})

    def create_project_command(self, arguments):
        parser = command_parser('projects create')
        parser.add_argument('project_id')
        parser.add_argument('root')
        parser.add_argument('--repo-id')
        args = parser.parse_args(arguments)
        project = create_project(Path(self.session.args.projects), args.project_id, args.root,
                                 args.repo_id or args.project_id)
        self.session.render.panel('项目已创建', json.dumps({
            'id': project.id, 'root': str(project.root), 'repoId': project.repo_id,
            'next': [f'/projects use {project.id}', '/remote set --repository OWNER/REPOSITORY',
                     f'/projects configure {project.id} --start CMD --reset CMD --static CMD --unit CMD --build CMD']},
            ensure_ascii=False, indent=2))

    def configure_project_command(self, arguments):
        parser = command_parser('projects configure')
        parser.add_argument('project_id', nargs='?', default=self.session.scope)
        for name in ('start', 'reset', 'static', 'unit', 'build'):
            parser.add_argument('--' + name, required=True)
        parser.add_argument('--url', default='http://app:3000')
        parser.add_argument('--source-commit', default='HEAD')
        args = parser.parse_args(arguments)
        projects = load_projects(Path(self.session.args.projects))
        if args.project_id not in projects:
            raise ValueError('未注册的项目')
        commands = {name: split_arguments(getattr(args, name)) for name in ('start', 'reset', 'static', 'unit', 'build')}
        if any(not command for command in commands.values()):
            raise ValueError('项目命令不能为空')
        profile_path = Path(self.session.args.projects).parent / f'{args.project_id}.yaml'
        profile = save_profile(profile_path, {'project': args.project_id, 'source_commit': args.source_commit,
                                              'url': args.url, 'allowed_origins': [args.url.rstrip('/')],
                                              'commands': commands})
        self.session.render.panel('项目命令已配置', json.dumps({
            'project': profile.project, 'profile': str(profile_path), 'commands': profile.commands,
            'remote': self.session.remote_snapshot() if args.project_id == self.session.scope else None},
            ensure_ascii=False, indent=2))

    def project(self, arguments):
        if arguments and arguments[0] == 'create':
            self.create_project_command(arguments[1:])
            return
        if arguments and arguments[0] == 'configure':
            self.configure_project_command(arguments[1:])
            return
        if arguments in (['help'], ['--help'], ['-h']):
            self.session.render.panel('项目命令', HELP['projects'])
            return
        parser = command_parser('projects')
        parser.add_argument('action', nargs='?', choices=['list', 'show', 'use'], default='list')
        parser.add_argument('id', nargs='?')
        args = parser.parse_args(arguments)
        if args.action == 'use':
            if not args.id:
                raise ValueError('用法：/projects use PROJECT_ID')
            self.session.select_project(args.id)
        records = self.call('projects')
        if args.action == 'list':
            if args.id:
                raise ValueError('/projects list 不接受项目 ID')
            self.session.render.table('项目空间', ['项目', '仓库 / 目录', '运行 / 文档', '配置'], [
                [('* ' if record['id'] == self.session.scope else '') + record['id'],
                 record['repoId'] + '\n' + record['root'], f"{record['runCount']} / {record['documentCount']}",
                 record['issue'] or '就绪'] for record in records])
        else:
            record = next((record for record in records if record['id'] == (args.id or self.session.scope)), None)
            if record is None:
                raise ValueError('未注册的项目')
            self.show('项目配置', record)

    def remote(self, arguments):
        if arguments in (['help'], ['--help'], ['-h']):
            self.session.render.panel('远程仓库命令', HELP['remote'])
            return
        parser = command_parser('remote')
        actions = parser.add_subparsers(dest='action', required=True)
        actions.add_parser('show', add_help=False)
        setter = actions.add_parser('set', add_help=False)
        setter.add_argument('--repository', required=True)
        setter.add_argument('--base', default='main')
        setter.add_argument('--branch')
        setter.add_argument('--destination')
        setter.add_argument('--scope', choices=['session', 'project'], default='project')
        setter.add_argument('--remote-write', action='store_true')
        clearer = actions.add_parser('clear', add_help=False)
        clearer.add_argument('--scope', choices=['session', 'project'], default='project')
        args = parser.parse_args(arguments or ['show'])
        if args.action == 'show':
            self.show('远程仓库配置', self.session.remote_snapshot())
            return
        if args.action == 'clear':
            self.session.clear_remote(args.scope)
            self.session.render.text(f'已清除{args.scope}级远程仓库配置')
            return
        value = {
            'repository': validate_repository(args.repository),
            'base': args.base.strip(),
            'branch': validate_branch(args.branch) if args.branch else None,
            'destination': args.destination.strip() if args.destination else None,
            'remote_write': bool(args.remote_write),
        }
        if not value['base']:
            raise ValueError('--base 不能为空')
        self.session.set_remote(value, args.scope)
        self.show('远程仓库配置已更新', self.session.remote_snapshot())

    async def knowledge(self, arguments):
        if arguments in (['help'], ['--help'], ['-h']):
            self.session.render.panel('知识库命令', HELP['knowledge'])
            return
        parser = command_parser('knowledge')
        actions = parser.add_subparsers(dest='action', required=True)
        listing = actions.add_parser('list', add_help=False)
        listing.add_argument('--query', default='')
        listing.add_argument('--kind', choices=['repair', 'testing', 'experience'])
        for name in ('show', 'enable', 'disable'):
            actions.add_parser(name, add_help=False).add_argument('id')
        search = actions.add_parser('search', add_help=False)
        search.add_argument('query', nargs='+')
        exporter = actions.add_parser('export', add_help=False)
        exporter.add_argument('id')
        exporter.add_argument('file')
        for name in ('import', 'new', 'edit'):
            subparser = actions.add_parser(name, add_help=False)
            subparser.add_argument('value')
            if name != 'import':
                subparser.add_argument('--file')
            subparser.add_argument('--title')
            subparser.add_argument('--kind', choices=['repair', 'testing', 'experience'])
            subparser.add_argument('--tags')
            if name == 'edit':
                subparser.add_argument('--version', type=int)
                subparser.add_argument('--scope', choices=['current', 'global'])
            else:
                subparser.add_argument('--global', dest='shared', action='store_true')
                subparser.add_argument('--disabled', action='store_true')
        args = parser.parse_args(arguments or ['list'])
        if args.action == 'list':
            records = self.call('documents', {'projectId': self.session.scope, 'query': args.query})
            self.session.render.table('知识文档 · 当前项目与全局', ['ID / 标题', '范围 / 类型', '版本 / 状态'], [
                [record['id'] + '\n' + record['title'], (record['projectId'] or '全局') + ' / ' + record['kind'],
                 f"v{record['version']} / " + ('启用' if record['enabled'] else '停用')]
                for record in records if not args.kind or args.kind == record['kind']])
            return
        if args.action == 'search':
            records = self.call('search', {'projectId': self.session.scope, 'query': ' '.join(args.query)})
            if not records:
                self.session.render.text('未命中相关且已启用的文档。')
            for record in records:
                self.session.render.panel(f"{record['title']} · v{record['version']} · 匹配分 {record['score']}",
                    f"{record['id']} · {record['projectId'] or '全局'} · 片段 {record['chunk']}\n{record['excerpt']}")
            return
        if args.action in {'show', 'enable', 'disable', 'export'}:
            record = self.document(args.id)
            if args.action == 'show':
                self.show('文档详情', {key: value for key, value in record.items() if key != 'content'})
                self.session.render.panel('正文', record['content'])
            elif args.action == 'export':
                path = export_text(args.file, record['content'])
                self.session.render.text(f"已导出 {path} · v{record['version']}；回写时使用 --version {record['version']}")
            else:
                saved = self.call('document.save', {**record, 'enabled': args.action == 'enable'})
                self.saved(saved)
            return
        record = self.document(args.value) if args.action == 'edit' else {
            'title': args.value if args.action == 'new' else Path(args.value).stem,
            'projectId': None if args.shared else self.session.scope, 'enabled': not args.disabled,
            'kind': 'experience', 'tags': [], 'content': '', 'sourceRunId': None}
        changed_metadata = False
        if args.action == 'edit' and args.scope:
            record['projectId'] = self.session.scope if args.scope == 'current' else None
            changed_metadata = True
        for field in ('title', 'kind', 'tags'):
            value = getattr(args, field)
            if value is not None:
                record[field] = [tag.strip() for tag in value.split(',') if tag.strip()] if field == 'tags' else value
                changed_metadata = True
        filename = args.value if args.action == 'import' else args.file
        temporary = None
        if args.action == 'edit':
            if filename and args.version is None:
                raise ValueError('从文件更新需指定导出时的版本：/knowledge edit ID --file FILE --version N')
            if args.version is not None and args.version != record['version']:
                raise ConflictError('文档版本已变化，请重新打开最新版本后合并修改')
        if filename:
            record['content'] = read_document_file(filename)
        elif args.action == 'new' or not changed_metadata:
            original = record['content'] or '## 适用场景\n\n## 修复或测试方法\n\n## 验证与局限\n'
            record['content'], temporary = await asyncio.to_thread(edit_document, original,
                self.session.documents.path.parent / 'document-drafts')
            if record['content'] == original:
                temporary.unlink()
                self.session.render.text('正文未修改，没有保存新版本。')
                return
        try:
            saved = self.call('document.save', record)
        except Exception:
            if temporary:
                self.session.render.text(f'保存未完成，编辑草稿保留在 {temporary}')
            raise
        if temporary:
            temporary.unlink()
        self.saved(saved)

    def saved(self, record):
        self.session.render.text(f"已保存 {record['id']} · {record['title']} · v{record['version']} · "
                                 + ('参与检索' if record['enabled'] else '已停用'))

    def runs(self, arguments):
        if arguments in (['help'], ['--help'], ['-h']):
            self.session.render.panel('运行记录命令', HELP['runs'])
            return
        parser = command_parser('runs')
        actions = parser.add_subparsers(dest='action', required=True)
        listing = actions.add_parser('list', add_help=False)
        listing.add_argument('--all', action='store_true')
        listing.add_argument('--status')
        listing.add_argument('--query', default='')
        listing.add_argument('--limit', type=int, default=30)
        for name in ('show', 'logs', 'trace', 'sources', 'remember', 'export'):
            subparser = actions.add_parser(name, add_help=False)
            subparser.add_argument('id')
            if name == 'export':
                subparser.add_argument('ref')
                subparser.add_argument('file')
            if name == 'remember':
                subparser.add_argument('--title')
                subparser.add_argument('--file')
        args = parser.parse_args(arguments or ['list'])
        self.call('runs.import')
        if args.action == 'list':
            if not 1 <= args.limit <= 200:
                raise ValueError('--limit 须在 1-200 之间')
            records = self.call('runs', {} if args.all else {'projectId': self.session.scope})
            records = [record for record in records if (not args.status or record['status'] == args.status.lower())
                       and args.query.casefold() in (record.get('goal', '') + record['id'] + record.get('agentRunId', '')).casefold()]
            self.session.render.table('运行记录', ['ID / 目标', '项目 / 服务', '状态 / 结论', '时间'], [
                [record['id'] + '\n' + record.get('goal', ''), record['projectId'] + ' / ' + record.get('mode', 'unknown'),
                 label(record['status'].upper()) + '\n' + label(record.get('outcome') or record.get('phase', ''))
                 + (f"\n曾非成功结束 · 继续 {record.get('continuationCount', 0)} 次" if record.get('abnormalTermination') else ''),
                 ('报告时间 ' if record.get('timeSource') else '') + record['startedAt']] for record in records[:args.limit]])
            return
        record = self.run(args.id)
        if args.action == 'show':
            self.show('运行详情', {key: value for key, value in record.items() if key != 'logs'})
        elif args.action == 'logs':
            self.session.render.panel('运行日志', ''.join(record.get('logs', [])) or '尚无进程日志；可查看运行详情与报告。')
        elif args.action == 'trace':
            self.show('完整任务轨迹', self.call('run.trace', {'id': record['id']}))
        elif args.action == 'sources':
            self.show('实际使用的知识来源', record.get('knowledge', []))
        elif args.action == 'export':
            content = self.call('artifact', {'id': record['id'], 'ref': args.ref})['content']
            self.session.render.text(f"已导出 {export_text(args.file, content)}")
        elif args.action == 'remember':
            content = read_document_file(args.file) if args.file else (
                f"# {record['goal']}\n\n## 来源\nRun: {record.get('agentRunId') or record['id']}\n"
                f"结果: {record.get('outcome') or record['status']}\n\n## 复现与定位\n\n"
                '请补充步骤、原因与证据。\n\n## 修复或测试方法\n\n## 适用条件与局限\n')
            saved = self.call('document.save', {'title': (args.title or record['goal'])[:120], 'content': content,
                'kind': 'experience', 'tags': [], 'projectId': record['projectId'], 'enabled': False,
                'sourceRunId': record['id']})
            self.saved(saved)
            self.session.render.text('经验草稿默认停用；完善内容后使用 /knowledge enable ID 加入检索。')
