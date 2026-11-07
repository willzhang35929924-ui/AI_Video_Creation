"""Project-owned opt-in guard; scoped wrappers never edit vendor/core files."""
from .guard import VideoFactoryLocalVLMGuard
from .compile_node import VideoFactoryLocalH3IRCompile

NODE_CLASS_MAPPINGS = {'VideoFactoryLocalVLMGuard': VideoFactoryLocalVLMGuard,
                       'VideoFactoryLocalH3IRCompile': VideoFactoryLocalH3IRCompile}
NODE_DISPLAY_NAME_MAPPINGS = {'VideoFactoryLocalVLMGuard': 'VideoFactory · Prepare Local 4B VLM',
                            'VideoFactoryLocalH3IRCompile':'VideoFactory · Local 4B OpenH3 Main'}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']
