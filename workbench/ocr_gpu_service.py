"""Isolated PaddleOCR GPU service for RTX 50-series Windows hosts.

This process intentionally has no PyTorch import.  The main worker keeps
PyTorch CUDA 12.8 for STTN/NVENC while this service runs Paddle's dedicated
CUDA 12.9 Blackwell wheel.  Keeping them apart prevents Windows DLL conflicts.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import socket
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

from common_io import atomic_write_json as atomic_json


def emit(writer, event: str, **payload) -> None:
    writer.write(json.dumps({"event": event, **payload}, ensure_ascii=False) + "\n")
    writer.flush()


def enhance(cv2, frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
    sharp = cv2.addWeighted(gray, 1.4, blur, -0.4, 0)
    return cv2.cvtColor(sharp, cv2.COLOR_GRAY2BGR)


def coordinates(polys):
    result = []
    # PaddleOCR returns a NumPy array here.  It cannot be used in a boolean
    # expression ("polys or []") because an empty array is ambiguous.
    if polys is None:
        return result
    for item in list(polys):
        points = list(item)
        if len(points) < 4:
            continue
        x1, y1 = int(points[0][0]), int(points[0][1])
        x2, y2 = int(points[1][0]), int(points[1][1])
        x3, y3 = int(points[2][0]), int(points[2][1])
        x4, y4 = int(points[3][0]), int(points[3][1])
        result.append((max(x1, x4), min(x2, x3), max(y1, y2), min(y3, y4)))
    return result


class GPUOCRService:
    BATCH_SIZE = 8

    def __init__(self, model_name: str, model_dir: str):
        # Imports live here so a launch failure is written as service state.
        import cv2
        import numpy as np
        import paddle
        from paddleocr import TextDetection

        self.cv2 = cv2
        self.np = np
        self.paddle = paddle
        paddle.set_device("gpu:0")
        if not paddle.is_compiled_with_cuda() or not str(paddle.get_device()).startswith("gpu"):
            raise RuntimeError("Paddle CUDA is unavailable; CPU fallback is forbidden")
        probe = paddle.ones([1], dtype="float32")
        if float(probe.numpy()[0]) != 1.0:
            raise RuntimeError("Paddle GPU tensor verification failed")
        self.detector = TextDetection(
            model_name=model_name,
            model_dir=model_dir,
            device="gpu:0",
            # Do not let optional ONNX/TRT providers silently select a second
            # incompatible engine during the first Blackwell production pass.
            enable_hpi=False,
        )
        list(self.detector.predict(np.zeros((192, 640, 3), dtype=np.uint8), batch_size=1))
        self.device_name = paddle.device.cuda.get_device_name(0)

    def _predict(self, images):
        if not images:
            return []
        results = list(self.detector.predict(images, batch_size=min(self.BATCH_SIZE, len(images))))
        if len(results) != len(images):
            raise RuntimeError(f"GPU OCR returned {len(results)} results for {len(images)} input crops")
        parsed = []
        for item in results:
            # PaddleOCR 3.x returns a mapping-like Result object rather than
            # always a concrete dict.  Use the same subscription API as the
            # existing backend detector.
            try:
                polys = item["dt_polys"]
            except (KeyError, TypeError):
                polys = item.get("dt_polys") if hasattr(item, "get") else None
            parsed.append(coordinates(polys))
        return parsed

    def detect(
        self,
        video_path: str,
        detect_fps: float,
        progress,
        minimum_hits_override=None,
        roi_top_ratio: float = 0.42,
        roi_bottom_ratio: float = 0.94,
        required_consecutive: int = 2,
        minimum_line_width_ratio: float = 0.05,
    ):
        cv2 = self.cv2
        np = self.np
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Unable to open video: {video_path}")
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if width <= 0 or height <= 0 or frame_count <= 0:
                raise RuntimeError("Unable to read video dimensions or frame count")
            roi_top = max(0, min(height - 1, int(height * float(roi_top_ratio))))
            roi_bottom = max(roi_top + 1, min(height, int(height * float(roi_bottom_ratio))))
            step = max(1, int(round(fps / max(float(detect_fps), 0.25))))
            indices = list(range(0, frame_count, step))
            center_min_ratio = max(0.30, float(roi_top_ratio) + 0.03)
            center_max_ratio = min(0.97, float(roi_bottom_ratio) - 0.01)
            stats = {
                "video_width": width, "video_height": height, "roi_top": roi_top, "roi_bottom": roi_bottom,
                "center_min": int(height * center_min_ratio), "center_max": int(height * center_max_ratio),
                "sampled_frames_requested": len(indices), "frames_read": 0, "frames_with_ocr": 0,
                "enhanced_retry_frames": 0, "ocr_boxes": 0, "rejected_width": 0, "rejected_height": 0,
                "rejected_position": 0, "accepted_boxes": 0, "frames_with_candidates": 0,
                "decodeMode": "sequential", "ocrDevice": "gpu:0", "ocrBatchSize": self.BATCH_SIZE,
                "decodeSeconds": 0.0, "originalInferenceSeconds": 0.0,
                "enhancedInferenceSeconds": 0.0, "postprocessSeconds": 0.0,
            }
            per_frame = []
            sample_no = 0
            target_set = set(indices)
            pending = []

            def consume(batch):
                nonlocal sample_no
                crops = [item[2] for item in batch]
                infer_started = time.perf_counter()
                predictions = self._predict(crops)
                stats["originalInferenceSeconds"] += time.perf_counter() - infer_started
                postprocess_started = time.perf_counter()
                retry = [(item[0], item[1], enhance(cv2, item[2])) for item, boxes in zip(batch, predictions) if not boxes]
                if retry:
                    stats["enhanced_retry_frames"] += len(retry)
                    retry_started = time.perf_counter()
                    retry_results = self._predict([item[2] for item in retry])
                    stats["enhancedInferenceSeconds"] += time.perf_counter() - retry_started
                    retry_by_index = {item[0]: boxes for item, boxes in zip(retry, retry_results)}
                else:
                    retry_by_index = {}
                for (frame_index, _, _), boxes in zip(batch, predictions):
                    if not boxes:
                        boxes = retry_by_index.get(frame_index, [])
                    sample_no += 1
                    if boxes:
                        stats["frames_with_ocr"] += 1
                        stats["ocr_boxes"] += len(boxes)
                    candidates = []
                    for xmin, xmax, ymin, ymax in boxes:
                        ymin += roi_top
                        ymax += roi_top
                        box_width, box_height = xmax - xmin, ymax - ymin
                        center_y = (ymin + ymax) / 2
                        if box_width < width * 0.05 or box_width > width * 0.95:
                            stats["rejected_width"] += 1
                            continue
                        if box_height < height * 0.012 or box_height > height * 0.12:
                            stats["rejected_height"] += 1
                            continue
                        if center_y < height * center_min_ratio or center_y > height * center_max_ratio:
                            stats["rejected_position"] += 1
                            continue
                        candidates.append((xmin, xmax, ymin, ymax))
                        stats["accepted_boxes"] += 1
                    if candidates:
                        stats["frames_with_candidates"] += 1
                        groups = defaultdict(list)
                        bucket_size = max(12, int(height * 0.035))
                        for candidate in candidates:
                            groups[((candidate[2] + candidate[3]) // 2) // bucket_size].append(candidate)
                        # Keep every plausible subtitle line.  Selecting only
                        # the widest group loses a second line or a subtitle
                        # that moves vertically between shots.
                        for group in groups.values():
                            per_frame.append((frame_index, min(item[0] for item in group), max(item[1] for item in group),
                                              min(item[2] for item in group), max(item[3] for item in group)))
                    progress(sample_no, len(indices), f"GPU OCR sample {sample_no}/{len(indices)}")
                stats["postprocessSeconds"] += time.perf_counter() - postprocess_started

            frame_index = 0
            while True:
                read_started = time.perf_counter()
                ok, frame = cap.read()
                stats["decodeSeconds"] += time.perf_counter() - read_started
                if not ok:
                    break
                if frame_index in target_set:
                    stats["frames_read"] += 1
                    pending.append((frame_index, sample_no + 1, frame[roi_top:roi_bottom, :, :]))
                    if len(pending) >= self.BATCH_SIZE:
                        consume(pending)
                        pending = []
                frame_index += 1
            if pending:
                consume(pending)
            stats["decodeSeconds"] = round(stats["decodeSeconds"], 3)

            minimum_hits = max(2, int(minimum_hits_override or int(len(indices) * 0.08)))
            stats["minimum_hits"] = minimum_hits
            stats["required_consecutive"] = max(1, int(required_consecutive))
            stats["minimum_line_width_ratio"] = float(minimum_line_width_ratio)
            stats["sample_step_frames"] = step
            stats["source_fps"] = fps
            stats["candidate_frames"] = len(per_frame)

            def candidate_bands(items):
                """Return conservative mask bands even when stability is unproven.

                These bands are deliberately only a retry hint.  They are never
                reported as a clean/stable detection; the caller must verify the
                rendered output before allowing it downstream.
                """
                if not items:
                    return []
                hint_bucket = max(18, int(height * 0.04))
                grouped = defaultdict(list)
                for item in items:
                    grouped[int(((item[3] + item[4]) / 2) // hint_bucket)].append(item)
                hints = []
                hint_margin = max(14, int(height * 0.018))
                for group in sorted(grouped.values(), key=lambda values: min(item[3] for item in values)):
                    ymin = min(int(item[3]) for item in group)
                    ymax = max(int(item[4]) for item in group)
                    if ymax <= ymin:
                        continue
                    hints.append([max(roi_top, ymin - hint_margin),
                                  min(roi_bottom, ymax + hint_margin), 0, width])
                return hints

            hint_bands = candidate_bands(per_frame)
            if len(per_frame) < minimum_hits:
                stats["status"] = "insufficient_candidate_frames"
                stats["candidate_frame_indices"] = sorted({int(item[0]) for item in per_frame[:24]})
                return {"band": None, "bands": [], "candidateBands": hint_bands, "diagnostics": stats}
            # Subtitles can move or use two lines.  Keep every stable vertical
            # cluster instead of collapsing the whole video into one median
            # band.  The top 42% remains excluded so account watermarks are
            # never promoted to subtitle masks.
            bucket_size = max(18, int(height * 0.04))
            clusters = defaultdict(list)
            for item in per_frame:
                clusters[int(((item[3] + item[4]) / 2) // bucket_size)].append(item)
            stable_clusters = []
            excluded_static = []
            for items in clusters.values():
                frame_indices = sorted({int(item[0]) for item in items})
                longest_run = 1 if frame_indices else 0
                current_run = longest_run
                for left, right in zip(frame_indices, frame_indices[1:]):
                    if right - left <= max(1, int(step * 1.5)):
                        current_run += 1
                        longest_run = max(longest_run, current_run)
                    else:
                        current_run = 1
                widths = [max(0, int(item[2]) - int(item[1])) / max(width, 1) for item in items]
                centers_x = [((int(item[1]) + int(item[2])) / 2) / max(width, 1) for item in items]
                centers_y = [((int(item[3]) + int(item[4])) / 2) / max(height, 1) for item in items]
                persistence = len(frame_indices) / max(len(indices), 1)
                width_mean = float(np.mean(widths)) if widths else 0.0
                width_cv = float(np.std(widths) / max(width_mean, 0.001)) if widths else 0.0
                geometry_static = (
                    persistence >= 0.65
                    and width_cv <= 0.08
                    and float(np.std(centers_x)) <= 0.015
                    and float(np.std(centers_y)) <= 0.01
                )
                # Persistent, nearly motionless text is normally a watermark,
                # package label or text built into the scene.  It is evidence
                # for the audit report but not a subtitle-removal failure.
                static_is_non_subtitle = geometry_static and (
                    float(np.mean(centers_y)) < 0.28 or max(widths or [0.0]) < 0.25
                )
                if static_is_non_subtitle:
                    excluded_static.append({
                        "frameCount": len(frame_indices), "persistence": round(persistence, 4),
                        "widthRatio": round(max(widths or [0.0]), 4),
                        "centerYRatio": round(float(np.mean(centers_y)), 4),
                    })
                    continue
                if (
                    len(frame_indices) >= minimum_hits
                    and longest_run >= max(1, int(required_consecutive))
                    and max(widths or [0.0]) >= float(minimum_line_width_ratio)
                ):
                    stable_clusters.append(items)
            stats["excluded_static_bands"] = excluded_static
            if not stable_clusters:
                stats["status"] = "insufficient_stable_frames"
                stats["candidate_frame_indices"] = [int(item[0]) for item in per_frame[:24]]
                return {"band": None, "bands": [], "candidateBands": hint_bands, "diagnostics": stats}
            bands = []
            stable_count = 0
            margin = max(10, int(height * 0.012))
            for stable in sorted(stable_clusters, key=lambda items: min(item[3] for item in items)):
                stable_count += len(stable)
                ymin = int(np.percentile([item[3] for item in stable], 10))
                ymax = int(np.percentile([item[4] for item in stable], 90))
                min_height = int(height * 0.035)
                if ymax - ymin < min_height:
                    center = (ymin + ymax) // 2
                    ymin, ymax = center - min_height // 2, center + min_height // 2
                bands.append([max(roi_top, ymin - margin), min(roi_bottom, ymax + margin), 0, width])
            confidence = round(min(1.0, stable_count / max(len(indices), 1)), 4)
            # Legacy callers still consume ``band``.  It is the union while
            # new callers use ``bands`` for precise multi-region masking.
            band = [min(item[0] for item in bands), max(item[1] for item in bands), 0, width]
            stats.update({
                "status": "detected", "final_area": band, "final_areas": bands,
                "confidence": confidence, "stable_frames": stable_count,
                "candidate_frame_indices": [int(item[0]) for item in per_frame[:24]],
            })
            for key in ("originalInferenceSeconds", "enhancedInferenceSeconds", "postprocessSeconds"):
                stats[key] = round(stats[key], 3)
            return {"band": band, "bands": bands, "confidence": confidence,
                    "sampledFrames": len(indices), "diagnostics": stats}
        finally:
            cap.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-dir", required=True)
    args = parser.parse_args()
    state_path = Path(args.state_file)
    try:
        started = time.perf_counter()
        service = GPUOCRService(args.model_name, args.model_dir)
        ready = {"status": "ready", "pid": __import__("os").getpid(), "ocrDevice": "gpu:0",
                 "ocrDeviceName": service.device_name, "ocrEngine": "paddle_static", "ocrBatchSize": service.BATCH_SIZE,
                 "ocrInitSeconds": round(time.perf_counter() - started, 3)}
        atomic_json(state_path, ready)
    except Exception as exc:
        atomic_json(state_path, {"status": "fatal", "message": str(exc), "traceback": traceback.format_exc()})
        raise
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.port))
    server.listen(4)
    while True:
        connection, _ = server.accept()
        with connection, connection.makefile("r", encoding="utf-8", errors="replace") as reader, connection.makefile("w", encoding="utf-8", newline="\n") as writer:
            emit(writer, "ready", **{key: value for key, value in ready.items() if key != "status"})
            for line in reader:
                try:
                    request = json.loads(line)
                    if request.get("event") != "detect":
                        raise ValueError("Unsupported GPU OCR request")
                    began = time.perf_counter()
                    result = service.detect(
                        str(request["videoPath"]),
                        float(request.get("detectFps") or 1.5),
                        lambda cur, total, msg: emit(writer, "progress", current=cur, total=total, message=msg),
                        request.get("minimumHits"),
                        0.04 if str(request.get("scanMode") or "subtitle") == "global" else 0.42,
                        0.98 if str(request.get("scanMode") or "subtitle") == "global" else 0.94,
                        int(request.get("requiredConsecutive") or 2),
                        float(request.get("minimumLineWidthRatio") or 0.05),
                    )
                    emit(writer, "result", success=True, ocrSeconds=round(time.perf_counter() - began, 3), **result)
                except Exception as exc:
                    emit(writer, "result", success=False, message=str(exc), traceback=traceback.format_exc())


if __name__ == "__main__":
    main()
