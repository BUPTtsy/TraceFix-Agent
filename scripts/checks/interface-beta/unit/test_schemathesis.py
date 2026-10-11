import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


module_path = Path(__file__).parents[1] / 'schemathesis_check.py'
module_spec = importlib.util.spec_from_file_location('schemathesis_check', module_path)
checker = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(checker)


@pytest.mark.parametrize('invalid', [False, True])
def test_real_schema_generation_and_response_validation(tmp_path, invalid):
    operation = {'parameters': [{'name': 'limit', 'in': 'query', 'schema': {
        'type': 'integer', 'minimum': 0, 'maximum': 2}}], 'responses': {
        '200': {'description': 'ok', 'content': {'application/json': {'schema': {
            'type': 'object', 'required': ['value'], 'properties': {'value': {'type': 'integer'}}}}}},
        '400': {'description': 'invalid'}}}
    schema = {'openapi': '3.0.3', 'info': {'title': 'isolated', 'version': '1'},
              'paths': {'/api/v1/example': {'get': operation}}}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            values = query.get('limit', [])
            rejected = values and not (values[0].isdigit() and 0 <= int(values[0]) <= 2)
            self.send_response(400 if rejected else 200)
            self.send_header('content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(schema if self.path == '/api/v1/openapi.json'
                                        else {} if rejected else {'value': 'invalid' if invalid else 1}).encode())
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    try:
        result = checker.check_schema({'url': url, 'isolation': {'disposable': True, 'service_url': url},
                                       'repo_id': 'test', 'commit': 'revision'}, tmp_path / 'report',
                                      max_requests=20, timeout_seconds=10)
        assert result['status'] == ('fail' if invalid else 'pass'), result
        assert result['requests'] > 0
        assert (tmp_path / 'report/openapi.json').is_file()
    finally:
        server.shutdown()
        server.server_close()


def test_mutation_rejects_unbound_live_service(tmp_path):
    result = checker.check_schema({'url': 'http://127.0.0.1:9999'}, tmp_path / 'report')
    assert result['status'] == 'inconclusive'
    assert result['requests'] == 0
