"""Persistent PP-OCRv5 + CUDA STTN worker for all workbench queue items."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from common_io import atomic_write_json as atomic_json
from ocr_gpu_client import OCRGpuClient


PORTABLE_ROOT = Path(r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable")
BACKEND_ROOT = PORTABLE_ROOT / "AI_Subtitle_Backend"
FFMPEG_PATH = BACKEND_ROOT / "backend" / "ffmpeg" / "win_x64" / "ffmpeg.exe"
FFPROBE_PATH = FFMPEG_PATH.with_name("ffprobe.exe")
PROTOCOL_STDOUT = sys.stdout
OCR_INIT_COUNT = 0
CURRENT_TIMINGS = None
EVENT_WRITER = None
SHARED_RUNTIME = None


def emit(event: str, **payload) -> None:
    line = json.dumps({"event": event, **payload}, ensure_ascii=False)
    if EVENT_WRITER is not None:
        EVENT_WRITER(line)
    else:
        print(line, file=PROTOCOL_STDOUT, flush=True)


def fail_fatal(message: str) -> int:
    emit("fatal", message=message, traceback=traceback.format_exc())
    return 1


def script_signature() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def initialize_runtime() -> dict:
    """Load STTN and the separate RTX 50-series OCR service once."""
    global OCR_INIT_COUNT, CURRENT_TIMINGS, SHARED_RUNTIME
    if SHARED_RUNTIME is not None:
        return SHARED_RUNTIME
    os.chdir(PORTABLE_ROOT)
    sys.path.insert(0, str(BACKEND_ROOT))
    os.environ.setdefault("VSR_VIDEO_ENCODER", "auto")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        import cv2
        import numpy as np
        import torch
        from backend.config import BASE_DIR, config
        from backend.inpaint.sttn_auto_inpaint import STTNInpaint
        from backend.main import SubtitleRemover
        from backend.tools.constant import InpaintMode, SubtitleDetectMode
        from backend.tools.ffmpeg_cli import FFmpegCLI
        from backend.tools.inpaint_tools import create_mask
        import backend.tools.model_config as model_config_module

    if not torch.cuda.is_available():
        raise RuntimeError("RTX CUDA unavailable; CPU fallback is forbidden")
    probe = torch.ones(1, device="cuda")
    if float(probe.item()) != 1.0:
        raise RuntimeError("RTX CUDA tensor verification failed")
    torch.cuda.synchronize()
    device_name = torch.cuda.get_device_name(0)

    # ModelConfig checks a historic typo, bit-lama.pt, and otherwise rebuilds
    # the unrelated big-lama model during every OCR construction.
    original_merge = model_config_module.merge_big_file_if_not_exists

    def safe_merge(model_dir, filename, manifest=None):
        root = Path(model_dir)
        if filename == "bit-lama.pt" and (root / "big-lama.pt").is_file():
            return
        return original_merge(str(root), filename, manifest)

    model_config_module.merge_big_file_if_not_exists = safe_merge
    detect_mode = config.subtitleDetectMode.value
    if detect_mode == SubtitleDetectMode.PP_OCRv5_MOBILE:
        model_name = "PP-OCRv5_mobile_det"
        model_dir = Path(BASE_DIR) / "models" / "V5" / "ch_det_fast"
    elif detect_mode == SubtitleDetectMode.PP_OCRv5_SERVER:
        model_name = "PP-OCRv5_server_det"
        model_dir = Path(BASE_DIR) / "models" / "V5" / "ch_det"
    else:
        raise RuntimeError(f"Unsupported PP-OCR mode: {detect_mode}")
    if not model_dir.is_dir():
        raise FileNotFoundError(f"PP-OCR model directory missing: {model_dir}")
    # The bundled Windows ffmpeg is already executable.  FFmpegCLI's POSIX
    # chmod call is unnecessary on Windows and can fail under a restricted
    # unattended account, causing a silent cv2/mp4v fallback.
    if os.name == "nt" and FFmpegCLI._instance is None:
        FFmpegCLI._instance = object.__new__(FFmpegCLI)
    # Paddle's supported Windows RTX 50-series runtime uses CUDA 12.9 while
    # this process uses Torch CUDA 12.8 for STTN.  OCR therefore runs in a
    # second persistent process, never in this Python interpreter.
    ocr_service = OCRGpuClient(
        PORTABLE_ROOT,
        Path(__file__).with_name("ocr_gpu_service.py"),
        model_name,
        model_dir,
    )
    ocr_ready = ocr_service.start()
    OCR_INIT_COUNT += 1

    original_sttn = STTNInpaint.inpaint

    def timed_sttn(instance, frames):
        begin = time.perf_counter()
        try:
            return original_sttn(instance, frames)
        finally:
            if CURRENT_TIMINGS is not None:
                CURRENT_TIMINGS["sttnSeconds"] += time.perf_counter() - begin

    STTNInpaint.inpaint = timed_sttn
    SHARED_RUNTIME = {
        "ocrInitSeconds": round(time.perf_counter() - started, 3),
        "ocrInitCount": OCR_INIT_COUNT,
        "device": "cuda",
        "deviceName": device_name,
        "torchVersion": torch.__version__,
        "torchCuda": torch.version.cuda,
        "ocrDevice": ocr_ready.get("ocrDevice"),
        "ocrDeviceName": ocr_ready.get("ocrDeviceName"),
        "ocrEngine": ocr_ready.get("ocrEngine"),
        "ocrBatchSize": ocr_ready.get("ocrBatchSize"),
        "ocrServiceInitSeconds": ocr_ready.get("ocrInitSeconds"),
        "cv2": cv2,
        "config": config,
        "torch": torch,
        "SubtitleRemover": SubtitleRemover,
        "InpaintMode": InpaintMode,
        "create_mask": create_mask,
        "ocrService": ocr_service,
    }
    return SHARED_RUNTIME


def ready_payload(runtime: dict) -> dict:
    private = {"cv2", "config", "torch", "SubtitleRemover", "InpaintMode", "create_mask", "ocrService"}
    payload = {key: value for key, value in runtime.items() if key not in private}
    # A persistent worker can outlive the workbench process.  Expose the
    # immutable source signature in the handshake so a newly deployed client
    # never silently reuses an older worker that is still listening on 18767.
    payload["scriptSignature"] = script_signature()
    payload["protocolVersion"] = "step12-subtitle-quality-v2"
    return payload


def verify_no_subtitle_band(input_path: Path, runtime: dict, detect_fps: float, request_id: str) -> dict:
    """Run a denser second OCR pass before accepting an untouched source.

    A missing *stable* band is not enough by itself: subtitles can move, or
    OCR can see text in only a few frames.  Only a second pass with zero OCR
    boxes may classify a source as genuinely subtitle-free.
    """
    diagnostics: dict = {}

    def verification_progress(current, total, message):
        emit(
            "progress",
            requestId=request_id,
            stage="ocr",
            current=current,
            total=total,
            message=f"No-subtitle verification: {message}",
        )

    result = runtime["ocrService"].detect_stable_subtitle_band(
        str(input_path), max(2.0, float(detect_fps or 0.0)), verification_progress,
        minimum_hits=3, scan_mode="global", required_consecutive=3,
        minimum_line_width_ratio=0.30,
    )
    diagnostics.update(result.get("diagnostics") or {})
    frames_read = int(diagnostics.get("frames_read") or 0)
    ocr_boxes = int(diagnostics.get("ocr_boxes") or 0)
    complete_bands = result.get("bands") or ([] if result.get("band") is None else [result["band"]])
    verified = frames_read >= 3 and not complete_bands
    return {
        "verified": verified,
        "verificationStatus": "verified_clean" if verified and ocr_boxes == 0 else ("warning" if verified else "residual_detected"),
        "detectFps": max(2.0, float(detect_fps or 0.0)),
        "framesRead": frames_read,
        "ocrBoxes": ocr_boxes,
        "residualBands": complete_bands,
        "diagnostics": diagnostics,
    }


def verify_processed_output(
    output_path: Path,
    runtime: dict,
    detect_fps: float,
    request_id: str,
    original_bands: list[list[int]],
) -> dict:
    """Verify the rendered video instead of trusting a processed filename.

    A stable band in at least two sampled frames is a hard residual.  Isolated
    detector boxes are kept as a warning because packaging and wall labels can
    legitimately appear in the lower part of a video.  The detector excludes
    the top watermark area by design.
    """
    try:
        result = runtime["ocrService"].detect_stable_subtitle_band(
            str(output_path), max(2.0, float(detect_fps or 0.0)),
            lambda current, total, message: emit(
                "progress", requestId=request_id, stage="verify_subtitle",
                current=current, total=total, message=f"Subtitle verification: {message}",
            ),
            minimum_hits=3,
            required_consecutive=3,
            minimum_line_width_ratio=0.30,
        )
        # A second, low-frequency pass starts below the usual watermark area.
        # It catches subtitles that moved above the normal lower-third ROI
        # without treating the account watermark strip as removable text.
        global_result = runtime["ocrService"].detect_stable_subtitle_band(
            str(output_path), max(0.5, min(1.0, float(detect_fps or 0.0))),
            lambda current, total, message: emit(
                "progress", requestId=request_id, stage="verify_subtitle_global",
                current=current, total=total, message=f"Global subtitle verification: {message}",
            ),
            minimum_hits=3,
            scan_mode="global",
            required_consecutive=3,
            minimum_line_width_ratio=0.30,
        )
        diagnostics = result.get("diagnostics") or {}
        global_diagnostics = global_result.get("diagnostics") or {}
        diagnostics = {**diagnostics, "globalScan": global_diagnostics}
        residual_bands = result.get("bands") or ([] if result.get("band") is None else [result["band"]])
        global_bands = global_result.get("bands") or ([] if global_result.get("band") is None else [global_result["band"]])
        residual_bands = list(dict.fromkeys(tuple(int(value) for value in band) for band in [*residual_bands, *global_bands]))
        residual_bands = [list(band) for band in residual_bands]
        candidate_bands = list(result.get("candidateBands") or [])
        candidate_bands.extend(global_result.get("candidateBands") or [])
        candidate_bands = list(dict.fromkeys(tuple(int(value) for value in band) for band in candidate_bands))
        candidate_bands = [list(band) for band in candidate_bands]
        sample_count = max(int(diagnostics.get("frames_read") or 0), int(global_diagnostics.get("frames_read") or 0))
        residual_frames = sorted({
            int(value) for value in (diagnostics.get("candidate_frame_indices") or [])
            + (global_diagnostics.get("candidate_frame_indices") or [])
        })
        ocr_boxes = int(diagnostics.get("ocr_boxes") or 0) + int(global_diagnostics.get("ocr_boxes") or 0)
        if sample_count < 3:
            status = "verification_failed"
        elif residual_bands:
            status = "residual_detected"
        elif ocr_boxes > 0:
            status = "warning"
        else:
            status = "verified_clean"

        evidence = []
        if status in {"residual_detected", "verification_failed"} and residual_frames:
            evidence_dir = output_path.parent / ".subtitle_verification_evidence" / request_id
            evidence_dir.mkdir(parents=True, exist_ok=True)
            cap = runtime["cv2"].VideoCapture(str(output_path))
            try:
                areas = residual_bands or original_bands
                ymin = max(0, min(int(area[0]) for area in areas))
                ymax = max(ymin + 1, max(int(area[1]) for area in areas))
                for frame_index in residual_frames[:3]:
                    cap.set(runtime["cv2"].CAP_PROP_POS_FRAMES, frame_index)
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    crop = frame[ymin:ymax, :, :]
                    target = evidence_dir / f"residual-frame-{frame_index}.jpg"
                    if crop.size and runtime["cv2"].imwrite(str(target), crop):
                        evidence.append(str(target))
            finally:
                cap.release()
        return {
            "verificationStatus": status,
            "detectedBand": original_bands,
            "sampleCount": sample_count,
            "residualFrames": residual_frames,
            "residualBands": residual_bands,
            "candidateBands": candidate_bands,
            "evidence": evidence,
            "verifiedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        return {
            "verificationStatus": "verification_failed",
            "detectedBand": original_bands,
            "sampleCount": 0,
            "residualFrames": [],
            "residualBands": [],
            "candidateBands": [],
            "evidence": [],
            "verifiedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "message": str(exc),
        }


def _probe_pixel_format(input_path: Path) -> str:
    """Return the source pixel format so blur limits use the real planes."""
    if not FFPROBE_PATH.is_file():
        return ""
    try:
        completed = subprocess.run(
            [
                str(FFPROBE_PATH), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt", "-of", "json", str(input_path),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        streams = json.loads(completed.stdout or "{}").get("streams") or []
        return str(streams[0].get("pix_fmt") or "").strip() if streams else ""
    except (OSError, ValueError, TypeError, IndexError):
        return ""


def _blur_parameters(input_path: Path, crop_w: int, crop_h: int) -> dict:
    """Calculate legal luma/chroma radii for a cropped subtitle region."""
    pix_fmt = _probe_pixel_format(input_path).lower()
    if "444" in pix_fmt or pix_fmt.startswith(("rgb", "bgr", "gbr")):
        hsub, vsub = 1, 1
    elif "422" in pix_fmt:
        hsub, vsub = 2, 1
    elif "411" in pix_fmt:
        hsub, vsub = 4, 1
    elif "410" in pix_fmt:
        hsub, vsub = 4, 2
    else:
        # Unknown YUV formats are treated conservatively as 4:2:0.  This is
        # also the output format enforced below, so chroma can never exceed a
        # smaller-than-expected plane.
        hsub, vsub = 2, 2
        pix_fmt = pix_fmt or "yuv420p(assumed)"
    chroma_w = max(1, (int(crop_w) + hsub - 1) // hsub)
    chroma_h = max(1, (int(crop_h) + vsub - 1) // vsub)
    luma_limit = max(1, min(int(crop_w), int(crop_h)) // 2 - 1)
    chroma_limit = max(0, min(chroma_w, chroma_h) // 2 - 1)
    luma_radius = min(20, luma_limit)
    chroma_radius = min(12, chroma_limit, max(1, luma_radius // 2)) if chroma_limit else 0
    if luma_radius < 1:
        raise RuntimeError("SUBTITLE_BLUR_PARAMETER_INVALID: subtitle crop is too small for luma blur")
    return {
        "pixelFormat": pix_fmt,
        "hsub": hsub,
        "vsub": vsub,
        "chromaWidth": chroma_w,
        "chromaHeight": chroma_h,
        "lumaRadius": luma_radius,
        "lumaPower": 2,
        "chromaRadius": chroma_radius,
        "chromaPower": 2,
    }


def apply_timed_subtitle_blur(
    input_path: Path,
    output_path: Path,
    verification: dict,
    original_bands: list[list[int]],
    runtime: dict,
    request_id: str,
) -> list[dict]:
    """Blur only the proven residual subtitle band and time windows.

    This is a last-resort fallback after one expanded-mask STTN retry.  It
    never blurs the whole video and it refuses to run without frame evidence.
    """
    if not FFMPEG_PATH.is_file():
        raise FileNotFoundError(f"FFmpeg missing for subtitle blur fallback: {FFMPEG_PATH}")
    bands = verification.get("residualBands") or original_bands
    frames = sorted({int(value) for value in (verification.get("residualFrames") or []) if int(value) >= 0})
    diagnostics = verification.get("diagnostics") or {}
    global_diagnostics = diagnostics.get("globalScan") if isinstance(diagnostics.get("globalScan"), dict) else {}
    fps = float(diagnostics.get("source_fps") or global_diagnostics.get("source_fps") or 0.0)
    step = int(diagnostics.get("sample_step_frames") or global_diagnostics.get("sample_step_frames") or 0)
    if not bands or not frames or fps <= 0:
        raise RuntimeError("SUBTITLE_BLUR_EVIDENCE_INCOMPLETE: timed blur requires residual bands and frame evidence")
    frame_gap = max(step * 2, int(fps * 0.8), 1)
    grouped: list[list[int]] = []
    for frame in frames:
        if not grouped or frame - grouped[-1][-1] > frame_gap:
            grouped.append([frame])
        else:
            grouped[-1].append(frame)
    segments = [{
        "start": round(max(0.0, group[0] / fps - 0.30), 3),
        "end": round(group[-1] / fps + max(step / fps, 0.50) + 0.30, 3),
    } for group in grouped]

    cap = runtime["cv2"].VideoCapture(str(input_path))
    width = int(cap.get(runtime["cv2"].CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(runtime["cv2"].CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    if width <= 0 or height <= 0:
        raise RuntimeError("SUBTITLE_BLUR_MEDIA_INVALID: unable to read video dimensions")
    ymin = max(0, min(int(area[0]) for area in bands))
    ymax = min(height, max(int(area[1]) for area in bands))
    xmin = max(0, min(int(area[2]) for area in bands))
    xmax = min(width, max(int(area[3]) for area in bands))
    pad_y = max(8, int(height * 0.008))
    ymin, ymax = max(0, ymin - pad_y), min(height, ymax + pad_y)
    crop_w = max(2, (xmax - xmin) // 2 * 2)
    crop_h = max(2, (ymax - ymin) // 2 * 2)
    crop_w = min(crop_w, width - xmin)
    crop_h = min(crop_h, height - ymin)
    if crop_w < 2 or crop_h < 2:
        raise RuntimeError("SUBTITLE_BLUR_PARAMETER_INVALID: subtitle crop is outside video bounds")
    blur_params = _blur_parameters(input_path, crop_w, crop_h)
    hsub, vsub = int(blur_params["hsub"]), int(blur_params["vsub"])
    aligned_w = max(hsub * 2, (crop_w // hsub) * hsub)
    aligned_h = max(vsub * 2, (crop_h // vsub) * vsub)
    if aligned_w != crop_w or aligned_h != crop_h:
        crop_w, crop_h = min(aligned_w, width - xmin), min(aligned_h, height - ymin)
        blur_params = _blur_parameters(input_path, crop_w, crop_h)
        hsub, vsub = int(blur_params["hsub"]), int(blur_params["vsub"])
    xmin = max(0, min(width - crop_w, (xmin // hsub) * hsub))
    ymin = max(0, min(height - crop_h, (ymin // vsub) * vsub))
    enable = "+".join(f"between(t,{item['start']},{item['end']})" for item in segments)
    blur_path = output_path.with_name(f".{output_path.stem}.{request_id}.blur{output_path.suffix or '.mp4'}")
    diagnostic_path = output_path.with_name(f".{output_path.stem}.{request_id}.blur-diagnostic.json")
    blur_diagnostic = {
        "schemaVersion": 1,
        "requestId": request_id,
        "inputPath": str(input_path),
        "outputPath": str(output_path),
        "crop": [xmin, ymin, crop_w, crop_h],
        "segments": segments,
        "bands": bands,
        "dimensions": {"width": width, "height": height},
        "parameters": blur_params,
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    atomic_json(diagnostic_path, blur_diagnostic)
    if blur_path.exists():
        blur_path.unlink()
    filter_graph = (
        f"[0:v]split=2[base][region];"
        f"[region]crop={crop_w}:{crop_h}:{xmin}:{ymin},"
        f"boxblur={blur_params['lumaRadius']}:{blur_params['lumaPower']}:{blur_params['chromaRadius']}:{blur_params['chromaPower']}[blur];"
        f"[base][blur]overlay={xmin}:{ymin}:enable='{enable}'[v]"
    )
    blur_diagnostic["filterGraph"] = filter_graph
    atomic_json(diagnostic_path, blur_diagnostic)
    emit("progress", requestId=request_id, stage="blur", current=0, total=100,
         message="Complete subtitle remained after STTN retry; applying localized timed blur")
    completed = subprocess.run([
        str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
        "-filter_complex", filter_graph, "-map", "[v]", "-map", "0:a?",
        "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", str(blur_path),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0 or not blur_path.is_file() or blur_path.stat().st_size < 1024:
        blur_diagnostic["ffmpegReturnCode"] = completed.returncode
        blur_diagnostic["ffmpegStderr"] = (completed.stderr or "").strip()[-4000:]
        atomic_json(diagnostic_path, blur_diagnostic)
        raise RuntimeError("SUBTITLE_BLUR_FAILED: " + (completed.stderr.strip() or f"ffmpeg exit {completed.returncode}"))
    blur_path.replace(output_path)
    emit("progress", requestId=request_id, stage="blur", current=100, total=100,
         message="Localized timed subtitle blur complete")
    return [{**item, "band": [ymin, ymax, xmin, xmax]} for item in segments]


def process_request(request: dict, runtime: dict) -> None:
    """Process one video with fresh detection/mask/tracking state."""
    global CURRENT_TIMINGS
    request_id = str(request.get("requestId") or "")
    if str(request.get("repairMode") or "").strip() == "blur_fallback":
        _process_blur_repair_request(request, runtime)
        return
    input_path = Path(request.get("input") or "")
    output_path = Path(request.get("output") or "")
    partial_path = output_path.with_name(f".{output_path.stem}.{request_id}.partial{output_path.suffix or '.mp4'}")
    remover = None
    failure_verification = None
    try:
        if not input_path.is_file():
            raise FileNotFoundError(f"Input video missing: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if partial_path.exists():
            partial_path.unlink()
        total_started = time.perf_counter()
        detect_started = time.perf_counter()
        diagnostics = {}

        def detection_progress(current, total, message):
            emit("progress", requestId=request_id, stage="ocr", current=current, total=total, message=message)

        ocr_result = runtime["ocrService"].detect_stable_subtitle_band(
            str(input_path), max(0.25, float(request.get("detectFps", 1.5))), detection_progress
        )
        diagnostics.update(ocr_result.get("diagnostics") or {})
        band = ocr_result.get("band")
        ocr_seconds = time.perf_counter() - detect_started
        if band is None:
            verification = verify_no_subtitle_band(
                input_path,
                runtime,
                float(request.get("detectFps") or 1.5),
                request_id,
            )
            if verification["verified"]:
                treatment = "minor_residual" if verification.get("verificationStatus") == "warning" else "verified_no_subtitle"
                emit(
                    "result",
                    requestId=request_id,
                    success=True,
                    outcome=treatment,
                    outputPath=str(input_path),
                    subtitleTreatment=treatment,
                    originalSubtitleBands=[],
                    captionPlacement="company_default",
                    ocrSeconds=round(time.perf_counter() - detect_started, 3),
                    verification=verification,
                    ocrInitCount=OCR_INIT_COUNT,
                    device="cuda",
                    deviceName=runtime["deviceName"],
                )
                return
            raise RuntimeError(
                "PP-OCRv5 found subtitle candidates but could not verify a stable subtitle region"
            )
        detected_bands = ocr_result.get("bands") or [band]
        subtitle_areas = [tuple(int(value) for value in area) for area in detected_bands]
        emit("detection", requestId=request_id, area=list(subtitle_areas[0]), areas=[list(area) for area in subtitle_areas], confidence=ocr_result.get("confidence"),
             sampledFrames=ocr_result.get("sampledFrames"), diagnostics=diagnostics)

        config = runtime["config"]
        config.set(config.hardwareAcceleration, True)
        config.set(config.inpaintMode, runtime["InpaintMode"].STTN_AUTO)
        config.set(config.sttnMaxLoadNum, 100)
        os.environ["VSR_VIDEO_QUALITY"] = str(max(0, min(51, int(request.get("quality", 18)))))
        CURRENT_TIMINGS = {"sttnSeconds": 0.0, "encodeSeconds": 0.0, "encodeAnnounced": False}
        mask_seconds = 0.0
        encoder = ""

        def render_once(areas: list[tuple[int, int, int, int]], attempt: int) -> None:
            nonlocal remover, mask_seconds, encoder
            if partial_path.exists():
                partial_path.unlink()
            with contextlib.redirect_stdout(sys.stderr):
                remover = runtime["SubtitleRemover"](str(input_path), gui_mode=False)
            if not remover.hardware_accelerator.has_cuda() or getattr(remover.hardware_accelerator.device, "type", "") != "cuda":
                raise RuntimeError("STTN is not using CUDA; CPU fallback is forbidden")
            encoder = str(getattr(remover.video_writer, "encoder", ""))
            if encoder != "h264_nvenc":
                raise RuntimeError(f"Expected h264_nvenc, actual encoder: {encoder or 'unknown'}")
            original_write = remover.video_writer.write
            original_release = remover.video_writer.release

            def timed_write(frame):
                if not CURRENT_TIMINGS["encodeAnnounced"]:
                    CURRENT_TIMINGS["encodeAnnounced"] = True
                    emit("progress", requestId=request_id, stage="encode", current=1, total=100, message="h264_nvenc encoding")
                begin = time.perf_counter()
                try:
                    return original_write(frame)
                finally:
                    CURRENT_TIMINGS["encodeSeconds"] += time.perf_counter() - begin

            def timed_release():
                begin = time.perf_counter()
                try:
                    return original_release()
                finally:
                    CURRENT_TIMINGS["encodeSeconds"] += time.perf_counter() - begin

            remover.video_writer.write = timed_write
            remover.video_writer.release = timed_release
            remover.sub_areas = areas
            remover.video_out_path = str(partial_path)
            mask_started = time.perf_counter()
            runtime["create_mask"](remover.mask_size, [(xmin, xmax, ymin, ymax) for ymin, ymax, xmin, xmax in areas])
            mask_seconds += time.perf_counter() - mask_started
            remover.add_progress_listener(lambda progress, finished: emit(
                "progress", requestId=request_id, stage="sttn", current=int(progress), total=100,
                finished=bool(finished), message=f"CUDA STTN subtitle removal attempt {attempt}",
            ))
            with contextlib.redirect_stdout(sys.stderr):
                remover.run()
            if not partial_path.is_file() or partial_path.stat().st_size < 1024:
                raise RuntimeError("STTN/NVENC output missing or incomplete")
            capture = runtime["cv2"].VideoCapture(str(partial_path))
            valid = capture.isOpened() and int(capture.get(runtime["cv2"].CAP_PROP_FRAME_COUNT) or 0) > 0
            capture.release()
            if not valid:
                raise RuntimeError("STTN/NVENC output failed decode validation")
            partial_path.replace(output_path)

        render_once(subtitle_areas, 1)
        verification = verify_processed_output(
            output_path, runtime, max(2.0, float(request.get("detectFps") or 1.5)), request_id,
            [list(area) for area in subtitle_areas],
        )
        blur_segments = []
        fallback_attempt = 0
        if verification["verificationStatus"] == "residual_detected":
            emit("progress", requestId=request_id, stage="verify_subtitle", current=0, total=1,
                 message="Subtitle evidence remains after first render; retrying this video once with expanded masks")
            capture = runtime["cv2"].VideoCapture(str(input_path))
            height = int(capture.get(runtime["cv2"].CAP_PROP_FRAME_HEIGHT) or 0)
            capture.release()
            padding = max(12, int(height * 0.018))
            retry_bands = list(subtitle_areas)
            evidence_bands = verification.get("residualBands") or verification.get("candidateBands") or []
            for area in evidence_bands:
                ymin, ymax, xmin, xmax = [int(value) for value in area]
                retry_bands.append((max(0, ymin - padding), min(height, ymax + padding), xmin, xmax))
            render_once(retry_bands, 2)
            verification = verify_processed_output(
                output_path, runtime, max(2.0, float(request.get("detectFps") or 1.5)), request_id,
                [list(area) for area in retry_bands],
            )
            if verification["verificationStatus"] == "residual_detected":
                fallback_attempt = 1
                try:
                    blur_segments = apply_timed_subtitle_blur(
                        output_path, output_path, verification,
                        [list(area) for area in retry_bands], runtime, request_id,
                    )
                except RuntimeError as blur_error:
                    # A transient encoder/filter failure may be recoverable
                    # without rerunning STTN.  Recompute the crop geometry and
                    # retry the localized fallback once; never retry a second
                    # full subtitle-removal pass here.
                    if str(blur_error).startswith(("SUBTITLE_BLUR_FAILED", "SUBTITLE_BLUR_PARAMETER_INVALID")):
                        emit("progress", requestId=request_id, stage="blur", current=0, total=100,
                             message="Localized blur failed once; recalculating plane-safe parameters and retrying")
                        blur_segments = apply_timed_subtitle_blur(
                            output_path, output_path, verification,
                            [list(area) for area in retry_bands], runtime, request_id,
                        )
                    else:
                        raise
                verification = verify_processed_output(
                    output_path, runtime, max(2.0, float(request.get("detectFps") or 1.5)), request_id,
                    [list(area) for area in retry_bands],
                )
        if verification["verificationStatus"] in {"residual_detected", "verification_failed"}:
            failure_verification = verification
            raise RuntimeError(
                "SUBTITLE_VERIFICATION_FAILED: " + verification["verificationStatus"]
            )
        emit("progress", requestId=request_id, stage="encode", current=100, total=100, message="h264_nvenc encoding complete")
        treatment = "blur_fallback" if blur_segments else (
            "minor_residual" if verification["verificationStatus"] == "warning" else "sttn_clean"
        )
        emit("result", requestId=request_id, success=True, outcome="processed", outputPath=str(output_path),
             verification=verification,
             subtitleTreatment=treatment,
             originalSubtitleBands=[list(area) for area in subtitle_areas],
             captionPlacement="company_default" if treatment == "blur_fallback" else "original_band",
             sourceVideoWidth=int(diagnostics.get("video_width") or 0),
             sourceVideoHeight=int(diagnostics.get("video_height") or 0),
             blurSegments=blur_segments,
             fallbackAttempt=fallback_attempt,
             ocrSeconds=round(ocr_seconds, 3), maskSeconds=round(mask_seconds, 3),
             sttnSeconds=round(CURRENT_TIMINGS["sttnSeconds"], 3),
             encodeSeconds=round(CURRENT_TIMINGS["encodeSeconds"], 3),
             totalSeconds=round(time.perf_counter() - total_started, 3),
             ocrInitCount=OCR_INIT_COUNT, device="cuda", deviceName=runtime["deviceName"],
             ocrDevice=runtime["ocrDevice"], ocrDeviceName=runtime["ocrDeviceName"],
             ocrEngine=runtime["ocrEngine"], ocrBatchSize=runtime["ocrBatchSize"],
             ocrDiagnostics=diagnostics, encoder=encoder)
    except Exception as exc:
        if partial_path.exists():
            try:
                partial_path.unlink()
            except OSError:
                pass
        # Runtime initialization errors are reported by ``serve`` before this
        # point.  A single source-video failure must remain visible as a task
        # failure, but it must not poison the shared OCR process for every
        # remaining video in the queue.
        error_text = str(exc)
        if error_text.startswith("SUBTITLE_VERIFICATION_FAILED"):
            error_code = "SUBTITLE_VERIFICATION_FAILED"
        elif error_text.startswith("SUBTITLE_BLUR_PARAMETER_INVALID"):
            error_code = "SUBTITLE_BLUR_PARAMETER_INVALID"
        elif error_text.startswith("SUBTITLE_BLUR_EVIDENCE_INCOMPLETE"):
            error_code = "SUBTITLE_BLUR_EVIDENCE_INCOMPLETE"
        elif error_text.startswith("SUBTITLE_BLUR_MEDIA_INVALID"):
            error_code = "SUBTITLE_BLUR_MEDIA_INVALID"
        elif error_text.startswith("SUBTITLE_BLUR_FAILED"):
            error_code = "SUBTITLE_BLUR_FAILED"
        else:
            error_code = "AI_SUBTITLE_PROCESSING_FAILED"
        emit("result", requestId=request_id, success=False, outcome="failed", fatal=False, message=str(exc),
             errorCode=error_code, phase="subtitle_output_verification" if failure_verification else "subtitle_processing",
             verification=failure_verification or {},
             fallbackAttempt=int(locals().get("fallback_attempt") or 0),
             traceback=traceback.format_exc(), ocrInitCount=OCR_INIT_COUNT)
    finally:
        CURRENT_TIMINGS = None
        remover = None


def _process_blur_repair_request(request: dict, runtime: dict) -> None:
    """Repair an existing STTN artifact without rerunning STTN/GPU work."""
    request_id = str(request.get("requestId") or "")
    input_path = Path(request.get("input") or "")
    output_path = Path(request.get("output") or request.get("input") or "")
    original_bands = request.get("originalSubtitleBands") or []
    try:
        if not input_path.is_file():
            raise FileNotFoundError(f"STTN artifact missing: {input_path}")
        verification = request.get("verification") if isinstance(request.get("verification"), dict) else {}
        if verification.get("verificationStatus") not in {"residual_detected", "warning", "verified_clean"}:
            verification = verify_processed_output(
                input_path, runtime, max(2.0, float(request.get("detectFps") or 2.0)), request_id,
                original_bands,
            )
        fallback_attempt = 0
        if verification.get("verificationStatus") == "residual_detected":
            fallback_attempt = 1
            try:
                segments = apply_timed_subtitle_blur(
                    input_path, output_path, verification, original_bands, runtime, request_id,
                )
            except RuntimeError as blur_error:
                if str(blur_error).startswith(("SUBTITLE_BLUR_FAILED", "SUBTITLE_BLUR_PARAMETER_INVALID")):
                    emit("progress", requestId=request_id, stage="blur", current=0, total=100,
                         message="Repair blur failed once; recalculating plane-safe parameters and retrying")
                    segments = apply_timed_subtitle_blur(
                        input_path, output_path, verification, original_bands, runtime, request_id,
                    )
                else:
                    raise
            verification = verify_processed_output(
                output_path, runtime, max(2.0, float(request.get("detectFps") or 2.0)), request_id,
                original_bands,
            )
        else:
            segments = []
        if verification.get("verificationStatus") in {"residual_detected", "verification_failed"}:
            raise RuntimeError("SUBTITLE_VERIFICATION_FAILED: " + str(verification.get("verificationStatus")))
        treatment = "blur_fallback" if fallback_attempt else (
            "minor_residual" if verification.get("verificationStatus") == "warning" else "sttn_clean"
        )
        emit("result", requestId=request_id, success=True, outcome="processed",
             outputPath=str(output_path if fallback_attempt else input_path), verification=verification,
             subtitleTreatment=treatment,
             originalSubtitleBands=original_bands,
             captionPlacement="company_default" if fallback_attempt else "original_band",
             blurSegments=segments, fallbackAttempt=fallback_attempt)
    except Exception as exc:
        error_text = str(exc)
        if error_text.startswith("SUBTITLE_VERIFICATION_FAILED"):
            error_code = "SUBTITLE_VERIFICATION_FAILED"
            phase = "subtitle_output_verification"
        elif error_text.startswith("SUBTITLE_BLUR_PARAMETER_INVALID"):
            error_code = "SUBTITLE_BLUR_PARAMETER_INVALID"
            phase = "subtitle_processing"
        elif error_text.startswith("SUBTITLE_BLUR_EVIDENCE_INCOMPLETE"):
            error_code = "SUBTITLE_BLUR_EVIDENCE_INCOMPLETE"
            phase = "subtitle_processing"
        elif error_text.startswith("SUBTITLE_BLUR_MEDIA_INVALID"):
            error_code = "SUBTITLE_BLUR_MEDIA_INVALID"
            phase = "subtitle_processing"
        elif error_text.startswith("SUBTITLE_BLUR_FAILED"):
            error_code = "SUBTITLE_BLUR_FAILED"
            phase = "subtitle_processing"
        else:
            error_code = "AI_SUBTITLE_PROCESSING_FAILED"
            phase = "subtitle_processing"
        emit("result", requestId=request_id, success=False, outcome="failed", fatal=False,
             message=error_text, errorCode=error_code, phase=phase,
             verification=verification if isinstance(verification, dict) else {},
             fallbackAttempt=int(locals().get("fallback_attempt") or 0),
             traceback=traceback.format_exc())


def serve_client(connection: socket.socket, runtime: dict) -> None:
    global EVENT_WRITER
    writer = connection.makefile("wb", buffering=0)
    reader = connection.makefile("r", encoding="utf-8", errors="replace")

    def socket_writer(line: str) -> None:
        writer.write((line + "\n").encode("utf-8"))

    EVENT_WRITER = socket_writer
    emit("ready", **ready_payload(runtime))
    try:
        for raw in reader:
            raw = raw.strip()
            if raw:
                process_request(json.loads(raw), runtime)
    finally:
        EVENT_WRITER = None
        reader.close()
        writer.close()


def serve(port: int, state_file: Path) -> int:
    signature = script_signature()
    try:
        runtime = initialize_runtime()
    except Exception as exc:
        atomic_json(state_file, {"status": "fatal", "signature": signature, "message": str(exc), "updatedAt": time.time()})
        return fail_fatal(f"Persistent model initialization failed: {exc}")
    atomic_json(state_file, {"status": "ready", "signature": signature, "pid": os.getpid(), "port": port,
                             "ready": ready_payload(runtime), "updatedAt": time.time()})
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(4)
        while True:
            connection, _address = server.accept()
            with connection:
                try:
                    serve_client(connection, runtime)
                except (BrokenPipeError, ConnectionResetError, json.JSONDecodeError):
                    EVENT_WRITER = None


def stdio_main() -> int:
    try:
        runtime = initialize_runtime()
    except Exception as exc:
        return fail_fatal(f"Persistent model initialization failed: {exc}")
    emit("ready", **ready_payload(runtime))
    for raw in sys.stdin:
        raw = raw.strip()
        if raw:
            process_request(json.loads(raw), runtime)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--state-file", default=str(Path(os.environ.get("TEMP", ".")) / "ai-video-workbench-ocr-worker.json"))
    args = parser.parse_args()
    raise SystemExit(serve(args.port, Path(args.state_file)) if args.serve else stdio_main())
