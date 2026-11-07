"""Place the sole shot marker after an existing photographic style sentence."""
from __future__ import annotations

import ast
from contextvars import ContextVar
import functools
import hashlib
import importlib
import json
import logging
from pathlib import Path
import re
import sys
import threading
import types

VERSION = "videofactory-writer-envelope-format-v1"
PROFILE = "local-h3ir-qwen4b-v3"
MODEL = "Qwen/Qwen3-VL-4B-Instruct"
PINS = {
    "h3ir.prose": "4ad714c522e907e2990cc8f9f2bf68b82aaf31668bc98167a0ab5d4375746876",
    "h3ir.compile": "f96d893ff2b895c524329f11c10bed8999a5436d6c09ce64d95590819195772b",
    "v3_profile": "1ce73ec41735a5cc71bb7b6461e7ee1cb0759b3b3b71e98a7803e3a0cb6471f5",
}
SECTIONS = ("subject_definitions", "summary", "retention_analysis", "detailed_description",
            "overall_soundscape", "non_diegetic_music")
HEADING = re.compile(r"(?m)^[ \t]*([A-Za-z_][A-Za-z_0-9 -]*):")
MARKER = "[Shot 1]"
STYLE_SENTENCE = re.compile(
    r"The (?:target )?video (?:uses|has|adopts) an? (?:[A-Za-z]+(?:-[A-Za-z]+)? ){0,4}photographic"
    r"(?: [A-Za-z]+(?:-[A-Za-z]+)?){0,3} (?:style|look)\b[^.!?\r\n\[\]]*\.(?=[ \t\r\n])")
SCOPE_NAME = re.compile(r"\d{8}-\d{6}-\d{6}Z-[0-9a-f]{8}")
_LOCK = threading.RLock()
_INSTALLED = None
_STATUS = {"installation_status": "NOT_INSTALLED", "inference_status": "NOT_RUN"}
log = logging.getLogger("videofactory.writer_envelope_format")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _splice(text, changes):
    """Return exact UTF-8 edits against the original and their applied text."""
    edits = []
    for start, end, replacement in changes:
        edits.append({"start": len(text[:start].encode("utf-8")), "end": len(text[:end].encode("utf-8")),
                      "before": text[start:end], "after": replacement})
    raw = text.encode("utf-8")
    transformed = raw
    for edit in reversed(edits):
        start, end = edit["start"], edit["end"]
        if raw[start:end] != edit["before"].encode("utf-8"):
            raise RuntimeError("Writer format edit does not match original bytes")
        transformed = transformed[:start] + edit["after"].encode("utf-8") + transformed[end:]
    return transformed.decode("utf-8"), edits


def format_envelope(text):
    """Return (text, operation, UTF-8 edits) for the two known one-shot forms."""
    if not isinstance(text, str) or "\r" in text.replace("\r\n", ""):
        return None
    if "\r\n" in text and "\n" in text.replace("\r\n", ""):
        return None
    if re.search(r"(?m)^[ \t]*(?:```|~~~|#{1,6}[ \t])", text):
        return None
    headers = list(HEADING.finditer(text))
    if (tuple(m.group(1) for m in headers) != SECTIONS or text[:headers[0].start()].strip()
            or any(m.group() != m.group(1) + ":" for m in headers)):
        return None
    bounds = [(m.end(), headers[i + 1].start() if i + 1 < len(headers) else len(text))
              for i, m in enumerate(headers)]
    if any(not text[start:end].strip() for start, end in bounds):
        return None
    start, end = bounds[3]
    description = text[start:end]
    start += len(description) - len(description.lstrip(" \t\r\n"))
    newline = "\r\n" if "\r\n" in text else "\n"
    changes = []
    operation = "insert_missing_marker"
    if text.startswith(MARKER, start):
        separator = re.match(r"[ \t]*(?:\r?\n)?", text[start + len(MARKER):end])
        if not separator.group():
            return None
        marker_end = start + len(MARKER) + len(separator.group())
        changes.append((start, marker_end, ""))
        start = marker_end
        operation = "move_leading_marker"
    # Any remaining bracketed form could be a valid, repeated, malformed or
    # multi-shot marker. The native validator owns every such document.
    remainder = text[start:end]
    if any(ch in remainder for ch in "[]［］"):
        return None
    if any(match.group() != MARKER for match in re.finditer(r"[\[［][ \t]*shot[^\]\r\n］]*[\]］]",
            text[:bounds[3][0]] + text[end:], flags=re.I)):
        return None
    opening = STYLE_SENTENCE.match(remainder)
    if (opening is None or re.search(r"\b(?:not|non|no|never|unlike|instead|rather|avoid)\b", opening.group(), re.I)
            or not any(ch.isalnum() for ch in remainder[opening.end():])):
        return None
    position = start + opening.end()
    changes.append((position, position, newline + MARKER))
    formatted, edits = _splice(text, changes)
    return formatted, operation, edits


def _source(module, name):
    path = Path(module.__file__).resolve(strict=True)
    raw = path.read_bytes()
    if sha(raw) != PINS[name]:
        raise RuntimeError(f"Unsupported {name} source SHA256")
    return path, raw


def _verify_native(module, function_name, path, source):
    function = getattr(module, function_name)
    compiled = compile(source, str(path), "exec", dont_inherit=True, optimize=0)
    code, = [item for item in compiled.co_consts
             if isinstance(item, types.CodeType) and item.co_name == function_name]
    definition, = [item for item in ast.parse(source).body
                   if isinstance(item, ast.FunctionDef) and item.name == function_name]
    args = definition.args
    defaults = tuple(ast.literal_eval(value) for value in args.defaults) or None
    keyword = {arg.arg: ast.literal_eval(value) for arg, value in zip(args.kwonlyargs, args.kw_defaults)
               if value is not None} or None
    if (type(function) is not types.FunctionType or function.__globals__ is not module.__dict__
            or function.__closure__ is not None or hasattr(function, "__wrapped__")
            or function.__code__ != code or function.__code__.co_filename != str(path)
            or function.__defaults__ != defaults or function.__kwdefaults__ != keyword):
        raise RuntimeError(f"Native {function_name} or its wrapper differs from the pinned callable")
    return function


def _fixed_endpoint(address):
    return isinstance(address, str) and address.rstrip("/") in (
        "http://127.0.0.1:8772/v1", "http://localhost:8772/v1")


def _active_scope(backend, backend_type):
    if type(backend) is not backend_type or getattr(backend_type, "_factory_local_profile_version", None) != PROFILE:
        return None
    if backend.cfg.model != MODEL or not _fixed_endpoint(backend.cfg.base_url):
        return None
    chat = getattr(backend.chat, "__func__", None)
    once = getattr(backend._once, "__func__", None)
    if type(chat) is not types.FunctionType or type(once) is not types.FunctionType:
        return None
    namespace = chat.__globals__
    owner = sys.modules.get(namespace.get("__name__"))
    active = namespace.get("_ACTIVE")
    if (owner is None or owner.__dict__ is not namespace or once.__globals__ is not namespace
            or namespace.get("VERSION") != PROFILE or not isinstance(active, ContextVar)
            or chat.__code__.co_qualname != "install.<locals>.chat"
            or once.__code__.co_qualname != "install.<locals>.once"):
        return None
    scope = active.get()
    if scope is None:
        return None
    profile_path = Path(namespace.get("__file__", ""))
    if profile_path.name != "local_h3ir_backend_profile.py" or not profile_path.is_file():
        return None
    profile_path = profile_path.resolve()
    profile_source = profile_path.read_bytes()
    if sha(profile_source) != PINS["v3_profile"]:
        return None
    code = compile(profile_source, str(profile_path), "exec", dont_inherit=True, optimize=0)
    install_code = next(item for item in code.co_consts if isinstance(item, types.CodeType) and item.co_name == "install")
    methods = {item.co_name: item for item in install_code.co_consts if isinstance(item, types.CodeType)}
    if any(function.__code__ != methods[name] or Path(function.__code__.co_filename).resolve() != profile_path
           for name, function in (("chat", chat), ("once", once))):
        return None
    setup = scope.get("setup", {})
    if (setup.get("server") or setup.get("llm_model") != MODEL or not _fixed_endpoint(setup.get("llm_url"))
            or scope.get("compiler_documents") != 0 or type(scope.get("requests")) is not int):
        return None
    return scope


def _request_binding(directory, number, text):
    if not isinstance(directory, Path) or directory.parent.name != "h3_local_backend_profile_v3" or not SCOPE_NAME.fullmatch(directory.name):
        raise ValueError("Writer format requires the active v3 receipt directory")
    folder = directory / f"request-{number:03d}"
    for path in (directory, directory / "profile.json", folder, *(folder / name for name in (
            "receipt.json", "response.raw.bin", "response.json", "request.wire.json"))):
        if path.resolve(strict=True) != path.absolute() or path.is_symlink() or path.is_junction():
            raise ValueError("Writer receipt path is not ordinary")
    profile = json.loads((directory / "profile.json").read_bytes())
    receipt = json.loads((folder / "receipt.json").read_bytes())
    raw = (folder / "response.raw.bin").read_bytes()
    wire = (folder / "request.wire.json").read_bytes()
    response = json.loads(raw)
    choice = response["choices"][0]
    content = choice["message"]["content"]
    factory = response.get("factory_receipt", {})
    if (profile.get("profile") != PROFILE or profile.get("source_sha256") != PINS["v3_profile"]
            or (profile.get("compiler_source_gate") or {}).get("required_source") != "written"
            or receipt.get("version") != PROFILE or receipt.get("stage") != "prose"
            or receipt.get("status") != "PASS" or receipt.get("finish_reason") != "stop"
            or receipt.get("response_sha256") != sha(raw) or receipt.get("response_bytes") != len(raw)
            or receipt.get("wire_request_sha256") != sha(wire) or json.loads(wire).get("model") != MODEL
            or receipt.get("factory_receipt") != factory or factory.get("offline") is not True
            or factory.get("model_id") != MODEL or factory.get("gpu_cleanup", {}).get("released") is not True
            or response != json.loads((folder / "response.json").read_bytes())
            or choice.get("finish_reason") != "stop" or not isinstance(content, str) or content.strip() != text):
        raise ValueError("Writer output does not match its actual successful v3 response")
    return folder, {"request_number": number, "request_directory": folder.name,
                    "response_sha256": sha(raw), "wire_request_sha256": sha(wire)}


def _record_change(folder, request, before, after, operation, edits):
    record = {"schema_version": 1, "status": "APPLIED", "policy_version": VERSION,
              "operation": operation, "section": "detailed_description",
              "reason": "Place the sole shot marker after an existing photographic style sentence",
              "mode": "ref2va", "pinned_shots": 1, "generation_task": True,
              "locked_dialogue": [], "locked_onscreen_text": [], "visual_facts_added": False,
              "source_pins": dict(PINS), "offset_unit": "original_utf8_bytes", "edits": edits,
              "before_sha256": sha(before), "after_sha256": sha(after), **request}
    for name, payload in (("writer-format.before.txt", before), ("writer-format.after.txt", after),
                          ("writer-format.json", json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))):
        with (folder / name).open("xb") as stream:
            stream.write(payload)


def _wrap(native, backend_type, ref_mode):
    @functools.wraps(native)
    def compose(backend, brief, *args, **kwargs):
        eligible = (kwargs.get("mode") is ref_mode and kwargs.get("generation_task") is True
                    and kwargs.get("prompt_name") == "compose.v2.txt" and type(brief.shots) is int
                    and brief.shots == 1 and not brief.dialogue and not brief.onscreen_text)
        scope = _active_scope(backend, backend_type) if eligible else None
        before_count = scope["requests"] if scope is not None else None
        raw = native(backend, brief, *args, **kwargs)
        if scope is None or scope["requests"] != before_count + 1 or _active_scope(backend, backend_type) is not scope:
            return raw
        change = format_envelope(raw)
        if change is None:
            return raw
        formatted, operation, edits = change
        try:
            folder, request = _request_binding(scope["directory"], scope["requests"], raw)
            _record_change(folder, request, raw.encode("utf-8"), formatted.encode("utf-8"), operation, edits)
        except (OSError, TypeError, ValueError, KeyError, IndexError, AttributeError) as error:
            log.warning("Writer envelope left to native validation because receipt proof failed: %s", error)
            return raw
        return formatted
    return compose


def install():
    global _INSTALLED, _STATUS
    with _LOCK:
        prose = importlib.import_module("h3ir.prose")
        compiler = importlib.import_module("h3ir.compile")
        if _INSTALLED is not None and compiler.compose_brief is _INSTALLED:
            return dict(_STATUS)
        prose_path, prose_source = _source(prose, "h3ir.prose")
        compiler_path, compiler_source = _source(compiler, "h3ir.compile")
        native = _verify_native(prose, "compose_brief", prose_path, prose_source)
        _verify_native(compiler, "compile_brief", compiler_path, compiler_source)
        if compiler.compose_brief is not native or compiler.Backend is not prose.Backend:
            raise RuntimeError("Native compiler compose alias or Backend changed")
        wrapper = _wrap(native, prose.Backend, prose.Mode.REF2VA)
        compiler.compose_brief = wrapper
        _INSTALLED = wrapper
        _STATUS = {"installation_status": "INSTALLED", "policy_version": VERSION,
                   "prose_source": str(prose_path), "compiler_source": str(compiler_path),
                   "source_pins": dict(PINS), "inference_status": "NOT_RUN"}
        return dict(_STATUS)


class VideoFactoryWriterEnvelopeFormatStatus:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "read_status"
    CATEGORY = "VideoFactory/diagnostics"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls):
        return float("nan")

    def read_status(self):
        value = json.dumps(_STATUS, ensure_ascii=False, sort_keys=True)
        return {"ui": {"text": [value]}, "result": (value,)}
