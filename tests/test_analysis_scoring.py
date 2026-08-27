from photo_person_review.analysis import (
    AnalysisResult,
    AppearanceObservation,
    FaceObservation,
    VisionEvidenceRequest,
    rank_candidates,
    rank_for_person,
    score_candidate,
)
from photo_person_review.review import ReviewMedia, select_packet_media


def test_orthogonal_vectors_do_not_create_positive_evidence():
    score = score_candidate(
        media_id="m1",
        batch_id="b1",
        faces=[FaceObservation("m1", "f1", (0, 0, 1, 1), embedding=(1.0, 0.0))],
        positive_face_references=[("ref", (0.0, 1.0))],
    )
    assert score.components.face == 0.0
    assert score.score == 0.0


def test_face_and_appearance_are_explainable_and_appearance_is_batch_scoped():
    face = FaceObservation("m1", "f1", (0, 0, 10, 10), embedding=(1.0, 0.0))
    score = score_candidate(
        media_id="m1",
        batch_id="day-2",
        faces=[face],
        appearances=[
            AppearanceObservation("m1", "p1", "day-1", (1.0, 0.0)),
            AppearanceObservation("m1", "p2", "day-2", (1.0, 0.0)),
        ],
        positive_face_references=[("ref-1", (1.0, 0.0))],
        appearance_references=[(1.0, 0.0)],
    )
    assert score.components.face == 1.0
    assert score.components.appearance == 1.0
    assert score.supporting_reference_id == "ref-1"
    assert score.supporting_appearance_subject_id == "p2"
    assert "face" in score.reasons


def test_hard_negative_caps_evidence_without_making_a_decision():
    score = score_candidate(
        media_id="m1",
        batch_id="b1",
        faces=[FaceObservation("m1", "f1", (0, 0, 1, 1), embedding=(1.0, 0.0))],
        positive_face_references=[("yes", (1.0, 0.0))],
        negative_face_references=[("no", (1.0, 0.0))],
    )
    assert 0.0 <= score.components.face < 0.2
    assert score.score > 0.0


def test_hard_negative_on_another_face_does_not_suppress_positive_face():
    score = score_candidate(
        media_id="m1",
        batch_id="b1",
        faces=[
            FaceObservation("m1", "chloe", (0, 0, 10, 10), embedding=(1.0, 0.0)),
            FaceObservation("m1", "isabella", (20, 0, 10, 10), embedding=(0.0, 1.0)),
        ],
        positive_face_references=[("chloe-ref", (1.0, 0.0))],
        negative_face_references=[("isabella-ref", (0.0, 1.0))],
    )
    assert score.components.face == 1.0
    assert score.supporting_face_id == "chloe"
    assert score.supporting_reference_id == "chloe-ref"


def test_faces_without_positive_references_do_not_claim_support():
    score = score_candidate(
        media_id="m1",
        batch_id="b1",
        faces=[FaceObservation("m1", "face", (0, 0, 10, 10), embedding=(1.0, 0.0))],
    )
    assert score.components.face == 0.0
    assert score.supporting_face_id is None
    assert score.supporting_reference_id is None


def test_rank_is_deterministic_for_ties():
    low = score_candidate(media_id="b", batch_id="x")
    high = score_candidate(media_id="a", batch_id="x", provider_hint=1.0)
    assert [x.media_id for x in rank_candidates([low, high])] == ["a", "b"]


def test_person_ranking_uses_long_lived_faces_but_current_batch_appearance():
    class FakeStore:
        def identity_conflicts(self, person_id=None):
            return []

        def list_people(self):
            return [{"person_id": "t"}]

        def list_identity_assertions(self, person_id, *, assertion_kind=None):
            return [{"assertion_id": "old", "embedding": (1.0, 0.0)}] if assertion_kind == "positive" else []

        def list_appearance_references(self, person_id, *, batch_id):
            return [{"feature": (1.0, 0.0)}] if batch_id == "b2" else []

    store = FakeStore()
    results = [
        AnalysisResult(
            "m",
            "b2",
            faces=(FaceObservation("m", "f", (0, 0, 1, 1), embedding=(1, 0)),),
            appearances=(AppearanceObservation("m", "p", "b2", (1, 0)),),
        ),
        AnalysisResult(
            "old-batch",
            "b1",
            appearances=(AppearanceObservation("old-batch", "p", "b1", (1, 0)),),
        ),
    ]
    ranked = rank_for_person("t", results, store)
    assert ranked[0].media_id == "m"
    assert ranked[0].components.appearance == 1.0


def test_remote_vision_requires_two_explicit_consent_flags():
    request = VisionEvidenceRequest("r", "t", ("m",))
    assert not request.can_upload
    try:
        request.validate_for_remote()
    except PermissionError:
        pass
    else:
        raise AssertionError("remote validation must reject the default policy")
    request = VisionEvidenceRequest(
        "r",
        "t",
        ("m",),
        remote_consent=True,
        allow_remote=True,
        max_cost_usd=0.25,
    )
    request.validate_for_remote()


def test_packet_selection_is_deterministic_and_excludes_decided_media(tmp_path):
    media = [ReviewMedia("b", tmp_path / "b.jpg"), ReviewMedia("a", tmp_path / "a.jpg")]
    chosen = select_packet_media(
        media,
        strategy="likely",
        scores={"a": {"score": 0.9}, "b": {"score": 0.2}},
        decisions={"a": "accept"},
    )
    assert [item.media_id for item in chosen] == ["b"]
