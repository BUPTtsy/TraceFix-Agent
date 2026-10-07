"""Real HTTP integration test for the authored BugBoard fixture, without Docker."""
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def main():
    root=Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='tracefix-backend-') as folder:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        proc=subprocess.Popen(['node','server/index.mjs'],cwd=root/'backend/apps/console-api',
            env={**os.environ,'PORT':str(port),'BUGBOARD_DATA':str(Path(folder)/'data.json')},
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        def request(route,method='GET',data=None):
            req=urllib.request.Request(base+route,method=method,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=3) as response:return response.status,json.load(response)
        try:
            for _ in range(40):
                try:request('/health');break
                except Exception:time.sleep(0.1)
            else:raise RuntimeError('服务启动失败')
            assert request('/__reset','POST')[0]==200
            assert len(request('/api/tasks')[1])==6
            assert request('/api/tasks/1','PATCH',{'status':'Done'})[1]['status']=='Done'
            assert request('/api/tasks/1')[1]['status']=='Done'
            code,task=request('/api/tasks','POST',{'title':'Integration task'})
            assert code==201
            assert request('/api/tasks/'+str(task['id']),'DELETE')[0]==200
            assert len(request('/api/tasks')[1])==6
            print(json.dumps({'backend_http':'PASS','checks':7,'gui':False,'message':'后端 HTTP 集成检查通过'},ensure_ascii=False))
        finally:
            proc.terminate()
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()

if __name__=='__main__':main()
