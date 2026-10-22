"""受控 MCP 场景：部署允许/拒绝两个 MCP 服务，校验宿主端策略与操作回执。

门禁原文：“须部署允许/拒绝两个 MCP 并校验宿主端策略与操作回执”。

拒绝侧（无需模型，宿主侧确定性校验）：
- SdkExecutionRequest 必须拒绝：白名单外 origin（拒绝 MCP）、stdio、带凭据 URL、http 明文、
  附带 headers 等额外字段、同主机不同端口；拒绝发生在请求构造阶段，不可能启动容器；
- 合法 MCP 绑定 + 出口网络策略不含 MCP origin 时，生产 Runner 的 _preflight 必须拒绝且不启动容器。

允许侧（真实模型 + 生产 Runner）：
- 在新建的 internal 网络 tf-sdk-egress-mcp 内部署 HTTPS MCP 夹具（mcp_fixture_server.py，
  两个端口分别是“允许”与“拒绝”服务），新建白名单代理 tf-sdk-egress-mcp-proxy；
- 白名单 = 模型 origin + 允许服务 origin；请求只绑定允许服务；
- 通过判定（全部满足）：init 中允许服务 status=connected；模型调用 mcp__allowed__record 且
  tool_result 非错误；宿主回执文件恰有本次 token 一条，且服务端随机 receipt_id 出现在 tool_result；
  拒绝服务无任何访问与回执；执行 SUCCEEDED。
- 夹具证书由本场景临时 CA 签发。worker 环境白名单不含 NODE_EXTRA_CA_CERTS、镜像只读，
  生产接口没有注入受控 CA 的途径，因此不做任何绕过；若 CLI 因证书不受信而无法连接，
  如实记为 not_run 并给出 blocker。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import secrets
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from sdk_live.common import (build_request, init_data, result_summary, run_execution, sdk_workspace,
                             tool_calls, tool_results)
from sdk_live.egress import ensure_egress

HERE = Path(__file__).resolve().parent
NETWORK = 'tf-sdk-egress-mcp'
PROXY = 'tf-sdk-egress-mcp-proxy'
FIXTURE = 'tf-sdk-mcp-fixture'
ALLOWED_HOST, ALLOWED_PORT = 'mcp-allowed.tracefix.test', 8443
DENIED_HOST, DENIED_PORT = 'mcp-denied.tracefix.test', 9443
ALLOWED_ORIGIN = f'https://{ALLOWED_HOST}:{ALLOWED_PORT}'
DENIED_ORIGIN = f'https://{DENIED_HOST}:{DENIED_PORT}'
SERVER_NAME = 'allowed'
TOOL_NAME = f'mcp__{SERVER_NAME}__record'


def docker(*arguments, check=True, timeout=120):
    result = subprocess.run(['docker', *arguments], capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'docker {arguments[0]} 失败: {result.stderr.strip()[:300]}')
    return result


def _short(content, limit=300):
    if isinstance(content, list):
        content = ' '.join(str(item.get('text', item)) if isinstance(item, dict) else str(item)
                           for item in content)
    return ('' if content is None else str(content))[:limit]


def _jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _containers(label):
    output = docker('ps', '-a', '--filter', 'label=' + label, '--format', '{{.Names}}', check=False).stdout
    return [name for name in output.split() if name]


# ---------------------------------------------------------------- 拒绝侧：宿主端策略

def _deny_cases(ctx, allowlisted_config):
    """在包含允许服务 origin 的白名单下，逐个构造越界 MCP 绑定，确认请求构造即被拒绝。"""
    cases = {
        'denied_origin_not_allowlisted': {'type': 'http', 'url': DENIED_ORIGIN + '/mcp'},
        'stdio_transport': {'type': 'stdio', 'command': '/usr/local/bin/python', 'args': ['-c', 'pass']},
        'credential_in_url': {'type': 'http', 'url': f'https://user:pass@{ALLOWED_HOST}:{ALLOWED_PORT}/mcp'},
        'plaintext_http_scheme': {'type': 'http', 'url': f'http://{ALLOWED_HOST}:{ALLOWED_PORT}/mcp'},
        'extra_headers_field': {'type': 'http', 'url': ALLOWED_ORIGIN + '/mcp',
                                'headers': {'Authorization': 'Bearer x'}},
        'same_host_other_port': {'type': 'http', 'url': f'https://{ALLOWED_HOST}:{DENIED_PORT}/mcp'},
        'sse_denied_origin': {'type': 'sse', 'url': DENIED_ORIGIN + '/sse'},
    }
    scoped = replace(ctx, config=allowlisted_config)
    outcomes = {}
    for name, server in cases.items():
        execution_id, workspace = sdk_workspace(scoped.child('deny-' + name.replace('_', '-')))
        try:
            build_request(scoped, execution_id, workspace, '拒绝侧探针', mcp_servers={'probe': server})
            outcomes[name] = {'rejected': False}
        except ValueError as error:
            # pydantic ValidationError 是 ValueError 子类；只取校验器的中文原因。
            message = str(error)
            reason = next((text for text in ('仅支持宿主冻结的受控远端 MCP 绑定', 'MCP 绑定超出已冻结出口')
                           if text in message), message[:160])
            outcomes[name] = {'rejected': True, 'reason': reason}
        outcomes[name]['containers'] = _containers('tracefix.execution=' + execution_id)
    # 对照：同一白名单下合法绑定必须被接受，证明上面的拒绝不是“全部拒绝”。
    execution_id, workspace = sdk_workspace(scoped.child('deny-control'))
    try:
        build_request(scoped, execution_id, workspace, '对照', mcp_servers={
            SERVER_NAME: {'type': 'http', 'url': ALLOWED_ORIGIN + '/mcp'}})
        control = {'accepted': True}
    except ValueError as error:
        control = {'accepted': False, 'error': str(error)[:200]}
    return outcomes, control


async def _preflight_case(ctx, allowlisted_config):
    """合法 MCP 绑定，但 Runner 使用的出口网络策略只含模型 origin：_preflight 必须拒绝且不启动容器。"""
    scoped = replace(ctx.child('deny-preflight'), config=allowlisted_config)
    execution_id, workspace = sdk_workspace(scoped)
    request = build_request(scoped, execution_id, workspace, '预检拒绝探针', mcp_servers={
        SERVER_NAME: {'type': 'http', 'url': ALLOWED_ORIGIN + '/mcp'}})
    outcome = {'runner_network': ctx.network}
    try:
        await run_execution(scoped, workspace, request)
        outcome.update(rejected=False)
    except PermissionError as error:
        outcome.update(rejected=True, reason=str(error)[:160])
    except Exception as error:  # 其他异常不算策略拒绝
        outcome.update(rejected=False, error_type=type(error).__name__, error=str(error)[:200])
    outcome['containers'] = _containers('tracefix.execution=' + execution_id)
    outcome['request_written'] = (workspace.execution_dir / 'control' / 'request.json').exists()
    return outcome


# ---------------------------------------------------------------- 允许侧：夹具部署

def _issue_certificates(directory: Path):
    """临时 CA + 服务端证书（SAN 同时覆盖允许/拒绝两个主机名）。CA 不注入任何系统信任库。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'TraceFix controlled_mcp fixture CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
              content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False,
              encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ALLOWED_HOST)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(ALLOWED_HOST), x509.DNSName(DENIED_HOST)]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (directory / 'ca.pem').write_bytes(ca.public_bytes(pem))
    (directory / 'server.pem').write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    (directory / 'server.key').write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
    return directory


def _start_fixture(image, pki: Path, receipts: Path):
    docker('rm', '-f', FIXTURE, check=False)
    receipts.mkdir(parents=True, exist_ok=True)
    docker('run', '-d', '--name', FIXTURE, '--network', NETWORK,
           '--network-alias', ALLOWED_HOST, '--network-alias', DENIED_HOST,
           '--label', 'tracefix.sdk_live=controlled_mcp',
           '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', '1000:1000',
           '--mount', f'type=bind,src={HERE.as_posix()},dst=/fixture,readonly',
           '--mount', f'type=bind,src={pki.as_posix()},dst=/pki,readonly',
           '--mount', f'type=bind,src={receipts.as_posix()},dst=/receipts',
           '--entrypoint', '/usr/local/bin/python', image, '-u', '/fixture/mcp_fixture_server.py',
           '--cert', '/pki/server.pem', '--key', '/pki/server.key', '--receipts', '/receipts',
           '--serve', f'allowed:{ALLOWED_PORT}', '--serve', f'denied:{DENIED_PORT}')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        logs = docker('logs', FIXTURE, check=False)
        if '"listening"' in logs.stdout:
            return
        if docker('container', 'inspect', FIXTURE, '--format', '{{.State.Running}}',
                  check=False).stdout.strip() != 'true':
            break
        time.sleep(0.5)
    raise RuntimeError('MCP 夹具未就绪: ' + (logs.stdout + logs.stderr)[-300:])


def _ip(container, network):
    output = docker('container', 'inspect', container, '--format',
                    '{{json .NetworkSettings.Networks}}', check=False).stdout
    try:
        return (json.loads(output).get(network) or {}).get('IPAddress')
    except (ValueError, AttributeError):
        return None


def _fixture_self_check(image, pki: Path):
    """在同一 internal 网络内用信任临时 CA 的 Python 客户端调用 initialize，证明夹具本身可用。"""
    program = ('import json,ssl,urllib.request as u;'
               'c=ssl.create_default_context(cafile="/pki/ca.pem");'
               f'r=u.Request("{ALLOWED_ORIGIN}/mcp",data=json.dumps({{"jsonrpc":"2.0","id":0,'
               '"method":"tools/list"}).encode(),headers={"Content-Type":"application/json",'
               '"Accept":"application/json, text/event-stream"});'
               'print(json.loads(u.urlopen(r,context=c,timeout=10).read())["result"]["tools"][0]["name"])')
    result = docker('run', '--rm', '--network', NETWORK, '--read-only', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges', '--user', '1000:1000',
                    '--mount', f'type=bind,src={pki.as_posix()},dst=/pki,readonly',
                    '--entrypoint', '/usr/local/bin/python', image, '-c', program, check=False)
    return {'ok': result.returncode == 0 and result.stdout.strip() == 'record',
            'output': (result.stdout or result.stderr).strip()[-200:]}


def _cli_mcp_log(workspace):
    """读取 CLI 写在 sdk-config 下的 MCP 连接日志（mcp-logs-<name>），只保留 debug/error 文本。"""
    lines = []
    for path in sorted((workspace.execution_dir / 'sdk-config').rglob(f'mcp-logs-{SERVER_NAME}/*.jsonl')):
        for item in _jsonl(path):
            text = item.get('error') or item.get('debug') or ''
            lines.append(str(text).splitlines()[0][:240] if text else '')
    return lines


def _teardown(created):
    """只删除本场景创建的资源：worker 容器、夹具、专属代理与网络（绝不触碰 tf-sdk-egress*）。"""
    removed = []
    for name in [*created.get('workers', []), FIXTURE, PROXY]:
        if docker('rm', '-f', name, check=False).returncode == 0:
            removed.append('container:' + name)
    if docker('network', 'rm', NETWORK, check=False).returncode == 0:
        removed.append('network:' + NETWORK)
    return removed


# ---------------------------------------------------------------- 场景入口

async def run(ctx) -> dict:
    model_origins = list(ctx.config.egress_allowlist)
    allowlist = sorted({*model_origins, ALLOWED_ORIGIN})
    mcp_config = ctx.config.model_copy(update={'egress_allowlist': allowlist})
    image = ctx.config.worker_image

    # 拒绝侧 1：请求构造阶段的宿主策略。
    cases, control = _deny_cases(ctx, mcp_config)
    # 拒绝侧 2：生产 Runner 预检（使用原出口网络，其策略不含 MCP origin）。
    preflight = await _preflight_case(ctx, mcp_config)
    deny_passed = (all(item['rejected'] and not item['containers'] for item in cases.values())
                   and control['accepted'] and preflight['rejected'] and not preflight['containers']
                   and not preflight['request_written'])
    deny_side = {'passed': deny_passed, 'cases': cases, 'control_allowed_binding': control,
                 'runner_preflight': preflight}

    # 允许侧：真实部署 + 真实模型。
    created = {'workers': []}
    allow = {'passed': False}
    blocker = ''
    try:
        egress = ensure_egress(allowlist, network=NETWORK, proxy=PROXY, image=image)
        allow_root = ctx.child('allow')
        pki = _issue_certificates(allow_root.root / 'pki')
        receipts = allow_root.root / 'receipts'
        _start_fixture(image, pki, receipts)
        allow['fixture_self_check'] = _fixture_self_check(image, pki)
        proxy_ip, fixture_ip = _ip(PROXY, NETWORK), _ip(FIXTURE, NETWORK)

        scoped = replace(allow_root, config=mcp_config, network=egress['network'],
                         egress_proxy=egress['egress_proxy'])
        token = 'tok-' + secrets.token_hex(6)
        prompt = (f'这是受控 MCP 能力探针。请调用 MCP 工具 `{TOOL_NAME}`，参数 token 精确为 `{token}`。'
                  '不要用 Bash、Read 或其他任何方式代替。调用成功后，在结构化结果 summary 中原样写出'
                  '工具返回的 receipt_id；工具不存在或调用失败时，在 summary 写明失败原因，unresolved 列出该步骤。')
        execution_id, workspace = sdk_workspace(scoped)
        created['workers'].append('tf-sdk-' + execution_id.lower())
        request = build_request(scoped, execution_id, workspace, prompt, tools=['Read', 'Glob', 'Grep'],
                                mcp_servers={SERVER_NAME: {'type': 'http', 'url': ALLOWED_ORIGIN + '/mcp'}})
        started = time.time()  # 之后的访问才属于 worker（排除夹具自检）
        result, events, _runner = await run_execution(scoped, workspace, request)

        init = init_data(events)
        mcp_status = init.get('mcp_servers') or []
        init_tools = [name for name in init.get('tools') or [] if name.startswith('mcp__')]
        calls = [call for call in tool_calls(events) if call['name'].startswith('mcp__') or
                 call['name'] in {'ToolSearch', 'ListMcpResourcesTool'}]
        results = tool_results(events)
        record_calls = []
        for call in calls:
            outcome = results.get(call['id'])
            record_calls.append({'name': call['name'], 'input': call['input'],
                'is_error': None if outcome is None else outcome['is_error'],
                'result': _short(outcome and outcome['content'])})
        allowed_receipts = _jsonl(receipts / 'allowed.jsonl')
        denied_receipts = _jsonl(receipts / 'denied.jsonl')
        access = [item for item in _jsonl(receipts / 'access.jsonl') if item.get('time', 0) >= started]
        cli_mcp_log = _cli_mcp_log(workspace)
        cert_errors = [line for line in cli_mcp_log if 'CERT' in line or 'certificate' in line]
        proxy_log = [line for line in docker('logs', PROXY, check=False).stdout.splitlines()
                     if '"allow"' in line or '"deny"' in line or 'upstream_error' in line]
        stderr_path = workspace.execution_dir / 'transcript' / 'worker.stderr'
        stderr_tail = stderr_path.read_text(encoding='utf-8', errors='replace')[-800:] if stderr_path.exists() else ''

        connected = any(item.get('name') == SERVER_NAME and item.get('status') == 'connected'
                        for item in mcp_status)
        successful = [item for item in record_calls if item['name'] == TOOL_NAME
                      and item['is_error'] is not True and token in json.dumps(item['input'])]
        matching = [item for item in allowed_receipts if item.get('token') == token]
        receipt_echoed = bool(matching) and any(matching[0]['receipt_id'] in item['result'] for item in successful)
        denied_touched = [item for item in access if item.get('server') == 'denied']
        peers = sorted({item.get('peer') for item in access if item.get('server') == 'allowed'})
        allow.update(
            execution=result_summary(result), init_mcp_servers=mcp_status, init_mcp_tools=init_tools,
            mcp_tool_calls=record_calls, receipts_allowed=allowed_receipts, receipts_denied=denied_receipts,
            access_events=[{k: item.get(k) for k in ('event', 'server', 'peer', 'method', 'rpc', 'tool', 'error')}
                           for item in access][:20],
            access_peers=peers, proxy_ip=proxy_ip, fixture_ip=fixture_ip,
            via_proxy=bool(peers) and peers == [proxy_ip],
            proxy_log=proxy_log[-10:], worker_stderr_tail=stderr_tail,
            cli_mcp_log_tail=cli_mcp_log[-6:], cli_cert_errors=cert_errors[:3],
            checks={'server_connected': connected, 'record_succeeded': bool(successful),
                    'single_matching_receipt': len(matching) == 1 and len(allowed_receipts) == 1,
                    'receipt_id_in_tool_result': receipt_echoed, 'denied_untouched': not denied_touched
                    and not denied_receipts, 'execution_succeeded': result.status == 'SUCCEEDED'})
        allow['passed'] = all(allow['checks'].values())

        if not allow['passed']:
            # 仅当 CLI 自身 MCP 日志明确报告证书不受信、且夹具对信任该 CA 的客户端可用时，才归因于 CA 注入缺失。
            if not connected and allow['fixture_self_check']['ok'] and cert_errors:
                blocker = ('CLI 经 HTTPS_PROXY CONNECT 到达受控 MCP 后 TLS 校验失败（SELF_SIGNED_CERT_IN_CHAIN），'
                           '夹具在信任临时 CA 的客户端下可用；worker 内 CLI 只信任内置/系统 CA，'
                           '而 claude_sdk_worker 环境白名单不含 NODE_EXTRA_CA_CERTS（或 CLAUDE_CODE_CERT_STORE）、镜像只读，'
                           '生产接口无法注入受控 CA。需生产代码支持宿主冻结并只读挂载受控 CA，'
                           '并在 worker 白名单/_cli_environment 中传递 NODE_EXTRA_CA_CERTS。')
    except Exception as error:
        allow.update(error_type=type(error).__name__, error=str(error)[:400])
    finally:
        removed = await asyncio.to_thread(_teardown, created)

    if deny_passed and allow['passed']:
        status = 'passed'
    elif not deny_passed:
        status = 'failed'
    elif blocker:
        status = 'not_run'  # 基础设施（受控 CA 注入）缺失，无法真实验证允许侧
    else:
        status = 'failed'
    return {'status': status, 'deny_side': deny_side, 'allow_side': allow, 'blocker': blocker,
            'allowlist': allowlist, 'docker_resources_removed': removed}
