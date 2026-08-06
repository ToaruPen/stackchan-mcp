from __future__ import annotations

from dataclasses import replace

import pytest

from stackchan_mcp.face_follow_detector import (
    AttentionDetection,
    BoundingBox,
    PintoFaceDetector,
    create_pinto_preprocessor,
    parse_pinto_detections,
    select_attention_target,
)


def _row(
    class_id: int,
    confidence: float,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
) -> list[float]:
    return [0.0, class_id, confidence, x_min, y_min, x_max, y_max]


def test_parse_pinto_detections_filters_classes_thresholds_and_invalid_boxes() -> None:
    rows = [
        *_row(1, 0.35, 32, 25.6, 160, 128),
        *_row(3, 0.40, 160, 128, 320, 256),
        *_row(1, 0.349, 0, 0, 100, 100),
        *_row(3, 0.399, 0, 0, 100, 100),
        *_row(2, 0.99, 0, 0, 100, 100),
        *_row(3, 0.99, 100, 100, 100, 120),
        *_row(1, 0.99, float("nan"), 0, 100, 100),
    ]

    detections = parse_pinto_detections(rows, (1, 7, 7))

    assert detections == [
        AttentionDetection(
            label="head",
            confidence=pytest.approx(0.35),
            bounding_box=BoundingBox(
                x_min=pytest.approx(0.1),
                y_min=pytest.approx(0.1),
                x_max=pytest.approx(0.5),
                y_max=pytest.approx(0.5),
            ),
        ),
        AttentionDetection(
            label="face",
            confidence=pytest.approx(0.40),
            bounding_box=BoundingBox(0.5, 0.5, 1.0, 1.0),
        ),
    ]


def test_parse_pinto_detections_clamps_coordinates_to_frame() -> None:
    detections = parse_pinto_detections(
        _row(3, 0.9, -30, -20, 400, 300),
        (1, 1, 7),
    )

    assert detections[0].bounding_box == BoundingBox(0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize(
    ("data", "shape"),
    [
        ([0.0] * 7, (7,)),
        ([0.0] * 7, (1, 1, 6)),
        ([0.0] * 7, (1, 2, 7)),
        ([0.0] * 7, (1, -1, 7)),
    ],
)
def test_parse_pinto_detections_rejects_invalid_output_shape(
    data: list[float], shape: tuple[int, ...]
) -> None:
    with pytest.raises(ValueError, match="rows of seven|dimensions"):
        parse_pinto_detections(data, shape)


def test_select_attention_target_prefers_face_then_confidence_times_sqrt_area() -> None:
    head = AttentionDetection("head", 0.99, BoundingBox(0.0, 0.0, 1.0, 1.0))
    small_face = AttentionDetection(
        "face", 0.95, BoundingBox(0.0, 0.0, 0.1, 0.1)
    )
    large_face = replace(
        small_face,
        confidence=0.50,
        bounding_box=BoundingBox(0.0, 0.0, 0.5, 0.5),
    )

    assert select_attention_target([head, small_face, large_face]) == large_face
    assert select_attention_target([head]) == head
    assert select_attention_target([]) is None


@pytest.mark.asyncio
async def test_pinto_detector_runs_preprocessor_and_named_onnx_io_off_loop() -> None:
    tensor = object()

    class Output:
        shape = (1, 1, 7)

        def reshape(self, *_shape: int) -> "Output":
            return self

        def tolist(self) -> list[float]:
            return _row(3, 0.8, 32, 25.6, 160, 128)

    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], dict[str, object]]] = []

        def run(
            self, output_names: list[str], feeds: dict[str, object]
        ) -> list[Output]:
            self.calls.append((output_names, feeds))
            return [Output()]

    session = Session()
    detector = PintoFaceDetector(
        session=session,
        input_name="images",
        output_name="detections",
        preprocess=lambda jpeg: tensor if jpeg == b"jpeg" else None,
    )

    detections = await detector.detect(b"jpeg")

    assert detections[0].label == "face"
    assert session.calls == [(["detections"], {"images": tensor})]


@pytest.mark.asyncio
async def test_pinto_detector_reports_raw_classes_rejected_by_thresholds() -> None:
    class Output:
        shape = (1, 2, 7)

        def reshape(self, *_shape: int) -> "Output":
            return self

        def tolist(self) -> list[float]:
            return [
                *_row(1, 0.34, 32, 25.6, 160, 128),
                *_row(3, 0.39, 160, 128, 300, 240),
            ]

    class Session:
        def run(
            self, _output_names: list[str], _feeds: dict[str, object]
        ) -> list[Output]:
            return [Output()]

    detector = PintoFaceDetector(
        session=Session(),
        input_name="images",
        output_name="detections",
        preprocess=lambda _jpeg: object(),
    )

    detections = await detector.detect(b"jpeg")

    assert detections == []
    assert detector.status() == {
        "frames": 1,
        "candidate_frames": 0,
        "no_candidate_frames": 1,
        "no_candidate_raw_class_frames": {"head": 1, "face": 1},
        "no_candidate_confidence_frames": {
            "head_below_threshold": 1,
            "face_below_threshold": 1,
            "head_at_or_above_threshold": 0,
            "face_at_or_above_threshold": 0,
        },
        "last_no_candidate_max_confidence_pct": {"head": 34, "face": 39},
    }


def test_pinto_preprocessor_produces_bgr_nchw_float_tensor() -> None:
    np = pytest.importorskip("numpy")
    image_module = pytest.importorskip("PIL.Image")
    from io import BytesIO

    source = image_module.new("RGB", (1, 1), (10, 20, 30))
    encoded = BytesIO()
    source.save(encoded, format="PNG")

    tensor = create_pinto_preprocessor()(encoded.getvalue())

    assert tensor.shape == (1, 3, 256, 320)
    assert tensor.dtype == np.float32
    assert tensor[0, :, 0, 0].tolist() == [30.0, 20.0, 10.0]
