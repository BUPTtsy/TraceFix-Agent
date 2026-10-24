"""Register the external test repository using the authoritative managed checkout."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/packages/agent/src'))

from dotenv import load_dotenv
from tracefix.repository_registry import RepositoryError, RepositoryRegistry


def registration_fields(root: Path, *, publish_branch=False, require_approval=False,
                        credential_ref='deployment/test-bug'):
    return {
        'repo_id': 'test-bug-repo', 'root': str(root), 'owner': 'BUPTtsy', 'repo': 'Test-Bug-repo',
        'default_ref': 'main', 'checkout_mode': 'managed', 'publish_branch': publish_branch,
        'require_approval': require_approval,
        'credential_ref': credential_ref, 'enabled': True,
        'allowed_files': ['src/**', 'server/**'], 'allowed_urls': ['http://app:3000'],
        'profile': {
            'project': 'test-bug-repo', 'source_commit': 'main', 'url': 'http://app:3000',
            'allowed_origins': ['http://app:3000'], 'image': 'tracefix-bugboard:1.0',
            'browser_image': 'tracefix-browser:1.0', 'port': 3000,
            'health_path': '/health', 'version_path': '/version', 'timeout_seconds': 120,
            'commands': {
                'prepare': ['node', '--version'], 'start': ['node', 'scripts/serve.mjs'],
                'reset': ['node', 'scripts/reset.mjs'],
                'static': ['node', 'node_modules/typescript/bin/tsc', '--noEmit'],
                'unit': ['node', '--test', 'tests/tasks.test.mjs'],
                'build': ['node', 'scripts/build.mjs'],
            },
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='为接口 Agent 注册并自动拉取 Test-Bug-repo')
    parser.add_argument('--registry', type=Path, default=ROOT / 'profiles/projects.yaml')
    parser.add_argument('--data', type=Path, default=ROOT / '.tracefix')
    parser.add_argument('--root', type=Path, default=ROOT / '.tracefix/repositories/test-bug-repo')
    parser.add_argument('--publish-branch', action='store_true')
    parser.add_argument('--require-approval', action=argparse.BooleanOptionalAction, default=None,
                        help='交付前是否需要人工审批（仓库配置 require_approval，新注册默认关闭；'
                             '--ensure-existing 时仅在显式指定后同步）')
    parser.add_argument('--credential-ref', default='deployment/test-bug')
    parser.add_argument('--ensure-existing', action='store_true', help='保留已注册仓库配置，仅同步本次发布与审批开关')
    parser.add_argument('--print-config', action='store_true', help='仅预览配置，不拉取或注册')
    args = parser.parse_args(argv)
    load_dotenv(ROOT / '.env', override=False, encoding='utf-8-sig')
    root = args.root.resolve()
    fields = registration_fields(root, publish_branch=args.publish_branch,
                                 require_approval=bool(args.require_approval), credential_ref=args.credential_ref)
    if args.print_config:
        print(json.dumps(fields, ensure_ascii=False, indent=2))
        return 0
    registry = RepositoryRegistry(args.registry.resolve(),
                                  authorized_roots=None if os.getenv('TRACEFIX_REPOSITORY_ROOTS') else [root.parent],
                                  data_root=args.data.resolve())
    try:
        existing = registry.get(fields['repo_id'])
    except RepositoryError as error:
        if error.code != 'REPOSITORY_NOT_FOUND':
            raise
        existing = None
    credential_ref = existing.get('credential_ref') if args.ensure_existing and existing else args.credential_ref
    if args.publish_branch:
        try:
            mapping = json.loads(os.getenv('TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP', '{}'))
        except json.JSONDecodeError:
            parser.error('TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP 必须是有效 JSON 对象')
        variable = mapping.get(credential_ref) if isinstance(mapping, dict) else None
        if not isinstance(variable, str) or not os.getenv(variable, '').strip():
            parser.error('启用分支发布前，请在部署环境映射 credential_ref 到已配置的 GitHub token 环境变量')
    if existing is None:
        configured = registry.register(fields)
    elif args.ensure_existing:
        switches = {'publish_branch': args.publish_branch}
        if args.require_approval is not None:
            switches['require_approval'] = args.require_approval
        changed = {key: value for key, value in switches.items() if existing.get(key, False) != value}
        configured = registry.update(fields['repo_id'], changed) if changed else existing
    else:
        configured = registry.update(fields['repo_id'], fields)
    print(json.dumps({'project_id': configured['repo_id'], 'root': configured['root'],
                      'commit': configured['commit'], 'checkout_mode': configured['checkout_mode'],
                      'publish_branch': configured['publish_branch'],
                      'require_approval': configured.get('require_approval', False)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
