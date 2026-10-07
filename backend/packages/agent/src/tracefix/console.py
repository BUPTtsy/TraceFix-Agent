"""JSON bridge shared by the web console and the actual Agent configuration."""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tracefix.config import load_profile, load_projects
from tracefix.knowledge.documents import ConflictError, DocumentLibrary, timestamp
from tracefix.storage.artifacts import Artifacts
from tracefix.rules.store import RuleConflictError, RuleLibrary
from tracefix.rules.models import Rule
from tracefix.rules.resolver import render_rule_context
from tracefix.remote import RemoteConfigStore
from tracefix.runtime.continuation import can_continue
from tracefix.runtime.guidance import GuidanceLedger
from tracefix.messages import error_message


def displayed_path(path):
    """跨盘符（如项目位于 C:、工作目录位于 D:）无法生成相对路径时退回绝对路径。"""
    try:
        return os.path.relpath(path, Path.cwd())
    except ValueError:
        return str(path)


def declared_project(profile_path):
    """Profile 通过文件内的 project 字段绑定项目；解析失败时才退回文件名。"""
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError):
        return None
    return raw['project'] if isinstance(raw, dict) and isinstance(raw.get('project'), str) else None


def project_catalog(projects_path=None, data_root=None):
    registry = Path(projects_path or os.getenv('TRACEFIX_PROJECTS', 'profiles/projects.yaml')).resolve()
    projects = load_projects(registry)
    remote_store = RemoteConfigStore(Path(data_root or os.getenv('TRACEFIX_DATA', '.tracefix')) / 'remote-config.json')
    profiles = {}
    errors = {}
    for profile_path in sorted(registry.parent.glob('*.yaml')):
        if profile_path == registry or '.example.' in profile_path.name:
            continue
        try:
            profile = load_profile(profile_path)
            profiles.setdefault(profile.project, (profile_path, profile))
        except (ValueError, TypeError, KeyError) as error:
            errors[declared_project(profile_path) or profile_path.stem] = error_message(error)
    result = []
    for project in projects.values():
        entry = profiles.get(project.id)
        issue = None
        if not project.root.is_dir():
            issue = '目标目录不存在，请先初始化或 clone 项目'
        elif not (project.root / '.git').exists():
            issue = '目标目录尚未初始化 Git'
        if entry is None and issue is None:
            issue = '缺少有效的项目 Profile' + (': ' + errors[project.id] if project.id in errors else '')
        result.append({'id': project.id, 'repoId': project.repo_id, 'root': displayed_path(project.root),
                       'profile': displayed_path(entry[0]) if entry else None,
                       'registry': str(registry), 'url': entry[1].url if entry else None,
                       'allowedFiles': project.allowed_files, 'commands': entry[1].commands if entry else {},
                       'remoteConfig': remote_store.get(project.id),
                       'ready': issue is None, 'issue': issue})
    return result


def dispatch(operation, fields, *, library=None, projects_path=None, data_root=None):
    library = library or DocumentLibrary()
    rule_library = RuleLibrary(library.path)
    data_root = Path(data_root or os.getenv('TRACEFIX_DATA', '.tracefix')).resolve()

    def catalog():
        if projects_path is None:
            return project_catalog()
        return project_catalog(projects_path, data_root)

    if operation == 'runs.import':
        artifacts = Artifacts(data_root / 'artifacts')
        known = {record.get('agentRunId') for record in library.runs()}
        imported = 0
        for project in catalog():
            for run_directory in (artifacts.root / project['id']).glob('run_*'):
                if not run_directory.is_dir() or run_directory.is_symlink() or run_directory.name in known:
                    continue
                try:
                    index = artifacts._index(project['id'], run_directory.name)
                except (ValueError, OSError):
                    continue
                for ref in reversed(list(index)):
                    if not ref.endswith('.json') or '修复报告数据' not in index[ref].get('用途', ''):
                        continue
                    try:
                        report = artifacts.json(project['id'], run_directory.name, ref)
                    except (ValueError, OSError):
                        continue
                    if not isinstance(report, dict) or report.get('run_id') != run_directory.name or report.get('scope_id') != project['id'] or 'run_status' not in report:
                        continue
                    finished = datetime.fromtimestamp((run_directory / ref).stat().st_mtime, timezone.utc).isoformat()
                    library.update_run('history_' + run_directory.name, {
                        'agentRunId': run_directory.name, 'projectId': project['id'], 'goal': report.get('goal', ''),
                        'mode': report.get('mode', 'unknown'), 'status': report['run_status'].lower(),
                        'phase': 'FINALIZE', 'outcome': report.get('outcome'), 'reportRef': ref,
                        'parentRunId': report.get('parent_run_id'),
                        'continuationInstruction': report.get('continuation_instruction'),
                        'continuationCount': report.get('continuation_count', 0),
                        'abnormalTermination': report.get('abnormal_termination', False),
                        'continuationMarkers': report.get('continuation_markers', []),
                        'branch': report.get('branch'), 'error': report.get('error'), 'finishedAt': finished,
                        'startedAt': finished, 'timeSource': 'report_file', 'origin': 'artifact', 'dataRoot': str(data_root),
                        'logs': ['此记录从已有报告导入。历史进程日志和实际启动时间未记录；列表按报告文件时间排序。'],
                    }, create=True)
                    imported += 1
                    break
        return {'imported': imported}
    if operation == 'projects':
        projects = catalog()
        runs, documents = library.runs(), library.documents()
        return [{**project, 'runCount': sum(run['projectId'] == project['id'] for run in runs),
                 'documentCount': sum(document['projectId'] in {None, project['id']} for document in documents)} for project in projects]
    if operation == 'runs':
        return [{key: value for key, value in record.items() if key != 'logs'} |
                {'canContinue': can_continue(record['status'], record.get('outcome'))}
                for record in library.runs(fields.get('projectId'))]
    if operation == 'run':
        record = library.run(fields['id'])
        record['canContinue'] = can_continue(record['status'], record.get('outcome'))
        record['artifacts'] = []
        record['issueReport'] = None
        if record.get('agentRunId'):
            artifacts = Artifacts(Path(record.get('dataRoot') or data_root) / 'artifacts')
            index = artifacts._index(record['projectId'], record['agentRunId'])
            report_ref = record.get('reportRef')
            if report_ref in index:
                record['issueReport'] = artifacts.json(record['projectId'], record['agentRunId'], report_ref)
            issue_refs = {ref for issue in (record['issueReport'] or {}).get('issues', [])
                          for ref in issue.get('evidence_refs', [])}
            report = record['issueReport'] or {}
            check_refs = {ref for item in report.get('check_results', []) + report.get('initial_check_results', [])
                          for ref in item.get('evidence_refs', [])}
            check_refs.update(report.get('check_result_refs', []))
            check_refs.update(report.get('initial_check_result_refs', []))
            check_refs.update(image['ref'] for image in report.get('images', []) if image.get('ref'))
            if report.get('check_plan_ref'):
                check_refs.add(report['check_plan_ref'])
            record.update({key: report[key] for key in
                           ('check_plan', 'check_results', 'check_summary', 'overall_status', 'images',
                            'initial_check_results', 'initial_check_summary')
                           if key in report})
            knowledge_refs = {entry['artifact_ref'] for entry in record.get('knowledge', [])}
            record['artifacts'] = [{'ref': ref, 'label': entry['用途'], 'bytes': entry['字节数']} for ref, entry in index.items()
                                   if ref.endswith(('.diff', '.html')) or ref == report_ref or ref in knowledge_refs or ref in issue_refs or ref in check_refs
                                   or any(name in entry['用途'] for name in ('修复报告数据', '完整事件数据', '继续执行前状态', '_阶段轨迹'))]
        return record
    if operation == 'run.trace':
        record = library.run(fields['id'])
        events = library.run_events(record['id'])
        if not events and record.get('agentRunId'):
            artifacts = Artifacts(Path(record.get('dataRoot') or data_root) / 'artifacts')
            index = artifacts._index(record['projectId'], record['agentRunId'])
            for ref, entry in reversed(list(index.items())):
                if '完整事件数据' in entry['用途']:
                    for event in artifacts.json(record['projectId'], record['agentRunId'], ref):
                        library.append_event(record['id'], event)
                    break
        return library.run_events(record['id'], int(fields.get('after', 0)))
    if operation in {'run.guidance', 'run.guidance.submit', 'run.guidance.confirm'}:
        # 控制台记录与 Agent Run ID 分开存储；引导账本必须绑定真实运行 ID 和项目作用域。
        record = library.run(fields['id'])
        run_id = record.get('agentRunId')
        if not run_id:
            raise ValueError('Agent 尚未建立 Run，暂时不能接收引导')
        ledger = GuidanceLedger(Path(record.get('dataRoot') or data_root) / 'guidance.sqlite3')
        if operation == 'run.guidance':
            return [entry.model_dump(mode='json') for entry in ledger.list(run_id, record['projectId'])]
        if record['status'] not in {'running', 'paused', 'waiting_input'}:
            raise ValueError('只有运行中或已暂停的 Run 可以接收引导')
        if operation == 'run.guidance.confirm':
            # 确认只更新账本，父 Run 的终结和子 Run 的派生交由运行时在安全边界处理。
            entry = ledger.confirm(fields.get('guidanceId'), run_id, record['projectId'])
        else:
            if not isinstance(fields.get('text'), str):
                raise ValueError('引导必须为文本')
            entry = ledger.submit(run_id, record['projectId'], fields['text'], level=fields.get('level'),
                author='web', phase=record.get('phase') if record.get('phase') not in {'STARTING', None} else None,
                expires=fields.get('expires', 'run'), constraints=fields.get('constraints'))
        # 同步写入控制台审计轨迹，让 Web 的状态展示能区分排队、拒绝和人工确认。
        library.append_event(record['id'], {'type': 'guidance.confirmed' if operation.endswith('confirm')
            else 'guidance.rejected' if entry.status == 'rejected' else 'guidance.queued',
            'phase': record.get('phase', 'PREPARE'), 'at': entry.created_at,
            'payload': entry.model_dump(mode='json'), 'seq': -int(entry.created_at * 1_000_000)})
        return entry.model_dump(mode='json')
    if operation == 'run.continue':
        previous = library.run(fields['id'])
        project = next((item for item in catalog() if item['id'] == previous['projectId']), None)
        dispatch('run.trace', fields, library=library, projects_path=projects_path, data_root=data_root)
        record = library.continue_run(fields['id'], fields.get('instruction'))
        if project:
            record = library.update_run(record['id'], {'profile': project['profile'], 'registry': project['registry']})
        return record
    if operation == 'artifact':
        record = dispatch('run', {'id': fields['id']}, library=library, projects_path=projects_path, data_root=data_root)
        if fields['ref'] not in {entry['ref'] for entry in record['artifacts']}:
            raise PermissionError('只允许读取此 Run 的报告或补丁')
        artifacts = Artifacts(Path(record.get('dataRoot') or data_root) / 'artifacts')
        return {'content': artifacts.read(record['projectId'], record['agentRunId'], fields['ref']).decode('utf-8')}
    if operation == 'run.create':
        project = next((item for item in catalog() if item['id'] == fields.get('projectId')), None)
        if project is None or not project['ready']:
            raise ValueError(project['issue'] if project else '请选择已注册的项目')
        return library.update_run(str(uuid.uuid4()), {**fields, 'profile': project['profile'], 'registry': project['registry'],
                                  'status': 'running', 'phase': 'STARTING', 'outcome': None, 'finishedAt': None,
                                  'origin': 'web', 'dataRoot': str(data_root)}, create=True)
    if operation == 'run.update':
        return library.update_run(fields['id'], fields['changes'])
    if operation == 'run.ended':
        record = library.run(fields['id'])
        changes = {'exitCode': fields.get('exitCode'), 'pid': None, 'processEndedAt': timestamp()}
        if record['status'] in {'running', 'stopping'}:
            changes.update(status='cancelled' if record['status'] == 'stopping' else 'failed', finishedAt=timestamp(),
                           error=fields.get('error') or record.get('error') or 'Agent 进程已结束，未记录最终结果；请查看日志')
        return library.update_run(fields['id'], changes)
    if operation == 'documents':
        records = library.documents(fields.get('projectId'))
        query = fields.get('query', '').strip().casefold()
        return [{key: value for key, value in record.items() if key != 'content'} |
                {'preview': record['content'][:180]} for record in records
                if not query or query in (record['title'] + ' ' + record['content'] + ' ' + ' '.join(record['tags'])).casefold()]
    if operation == 'document':
        return library.document(fields['id'])
    if operation in {'document.save', 'search'}:
        projects = {project['id'] for project in catalog()}
        project_id = fields.get('projectId') or None
        if project_id is not None and project_id not in projects:
            raise ValueError('知识范围必须是已注册项目或全局')
        if operation == 'search':
            if not project_id:
                raise ValueError('检索必须指定项目')
            return library.search(fields.get('query', ''), project_id)
        source_run_id = library.document(fields['id'])['sourceRunId'] if fields.get('id') else fields.get('sourceRunId')
        if source_run_id:
            source = library.run(source_run_id)
            if source['projectId'] != project_id:
                raise ValueError('运行经验须先归档到来源项目')
        return library.save_document(fields, fields.get('id'))
    if operation == 'rules':
        return [rule.model_dump(mode='json') for rule in rule_library.rules(
            project_id=fields.get('projectId'), status=fields.get('status'),
            query=fields.get('q', ''), include_archived=fields.get('includeArchived', False))]
    if operation == 'rule':
        return rule_library.rule(fields['id']).model_dump(mode='json')
    if operation == 'rule.save':
        data = dict(fields)
        rule_id = data.pop('id', None)
        expected = data.pop('expectedVersion', data.pop('expected_version', None))
        return rule_library.save_rule(data, rule_id, expected_version=expected,
                                      author=data.pop('author', 'web'), change_note=data.pop('changeNote', '')) .model_dump(mode='json')
    if operation == 'rule.status':
        return rule_library.set_status(fields['id'], fields['status'],
                                       expected_version=fields.get('expectedVersion'),
                                       author=fields.get('author', 'web')).model_dump(mode='json')
    if operation == 'rule.delete':
        rule_library.delete_rule(fields['id'])
        return {'ok': True}
    if operation == 'rule.versions':
        return rule_library.versions(fields['id'])
    if operation == 'rule.preview':
        rule = rule_library.rule(fields['id'])
        return {**render_rule_context([rule], max_tokens=int(fields.get('maxTokens', 1200))),
                'rule': rule.model_dump(mode='json')}
    if operation == 'rule.rollback':
        return rule_library.rollback(fields['id'], int(fields['version']),
                                     author=fields.get('author', 'web')).model_dump(mode='json')
    if operation == 'rule.findings':
        return [finding.model_dump(mode='json') for finding in rule_library.findings(
            rule_id=fields.get('ruleId'), run_id=fields.get('runId'), status=fields.get('status'),
            limit=int(fields.get('limit', 100)))]
    if operation == 'rule.insights':
        return rule_library.insights(from_time=fields.get('from'), to_time=fields.get('to'))
    if operation == 'rule-sets':
        return rule_library.rule_sets()
    if operation == 'rule-set.save':
        return rule_library.save_rule_set(fields, fields.get('id'))
    raise ValueError('未知控制台操作')


def main():
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        request = json.load(sys.stdin)
        result = dispatch(request['operation'], request.get('fields', {}))
        print(json.dumps({'result': result}, ensure_ascii=False))
    except Exception as error:
        status = 409 if isinstance(error, (ConflictError, RuleConflictError)) else 404 if isinstance(error, FileNotFoundError) else 400
        print(json.dumps({'error': error_message(error), 'status': status}, ensure_ascii=False))


if __name__ == '__main__':
    main()
