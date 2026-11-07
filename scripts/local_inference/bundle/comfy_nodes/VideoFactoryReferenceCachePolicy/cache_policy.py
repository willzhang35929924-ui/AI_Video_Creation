"""Restrict explicitly declared reference units on the pinned local analyser."""
from __future__ import annotations

import ast
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import importlib
import json
import logging
from pathlib import Path
import threading
import types

POLICY_VERSION = "videofactory-reference-cache-policy-v2r1"
UNIT_NAMESPACE = "videofactory/reference-analysis-explicit-unit/v2"
V1_VERSION = "videofactory-reference-cache-policy-v1"
V1_NAMESPACE = "videofactory/reference-analysis-effective-inputs/v1"
MODEL = "Qwen/Qwen3-VL-4B-Instruct"
ENDPOINTS = frozenset(("http://127.0.0.1:8772/v1", "http://localhost:8772/v1"))
SOURCE_PINS = {
    "analyse": "fc2814f60951e76acb384ce05937568f85430699aee88e83f2ee00fd77f59d50",
    "backend": "b5d219f873a7c7b60fea19d44f4273c3b3f09fdaf9d51131d937f281c1959d39",
    "compile": "f96d893ff2b895c524329f11c10bed8999a5436d6c09ce64d95590819195772b",
}
UNIT_RULES = {
    "product-unit:": (
        "object", "Identify one visually discernible physical product as a coherent whole. "
        "For a box, its visible face text, printed illustrations or mascots, and any visibly attached "
        "ribbon are attributes of that same product. Exclude those features and nearby props from "
        "the subject list. Describe the physical product in the descriptor and put only observed "
        "features in attributes."),
    "character-unit:": (
        "person", "Identify one visually discernible foreground person as a coherent whole. "
        "Hair, garments, and worn accessories are attributes of that person. Exclude those features, "
        "depicted people on clothing or signs, and background figures from the subject list. Describe "
        "only the portions and features visible in the image. Treat multiple views as one person "
        "only when the visible reference sheet supports that reading."),
}
_LOCAL_BATCH = ContextVar("videofactory_reference_unit_local_batch", default=False)
_LOCK = threading.RLock()
_INSTALLED = None
_STATUS = {"installation_status": "NOT_INSTALLED", "inference_status": "NOT_RUN"}
log = logging.getLogger("videofactory.reference_cache_policy_v2")


class CachePolicyError(RuntimeError):
    """An unsupported analyser or an unresolved reference unit blocks compilation."""


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _code_payload(value):
    # Exact live source code comparison without marshal intern-table artifacts.
    if isinstance(value, types.CodeType):
        names = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
                 "co_stacksize", "co_flags", "co_code", "co_consts", "co_names", "co_varnames",
                 "co_filename", "co_name", "co_qualname", "co_firstlineno", "co_linetable",
                 "co_exceptiontable", "co_freevars", "co_cellvars")
        return ("code", tuple((name, _code_payload(getattr(value, name))) for name in names))
    if isinstance(value, tuple):
        return ("tuple", tuple(_code_payload(item) for item in value))
    if isinstance(value, frozenset):
        return ("frozenset", frozenset(_code_payload(item) for item in value))
    if type(value) is float:
        return ("float", value.hex())
    if type(value) in (str, bytes, int, bool, complex) or value is None:
        return (type(value).__name__, value)
    raise CachePolicyError(f"Unsupported native code constant: {type(value).__name__}")


def _pinned_source(module, name):
    path = Path(module.__file__).resolve(strict=True)
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_PINS[name]:
        raise CachePolicyError(f"Unsupported h3ir.{name} source SHA256")
    return path, source


def _check_functions(module, path, source, names):
    compiled = compile(source, str(path), "exec", dont_inherit=True, optimize=0)
    expected = {item.co_name: item for item in compiled.co_consts if isinstance(item, types.CodeType)}
    definitions = {item.name: item for item in ast.parse(source).body if isinstance(item, ast.FunctionDef)}
    for name in names:
        function = getattr(module, name)
        args = definitions[name].args
        defaults = tuple(ast.literal_eval(value) for value in args.defaults) or None
        kwdefaults = {arg.arg: ast.literal_eval(value) for arg, value in zip(args.kwonlyargs, args.kw_defaults)
                      if value is not None} or None
        if (type(function) is not types.FunctionType or function.__globals__ is not module.__dict__
                or function.__closure__ is not None or hasattr(function, "__wrapped__")
                or function.__defaults__ != defaults or function.__kwdefaults__ != kwdefaults
                or _code_payload(function.__code__) != _code_payload(expected[name])):
            raise CachePolicyError(f"Native callable mismatch or unknown wrapper: {name}")


def _native_image_contract(source):
    values = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("IMAGE_SCHEMA", "IMAGE_INSTRUCTIONS"):
                values[name] = ast.literal_eval(node.value)
    return values["IMAGE_SCHEMA"], values["IMAGE_INSTRUCTIONS"]


def _matches_backend(backend, backend_type):
    if type(backend) is not backend_type:
        return False
    cfg = backend.cfg
    address = cfg.base_url
    return cfg.model == MODEL and isinstance(address, str) and address.rstrip("/") in ENDPOINTS


def _unit(ref):
    if ref.kind.value != "image" or not isinstance(ref.note, str):
        return None
    note = ref.note
    direct = next((prefix for prefix in UNIT_RULES if note.startswith(prefix)), None)
    if direct is not None:
        return direct
    # The pinned tray.note_for prefixes a validated label and exactly ': '.
    # Inspect one prefix for scope selection while native analysis gets ref.note.
    label, separator, caller_note = note.partition(": ")
    if not separator or not label.isascii() or not label.replace("-", "").isalnum():
        return None
    return next((prefix for prefix in UNIT_RULES if caller_note.startswith(prefix)), None)


def _effective_inputs(ref):
    # Match the installed v1 cache for the paths that use ordinary analysis.
    material = {"note": str(ref.note) if ref.note else ""}
    if ref.kind.value == "image":
        provenance = ref.provenance
        selected = (provenance.get("source_prompt") or provenance.get("edit_instruction")) if provenance else None
        material["provenance_hint"] = str(selected) if selected else ""
        material["composition"] = ref.composition
    else:
        material["provenance_hint"] = "NOT_READ_BY_NATIVE_ANALYSER"
        material["composition"] = "NOT_READ_BY_NATIVE_ANALYSER"
    return material


def _priority_instruction(prefix):
    kind, rule = UNIT_RULES[prefix]
    return (f"Priority reference scope from VideoFactory {POLICY_VERSION} ({prefix}). "
            "For the subjects field, follow this scope before the generic catalogue instructions. "
            f"{rule} Use at most one subject of kind {kind}. When the image does not establish "
            "one coherent visible subject of that kind, return an empty subjects array. "
            "The caller note selects the scope. Establish visual details from the image and abstain "
            "when uncertain. Keep the other ImageCard fields factual and retain the identity/pose split.")


def _validate_subjects(subjects, prefix, origin):
    kind, _ = UNIT_RULES[prefix]
    if isinstance(subjects, list) and len(subjects) == 1 and isinstance(subjects[0], dict):
        subject = subjects[0]
        descriptor = subject.get("descriptor")
        if (subject.get("kind") == kind and isinstance(descriptor, str)
                and descriptor.strip().casefold() not in ("", "unknown", "uncertain", "unobservable")):
            return
    raise CachePolicyError(f"{origin} did not establish one {kind} subject for {prefix}")


class _ImageBackendProxy:
    """Forward the pinned native image call through the original Backend."""

    def __init__(self, backend, prefix, native_schema, native_instructions):
        self.backend = backend
        self.prefix = prefix
        self.native_schema = native_schema
        self.native_instructions = native_instructions

    @property
    def cfg(self):
        return self.backend.cfg

    def json_call(self, messages, schema, **kwargs):
        if (schema != self.native_schema or len(messages) != 2
                or messages[0] != {"role": "system", "content": self.native_instructions}
                or messages[1].get("role") != "user"):
            raise CachePolicyError("Unexpected native ImageCard request contract")
        content = messages[1].get("content")
        if not isinstance(content, list) or sum(part.get("type") == "image_url" for part in content) != 1:
            raise CachePolicyError("Explicit reference unit requires the native image input")
        focused = deepcopy(schema)
        subjects = focused["properties"]["subjects"]
        subjects["maxItems"] = 1
        subjects["items"]["properties"]["kind"]["enum"] = [UNIT_RULES[self.prefix][0]]
        system = {"role": "system", "content": _priority_instruction(self.prefix) + "\n\n" + self.native_instructions}
        reply = self.backend.json_call([system, messages[1]], focused, **kwargs)
        _validate_subjects(reply.get("subjects") if isinstance(reply, dict) else None, self.prefix, "Model ImageCard")
        return reply


def _make_wrappers(analyser, native_schema, native_instructions):
    native_key = analyser._cache_key
    native_image = analyser.analyse_image
    native_batch = analyser.analyse_all
    backend_type = analyser.Backend

    def cache_key(ref, model):
        original = native_key(ref, model)
        if model != MODEL:
            return original
        material = {"namespace": V1_NAMESPACE, "policy_version": V1_VERSION, "native_key": original,
                    "analysis_effective_inputs": _effective_inputs(ref)}
        prefix = _unit(ref) if _LOCAL_BATCH.get() else None
        if prefix:
            material.update(namespace=UNIT_NAMESPACE, policy_version=POLICY_VERSION,
                            unit={"prefix": prefix, "kind": UNIT_RULES[prefix][0],
                                  "instruction_sha256": _digest(_priority_instruction(prefix)),
                                  "native_image_schema_sha256": _digest(native_schema)})
        return _digest(material)[:24]

    def analyse_image(backend, ref, *, seed=None):
        prefix = _unit(ref) if _matches_backend(backend, backend_type) else None
        if prefix is None:
            return native_image(backend, ref, seed=seed)
        log.info("%s applies %s to ImageCard %s", POLICY_VERSION, prefix, ref.sha256[:12])
        proxy = _ImageBackendProxy(backend, prefix, native_schema, native_instructions)
        return native_image(proxy, ref, seed=seed)

    def analyse_all(backend, refs, *, use_cache=True, seed=None, transcripts=None):
        token = _LOCAL_BATCH.set(_matches_backend(backend, backend_type))
        try:
            units = [(ref, _unit(ref)) for ref in refs] if _LOCAL_BATCH.get() else []
            for ref, prefix in units:
                if prefix and any(other.sha256 == ref.sha256 and cache_key(other, MODEL) != cache_key(ref, MODEL)
                                  for other in refs):
                    raise CachePolicyError("One media SHA has incompatible explicit reference inputs in this batch")
            cards = native_batch(backend, refs, use_cache=use_cache, seed=seed, transcripts=transcripts)
            for ref, prefix in units:
                if not prefix:
                    continue
                card = cards.get(ref.sha256)
                if (card is None or card.sha256 != ref.sha256 or card.kind is not analyser.AssetKind.IMAGE
                        or card.model_id != MODEL or card.analyzer_version != analyser.ANALYZER_VERSION):
                    raise CachePolicyError("Explicit reference card does not match the analysed media/model")
                _validate_subjects(card.subjects, prefix, "Returned or cached ImageCard")
            return cards
        finally:
            _LOCAL_BATCH.reset(token)

    return {"_cache_key": cache_key, "analyse_image": analyse_image, "analyse_all": analyse_all}


def install():
    """Verify the native analyser and compiler alias before installing in memory."""
    global _INSTALLED, _STATUS
    with _LOCK:
        analyser = importlib.import_module("h3ir.analyse")
        backend = importlib.import_module("h3ir.backend")
        compiler = importlib.import_module("h3ir.compile")
        if _INSTALLED is not None:
            if (all(getattr(analyser, name) is function for name, function in _INSTALLED.items())
                    and compiler.analyse_all is _INSTALLED["analyse_all"]):
                return status()
            raise CachePolicyError("Installed reference unit wrappers changed")
        try:
            analyse_path, analyse_source = _pinned_source(analyser, "analyse")
            backend_path, backend_source = _pinned_source(backend, "backend")
            compile_path, _ = _pinned_source(compiler, "compile")
            _check_functions(analyser, analyse_path, analyse_source,
                             ("_cache_key", "_cache_path", "load_cached", "save_cached", "image_mime",
                              "analyse_image", "analyse_video", "analyse_all"))
            _check_functions(backend, backend_path, backend_source, ("user_message", "image_data_url"))
            schema, instructions = _native_image_contract(analyse_source)
            if (analyser.IMAGE_SCHEMA != schema or analyser.IMAGE_INSTRUCTIONS != instructions
                    or analyser.ANALYZER_VERSION != "5" or analyser.Backend is not backend.Backend
                    or analyser.user_message is not backend.user_message or compiler.analyse_all is not analyser.analyse_all):
                raise CachePolicyError("Pinned ImageCard globals or compiler analysis alias differ")
            replacements = _make_wrappers(analyser, schema, instructions)
            verified = {"installation_status": "INSTALLED", "policy_version": POLICY_VERSION,
                       "unit_namespace": UNIT_NAMESPACE, "ordinary_cache_namespace": V1_NAMESPACE,
                       "target_model_exact": MODEL, "target_endpoints": sorted(ENDPOINTS),
                       "unit_markers": {prefix: kind for prefix, (kind, _) in UNIT_RULES.items()},
                       "source_sha256": {str(path): SOURCE_PINS[name] for name, path in
                                         (("analyse", analyse_path), ("backend", backend_path), ("compile", compile_path))},
                       "native_image_schema_sha256": _digest(schema),
                       "request_provenance": "Native image and caller note through original Backend. The v3 profile captures the focused outgoing system and ImageCard schema when its compiler scope is active.",
                       "inference_status": "NOT_RUN", "visual_quality_status": "NOT_REVIEWED"}
            for name, function in replacements.items():
                setattr(analyser, name, function)
            compiler.analyse_all = replacements["analyse_all"]
            _INSTALLED = replacements
            _STATUS = verified
            return status()
        except Exception as error:
            _STATUS = {"installation_status": "BLOCKED", "policy_version": POLICY_VERSION,
                       "reason": str(error), "inference_status": "NOT_RUN"}
            raise


def status():
    return deepcopy(_STATUS)


class VideoFactoryReferenceCachePolicyStatus:
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
        value = json.dumps(status(), ensure_ascii=False, sort_keys=True)
        return {"ui": {"text": [value]}, "result": (value,)}
