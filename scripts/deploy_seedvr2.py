"""Pinned SeedVR2 deployment and native Comfy workflow preparation.

Only the two approved Comfy-Org files are downloaded. This module never imports
Torch, installs dependencies, changes Comfy startup/defaults, or submits a GPU job
unless the separate explicit ``submit`` command is requested.
"""
from __future__ import annotations

import argparse
import collections
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

REPO = "Comfy-Org/SeedVR2"
COMMIT = "df48879708206a403d2a61acd55578c2e80fd233"
SPECS = (
    ("diffusion_models/seedvr2_3b_fp8_e4m3fn.safetensors", 3392794232,
     "a0226eaa2c3e6f47ae5ce83225120f16479da890ced1a3bc32b1a14619787914"),
    ("vae/seedvr2_ema_vae_fp16.safetensors", 501324814,
     "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1"),
)
RUNTIME = Path("E:/VideoFactoryRuntime")
MODELS = Path("E:/Models/H3R2V")
DTYPE_BYTES = {"BOOL": 1, "U8": 1, "I8": 1, "F8_E4M3": 1,
               "F8_E5M2": 1, "F8_E4M3FN": 1, "I16": 2, "U16": 2,
               "F16": 2, "BF16": 2, "I32": 4, "U32": 4, "F32": 4,
               "I64": 8, "U64": 8, "F64": 8}
MAX_HEADER = 32 * 1024 * 1024


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def within(path, parent):
    resolved, base = Path(path).resolve(), Path(parent).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path escapes expected root: {path}")
    return resolved


def checked_plan(path):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if plan["repo"] != REPO or plan["commit"] != COMMIT:
        raise ValueError("Plan repository/revision differs from pinned approved source")
    if len(plan["files"]) != len(SPECS):
        raise ValueError("Plan must contain exactly the two approved components")
    for entry, (name, size, digest) in zip(plan["files"], SPECS):
        if (entry["repo_path"], entry["expected_bytes"], entry["expected_sha256"]) != (name, size, digest):
            raise ValueError("Plan component differs from approved source")
        if entry["url"] != f"https://huggingface.co/{REPO}/resolve/{COMMIT}/{name}":
            raise ValueError("Unexpected download URL")
        if within(entry["target"], MODELS) != (MODELS / name).resolve():
            raise ValueError("Unexpected component target")
        within(entry["partial"], RUNTIME / "temp")
    return plan


def inspect_header_bytes(data, total_size):
    if len(data) < 8:
        raise ValueError("Truncated safetensors length")
    length = struct.unpack("<Q", data[:8])[0]
    if length <= 2 or length > MAX_HEADER or 8 + length > total_size:
        raise ValueError("Invalid safetensors header length")
    if len(data) != 8 + length:
        raise ValueError("Incomplete header read")
    head = json.loads(data[8:])
    tensors = {key: val for key, val in head.items() if key != "__metadata__"}
    if not tensors:
        raise ValueError("No tensor entries")
    ranges = []
    for name, val in tensors.items():
        dtype = val["dtype"]
        if dtype not in DTYPE_BYTES or any(not isinstance(x, int) or x < 0 for x in val["shape"]):
            raise ValueError(f"Invalid dtype/shape: {name}")
        start, end = val["data_offsets"]
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            raise ValueError(f"Invalid tensor offsets: {name}")
        expected = math.prod(val["shape"]) * DTYPE_BYTES[dtype]
        if end - start != expected:
            raise ValueError(f"Tensor byte length mismatch: {name}")
        ranges.append((start, end, name))
    cursor = 0
    for start, end, name in sorted(ranges):
        if start != cursor:
            raise ValueError(f"Overlapping or non-contiguous tensor payload: {name}")
        cursor = end
    if cursor + 8 + length != total_size:
        raise ValueError("Tensor payload does not cover exact file length")
    metadata = head.get("__metadata__", {})
    summary = {"header_bytes": length, "tensor_count": len(tensors),
               "dtype_counts": dict(collections.Counter(v["dtype"] for v in tensors.values())),
               "payload_size_valid": True, "metadata_keys": sorted(metadata),
               "detection_keys": {k: tensors.get(k) for k in (
                   "blocks.31.mlp.all.proj_in_gate.weight", "positive_conditioning",
                   "negative_conditioning", "decoder.up_blocks.2.upsamplers.0.upscale_conv.weight")}}
    return summary


def inspect_local(path):
    path = Path(path)
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ValueError("Truncated safetensors file")
        size = struct.unpack("<Q", prefix)[0]
        if size > MAX_HEADER:
            raise ValueError("Header exceeds read bound")
        data = prefix + stream.read(size)
    return inspect_header_bytes(data, path.stat().st_size)


def verify(path, entry):
    path = Path(path)
    if path.stat().st_size != entry["expected_bytes"]:
        raise ValueError(f"Wrong existing file size: {path.name}")
    digest = sha(path)
    if digest != entry["expected_sha256"]:
        raise ValueError(f"Wrong file SHA256: {path.name}; existing file preserved")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest,
            "header": inspect_local(path), "verified_at_utc": now(), "loaded": False}


def request(url, start=0, end=None):
    headers = {"User-Agent": "VideoFactory-SeedVR2-Pinned-Deployment/1", "Accept-Encoding": "identity"}
    if start or end is not None:
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)


def remote_prefix(entry):
    # Never read the complete large file for metadata and never log signed redirects.
    with request(entry["url"], 0, 7) as response:
        if response.status != 206 or not response.headers.get("Content-Range", "").startswith("bytes 0-7/"):
            raise ValueError("Source did not honor bounded header Range")
        prefix = response.read(9)
    if len(prefix) != 8:
        raise ValueError("Remote prefix length mismatch")
    length = struct.unpack("<Q", prefix)[0]
    if length > MAX_HEADER:
        raise ValueError("Remote header exceeds bound")
    with request(entry["url"], 0, 7 + length) as response:
        content_range = response.headers.get("Content-Range", "")
        if response.status != 206 or content_range != f"bytes 0-{7 + length}/{entry['expected_bytes']}":
            raise ValueError("Remote header Content-Range mismatch")
        data = response.read(9 + length)
    summary = inspect_header_bytes(data, entry["expected_bytes"])
    if entry["repo_path"].startswith("diffusion_models/"):
        if not all(summary["detection_keys"].get(k) for k in (
                "blocks.31.mlp.all.proj_in_gate.weight", "positive_conditioning", "negative_conditioning")):
            raise ValueError("FP8 checkpoint lacks current native SeedVR2 3B/conditioning detection keys")
        if not any(k.startswith("F8_") for k in summary["dtype_counts"]):
            raise ValueError("Requested FP8 checkpoint has no FP8 tensors")
    elif not summary["detection_keys"].get("decoder.up_blocks.2.upsamplers.0.upscale_conv.weight"):
        raise ValueError("VAE lacks current native SeedVR VAE detection key")
    return summary


def plan_command(args):
    if Path(args.output).exists():
        raise FileExistsError("Download plan exists; preserve it and choose a new filename for a revised plan")
    source = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    data = source["sources"]["seedvr_comfy"]["data"]
    if data["id"] != REPO or data["sha"] != COMMIT:
        raise ValueError("Primary-source manifest revision changed; review before deployment")
    expected = {x["path"]: x for x in data["files"]}
    files = []
    for name, size, digest in SPECS:
        current = expected[name]
        if current["bytes"] != size or current["lfs"]["sha256"] != digest:
            raise ValueError("Primary-source size/SHA mismatch")
        files.append({"repo_path": name, "url": f"https://huggingface.co/{REPO}/resolve/{COMMIT}/{name}",
                      "expected_bytes": size, "expected_sha256": digest,
                      "target": str(MODELS / name),
                      "partial": str(RUNTIME / "temp" / (digest + ".safetensors.partial")),
                      "existing_target": (MODELS / name).exists()})
    disk = shutil.disk_usage(Path("E:/"))
    total = sum(x[1] for x in SPECS)
    reserve = 20 * 1024 ** 3
    required = total * 2 + reserve
    if disk.free < required:
        raise ValueError("Insufficient E disk space for files + retry/copy margin +20GiB reserve")
    out = {"created_at_utc": now(), "status": "PLANNED_NOT_DOWNLOADED", "repo": REPO,
           "commit": COMMIT, "license": "Apache-2.0 per primary-source repo metadata",
           "source_manifest": str(Path(args.sources).resolve()), "source_manifest_sha256": sha(args.sources),
           "disk_e": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free,
                      "planned_weight_bytes": total, "temporary_copy_margin_bytes": total,
                      "reserved_after_margin_bytes": reserve, "required_free_bytes": required},
           "files": files, "excluded": ["3B Int8", "7B", "existing H3", "existing Klein", "existing RealESRGAN"],
           "gpu_submission": "NOT AUTHORIZED BY THIS COMMAND", "existing_files_policy": "Verify exact bytes/SHA/header; never skip merely by name or overwrite invalid existing file",
           "cache_temp_roots": [str(RUNTIME / "temp"), "E:/Models/.cache/huggingface", "E:/Models/.cache/torch"]}
    write_json(args.output, out)
    print(json.dumps({"status": out["status"], "plan": str(Path(args.output).resolve()), "planned_bytes": total}))


def setup_dirs():
    for name in ("apps", "envs", "temp", "workflows", "manifests", "outputs"):
        within(RUNTIME / name, RUNTIME).mkdir(parents=True, exist_ok=True)
    for name in ("huggingface", "torch"):
        within(Path("E:/Models/.cache") / name, "E:/Models/.cache").mkdir(parents=True, exist_ok=True)


@contextmanager
def exclusive_download():
    """A crashed process releases the OS lock; a stale lock file is harmless."""
    import msvcrt
    setup_dirs()
    lock = within(RUNTIME / "temp" / "seedvr2_download.lock", RUNTIME / "temp")
    with lock.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Another SeedVR deployment owns the download lock; no partial file opened") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def inspect_command(args):
    plan = checked_plan(args.plan)
    out = {"captured_at_utc": now(), "repo": REPO, "commit": COMMIT,
           "native_support": "HEADER_MATCHED_CURRENT_NATIVE_DETECTION_NOT_RUNTIME_TESTED",
           "model_loaded": False, "files": [{"repo_path": x["repo_path"], "url": x["url"],
               "header": remote_prefix(x)} for x in plan["files"]]}
    write_json(args.output, out)
    print(json.dumps({"status": out["native_support"], "report": str(Path(args.output).resolve())}))


def download_command(args):
    plan = checked_plan(args.plan)
    setup_dirs()
    component = getattr(args, "component", "all")
    entries = [entry for entry in plan["files"] if component == "all" or
               entry["repo_path"].split("/")[0] == component]
    if not entries:
        raise ValueError("No approved components selected")
    receipt = {"started_at_utc": now(), "repo": REPO, "commit": COMMIT,
               "status": "RUNNING", "component_selection": component, "model_loaded": False, "files": []}
    write_json(args.output, receipt)
    for entry in entries:
        target, partial = Path(entry["target"]), Path(entry["partial"])
        result = {"repo_path": entry["repo_path"], "target": str(target), "url": entry["url"], "attempts": []}
        receipt["files"].append(result)
        if target.exists():
            result.update(verify(target, entry)); result["status"] = "REUSED_VERIFIED"
            write_json(args.output, receipt)
            continue
        result["remote_header"] = remote_prefix(entry)
        for attempt in range(1, 9):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > entry["expected_bytes"]:
                raise ValueError("Existing partial exceeds expected bytes; preserved for inspection")
            if offset == entry["expected_bytes"]:
                break
            item = {"attempt": attempt, "resume_offset": offset, "at_utc": now()}
            result["attempts"].append(item)
            try:
                with request(entry["url"], offset) as response:
                    if offset and (response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")):
                        raise ValueError("Server ignored resume Range; partial retained without appending")
                    mode = "ab" if offset else "wb"
                    with partial.open(mode) as stream:
                        last_report = offset
                        while chunk := response.read(4 * 1024 * 1024):
                            stream.write(chunk)
                            size = stream.tell()
                            if size > entry["expected_bytes"]:
                                raise ValueError("Download exceeds pinned expected bytes")
                            if size - last_report >= 128 * 1024 * 1024:
                                item["downloaded_bytes"] = size
                                write_json(args.output, receipt)
                                print(json.dumps({"file": target.name, "bytes": size, "expected": entry["expected_bytes"]}), flush=True)
                                last_report = size
                item["status"] = "TRANSFER_FINISHED"
                if partial.stat().st_size == entry["expected_bytes"]:
                    break
                item["status"] = "INCOMPLETE_RETRY"
            except Exception as error:
                # Avoid error strings that could contain signed CDN redirect URLs.
                item["status"] = "RETRY"; item["error_type"] = type(error).__name__
                if attempt == 8:
                    receipt["status"] = "FAILED_TRANSFER"; write_json(args.output, receipt)
                    raise RuntimeError(f"Download failed for {target.name}: {type(error).__name__}") from None
            write_json(args.output, receipt)
            time.sleep(min(2 ** attempt, 30))
        try:
            result.update(verify(partial, entry))
        except Exception as error:
            result.update(status="FAILED_INTEGRITY", error_type=type(error).__name__)
            receipt["status"] = "FAILED_INTEGRITY"
            write_json(args.output, receipt)
            raise
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError("Target appeared during download; not overwritten")
        os.replace(partial, target)
        result.update(path=str(target), status="DOWNLOADED_VERIFIED")
        write_json(args.output, receipt)
    receipt.update(status="VERIFIED_NOT_LOADED", completed_at_utc=now())
    write_json(args.output, receipt)
    manifest_name = "seedvr2_deployment.json" if component == "all" else f"seedvr2_download_{component}.json"
    write_json(RUNTIME / "manifests" / manifest_name, receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(Path(args.output).resolve())}))


def api(base, path, payload=None):
    if base.rstrip("/") != "http://127.0.0.1:8189":
        raise ValueError("Only the dedicated project ComfyUI endpoint is permitted")
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=body,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


class LocalGPUQueueBusyError(RuntimeError):
    pass


def _refused(error):
    pending=[error];seen=set()
    while pending:
        item=pending.pop()
        if id(item) in seen:continue
        seen.add(id(item))
        if isinstance(item,ConnectionRefusedError) or getattr(item,'winerror',None)==10061 or getattr(item,'errno',None) in {61,111,10061}:
            return True
        pending.extend(value for value in [getattr(item,'__cause__',None),getattr(item,'__context__',None),getattr(item,'reason',None)] if isinstance(value,BaseException))
        pending.extend(value for value in getattr(item,'exceptions',()) if isinstance(value,BaseException))
    return False


def local_queues_idle():
    """Known same-card local Comfy servers only; never free an external queue."""
    results={}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in (8188,8189):
        try:
            with opener.open(f'http://127.0.0.1:{port}/queue',timeout=5) as response:
                value=json.load(response)
            if not isinstance(value.get('queue_running'),list) or not isinstance(value.get('queue_pending'),list):
                raise ValueError('Unknown Comfy queue response')
        except Exception as error:
            if port==8188 and _refused(error):
                results[str(port)]={'state':'not_started_connection_refused'};continue
            raise LocalGPUQueueBusyError(f'Local Comfy{port} queue state unknown; waiting without submit') from None
        results[str(port)]={'state':'busy' if value['queue_running'] or value['queue_pending'] else 'idle',
                            'running':len(value['queue_running']),'pending':len(value['queue_pending'])}
        if value['queue_running'] or value['queue_pending']:
            raise LocalGPUQueueBusyError(f'Local Comfy{port} queue is busy; no submission performed')
    return results


def upload_video(base, path, source_sha, frames):
    """Use core's upload boundary, never bypass its path validation with a link."""
    if base.rstrip("/") != "http://127.0.0.1:8189":
        raise ValueError("Only the dedicated project ComfyUI endpoint is permitted")
    boundary = "SeedVR2Upload" + uuid.uuid4().hex
    filename = f"seedvr2_{source_sha}_{frames}.mp4"
    parts = []
    for key, value in (("type", "input"), ("subfolder", "seedvr2_tests"), ("overwrite", "false")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
    parts.append(Path(path).read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(base.rstrip("/") + "/upload/image", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as response:
        uploaded = json.load(response)
    relative = str(Path(uploaded.get("subfolder", "")) / uploaded["name"]).replace("\\", "/")
    if Path(relative).is_absolute() or ".." in Path(relative).parts or uploaded.get("type") != "input":
        raise ValueError("Unexpected native upload path/type")
    argv = api(base, "/system_stats")["system"]["argv"]
    input_root = Path(argv[argv.index("--input-directory") + 1])
    uploaded_path = within(input_root / relative, input_root)
    expected_sha = sha(path)
    if sha(uploaded_path) != expected_sha:
        raise ValueError("Actual Comfy upload SHA differs from frozen E input")
    return {"response": uploaded, "relative_path": relative, "actual_path": str(uploaded_path),
            "sha256": expected_sha, "endpoint": base.rstrip("/") + "/upload/image"}


def run_process(args):
    process = subprocess.run([str(x) for x in args], capture_output=True)
    if process.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: " + process.stderr.decode("utf-8", "replace")[-2000:])
    return process.stdout


def probe(path, executable):
    value = json.loads(run_process([executable, "-v", "error", "-count_frames", "-show_streams",
                                   "-show_format", "-of", "json", path]))
    video = next(x for x in value["streams"] if x.get("codec_type") == "video")
    return {"width": video["width"], "height": video["height"],
            "frames": int(video["nb_read_frames"]), "fps": video["avg_frame_rate"],
            "duration": float(value["format"]["duration"]), "codec": video["codec_name"],
            "pixel_format": video["pix_fmt"],
            "audio": [x for x in value["streams"] if x.get("codec_type") == "audio"],
            "raw": value}


def audio_hash(path, executable):
    return run_process([executable, "-v", "error", "-i", path, "-map", "0:a", "-c", "copy",
                        "-f", "hash", "-hash", "sha256", "-"]).decode("ascii").strip()


def graph_value(value):
    """Only canonicalize exactly equal integral float/int JSON representations.

    Comfy casts FLOAT widgets during prompt validation. Keep booleans, strings,
    values, node/link/reference order and all other fields exact; no rounding.
    """
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite graph numeric value")
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {k: graph_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [graph_value(v) for v in value]
    return value


def graph_digest(graph):
    return hashlib.sha256(json.dumps(graph_value(graph), sort_keys=True).encode()).hexdigest()


def graph_numeric_coercions(frozen, actual, path=""):
    if isinstance(frozen, dict) and isinstance(actual, dict):
        return [entry for key in frozen.keys() & actual.keys()
                for entry in graph_numeric_coercions(frozen[key], actual[key], path + "/" + key)]
    if isinstance(frozen, list) and isinstance(actual, list):
        return [entry for i, (left, right) in enumerate(zip(frozen, actual))
                for entry in graph_numeric_coercions(left, right, path + "/" + str(i))]
    if {type(frozen), type(actual)} == {int, float} and frozen == actual:
        return [{"path": path, "frozen": frozen, "actual": actual,
                 "frozen_type": type(frozen).__name__, "actual_type": type(actual).__name__}]
    return []


def prepare_command(args):
    plan = checked_plan(args.plan)
    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError("Prepared evidence already exists; choose a new report path")
    # Verify the exact two files even when they already existed before deployment.
    verification = [verify(x["target"], x) for x in plan["files"]]
    original_sha = sha(source)
    original_probe = probe(source, args.ffprobe)
    count = args.frames or original_probe["frames"]
    if count < 1 or count > original_probe["frames"]:
        raise ValueError("Requested sample frame count is outside the source")
    if args.width % 2 or args.height % 2:
        raise ValueError("Target dimensions must be even")
    if args.chunk_frames < 5 or (args.chunk_frames - 1) % 4:
        raise ValueError("Temporal chunk frames must be a4n+1 count of at least5")
    if abs(args.width / args.height - original_probe["width"] / original_probe["height"]) > 0.001:
        raise ValueError("This fixed workflow preserves source aspect ratio; target ratio differs")
    setup_dirs()
    input_copy = within(RUNTIME / "temp" / f"seedvr2_source_{original_sha}_{count}.mp4", RUNTIME / "temp")
    if count == original_probe["frames"]:
        if input_copy.exists():
            if sha(input_copy) != original_sha:
                raise ValueError("Existing E input copy differs; preserved")
        else:
            with source.open("rb") as src, input_copy.open("xb") as dst:
                shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
    elif not input_copy.exists():
        fps_num, fps_den = map(int, original_probe["fps"].split("/"))
        duration = count * fps_den / fps_num
        run_process([args.ffmpeg, "-v", "error", "-nostdin", "-n", "-i", source,
                     "-map", "0:v:0", "-map", "0:a?", "-frames:v", str(count),
                     "-t", str(duration), "-c:v", "libx264", "-crf", "16", "-preset", "fast",
                     "-c:a", "aac", input_copy])
    input_probe = probe(input_copy, args.ffprobe)
    if input_probe["frames"] != count:
        raise ValueError("Prepared input frame count mismatch")
    template = Path(args.workflow).resolve()
    workflow = json.loads(template.read_text(encoding="utf-8"))
    graph = workflow["prompt"]
    upload = upload_video(args.base_url, input_copy, original_sha, count)
    graph["1"]["inputs"]["file"] = upload["relative_path"]
    graph["3"]["inputs"].update(width=args.width, height=args.height)
    graph["8"]["inputs"]["chunking_mode.frames_per_chunk"] = args.chunk_frames
    graph["10"]["inputs"]["seed"] = args.seed
    graph["15"]["inputs"]["filename_prefix"] = "seedvr2_tests/" + output.stem + "_" + uuid.uuid4().hex[:8]
    graph_sha = hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()
    prepared = {"status": "PREPARED_NOT_SUBMITTED", "created_at_utc": now(), "method": "SeedVR2 3B FP8 native temporal video restoration",
                "temporal_model": True, "repo": REPO, "commit": COMMIT, "models": verification,
                "original_source": str(source), "original_source_sha256": original_sha,
                "original_probe": original_probe, "input_copy": str(input_copy),
                "input_sha256": sha(input_copy), "input_probe": input_probe,
                "comfy_upload": upload,
                "sample_only": count != original_probe["frames"], "expected_frames": count,
                "target": {"width": args.width, "height": args.height},
                "parameters": {"seed": args.seed, "steps": 1, "cfg": 1, "sampler": "euler", "scheduler": "simple",
                               "temporal_chunk_pixel_frames": args.chunk_frames, "temporal_overlap_latent_frames": 1,
                               "vae_tile": 512, "vae_overlap": 64, "vae_temporal_size": 16, "vae_temporal_overlap": 4},
                "workflow_template": str(template), "workflow_template_sha256": sha(template),
                "graph_sha256": graph_sha, "prompt": graph,
                "audio_policy": "Comfy preview audio re-encodes; collect remuxes all source audio packets unchanged into a separate E delivery file",
                "ffmpeg": args.ffmpeg, "ffprobe": args.ffprobe, "helper_sha256": sha(__file__),
                "source_unchanged_after_prepare": sha(source) == original_sha,
                "gpu_submission": "NOT RUN; explicit submit requires root GPU scheduling"}
    write_json(output, prepared)
    write_json(RUNTIME / "workflows" / (output.stem + ".json"), {"prompt": graph})
    print(json.dumps({"status": prepared["status"], "frames": count, "prepared": str(output), "graph_sha256": graph_sha}))


def submit_command(args):
    prepared = json.loads(Path(args.prepared).read_text(encoding="utf-8"))
    if Path(args.output).exists():
        raise FileExistsError("Submission receipt exists; never automatically resubmit")
    if sha(prepared["input_copy"]) != prepared["input_sha256"]:
        raise ValueError("Frozen input changed")
    actual_graph_sha = hashlib.sha256(json.dumps(prepared["prompt"], sort_keys=True).encode()).hexdigest()
    if actual_graph_sha != prepared["graph_sha256"]:
        raise ValueError("Frozen workflow graph changed")
    stats = api(args.base_url, "/system_stats")
    if stats["system"]["comfyui_version"] != "0.39.0":
        raise ValueError("Native Comfy version changed; review workflow before submit")
    known_queues=local_queues_idle()
    queue = api(args.base_url, "/queue")
    if queue.get("queue_running") or queue.get("queue_pending"):
        raise LocalGPUQueueBusyError("GPU Comfy queue is busy; no submission performed")
    receipt = {"created_at_utc": now(), "status": "SUBMITTING_NO_AUTORETRY", "prepared": str(Path(args.prepared).resolve()),
               "prepared_sha256": sha(args.prepared), "graph_sha256": prepared["graph_sha256"], "queue_before": queue,
               "gpu_scheduler_authorization": args.scheduler_note, "base_url": args.base_url,'known_local_queues_before':known_queues}
    # Write before POST so an ambiguous timeout cannot trigger silent duplicate submission.
    write_json(args.output, receipt)
    try:
        response = api(args.base_url, "/prompt", {"prompt": prepared["prompt"], "client_id": "seedvr2-" + uuid.uuid4().hex})
    except urllib.error.HTTPError as error:
        receipt.update(status="REJECTED", response_body=error.read().decode("utf-8", "replace")[:20000])
        write_json(args.output, receipt)
        raise RuntimeError("Comfy rejected prompt; see preserved submission receipt") from None
    except Exception:
        receipt["status"] = "UNKNOWN_DO_NOT_RESUBMIT"; write_json(args.output, receipt)
        raise
    receipt.update(status="SUBMITTED", response=response, prompt_id=response["prompt_id"])
    write_json(args.output, receipt)
    print(json.dumps({"status": receipt["status"], "prompt_id": response["prompt_id"]}))


def collect_command(args):
    if Path(args.output).exists():
        raise FileExistsError("Collection receipt exists; choose a new filename; original receipt preserved")
    submission = json.loads(Path(args.submission).read_text(encoding="utf-8"))
    prepared = json.loads(Path(submission["prepared"]).read_text(encoding="utf-8"))
    if sha(submission["prepared"]) != submission["prepared_sha256"]:
        raise ValueError("Prepared manifest changed after submit")
    history = api(submission["base_url"], "/history/" + submission["prompt_id"])
    item = history.get(submission["prompt_id"])
    if not item:
        print(json.dumps({"status": "PENDING", "prompt_id": submission["prompt_id"]})); return
    history_path = Path(args.output).with_name(Path(args.output).stem + "_history.json")
    write_json(history_path, history)
    if item.get("status", {}).get("status_str") != "success":
        write_json(args.output, {"status": "FAILED_OR_INCOMPLETE", "history": str(history_path),
                                "prompt_id": submission["prompt_id"], "actual_status": item.get("status")})
        print(json.dumps({"status": "FAILED_OR_INCOMPLETE", "history": str(history_path)})); return
    def outputs(value):
        if isinstance(value, dict):
            if "filename" in value and value.get("type") == "output":
                yield value
            for child in value.values():
                yield from outputs(child)
        elif isinstance(value, list):
            for child in value:
                yield from outputs(child)
    candidates = list(outputs(item.get("outputs", {}).get("15", {})))
    if not candidates:
        raise ValueError("No actual SaveVideo output in Comfy history")
    stats = api(submission["base_url"], "/system_stats")
    argv = stats["system"]["argv"]
    native_root = Path(argv[argv.index("--output-directory") + 1])
    file = candidates[0]
    native = within(native_root / file.get("subfolder", "") / file["filename"], native_root)
    destination = within(args.delivery, RUNTIME / "outputs")
    if destination.exists() or Path(args.output).exists():
        raise FileExistsError("Delivery/receipt exists; choose new paths; originals preserved")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Preserve all original compressed audio packets independently of Comfy's preview encoder.
    run_process([prepared["ffmpeg"], "-v", "error", "-nostdin", "-n", "-i", native,
                 "-i", prepared["input_copy"], "-map", "0:v:0", "-map", "1:a?", "-c", "copy", destination])
    actual = probe(destination, prepared["ffprobe"])
    run_process([prepared["ffmpeg"], "-v", "error", "-nostdin", "-i", destination, "-f", "null", "-"])
    source_unchanged = sha(prepared["original_source"]) == prepared["original_source_sha256"]
    actual_graph = item.get("prompt", [None, None, None])[2]
    actual_graph_sha = hashlib.sha256(json.dumps(actual_graph, sort_keys=True).encode()).hexdigest()
    audio = {"policy": "packet-copy all source audio streams", "has_audio": bool(prepared["input_probe"]["audio"])}
    if audio["has_audio"]:
        audio["source_packet_sha256"] = audio_hash(prepared["input_copy"], prepared["ffmpeg"])
        audio["output_packet_sha256"] = audio_hash(destination, prepared["ffmpeg"])
        audio["packet_sha_equal"] = audio["source_packet_sha256"] == audio["output_packet_sha256"]
    checks = {"frames_equal": actual["frames"] == prepared["expected_frames"],
              "target_dimensions_equal": (actual["width"], actual["height"]) == (prepared["target"]["width"], prepared["target"]["height"]),
              "fps_equal": actual["fps"] == prepared["input_probe"]["fps"], "full_decode_exit_code": 0,
              "source_unchanged": source_unchanged, "audio_packets_equal": audio.get("packet_sha_equal", True),
              "input_copy_unchanged": sha(prepared["input_copy"]) == prepared["input_sha256"],
              "comfy_upload_unchanged": sha(prepared["comfy_upload"]["actual_path"]) == prepared["input_sha256"],
              "actual_graph_matches_frozen": graph_digest(actual_graph) == graph_digest(prepared["prompt"])}
    receipt = {"status": "PASS_TECHNICAL_FILE" if all(v is True or k == "full_decode_exit_code" and v == 0 for k, v in checks.items()) else "FAILED_CHECKS",
               "captured_at_utc": now(), "prompt_id": submission["prompt_id"], "method": prepared["method"],
               "temporal_model": True, "human_visual_approval": False, "visual_score": None,
               "prepared": submission["prepared"], "prepared_sha256": submission["prepared_sha256"],
               "graph_sha256": prepared["graph_sha256"], "history": str(history_path), "history_sha256": sha(history_path),
               "actual_graph_sha256": actual_graph_sha,
               "actual_graph_raw_sha_matches_frozen": actual_graph_sha == prepared["graph_sha256"],
               "actual_graph_semantic_sha256": graph_digest(actual_graph),
               "frozen_graph_semantic_sha256": graph_digest(prepared["prompt"]),
               "graph_numeric_type_coercions": graph_numeric_coercions(prepared["prompt"], actual_graph),
               "graph_equivalence_policy": "Exact JSON after only integral float/int same-value canonicalization; bool, strings, values, links and ordered references remain exact; no rounding or ignored fields",
               "source": prepared["original_source"], "source_sha256": prepared["original_source_sha256"],
               "input_sha256": prepared["input_sha256"], "models": prepared["models"], "parameters": prepared["parameters"],
               "native_comfy_output": str(native), "native_comfy_output_sha256": sha(native),
               "output": str(destination), "output_bytes": destination.stat().st_size, "output_sha256": sha(destination),
               "frames_processed": actual["frames"], "source_frames": prepared["expected_frames"],
               "actual_probe": actual, "audio": audio, "checks": checks,
               "comfy_execution_status": item.get("status"), "sample_only": prepared["sample_only"],
               "limitation": "Technical completion does not certify face identity, material detail, temporal flicker, lip sync or human approval."}
    write_json(args.output, receipt)
    print(json.dumps({"status": receipt["status"], "frames": actual["frames"], "output": str(destination), "sha256": receipt["output_sha256"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--sources", required=True); plan.add_argument("--output", required=True)
    plan.set_defaults(func=plan_command)
    for name, func in (("inspect-remote", inspect_command), ("download", download_command)):
        cmd = sub.add_parser(name); cmd.add_argument("--plan", required=True)
        cmd.add_argument("--output", required=True); cmd.set_defaults(func=func)
        if name == "download":
            cmd.add_argument("--component", choices=["all", "diffusion_models", "vae"], default="all")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--plan", required=True); prepare.add_argument("--input", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--workflow", default=str(Path(__file__).resolve().parents[1] / "workflows/seedvr2_video_v1.json"))
    prepare.add_argument("--ffmpeg", default="E:/ComfyUI/ffmpeg.exe")
    prepare.add_argument("--ffprobe", default="E:/ComfyUI/ffprobe.exe")
    prepare.add_argument("--width", type=int, default=1280); prepare.add_argument("--height", type=int, default=720)
    prepare.add_argument("--frames", type=int, default=0); prepare.add_argument("--seed", type=int, default=20261024)
    prepare.add_argument("--base-url", default="http://127.0.0.1:8189")
    prepare.add_argument("--chunk-frames", type=int, default=9)
    prepare.set_defaults(func=prepare_command)
    submit = sub.add_parser("submit")
    submit.add_argument("--prepared", required=True); submit.add_argument("--output", required=True)
    submit.add_argument("--base-url", default="http://127.0.0.1:8189")
    submit.add_argument("--scheduler-note", required=True)
    submit.set_defaults(func=submit_command)
    collect = sub.add_parser("collect")
    collect.add_argument("--submission", required=True); collect.add_argument("--delivery", required=True)
    collect.add_argument("--output", required=True); collect.set_defaults(func=collect_command)
    args = parser.parse_args()
    if args.command == "download":
        checked_plan(args.plan)
        with exclusive_download():
            args.func(args)
    else:
        args.func(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__,
                          "message": str(error) if not isinstance(error, urllib.error.URLError) else "Network request failed; no signed URL logged"}), file=sys.stderr)
        raise SystemExit(1)
