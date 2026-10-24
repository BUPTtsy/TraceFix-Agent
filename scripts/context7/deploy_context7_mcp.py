from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/packages/agent/src'))

from dotenv import load_dotenv

from tracefix.connectors.context7_mcp import (
    CONTEXT7_URL, Context7MCPConnector, Context7MCPError, context7_enabled,
)


async def inspect_deployment(verify=False):
    connector = Context7MCPConnector()
    report = {'deployment': await connector.probe(), 'real_read': {'status': 'not_run'},
        'runtime_integration': {'supervisor_tools': ['context7.resolve-library-id', 'context7.query-docs'],
            'sdk_server': {'context7': {'type': 'http', 'url': CONTEXT7_URL}}}}
    if verify:
        try:
            resolved = await connector.call('resolve-library-id', {
                'libraryName': 'React', 'query': 'React useEffect cleanup documentation'})
            libraries = re.findall(r'Context7-compatible library ID:\s*(/\S+)', resolved['content'])
            library_id = next((value for value in libraries if value.startswith((
                '/facebook/react', '/websites/react_dev', '/reactjs/react.dev'))), None)
            if library_id is None:
                raise Context7MCPError('verification_library_missing')
            document = await connector.call('query-docs', {
                'libraryId': library_id, 'query': 'useEffect cleanup function example'})
            if 'useEffect' not in document['content']:
                raise Context7MCPError('verification_document_missing')
            report['real_read'] = {'status': 'passed', 'library_id': library_id,
                'resolve_digest': resolved['provenance']['content_digest'],
                'document_digest': document['provenance']['content_digest'],
                'document_bytes': document['provenance']['returned_bytes']}
        except Context7MCPError as error:
            report['real_read'] = {'status': 'failed', 'code': error.code}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='部署并校验 Context7 官方 HTTP MCP 接入')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare', action='store_true', help='启动时校验 initialize、工具契约和 ping')
    mode.add_argument('--verify', action='store_true', help='额外执行真实库解析和文档读取')
    parser.add_argument('--report', type=Path, default=ROOT / '.tracefix/context7/deployment-report.json')
    args = parser.parse_args(argv)
    load_dotenv(ROOT / '.env', override=False, encoding='utf-8-sig', interpolate=False)
    try:
        if not context7_enabled():
            report = {'deployment': {'status': 'disabled'}, 'real_read': {'status': 'not_run'}}
        else:
            report = asyncio.run(inspect_deployment(args.verify))
        code = 2 if report['real_read']['status'] == 'failed' else 0
    except Exception as error:
        report = {'deployment': {'status': 'failed', 'code': getattr(error, 'code', 'deployment_failed')},
            'real_read': {'status': 'not_passed' if args.verify else 'not_run'}}
        print('Context7 MCP 部署未完成，请检查网络和服务配额。', file=sys.stderr)
        code = 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
