"""Fixed optional Qwen3-VL-4B profile; shares the existing offline adapter."""
from __future__ import annotations

import hashlib
import os
import importlib.util
from pathlib import Path
import sys

BASE=Path(__file__).with_name('serve_screenplay_qwen.py')
BIND_HOST='127.0.0.1'
BIND_PORT=8772
spec=importlib.util.spec_from_file_location('video_factory_h3ir_qwen4b_base',BASE)
runtime=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=runtime
spec.loader.exec_module(runtime)

runtime.MODEL=Path(os.environ['VF_QWEN_MODEL']).resolve()
runtime.MODEL_ID='Qwen/Qwen3-VL-4B-Instruct'
runtime.PROFILE='h3ir-qwen4b-32k'
runtime.SERVICE_ID='video-factory-h3ir-qwen4b-v1'
runtime.MAX_CONTEXT_TOKENS=32768
runtime.MAX_PROMPT_TOKENS=32767
runtime.MAX_INPUT_CHARACTERS=500000
runtime.MAX_IMAGE_EDGE=1280
runtime.MIN_VISION_PIXELS=64*32*32
runtime.MAX_VISION_PIXELS=512*32*32
runtime.VRAM_PREFLIGHT=True
runtime.SAFE_TEXT_SDPA=True
base_bytes=BASE.read_bytes()
wrapper_bytes=Path(__file__).read_bytes()
runtime.SCRIPT_SHA=hashlib.sha256(b'video-factory-qwen-profile-v1\0'+base_bytes+b'\0'+wrapper_bytes).hexdigest()
runtime.SOURCE_COMPONENTS={'base_script_sha256':hashlib.sha256(base_bytes).hexdigest(),
                           'profile_script_sha256':hashlib.sha256(wrapper_bytes).hexdigest()}
app=runtime.app

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host=BIND_HOST,port=BIND_PORT,workers=1)
