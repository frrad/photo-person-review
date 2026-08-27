"""Pluggable local analysis contracts and orchestration."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from PIL import Image, ImageOps

from .models import AnalysisResult, FaceObservation


class LocalAnalyzer(Protocol):
    """A local, deterministic analyzer that emits measurements only."""

    analyzer_version: str

    def analyze(self, media_id: str, path: Path, *, batch_id: str) -> AnalysisResult: ...


class AnalysisRepository(Protocol):
    def save_analysis(self, result: AnalysisResult) -> None: ...


def analyze_media(
    analyzer: LocalAnalyzer,
    media: Iterable[tuple[str, Path]],
    *,
    batch_id: str,
    repository: AnalysisRepository | None = None,
) -> list[AnalysisResult]:
    """Analyze media and optionally persist each result as it completes."""
    results: list[AnalysisResult] = []
    for media_id, path in media:
        result = analyzer.analyze(media_id, path, batch_id=batch_id)
        results.append(result)
        if repository is not None:
            repository.save_analysis(result)
    return results


class MissingModelError(RuntimeError):
    """Raised when an optional OpenCV model has not been installed."""


class OpenCVAnalyzer:
    """YuNet + SFace analyzer with no person bytes retained.

    Person detection and appearance extraction are intentionally not inferred
    here yet.  A future adapter may add those observations without changing
    face IDs or embeddings. ``cv2_module`` and model factories are injectable
    so tests do not need OpenCV or downloaded model files.
    """

    analyzer_version = "opencv-unconfigured"

    def __init__(
        self,
        *,
        face_model: Path | None = None,
        recognition_model: Path | None = None,
        person_model: Path | None = None,
        cv2_module: Any | None = None,
        detector: Any | None = None,
        recognizer: Any | None = None,
        image_to_array: Callable[[Image.Image], Any] | None = None,
        analyzer_version: str = "yunet+sface",
        face_score_threshold: float = 0.50,
        face_max_side: int = 2000,
    ):
        if face_max_side < 0:
            raise ValueError("face_max_side must be non-negative")
        self.face_model = Path(face_model) if face_model else None
        self.recognition_model = Path(recognition_model) if recognition_model else None
        # Kept as a forward-compatible constructor argument; person analysis
        # is not required for face-only operation.
        self.person_model = Path(person_model) if person_model else None
        self.cv2 = cv2_module
        self._detector = detector
        self._recognizer = recognizer
        self._image_to_array = image_to_array
        # CLI callers include this component in their complete version string
        # so ``--new`` can distinguish resize policies.  Add it for direct API
        # users as well, without double-appending an explicitly versioned name.
        self.analyzer_version = (
            analyzer_version if "max-side:" in analyzer_version else f"{analyzer_version}+max-side:{face_max_side}"
        )
        self.face_score_threshold = face_score_threshold
        self.face_max_side = face_max_side

    def validate_models(self) -> None:
        missing = [
            str(path) for path in (self.face_model, self.recognition_model) if path is None or not path.is_file()
        ]
        if missing:
            raise MissingModelError(
                "OpenCV analysis models are not installed. Run `ppr models install` "
                f"or configure valid model files (missing: {', '.join(missing)})."
            )
        if self.cv2 is None and importlib.util.find_spec("cv2") is None:
            raise MissingModelError("OpenCV is not installed; install the optional local-analysis dependencies.")

    def _cv2(self) -> Any:
        if self.cv2 is None:
            try:
                self.cv2 = importlib.import_module("cv2")
            except ImportError as exc:
                raise MissingModelError(
                    "OpenCV is not installed; install the optional local-analysis dependencies."
                ) from exc
        return self.cv2

    def _load_models(self) -> tuple[Any, Any]:
        if self._detector is not None and self._recognizer is not None:
            return self._detector, self._recognizer
        self.validate_models()
        cv2 = self._cv2()
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(self.face_model), "", (320, 320), self.face_score_threshold, 0.3, 5000
            )
            self._recognizer = cv2.FaceRecognizerSF.create(str(self.recognition_model), "")
        except Exception as exc:
            raise MissingModelError(
                "OpenCV could not load YuNet/SFace models; reinstall pinned model files with `ppr models install`."
            ) from exc
        return self._detector, self._recognizer

    def _to_bgr(self, image: Image.Image) -> Any:
        if self._image_to_array is not None:
            return self._image_to_array(image)
        if importlib.util.find_spec("numpy") is None:
            raise MissingModelError("NumPy is required for local face analysis.")
        np = importlib.import_module("numpy")
        array = np.asarray(image)
        cv2 = self._cv2()
        code = getattr(cv2, "COLOR_RGB2BGR", None)
        return cv2.cvtColor(array, code) if code is not None else array

    def _scaled_image(self, image: Image.Image) -> tuple[Image.Image, float, float]:
        """Return the detector image and factors mapping it to source pixels.

        The detector and SFace recognizer work on the smaller image.  Geometry
        emitted by the analyzer is converted back to the EXIF-corrected source
        dimensions before it crosses the persistence boundary.
        """
        if self.face_max_side == 0 or max(image.size) <= self.face_max_side:
            return image, 1.0, 1.0
        source_width, source_height = image.size
        scale = self.face_max_side / max(source_width, source_height)
        scaled_size = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
        scaled = image.resize(scaled_size, Image.Resampling.LANCZOS)
        return scaled, source_width / scaled_size[0], source_height / scaled_size[1]

    @staticmethod
    def _detections(raw: Any) -> Any:
        if isinstance(raw, tuple) and len(raw) == 2:
            return raw[1]
        return raw

    @staticmethod
    def _row_value(row: Any, index: int, default: float = 0.0) -> float:
        try:
            return float(row[index])
        except (IndexError, TypeError, ValueError):
            return default

    def analyze(self, media_id: str, path: Path, *, batch_id: str) -> AnalysisResult:
        detector, recognizer = self._load_models()
        if not path.is_file():
            raise FileNotFoundError(path)
        with Image.open(path) as source:
            corrected = ImageOps.exif_transpose(source).convert("RGB")
            detector_image, x_factor, y_factor = self._scaled_image(corrected)
            image = self._to_bgr(detector_image)
        height, width = image.shape[:2]
        detector.setInputSize((width, height))
        detected = self._detections(detector.detect(image))
        faces: list[FaceObservation] = []
        if detected is None:
            detected = []
        for index, row in enumerate(detected, 1):
            detector_x = max(0, round(self._row_value(row, 0)))
            detector_y = max(0, round(self._row_value(row, 1)))
            detector_box_width = max(1, round(self._row_value(row, 2)))
            detector_box_height = max(1, round(self._row_value(row, 3)))
            detector_x = min(detector_x, max(0, width - 1))
            detector_y = min(detector_y, max(0, height - 1))
            detector_box_width = min(detector_box_width, width - detector_x)
            detector_box_height = min(detector_box_height, height - detector_y)
            x = min(max(0, round(detector_x * x_factor)), corrected.width - 1)
            y = min(max(0, round(detector_y * y_factor)), corrected.height - 1)
            box_width = min(max(1, round(detector_box_width * x_factor)), corrected.width - x)
            box_height = min(max(1, round(detector_box_height * y_factor)), corrected.height - y)
            landmarks = tuple(
                (
                    self._row_value(row, offset) * x_factor,
                    self._row_value(row, offset + 1) * y_factor,
                )
                for offset in (4, 6, 8, 10, 12)
            )
            try:
                aligned = recognizer.alignCrop(image, row)
                feature = recognizer.feature(aligned)
                values = [float(item) for item in feature.reshape(-1)]
            except Exception as exc:
                raise RuntimeError(f"SFace failed to embed face {index} in {path}") from exc
            norm = sum(item * item for item in values) ** 0.5
            embedding = tuple(item / norm for item in values) if norm else tuple(values)
            faces.append(
                FaceObservation(
                    media_id=media_id,
                    face_id=f"{media_id}:face:{index:02d}",
                    bbox=(x, y, box_width, box_height),
                    quality=max(0.0, min(1.0, self._row_value(row, 14, 1.0))),
                    embedding=embedding,
                    detector_version="yunet",
                    landmarks=landmarks,
                )
            )
        return AnalysisResult(
            media_id=media_id,
            batch_id=batch_id,
            faces=tuple(faces),
            analyzer_version=self.analyzer_version,
        )
