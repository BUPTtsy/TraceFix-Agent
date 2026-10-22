"""SDK 受控出口代理（本地实测用）：只放行白名单 origin 的 HTTPS CONNECT，其余一律 403。"""

import argparse
import asyncio
import json
from urllib.parse import urlsplit


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def reply(writer, status):
    writer.write(f'HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
    await writer.drain()
    writer.close()


async def handle(reader, writer, allowed):
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 30)
    except Exception:
        writer.close()
        return
    line = head.split(b'\r\n', 1)[0].decode('latin-1')
    parts = line.split()
    if len(parts) != 3 or parts[0] != 'CONNECT':
        log(event='deny', reason='not_connect', request=line[:120])
        return await reply(writer, '403 Forbidden')
    host, _, port = parts[1].rpartition(':')
    if (host.lower(), port) not in allowed:
        log(event='deny', reason='not_allowlisted', target=parts[1])
        return await reply(writer, '403 Forbidden')
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 30)
    except Exception as error:
        log(event='upstream_error', target=parts[1], error=type(error).__name__)
        return await reply(writer, '502 Bad Gateway')
    log(event='allow', target=parts[1])
    writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
    await writer.drain()
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow', action='append', required=True, help='精确 HTTPS origin，可重复')
    parser.add_argument('--port', type=int, default=3128)
    args = parser.parse_args()
    allowed = set()
    for origin in args.allow:
        parsed = urlsplit(origin)
        if parsed.scheme != 'https' or not parsed.hostname:
            raise SystemExit('只接受 HTTPS origin')
        allowed.add((parsed.hostname.lower(), str(parsed.port or 443)))
    server = await asyncio.start_server(lambda r, w: handle(r, w, allowed), '0.0.0.0', args.port)
    log(event='listening', port=args.port, allowed=sorted(f'{h}:{p}' for h, p in allowed))
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
