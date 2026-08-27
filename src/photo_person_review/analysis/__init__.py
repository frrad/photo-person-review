"""Local analysis and explainable ranking primitives."""

from .model_manager import PINNED_MODELS, ModelInstallError, ModelManager, PinnedModel
from .models import (
    AnalysisResult,
    AppearanceObservation,
    FaceObservation,
    PersonObservation,
    VisionEvidenceBackend,
    VisionEvidenceRequest,
    VisionEvidenceResult,
)
from .pipeline import LocalAnalyzer, MissingModelError, OpenCVAnalyzer, analyze_media
from .ranking import CandidateEvidence, rank_for_target
from .repository import CatalogAnalysisRepository
from .scoring import (
    CandidateScore,
    EvidenceComponents,
    ScoreWeights,
    appearance_evidence,
    cosine_similarity,
    face_evidence,
    rank_candidates,
    score_candidate,
)

__all__ = [
    "AnalysisResult",
    "AppearanceObservation",
    "FaceObservation",
    "PersonObservation",
    "VisionEvidenceBackend",
    "VisionEvidenceRequest",
    "VisionEvidenceResult",
    "LocalAnalyzer",
    "MissingModelError",
    "OpenCVAnalyzer",
    "analyze_media",
    "ModelInstallError",
    "ModelManager",
    "PinnedModel",
    "PINNED_MODELS",
    "CatalogAnalysisRepository",
    "CandidateEvidence",
    "rank_for_target",
    "CandidateScore",
    "EvidenceComponents",
    "ScoreWeights",
    "appearance_evidence",
    "cosine_similarity",
    "face_evidence",
    "rank_candidates",
    "score_candidate",
]
