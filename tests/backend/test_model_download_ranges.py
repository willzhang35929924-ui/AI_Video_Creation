"""Downloader protocol tests with byte fixtures; never model inference evidence."""
import hashlib
import io
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import deployment_common as download


class RangeResponse(io.BytesIO):
    status=206
    def __init__(self,data,start,end,total):
        super().__init__(data);self.headers={'Content-Range':f'bytes {start}-{end}/{total}'}
    def geturl(self):return 'https://huggingface.co/official/pinned/model'


def fixture_spec(tmp_path,data):
    return {'url':'https://huggingface.co/official/pinned/model','target':str(tmp_path/'model.bin'),
        'partial':str(tmp_path/'model.partial'),'expected_bytes':len(data),
        'expected_hash':hashlib.sha256(data).hexdigest(),'hash_algorithm':'sha256'}


def fake_ranges(monkeypatch,data,calls):
    monkeypatch.setattr(download,'PARALLEL_RANGE_BYTES',4)
    def opened(request,timeout):
        raw=request.get_header('Range').removeprefix('bytes=');start,end=map(int,raw.split('-'))
        calls.append((start,end));return RangeResponse(data[start:end+1],start,end,len(data))
    monkeypatch.setattr(download.urllib.request,'urlopen',opened)


def test_parallel_ranges_reuse_prefix_and_verified_target(tmp_path,monkeypatch):
    data=b'0123456789abcdefghijklmnopqrstuvwxyz';spec=fixture_spec(tmp_path,data);calls=[]
    Path(spec['partial']).write_bytes(data[:5]);fake_ranges(monkeypatch,data,calls)
    result=download.download_component(spec,workers=4)
    assert result.read_bytes()==data and min(start for start,_ in calls)==5
    count=len(calls);download.download_component(spec,workers=4)
    assert len(calls)==count and not Path(spec['partial']).exists()


def test_crash_mid_merge_resumes_completed_ranges_without_redownload(tmp_path,monkeypatch):
    import shutil
    data=b'0123456789abcdefghijklmnop';spec=fixture_spec(tmp_path,data);calls=[]
    Path(spec['partial']).write_bytes(data[:5]);fake_ranges(monkeypatch,data,calls)
    original=shutil.copyfileobj
    def interrupted(source,destination,length):
        destination.write(source.read(3));raise OSError('simulated merge interruption')
    monkeypatch.setattr(shutil,'copyfileobj',interrupted)
    with pytest.raises(OSError):download.download_component(spec,workers=2)
    assert Path(spec['partial']).read_bytes()==data[:8]
    count=len(calls);monkeypatch.setattr(shutil,'copyfileobj',original)
    assert download.download_component(spec).read_bytes()==data
    assert len(calls)==count


def test_ignored_range_never_publishes_or_changes_prefix(tmp_path,monkeypatch):
    data=b'0123456789';spec=fixture_spec(tmp_path,data);Path(spec['partial']).write_bytes(data[:5])
    monkeypatch.setattr(download,'PARALLEL_RANGE_BYTES',4)
    def ignored(request,timeout):
        result=RangeResponse(data,0,9,10);result.status=200;return result
    monkeypatch.setattr(download.urllib.request,'urlopen',ignored)
    with pytest.raises(ValueError):download.download_component(spec,workers=2)
    assert not Path(spec['target']).exists() and Path(spec['partial']).read_bytes()==data[:5]


def test_full_digest_rejects_corrupt_ranges(tmp_path,monkeypatch):
    data=b'0123456789abcdefghijk';spec=fixture_spec(tmp_path,data);calls=[]
    fake_ranges(monkeypatch,b'x'*len(data),calls)
    with pytest.raises(ValueError,match='hash'):download.download_component(spec,workers=2)
    assert not Path(spec['target']).exists() and Path(spec['partial']).stat().st_size==len(data)
