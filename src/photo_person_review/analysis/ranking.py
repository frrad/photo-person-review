"""Target-aware ranking service for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .models import AnalysisResult
from .scoring import CandidateScore, ScoreWeights, rank_candidates, score_candidate


class ReferenceStore(Protocol):
    def list_references(self, target_id: str, *, kind: str | None = None) -> list[Mapping[str, Any]]: ...

    def list_appearance_references(self, target_id: str, *, batch_id: str) -> list[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class CandidateEvidence:
    """Non-image evidence supplied by importers or a future context analyzer."""

    duplicate: float = 0.0
    temporal: float = 0.0
    context: float = 0.0
    provider_hint: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


def rank_for_target(
    target_id: str,
    results: Sequence[AnalysisResult],
    store: ReferenceStore,
    *,
    evidence: Mapping[str, CandidateEvidence] | None = None,
    weights: ScoreWeights | None = None,
) -> list[CandidateScore]:
    """Rank results using persistent face refs and current-batch appearance refs.

    Face references intentionally have no batch filter: they are the target's
    long-lived identity evidence. Appearance references are fetched per result
    batch, preventing an outfit from leaking into future or unrelated batches.
    """
    positives = [
        (str(row["reference_id"]), row["embedding"])
        for row in store.list_references(target_id, kind="positive")
        if row.get("embedding") is not None
    ]
    negatives = [
        (str(row["reference_id"]), row["embedding"])
        for row in store.list_references(target_id, kind="negative")
        if row.get("embedding") is not None
    ]
    appearance_by_batch: dict[str, list[Sequence[float]]] = {}
    for result in results:
        if result.batch_id not in appearance_by_batch:
            appearance_by_batch[result.batch_id] = [
                row["feature"] for row in store.list_appearance_references(target_id, batch_id=result.batch_id)
            ]
    ranked: list[CandidateScore] = []
    for result in results:
        extra = (evidence or {}).get(result.media_id, CandidateEvidence())
        ranked.append(
            score_candidate(
                media_id=result.media_id,
                batch_id=result.batch_id,
                faces=result.faces,
                appearances=result.appearances,
                positive_face_references=positives,
                negative_face_references=negatives,
                appearance_references=appearance_by_batch[result.batch_id],
                duplicate=extra.duplicate,
                temporal=extra.temporal,
                context=extra.context,
                provider_hint=extra.provider_hint,
                weights=weights,
                metadata=extra.metadata,
            )
        )
    return rank_candidates(ranked)
