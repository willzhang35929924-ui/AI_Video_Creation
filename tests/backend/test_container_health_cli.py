"""Exercise the image's health CLI in a fresh process against real local HTTP."""
import json
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


@pytest.mark.parametrize('ready,expected_exit',[(True,0),(False,1)])
def test_health_cli_checks_readiness_without_importing_startup_stack(ready,expected_exit):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body=json.dumps({'application_ready':ready}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    code='''
import sys,runpy
class StartupImportForbidden:
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='app.core.deployment' or fullname.split('.')[0] in {'sqlalchemy','alembic','fastapi','starlette','pydantic'}:
            raise RuntimeError('Health check imported the database/startup stack')
sys.meta_path.insert(0,StartupImportForbidden())
sys.argv=['container_entrypoint','healthcheck']
runpy.run_module('app.container_entrypoint',run_name='__main__')
'''
    env=os.environ.copy();env['APP_PORT']=str(server.server_port)
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[2]/'backend')
    try:
        result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=20)
        assert result.returncode==expected_exit,result.stderr
        assert requests==['/api/health/runtime']
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
