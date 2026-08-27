"""Explainable candidate scoring.

Scores are ranking aids, not identity decisions.  All component values are
kept in the result so the conversational reviewer can explain every packet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import AppearanceObservation, FaceObservation


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    nl = math.sqrt(sum(a * a for a in left))
    nr = math.sqrt(sum(b * b for b in right))
    if nl == 0 or nr == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (nl * nr)))


def _positive_similarity(value: float) -> float:
    # Orthogonal embeddings carry no positive evidence. Mapping [-1, 1] into
    # [0, 1] would incorrectly give unrelated vectors a 0.5 baseline.
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class EvidenceComponents:
    face: float = 0.0
    appearance: float = 0.0
    duplicate: float = 0.0
    temporal: float = 0.0
    context: float = 0.0
    provider_hint: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "face": self.face,
            "appearance": self.appearance,
            "duplicate": self.duplicate,
            "temporal": self.temporal,
            "context": self.context,
            "provider_hint": self.provider_hint,
        }


@dataclass(frozen=True)
class CandidateScore:
    media_id: str
    batch_id: str
    score: float
    components: EvidenceComponents
    supporting_face_id: str | None = None
    supporting_reference_id: str | None = None
    supporting_person_id: str | None = None
    reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "batch_id": self.batch_id,
            "score": self.score,
            "components": self.components.as_dict(),
            "supporting_face_id": self.supporting_face_id,
            "supporting_reference_id": self.supporting_reference_id,
            "supporting_person_id": self.supporting_person_id,
            "reasons": list(self.reasons),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ScoreWeights:
    face: float = 0.60
    appearance: float = 0.18
    duplicate: float = 0.10
    temporal: float = 0.05
    context: float = 0.04
    provider_hint: float = 0.03


def face_evidence(
    faces: Iterable[FaceObservation],
    positive_references: Iterable[tuple[str, Sequence[float]]],
    negative_references: Iterable[tuple[str, Sequence[float]]] = (),
) -> tuple[float, str | None, str | None]:
    faces = list(faces)
    refs = list(positive_references)
    negs = list(negative_references)
    best: tuple[float, str | None, str | None] = (-1.0, None, None)
    for face in faces:
        if face.embedding is None:
            continue
        face_positive: tuple[float, str | None] = (0.0, None)
        for ref_id, ref in refs:
            similarity = _positive_similarity(cosine_similarity(face.embedding, ref))
            # Quality only reduces weak detections; it cannot boost evidence.
            similarity *= max(0.0, min(1.0, face.quality))
            if similarity > face_positive[0]:
                face_positive = (similarity, ref_id)
        if face_positive[1] is None:
            continue
        # Apply a hard-negative cap to this face's own positive evidence. A
        # different face in a group photo must not suppress the best match.
        negative = max(
            (_positive_similarity(cosine_similarity(face.embedding, ref)) for _, ref in negs),
            default=0.0,
        )
        adjusted = face_positive[0] * max(0.0, 1.0 - negative * 0.85)
        if adjusted > best[0]:
            best = (adjusted, face.face_id, face_positive[1])
    return max(0.0, best[0]), best[1], best[2]


def appearance_evidence(
    appearances: Iterable[AppearanceObservation],
    references: Iterable[Sequence[float]],
    *,
    batch_id: str,
) -> tuple[float, str | None]:
    refs = list(references)
    best: tuple[float, str | None] = (0.0, None)
    for item in appearances:
        if item.batch_id != batch_id:
            continue
        for ref in refs:
            value = _positive_similarity(cosine_similarity(item.feature, ref))
            if value > best[0]:
                best = (value, item.person_id)
    return best


def score_candidate(
    *,
    media_id: str,
    batch_id: str,
    faces: Iterable[FaceObservation] = (),
    appearances: Iterable[AppearanceObservation] = (),
    positive_face_references: Iterable[tuple[str, Sequence[float]]] = (),
    negative_face_references: Iterable[tuple[str, Sequence[float]]] = (),
    appearance_references: Iterable[Sequence[float]] = (),
    duplicate: float = 0.0,
    temporal: float = 0.0,
    context: float = 0.0,
    provider_hint: float = 0.0,
    weights: ScoreWeights | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> CandidateScore:
    weights = weights or ScoreWeights()
    face, face_id, ref_id = face_evidence(faces, positive_face_references, negative_face_references)
    appearance, person_id = appearance_evidence(appearances, appearance_references, batch_id=batch_id)
    components = EvidenceComponents(
        face=face,
        appearance=appearance,
        duplicate=max(0.0, min(1.0, duplicate)),
        temporal=max(0.0, min(1.0, temporal)),
        context=max(0.0, min(1.0, context)),
        provider_hint=max(0.0, min(1.0, provider_hint)),
    )
    total = sum(value * getattr(weights, key) for key, value in components.as_dict().items())
    reasons = tuple(key for key, value in components.as_dict().items() if value >= 0.5)
    return CandidateScore(
        media_id=media_id,
        batch_id=batch_id,
        score=total,
        components=components,
        supporting_face_id=face_id,
        supporting_reference_id=ref_id,
        supporting_person_id=person_id,
        reasons=reasons,
        metadata=metadata or {},
    )


def rank_candidates(candidates: Iterable[CandidateScore]) -> list[CandidateScore]:
    return sorted(candidates, key=lambda x: (-x.score, x.media_id))
