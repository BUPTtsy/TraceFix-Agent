"""controlled_mcp 场景的受控 MCP 夹具：标准库实现的最小 Streamable HTTP（JSON 响应）端点。

只在本地 internal 网络内运行，不访问外网。每个监听端口代表一个独立 MCP 服务：
- 提供唯一工具 record(token)：把调用追加写入宿主可见的回执文件 <receipts>/<name>.jsonl，
  并返回服务端生成的随机 receipt_id（模型无法预知，用于交叉核对 transcript）；
- 所有 TLS 握手失败、HTTP 请求都写入 <receipts>/access.jsonl 与 stdout，作为“是否被访问、经谁访问”的证据。

用法（在 worker 镜像的 Python 3.12 中运行）：
    python mcp_fixture_server.py --cert C --key K --receipts DIR --serve allowed:8443 --serve denied:9443
"""

from __future__ import annotations

import argparse
import json
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOCK = threading.Lock()
TOOL = {'name': 'record', 'description': '把 token 写入受控回执文件并返回服务端回执编号。',
        'inputSchema': {'type': 'object', 'properties': {'token': {'type': 'string'}},
                        'required': ['token'], 'additionalProperties': False}}


def append(path: Path, record: dict):
    """线程安全地追加一行 JSON，并同步打印到 stdout 便于 docker logs 观察。"""
    line = json.dumps(record, ensure_ascii=False)
    with LOCK:
        with path.open('a', encoding='utf-8') as output:
            output.write(line + '\n')
        print(line, flush=True)


class TlsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, context, name, receipts):
        self.context, self.name, self.receipts = context, name, receipts
        super().__init__(address, handler)

    def get_request(self):
        # 逐连接握手，握手失败（例如客户端不信任证书）也记录下来，而不是被 socketserver 静默吞掉。
        sock, peer = self.socket.accept()
        try:
            return self.context.wrap_socket(sock, server_side=True), peer
        except (ssl.SSLError, OSError) as error:
            append(self.receipts / 'access.jsonl', {'event': 'tls_error', 'server': self.name,
                'peer': peer[0], 'error': f'{type(error).__name__}: {error}'[:300], 'time': time.time()})
            sock.close()
            raise


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):  # 统一走 access.jsonl
        return

    def _access(self, **fields):
        append(self.server.receipts / 'access.jsonl', {'event': 'http', 'server': self.server.name,
            'peer': self.client_address[0], 'method': self.command, 'path': self.path,
            'time': time.time(), **fields})

    def _send(self, status, body=None, headers=None):
        payload = b'' if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        if body is not None:
            self.send_header('Content-Type', 'application/json')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # 不提供服务端主动推送的 SSE 流；按 Streamable HTTP 规范返回 405。
        self._access(status=405)
        self._send(405, headers={'Allow': 'POST, DELETE'})

    def do_DELETE(self):
        self._access(status=200)
        self._send(200)

    def do_POST(self):
        if self.path.split('?', 1)[0] != '/mcp':
            self._access(status=404)
            return self._send(404)
        try:
            message = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'null')
        except (ValueError, UnicodeDecodeError):
            self._access(status=400)
            return self._send(400, {'jsonrpc': '2.0', 'id': None,
                                    'error': {'code': -32700, 'message': 'parse error'}})
        batch = isinstance(message, list)
        replies = [reply for item in (message if batch else [message])
                   if (reply := self._dispatch(item)) is not None]
        if not replies:
            return self._send(202)  # 仅通知/响应时按规范返回 202 且无正文
        self._send(200, replies if batch else replies[0], {'Mcp-Session-Id': self.server.session})

    def _dispatch(self, item):
        if not isinstance(item, dict):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}}
        method, identity, params = item.get('method'), item.get('id'), item.get('params') or {}
        self._access(rpc=method, rpc_id=identity, tool=params.get('name') if method == 'tools/call' else None)
        if identity is None:  # 通知（如 notifications/initialized）不回包
            return None
        if method == 'initialize':
            result = {'protocolVersion': params.get('protocolVersion') or '2025-06-18',
                      'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'tracefix-fixture-' + self.server.name, 'version': '1.0.0'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': [TOOL]}
        elif method == 'tools/call' and params.get('name') == 'record':
            token = str((params.get('arguments') or {}).get('token', ''))[:200]
            receipt_id = 'rcpt-' + secrets.token_hex(8)
            append(self.server.receipts / f'{self.server.name}.jsonl', {'server': self.server.name,
                'token': token, 'receipt_id': receipt_id, 'peer': self.client_address[0], 'time': time.time()})
            result = {'content': [{'type': 'text', 'text': f'recorded token={token} receipt_id={receipt_id}'}],
                      'isError': False}
        else:
            return {'jsonrpc': '2.0', 'id': identity, 'error': {'code': -32601, 'message': 'method not found'}}
        return {'jsonrpc': '2.0', 'id': identity, 'result': result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--receipts', required=True)
    parser.add_argument('--serve', action='append', required=True, help='name:port，可重复')
    args = parser.parse_args()
    receipts = Path(args.receipts)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    servers = []
    for spec in args.serve:
        name, _, port = spec.partition(':')
        server = TlsServer(('0.0.0.0', int(port)), Handler, context=context, name=name, receipts=receipts)
        server.session = secrets.token_hex(8)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({'event': 'listening', 'serve': args.serve}), flush=True)
    threading.Event().wait()


if __name__ == '__main__':
    main()
