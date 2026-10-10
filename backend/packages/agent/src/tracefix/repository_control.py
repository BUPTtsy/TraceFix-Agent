"""Noninteractive JSON service bridge for the authoritative repository registry."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from tracefix.repository_registry import RepositoryError, RepositoryRegistry


def dispatch(operation, fields, *, projects_path=None):
    registry = RepositoryRegistry(Path(projects_path or 'profiles/projects.yaml'))
    if operation == 'repository.register':
        return registry.register(fields)
    if operation == 'repository.list':
        return registry.list()
    if operation == 'repository.get':
        return registry.get(fields['id'])
    if operation == 'repository.update':
        return registry.update(fields['id'], fields['patch'])
    if operation == 'repository.freeze':
        return registry.freeze(fields['id'], fields['run_id'])
    if operation == 'project.create':
        project = registry.create_legacy(fields['id'], fields['root'], fields.get('repoId') or fields['id'])
        return project.model_dump(mode='json')
    if operation == 'project.configure':
        payload = dict(fields['fields'])
        payload = {'project': fields['id'], 'commands': payload['commands'],
                   'source_commit': payload.get('sourceCommit', 'HEAD'),
                   'url': payload.get('url', 'http://app:3000'),
                   'allowed_origins': [payload.get('url', 'http://app:3000').rstrip('/')]}
        profile = registry.configure_legacy(fields['id'], payload)
        return {**profile.model_dump(mode='json'), 'profilePath': str(registry.path.parent / (fields['id'] + '.yaml'))}
    raise RepositoryError('UNKNOWN_OPERATION', '未知仓库控制操作')


def main():
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        request = json.load(sys.stdin)
        result = dispatch(request['operation'], request.get('fields', {}), projects_path=request.get('projects_path'))
        response = {'ok': True, 'data': result}
    except RepositoryError as error:
        response = {'ok': False, 'error': error.as_dict()}
    except (KeyError, TypeError, ValueError, PermissionError):
        response = {'ok': False, 'error': {'code': 'INVALID_REQUEST', 'message': '仓库控制请求或配置无效', 'status': 400}}
    except (OSError, RuntimeError):
        response = {'ok': False, 'error': {'code': 'REPOSITORY_UNAVAILABLE', 'message': '仓库控制服务暂不可用', 'status': 503}}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == '__main__':
    main()
