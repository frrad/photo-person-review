from photo_person_review.analysis import (
    AnalysisResult,
    AppearanceObservation,
    CatalogAnalysisRepository,
    FaceObservation,
    PersonObservation,
)
from photo_person_review.db import Catalog


def test_analysis_repository_persists_measurements_and_queries_batch(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        batch_id = catalog.create_batch(capture_date="2026-08-27")
        photo_id = catalog.upsert_photo("e" * 64)
        other_id = catalog.upsert_photo("f" * 64)
        catalog.observe_batch_photo(batch_id, photo_id)
        catalog.observe_batch_photo(batch_id, other_id)
        result = AnalysisResult(
            media_id=photo_id,
            batch_id=batch_id,
            analyzer_version="fake-v1",
            faces=(FaceObservation(photo_id, "face-1", (1, 2, 30, 40), embedding=(0.1, 0.2)),),
            people=(PersonObservation(photo_id, "person-1", (0, 1, 50, 60), face_id="face-1"),),
            appearances=(AppearanceObservation(photo_id, "person-1", batch_id, (0.3, 0.4)),),
        )
        repository = CatalogAnalysisRepository(catalog)
        run_id = repository.save_analysis(result)

        assert (
            catalog.connection.execute(
                "SELECT status FROM analysis_runs WHERE analysis_run_id=?", (run_id,)
            ).fetchone()[0]
            == "complete"
        )
        assert catalog.counts()["analysis_results"] == 1
        assert catalog.counts()["faces"] == 1
        assert catalog.counts()["person_boxes"] == 1
        assert catalog.counts()["numeric_features"] == 2
        assert repository.unanalysed_batch_photos(batch_id) == [other_id]
        assert repository.unanalyzed_batch_photos(batch_id) == [other_id]
        assert repository.analyzed_batch_photos(batch_id) == [photo_id]
        assert repository.batch_photo_records(batch_id, analyzed=False)[0]["source_path"] is None
        rows = repository.latest_face_observations(batch_id)
        assert len(rows) == 1
        assert rows[0]["photo_id"] == photo_id
        assert rows[0]["metadata_json"]


def test_analysis_repository_latest_face_run_is_used_and_runs_append(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        batch_id = catalog.create_batch()
        photo_id = catalog.upsert_photo("1" * 64)
        catalog.observe_batch_photo(batch_id, photo_id)
        repository = CatalogAnalysisRepository(catalog)
        first = AnalysisResult(
            photo_id,
            batch_id,
            faces=(FaceObservation(photo_id, "face", (1, 1, 2, 2)),),
            analyzer_version="v1",
        )
        second = AnalysisResult(
            photo_id,
            batch_id,
            faces=(FaceObservation(photo_id, "face", (9, 9, 8, 8)),),
            analyzer_version="v2",
        )
        repository.save_analysis(first)
        repository.save_analysis(second)
        assert catalog.counts()["analysis_runs"] == 2
        assert catalog.counts()["analysis_results"] == 2
        assert repository.latest_face_observations(batch_id)[0]["x"] == 9
        assert repository.unanalysed_batch_photos(batch_id, analyzer_version="v3") == [photo_id]
