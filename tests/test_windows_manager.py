"""Cross-platform contract tests; native Windows execution still requires Windows."""
import ctypes
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1]/'VideoFactory管理'
sys.path.insert(0,str(ROOT))
import cloud_backend as cloud
import windows_manager as wm

class Function:
    def __init__(self,fn): self.fn=fn
    def __call__(self,*args): return self.fn(*args)

class FakeWinCred:
    def __init__(self):
        self.data={}; self.error=1168; self.keep=None
        self.CredWriteW=Function(self.write); self.CredReadW=Function(self.read)
        self.CredDeleteW=Function(self.delete); self.CredFree=Function(lambda _:None)
    def write(self,ptr,flags):
        obj=ctypes.cast(ptr,ctypes.POINTER(cloud.WindowsCredentialManager.CREDENTIAL)).contents
        self.data[obj.TargetName]=ctypes.string_at(obj.CredentialBlob,obj.CredentialBlobSize)
        return 1
    def read(self,target,kind,flags,out):
        if target not in self.data: self.error=1168; return 0
        value=ctypes.create_string_buffer(self.data[target]); obj=cloud.WindowsCredentialManager.CREDENTIAL()
        obj.CredentialBlob=ctypes.cast(value,ctypes.c_void_p);obj.CredentialBlobSize=len(self.data[target]); ptr=ctypes.pointer(obj)
        self.keep=(value,obj,ptr); ctypes.cast(out,ctypes.POINTER(ctypes.POINTER(cloud.WindowsCredentialManager.CREDENTIAL)))[0]=ptr
        return 1
    def delete(self,target,kind,flags):
        if target not in self.data: self.error=1168; return 0
        del self.data[target];self.error=1168;return 1

class WindowsManagerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='win32','requires Windows Credential Manager')
    def test_windows_native_credential_roundtrip(self):
        store=cloud.WindowsCredentialManager(); ident='test-'+uuid.uuid4().hex
        try:
            store.set(ident,'temporary-test-only')
            self.assertEqual(store.get(ident),'temporary-test-only')
        finally: store.delete(ident)
        with self.assertRaises(RuntimeError): store.get(ident)
    def test_credential_roundtrip_and_deletion(self):
        fake=FakeWinCred();store=cloud.WindowsCredentialManager(fake,lambda:fake.error)
        store.set('test-id','密钥-example');self.assertEqual(store.get('test-id'),'密钥-example')
        store.delete('test-id');store.delete('test-id')
        with self.assertRaises(RuntimeError):store.get('test-id')
        with self.assertRaises(ValueError):store.set('test-id','x'*2561)
    def test_actions_use_project_scoped_commands(self):
        calls=[]
        def call(args,**kwargs):calls.append((args,kwargs));return 'OK'
        with patch.object(wm,'docker',side_effect=call),patch.object(wm,'check_engine'):
            self.assertIn('项目已启动',wm.execute('start'))
            self.assertIn('本项目已停止',wm.execute('stop'))
        self.assertEqual(calls[0][0],['up','-d','--wait','--wait-timeout','180'])
        self.assertEqual(calls[1][0],['stop','-t','10'])
    def test_only_local_windows_docker_context_is_allowed(self):
        def response(endpoint):
            return [SimpleNamespace(returncode=0,stdout=endpoint),SimpleNamespace(returncode=0,stdout=b'linux\n')]
        with patch.dict(wm.os.environ,{},clear=True),patch.object(wm.subprocess,'run',side_effect=response(b'npipe:////./pipe/dockerDesktopLinuxEngine\n')):
            wm.check_engine()
        with patch.dict(wm.os.environ,{},clear=True),patch.object(wm.subprocess,'run',side_effect=response(b'tcp://remote.example:2376\n')):
            with self.assertRaises(RuntimeError):wm.check_engine()
        with patch.dict(wm.os.environ,{'DOCKER_HOST':'tcp://example:2376'},clear=True),patch.object(wm.subprocess,'run') as runner:
            with self.assertRaises(RuntimeError):wm.check_engine()
            runner.assert_not_called()
    def test_host_cleanup_preserves_profiles(self):
        with tempfile.TemporaryDirectory() as t,patch.object(wm,'ROOT',Path(t)):
            root=Path(t); profile=root/'云端配置'/('a'*32);profile.mkdir(parents=True)
            (profile/'profile.json').write_text('{}');(profile/'history.json').write_text('[]')
            videos=root/'云端视频';videos.mkdir();(videos/'video.mp4').write_bytes(b'video')
            wm.clean_host_data()
            self.assertTrue((profile/'profile.json').exists());self.assertFalse((profile/'history.json').exists())
            self.assertFalse((videos/'video.mp4').exists())
    def test_cleanup_failure_stops_before_host_deletion(self):
        with patch.object(wm,'check_engine'),patch.object(wm,'docker',side_effect=RuntimeError('fail')),patch.object(wm,'clean_host_data') as clean:
            with self.assertRaises(RuntimeError):wm.execute('clear')
            clean.assert_not_called()

if __name__=='__main__':unittest.main()
