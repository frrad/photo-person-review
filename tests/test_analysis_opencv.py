from pathlib import Path

import pytest
from PIL import Image

from photo_person_review.analysis import (
    ModelInstallError,
    ModelManager,
    OpenCVAnalyzer,
    PinnedModel,
)


class _Frame:
    shape = (60, 100, 3)


class _Detector:
    def __init__(self):
        self.input_size = None

    def setInputSize(self, value):
        self.input_size = value

    def detect(self, image):
        return 1, [[10, 12, 30, 25, 18, 18, 32, 18, 25, 25, 20, 32, 30, 32, 0.92]]


class _Vector:
    def reshape(self, *_args):
        return [3.0, 4.0]


class _Recognizer:
    def __init__(self):
        self.rows = []

    def alignCrop(self, image, row):
        self.rows.append(row)
        return image

    def feature(self, aligned):
        return _Vector()


def test_opencv_analyzer_emits_boxes_landmarks_quality_and_normalized_embedding(tmp_path: Path):
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (100, 60), "white").save(source)
    analyzer = OpenCVAnalyzer(
        detector=_Detector(),
        recognizer=_Recognizer(),
        image_to_array=lambda image: _Frame(),
    )
    result = analyzer.analyze("photo-id", source, batch_id="day-1")
    assert len(result.faces) == 1
    face = result.faces[0]
    assert face.bbox == (10, 12, 30, 25)
    assert face.quality == 0.92
    assert face.landmarks == ((18.0, 18.0), (32.0, 18.0), (25.0, 25.0), (20.0, 32.0), (30.0, 32.0))
    assert face.embedding == (0.6, 0.8)


def test_opencv_analyzer_defaults_to_review_oriented_recall():
    assert OpenCVAnalyzer().face_score_threshold == 0.50
    assert "max-side:2000" in OpenCVAnalyzer().analyzer_version


def test_opencv_analyzer_downscales_before_detection_and_maps_geometry_back(tmp_path: Path):
    source = tmp_path / "large-photo.jpg"
    Image.new("RGB", (4000, 2000), "white").save(source)

    class ScaledFrame:
        shape = (500, 1000, 3)

    class ScaledDetector(_Detector):
        def detect(self, image):
            assert image.shape[:2] == (500, 1000)
            return 1, [[100, 50, 200, 100, 120, 60, 260, 60, 200, 100, 140, 130, 250, 130, 0.91]]

    recognizer = _Recognizer()
    seen_sizes = []

    def image_to_array(image):
        seen_sizes.append(image.size)
        return ScaledFrame()

    analyzer = OpenCVAnalyzer(
        detector=ScaledDetector(),
        recognizer=recognizer,
        image_to_array=image_to_array,
        face_max_side=1000,
    )
    result = analyzer.analyze("photo-id", source, batch_id="day-1")

    assert seen_sizes == [(1000, 500)]
    face = result.faces[0]
    assert face.bbox == (400, 200, 800, 400)
    assert face.landmarks == ((480.0, 240.0), (1040.0, 240.0), (800.0, 400.0), (560.0, 520.0), (1000.0, 520.0))
    assert recognizer.rows[0][0:4] == [100, 50, 200, 100]


def test_opencv_analyzer_zero_max_side_preserves_original_size(tmp_path: Path):
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (4000, 2000), "white").save(source)
    seen_sizes = []

    def image_to_array(image):
        seen_sizes.append(image.size)
        return _Frame()

    analyzer = OpenCVAnalyzer(
        detector=_Detector(),
        recognizer=_Recognizer(),
        image_to_array=image_to_array,
        face_max_side=0,
    )
    analyzer.analyze("photo-id", source, batch_id="day-1")
    assert seen_sizes == [(4000, 2000)]


def test_opencv_analyzer_reports_missing_model_action(tmp_path: Path):
    analyzer = OpenCVAnalyzer()
    with pytest.raises(RuntimeError, match="ppr models install"):
        analyzer.analyze("photo-id", tmp_path / "missing.jpg", batch_id="day-1")


def test_model_manager_verifies_and_records_pinned_install(tmp_path: Path):
    payload = b"deterministic model fixture"
    import hashlib

    model = PinnedModel(
        "fixture",
        "fixture.onnx",
        "https://example.test/fixture.onnx",
        hashlib.sha256(payload).hexdigest(),
        "MIT",
        "https://example.test/LICENSE",
        "test model",
    )
    manager = ModelManager(tmp_path / "models", models={"fixture": model})

    def download(url, output):
        assert url == model.url
        output.write(payload)

    path = manager.install("fixture", downloader=download)
    assert path.read_bytes() == payload
    assert manager.is_installed("fixture")
    assert "fixture" in (tmp_path / "models" / "models.json").read_text()


def test_model_manager_rejects_bad_checksum_and_leaves_no_partial_model(tmp_path: Path):
    model = PinnedModel(
        "fixture",
        "fixture.onnx",
        "https://example.test/fixture.onnx",
        "0" * 64,
        "MIT",
        "https://example.test/LICENSE",
        "test model",
    )
    manager = ModelManager(tmp_path / "models", models={"fixture": model})
    with pytest.raises(ModelInstallError, match="Checksum mismatch"):
        manager.install("fixture", downloader=lambda _url, output: output.write(b"wrong"))
    assert not (tmp_path / "models" / "fixture.onnx").exists()
