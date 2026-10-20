import json
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[2] / 'artifacts/harness-migration/sdk-worker-isolation'
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'source'
    source.mkdir(exist_ok=True)
    (source / 'app.py').write_bytes(b'value = 1\n')
    image = subprocess.check_output(['docker', 'image', 'inspect', 'tracefix-sdk-worker:0.2.168',
                                     '--format', '{{.Id}}'], text=True).strip()
    program = '''import asyncio,importlib.metadata,json,os,socket,subprocess,sys
from pathlib import Path
from types import SimpleNamespace
from tracefix.agents import claude_sdk_worker
from tracefix.execution.candidate_export import _snapshot
import claude_agent_sdk
root=Path('/workspace')
assert os.getuid()!=0
assert not (root/'.git').exists()
assert not (root/'.env').exists()
assert not Path('/var/run/docker.sock').exists()
try:
 assert not Path('/root/.claude').exists()
except PermissionError: pass
try:
 Path('/opt/unapproved').write_text('denied')
 raise AssertionError('rootfs writable')
except PermissionError: pass
except OSError as error:
 assert error.errno==30
try:
 socket.create_connection(('1.1.1.1',443),timeout=2)
 raise AssertionError('network enabled')
except OSError: pass
(root/'app.py').write_bytes(b'value = 2\\n')
binary=Path(claude_agent_sdk.__file__).parent/'_bundled'/'claude'
version=subprocess.check_output([str(binary),'--version'],text=True).strip()
assert importlib.metadata.version('claude-agent-sdk')=='0.2.168'
assert version.startswith('2.1.301')
request_path=Path('/tmp/request.json')
request_path.write_text('{}')
os.environ['HOST_SECRET_PROBE']='must-be-removed'
os.environ['TRACEFIX_SDK_WORKER']='1'
os.environ['ANTHROPIC_API_KEY']='fake-probe-key'
os.environ['CLAUDE_CONFIG_DIR']='/tmp/sdk-config'
claude_sdk_worker.SdkExecutionRequest=SimpleNamespace(model_validate_json=lambda raw:SimpleNamespace(workspace=root))
class FakeAdapter:
 async def start(self,request,emit):
  assert 'HOST_SECRET_PROBE' not in os.environ
  return SimpleNamespace(status='SUCCEEDED',model_dump_json=lambda **options:'{"status":"SUCCEEDED"}')
claude_sdk_worker.ClaudeSdkAdapter=FakeAdapter
assert asyncio.run(claude_sdk_worker.run_worker(request_path,Path('/tmp/result.json')))==0
assert 'HOST_SECRET_PROBE' not in os.environ
print(json.dumps({'uid':os.getuid(),'python_version':sys.version.split()[0],'sdk_version':importlib.metadata.version('claude-agent-sdk'),'cli_version':version,'snapshot':_snapshot(root).model_dump(),'rootfs_readonly':True,'network_denied':True,'host_metadata_absent':True,'worker_environment_allowlist':True}))
'''
    command = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--user', '1000:1000', '--tmpfs', '/tmp:rw,size=64m,mode=1777',
               '--mount', 'type=bind,src=' + source.as_posix() + ',dst=/workspace',
               '--entrypoint', '/usr/local/bin/python', image, '-c', program]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    evidence = {'image': image, 'exit_code': result.returncode,
                'stdout': result.stdout, 'stderr': result.stderr,
                'persistent_write': (source / 'app.py').read_bytes() == b'value = 2\n',
                'model_live': 'not_executed: no credential and enforced egress deployment'}
    (root.parent / 'sdk-worker-isolation-result.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False))
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
