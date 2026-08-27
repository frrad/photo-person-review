"""Small, serialisable value objects used by local analysis and ranking.

The analysis layer deliberately does not know how media are imported or where
the application's database lives.  In particular, an observation contains an
ID and measurements, never the image bytes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Sequence

NumberVector = Sequence[float]


@dataclass(frozen=True)
class FaceObservation:
    media_id: str
    face_id: str
    bbox: tuple[int, int, int, int]
    quality: float = 1.0
    embedding: tuple[float, ...] | None = None
    detector_version: str = "unknown"
    landmarks: tuple[tuple[float, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PersonObservation:
    media_id: str
    person_box_id: str
    bbox: tuple[int, int, int, int]
    face_id: str | None = None
    confidence: float = 1.0
    detector_version: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppearanceObservation:
    media_id: str
    appearance_subject_id: str
    batch_id: str
    feature: tuple[float, ...]
    extractor_version: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisResult:
    """Output of an analyzer for one media item."""

    media_id: str
    batch_id: str
    faces: tuple[FaceObservation, ...] = ()
    people: tuple[PersonObservation, ...] = ()
    appearances: tuple[AppearanceObservation, ...] = ()
    provider_hints: Mapping[str, Any] = field(default_factory=dict)
    analyzer_version: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "batch_id": self.batch_id,
            "faces": [x.to_dict() for x in self.faces],
            "people": [x.to_dict() for x in self.people],
            "appearances": [x.to_dict() for x in self.appearances],
            "provider_hints": dict(self.provider_hints),
            "analyzer_version": self.analyzer_version,
        }


@dataclass(frozen=True)
class VisionEvidenceRequest:
    """Provider-neutral request for a future VLM backend.

    ``image_ids`` refer to media/crop artifacts prepared by the caller.  They
    are intentionally not paths or bytes so a backend cannot accidentally
    transmit arbitrary files.
    """

    request_id: str
    person_id: str
    image_ids: tuple[str, ...]
    evidence_types: tuple[str, ...] = ("correspondence",)
    prompt_version: str = "1"
    max_images: int = 16
    remote_consent: bool = False
    allow_remote: bool = False
    privacy_mode: Literal["zdr", "private", "standard"] = "zdr"
    data_collection: Literal["deny", "allow"] = "deny"
    provider_allowlist: tuple[str, ...] = ()
    max_cost_usd: float | None = None
    max_input_bytes: int = 8_000_000
    allow_provider_fallbacks: bool = False

    def __post_init__(self) -> None:
        if self.max_images < 1:
            raise ValueError("max_images must be positive")
        if self.max_input_bytes < 1:
            raise ValueError("max_input_bytes must be positive")
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("max_cost_usd cannot be negative")

    @property
    def can_upload(self) -> bool:
        """Whether a backend has explicit consent to transmit image data."""
        return self.remote_consent and self.allow_remote

    def validate_for_remote(self) -> None:
        """Validate explicit consent and privacy policy before an upload."""
        if not self.can_upload:
            raise PermissionError("Remote vision is disabled: set both remote_consent and allow_remote explicitly.")
        if self.privacy_mode == "zdr" and self.data_collection != "deny":
            raise ValueError("zdr privacy_mode requires data_collection='deny'")


@dataclass(frozen=True)
class VisionEvidenceResult:
    request_id: str
    backend_id: str
    model: str
    evidence: Mapping[str, Any]
    response_hash: str | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)


class VisionEvidenceBackend:
    """Structural protocol implemented by a future OpenRouter adapter.

    This concrete, dependency-free base contract makes it possible to test
    the ranking pipeline with a fake backend today.  A live network client is
    deliberately outside this package's first milestone.
    """

    backend_id = "abstract"

    def analyze(self, request: VisionEvidenceRequest) -> VisionEvidenceResult:
        raise NotImplementedError
