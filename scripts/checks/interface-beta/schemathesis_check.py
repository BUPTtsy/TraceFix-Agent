from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit


class RequestLimit(Exception):
    pass


def check_schema(input_data: dict, output: Path, *, max_requests=30, timeout_seconds=30) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('Schemathesis 产物目录必须为空')
    result = {'status': 'inconclusive', 'passed': None, 'requests': 0, 'failures': [], 'coverage': []}
    requests = []
    try:
        import requests as http
        import schemathesis
        from hypothesis import HealthCheck, Phase, given, settings
        from schemathesis import GenerationMode
        from schemathesis.core.result import Ok
        from schemathesis.core.failures import FailureGroup
        url = input_data['url']
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                or input_data.get('isolation') != {'disposable': True, 'service_url': url}):
            raise ValueError('变异检查必须绑定 disposable 隔离 HTTP 服务')
        if not 1 <= max_requests <= 200 or not 1 <= timeout_seconds <= 300:
            raise ValueError('请求或时间额度超出允许范围')
        token = os.getenv('TRACEFIX_CONSOLE_API_TOKEN')
        headers = {'authorization': 'Bearer ' + token} if token else {}
        started = time.monotonic()
        session = http.Session()
        def call(case):
            if len(requests) >= max_requests or time.monotonic() - started >= timeout_seconds:
                raise RequestLimit('请求或时间额度已耗尽')
            remaining = timeout_seconds - (time.monotonic() - started)
            response = case.call(base_url=url.rstrip('/'), session=session, headers=headers,
                                 timeout=min(5, remaining), allow_redirects=False)
            record = {'method': case.method, 'path': case.path, 'body': case.body if isinstance(case.body, (dict, list, str, int, float, bool)) else None,
                      'path_parameters': case.path_parameters, 'query': case.query,
                      'status': response.status_code, 'response': response.text[:16000]}
            requests.append(record)
            try:
                case.validate_response(response, headers=headers)
            except (AssertionError, FailureGroup) as error:
                record['error'] = str(error)[:12000]
                result['failures'].append(record)
            return response
        schema = schemathesis.openapi.from_url(url.rstrip('/') + '/api/v1/openapi.json',
                                               headers=headers, timeout=5)
        (output / 'openapi.json').write_text(json.dumps(schema.raw_schema, ensure_ascii=False, indent=2), encoding='utf-8')
        if input_data.get('task'):
            response = call(schema['/api/v1/tasks']['POST'].Case(body=input_data['task'], media_type='application/json'))
            if response.status_code == 201:
                task_id = response.json()['task']['id']
                for route, method, body in [('/api/v1/tasks/{id}', 'GET', None),
                                             ('/api/v1/tasks/{id}/stop', 'POST', {})]:
                    call(schema[route][method].Case(path_parameters={'id': task_id},
                         **({'body': body, 'media_type': 'application/json'} if body is not None else {})))
                result['stateful'] = {'created_task_id': task_id, 'checks': ['create', 'read', 'stop']}
            else:
                result['stateful'] = {'status': 'inconclusive', 'error': '无法建立合法 Task 状态'}
        for loaded in schema.get_all_operations():
            if not isinstance(loaded, Ok):
                raise ValueError('OpenAPI operation 无法解析')
            operation = loaded.ok()
            for mode in (GenerationMode.POSITIVE, GenerationMode.NEGATIVE):
                @settings(max_examples=2, deadline=None, database=None, derandomize=True,
                          phases=[Phase.generate], suppress_health_check=list(HealthCheck))
                @given(case=operation.as_strategy(generation_mode=mode))
                def generated(case):
                    call(case)
                try:
                    generated()
                except RequestLimit:
                    raise
                except Exception as error:
                    result['coverage'].append({'path': operation.path, 'method': operation.method,
                                               'mode': mode.value, 'generation_error': str(error)[:1000]})
                    continue
                result['coverage'].append({'path': operation.path, 'method': operation.method, 'mode': mode.value})
        incomplete = any(item.get('generation_error') for item in result['coverage']) or result.get('stateful', {}).get('status') == 'inconclusive'
        status = 'fail' if result['failures'] else 'inconclusive' if incomplete else 'pass'
        result.update(status=status, passed=True if status == 'pass' else False if status == 'fail' else None,
                      elapsed_seconds=time.monotonic() - started)
    except RequestLimit as error:
        result.update(status='fail' if result['failures'] else 'inconclusive',
                      passed=False if result['failures'] else None, error=str(error))
    except Exception as error:
        result.update(status='fail' if result['failures'] else 'inconclusive',
                      passed=False if result['failures'] else None, error=f'{type(error).__name__}: {error}')
    finally:
        if 'session' in locals():
            session.close()
    result['requests'] = len(requests)
    result['binding'] = {key: input_data.get(key) for key in ('repo_id', 'commit')}
    result['evidence_refs'] = [str(output / name) for name in ('request.json', 'requests.json', 'openapi.json')]
    (output / 'request.json').write_text(json.dumps(input_data, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'requests.json').write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-requests', type=int, default=120)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--required', action='store_true')
    args = parser.parse_args()
    try:
        result = check_schema(json.loads(args.request.read_text(encoding='utf-8')), args.output,
                              max_requests=args.max_requests, timeout_seconds=args.timeout)
    except Exception as error:
        result = {'status': 'inconclusive', 'passed': None, 'error': str(error)}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['status'] == 'pass' else 1 if result['status'] == 'fail' else 2


if __name__ == '__main__':
    raise SystemExit(main())
