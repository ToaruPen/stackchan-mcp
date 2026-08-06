"""PINTO head/face detection used by the gateway-owned follow runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
import math
from pathlib import Path
from typing import Any, Literal, Protocol


PINTO_INPUT_WIDTH = 320
PINTO_INPUT_HEIGHT = 256
PINTO_HEAD_CLASS_ID = 1
PINTO_FACE_CLASS_ID = 3
PINTO_HEAD_CONFIDENCE = 0.35
PINTO_FACE_CONFIDENCE = 0.40
PINTO_OUTPUT_ROW_LENGTH = 7

AttentionLabel = Literal["head", "face"]


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True, slots=True)
class AttentionDetection:
    label: AttentionLabel
    confidence: float
    bounding_box: BoundingBox


class AttentionDetector(Protocol):
    async def detect(self, jpeg: bytes) -> list[AttentionDetection]: ...


class OnnxSession(Protocol):
    def run(
        self,
        output_names: list[str],
        feeds: dict[str, object],
    ) -> Sequence[Any]: ...


def parse_pinto_detections(
    values: Sequence[float],
    dimensions: Sequence[int],
    *,
    input_width: int = PINTO_INPUT_WIDTH,
    input_height: int = PINTO_INPUT_HEIGHT,
    head_confidence: float = PINTO_HEAD_CONFIDENCE,
    face_confidence: float = PINTO_FACE_CONFIDENCE,
) -> list[AttentionDetection]:
    """Validate and parse PINTO's ``[..., 7]`` head/face output."""
    _validate_parser_config(
        input_width=input_width,
        input_height=input_height,
        head_confidence=head_confidence,
        face_confidence=face_confidence,
    )
    if len(dimensions) < 2 or dimensions[-1] != PINTO_OUTPUT_ROW_LENGTH:
        raise ValueError("PINTO output must end in rows of seven values")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in dimensions):
        raise ValueError("PINTO output dimensions are invalid")
    element_count = math.prod(dimensions)
    if element_count != len(values):
        raise ValueError("PINTO output dimensions do not match its data")

    detections: list[AttentionDetection] = []
    for offset in range(0, len(values), PINTO_OUTPUT_ROW_LENGTH):
        detection = _parse_row(
            values[offset : offset + PINTO_OUTPUT_ROW_LENGTH],
            input_width=input_width,
            input_height=input_height,
            head_confidence=head_confidence,
            face_confidence=face_confidence,
        )
        if detection is not None:
            detections.append(detection)
    return detections


def select_attention_target(
    detections: Sequence[AttentionDetection],
) -> AttentionDetection | None:
    """Select face before head, then rank by confidence and box area."""
    for label in ("face", "head"):
        candidates = [item for item in detections if item.label == label]
        if candidates:
            return max(candidates, key=_target_rank)
    return None


class PintoFaceDetector:
    """Small async wrapper around a loaded ONNX Runtime session."""

    def __init__(
        self,
        *,
        session: OnnxSession,
        input_name: str,
        output_name: str,
        preprocess: Callable[[bytes], object],
    ) -> None:
        if not input_name or not output_name:
            raise ValueError("PINTO ONNX input and output names are required")
        self._session = session
        self._input_name = input_name
        self._output_name = output_name
        self._preprocess = preprocess
        self._frames = 0
        self._candidate_frames = 0
        self._no_candidate_frames = 0
        self._no_candidate_raw_class_frames = {"head": 0, "face": 0}
        self._no_candidate_confidence_frames = {
            "head_below_threshold": 0,
            "face_below_threshold": 0,
            "head_at_or_above_threshold": 0,
            "face_at_or_above_threshold": 0,
        }
        self._last_no_candidate_max_confidence_pct: dict[
            str, int | None
        ] = {"head": None, "face": None}

    async def detect(self, jpeg: bytes) -> list[AttentionDetection]:
        """Decode, infer, and parse one JPEG without blocking the event loop."""
        return await asyncio.to_thread(self._detect_sync, jpeg)

    def _detect_sync(self, jpeg: bytes) -> list[AttentionDetection]:
        tensor = self._preprocess(jpeg)
        outputs = self._session.run(
            [self._output_name],
            {self._input_name: tensor},
        )
        if len(outputs) != 1:
            raise RuntimeError("PINTO attention ONNX output is missing")
        output = outputs[0]
        shape = getattr(output, "shape", None)
        if not isinstance(shape, Sequence):
            raise RuntimeError("PINTO attention ONNX output shape is missing")
        flat = output.reshape(-1).tolist()
        if not isinstance(flat, list):
            raise RuntimeError("PINTO attention ONNX output data is invalid")
        detections = parse_pinto_detections(
            flat,
            tuple(int(item) for item in shape),
        )
        self._record_diagnostics(flat, detections)
        return detections

    def status(self) -> dict[str, Any]:
        """Return image-free counters for detector threshold ownership."""
        return {
            "frames": self._frames,
            "candidate_frames": self._candidate_frames,
            "no_candidate_frames": self._no_candidate_frames,
            "no_candidate_raw_class_frames": dict(
                self._no_candidate_raw_class_frames
            ),
            "no_candidate_confidence_frames": dict(
                self._no_candidate_confidence_frames
            ),
            "last_no_candidate_max_confidence_pct": dict(
                self._last_no_candidate_max_confidence_pct
            ),
        }

    def _record_diagnostics(
        self,
        values: Sequence[float],
        detections: Sequence[AttentionDetection],
    ) -> None:
        self._frames += 1
        if detections:
            self._candidate_frames += 1
            return
        self._no_candidate_frames += 1
        maxima = _maximum_attention_confidences(values)
        thresholds = {
            "head": PINTO_HEAD_CONFIDENCE,
            "face": PINTO_FACE_CONFIDENCE,
        }
        for label in ("head", "face"):
            confidence = maxima[label]
            self._last_no_candidate_max_confidence_pct[label] = (
                None
                if confidence is None
                else _confidence_percent(confidence)
            )
            if confidence is None:
                continue
            self._no_candidate_raw_class_frames[label] += 1
            suffix = (
                "below_threshold"
                if confidence < thresholds[label]
                else "at_or_above_threshold"
            )
            self._no_candidate_confidence_frames[f"{label}_{suffix}"] += 1


def load_pinto_face_detector(model_path: str) -> PintoFaceDetector:
    """Load the configured model and optional face-follow dependencies."""
    path = Path(model_path).expanduser()
    if not path.is_file():
        raise RuntimeError("STACKCHAN_FACE_FOLLOW_MODEL must name an ONNX file")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError(
            "face follow dependencies are missing; install stackchan-mcp[face-follow]"
        ) from exc

    session = ort.InferenceSession(
        str(path),
        providers=["CPUExecutionProvider"],
    )
    inputs = session.get_inputs()
    outputs = session.get_outputs()
    if not inputs or not outputs:
        raise RuntimeError("PINTO attention ONNX model has no input or output")

    return PintoFaceDetector(
        session=session,
        input_name=inputs[0].name,
        output_name=outputs[0].name,
        preprocess=create_pinto_preprocessor(),
    )


def create_pinto_preprocessor() -> Callable[[bytes], object]:
    """Create the lazy Pillow/NumPy RGB-to-BGR NCHW preprocessor."""
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "face follow dependencies are missing; install stackchan-mcp[face-follow]"
        ) from exc

    def preprocess(jpeg: bytes) -> object:
        with Image.open(BytesIO(jpeg)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = image.resize(
                (PINTO_INPUT_WIDTH, PINTO_INPUT_HEIGHT),
                resample=Image.Resampling.LANCZOS,
            )
            rgb = np.asarray(image, dtype=np.float32)
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        return np.transpose(bgr, (2, 0, 1))[None, ...]

    return preprocess


def _parse_row(
    row: Sequence[float],
    *,
    input_width: int,
    input_height: int,
    head_confidence: float,
    face_confidence: float,
) -> AttentionDetection | None:
    class_id = row[1]
    confidence = row[2]
    label: AttentionLabel | None
    if class_id == PINTO_HEAD_CLASS_ID:
        label = "head"
        threshold = head_confidence
    elif class_id == PINTO_FACE_CLASS_ID:
        label = "face"
        threshold = face_confidence
    else:
        return None
    if not math.isfinite(confidence) or confidence < threshold:
        return None

    coordinates = (
        _normalized_coordinate(row[3], input_width),
        _normalized_coordinate(row[4], input_height),
        _normalized_coordinate(row[5], input_width),
        _normalized_coordinate(row[6], input_height),
    )
    if any(item is None for item in coordinates):
        return None
    x_min, y_min, x_max, y_max = coordinates
    assert x_min is not None and y_min is not None
    assert x_max is not None and y_max is not None
    if x_max <= x_min or y_max <= y_min:
        return None
    return AttentionDetection(
        label=label,
        confidence=float(confidence),
        bounding_box=BoundingBox(x_min, y_min, x_max, y_max),
    )


def _normalized_coordinate(value: float, dimension: int) -> float | None:
    if not math.isfinite(value):
        return None
    return min(1.0, max(0.0, float(value) / dimension))


def _target_rank(detection: AttentionDetection) -> float:
    box = detection.bounding_box
    area = (box.x_max - box.x_min) * (box.y_max - box.y_min)
    return detection.confidence * math.sqrt(area)


def _maximum_attention_confidences(
    values: Sequence[float],
) -> dict[str, float | None]:
    maxima: dict[str, float | None] = {"head": None, "face": None}
    for offset in range(0, len(values), PINTO_OUTPUT_ROW_LENGTH):
        row = values[offset : offset + PINTO_OUTPUT_ROW_LENGTH]
        class_id = row[1]
        confidence = row[2]
        if not math.isfinite(confidence):
            continue
        label: str | None = None
        if class_id == PINTO_HEAD_CLASS_ID:
            label = "head"
        elif class_id == PINTO_FACE_CLASS_ID:
            label = "face"
        if label is None:
            continue
        current = maxima[label]
        maxima[label] = confidence if current is None else max(current, confidence)
    return maxima


def _confidence_percent(confidence: float) -> int:
    return round(min(1.0, max(0.0, confidence)) * 100)


def _validate_parser_config(
    *,
    input_width: int,
    input_height: int,
    head_confidence: float,
    face_confidence: float,
) -> None:
    if input_width < 1 or input_height < 1:
        raise ValueError("PINTO input dimensions must be positive")
    if not 0 <= head_confidence <= 1 or not 0 <= face_confidence <= 1:
        raise ValueError("PINTO confidence thresholds must be from zero through one")
