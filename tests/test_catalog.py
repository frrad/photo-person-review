import pytest

from photo_person_review.db import Catalog, stable_photo_id
from photo_person_review.review import ReviewStore


def test_catalog_is_metadata_only_and_migrates(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        assert catalog.connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        columns = {row[1] for row in catalog.connection.execute("PRAGMA table_info(photos)")}
        assert not columns.intersection({"image", "image_bytes", "blob", "photo_bytes"})
        tables = catalog.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        declared_types = {
            str(column[2]).upper()
            for table in tables
            for column in catalog.connection.execute(f"PRAGMA table_info({table[0]})")
        }
        assert "BLOB" not in declared_types
        assert catalog.counts()["photos"] == 0


def test_same_photo_is_upserted_but_source_observations_accumulate(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source = catalog.create_source("folder", root_path="/photos")
        run = catalog.create_import_run(source)
        digest = "a" * 64
        photo_id = catalog.upsert_photo(digest, width=100, height=200)
        catalog.observe_source_file(source, photo_id, "/photos/a.jpg", file_size=10, import_run_id=run)
        run2 = catalog.create_import_run(source)
        catalog.observe_source_file(source, photo_id, "/photos/a.jpg", file_size=10, import_run_id=run2)
        catalog.add_metadata(photo_id, "exif.make", "Camera", "exif", run)
        catalog.add_metadata(photo_id, "exif.make", "Camera 2", "exif", run)
        assert catalog.upsert_photo(digest) == photo_id
        assert catalog.counts()["photos"] == 1
        assert catalog.counts()["source_files"] == 2
        batch = catalog.create_batch(source, capture_date="2026-08-27")
        catalog.observe_batch_photo(batch, photo_id, import_run_id=run)
        catalog.observe_batch_photo(batch, photo_id, import_run_id=run2, observation_state="replaced")
        assert catalog.counts()["batch_photos"] == 1
        assert catalog.counts()["batch_photo_observations"] == 2
        assert catalog.counts()["photo_metadata"] == 2


def test_decisions_and_tag_assignments_are_append_only(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        photo_id = catalog.upsert_photo("b" * 64)
        catalog.create_person("person-1")
        tag_id = catalog.create_tag("contains-person")
        catalog.assign_tag(photo_id, tag_id, provenance="local-model", confidence=0.8)
        catalog.record_decision("person-1", photo_id, "accept")
        catalog.record_decision("person-1", photo_id, "unsure")
        assert catalog.counts()["tag_assignments"] == 1
        assert catalog.counts()["decisions"] == 2
        assert catalog.latest_decisions("person-1")[0]["decision"] == "unsure"


def test_invalid_sha_and_decision_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        stable_photo_id("nope")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        photo_id = catalog.upsert_photo("c" * 64)
        catalog.create_person("person-1")
        with pytest.raises(ValueError):
            catalog.record_decision("person-1", photo_id, "maybe")


def test_target_creation_is_idempotent_and_can_add_a_label(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        assert catalog.create_person("person-1") == "person-1"
        assert catalog.create_person("person-1", "Person") == "person-1"
        row = catalog.connection.execute("SELECT label FROM people WHERE person_id='person-1'").fetchone()
        assert row["label"] == "Person"
        assert catalog.counts()["people"] == 1


def test_review_store_uses_core_targets_decisions_and_reference_events(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        photo_id = catalog.upsert_photo("d" * 64)
        batch_id = catalog.create_batch()
        with ReviewStore(catalog.connection) as review:
            review.create_person("person-1")
            reference_id = review.add_identity_assertion(
                "person-1", media_id=photo_id, batch_id=batch_id, embedding=(1, 0)
            )
            review.retire_identity_assertion(reference_id)
            review.add_decision("person-1", photo_id, "accept", batch_id=batch_id)
        assert (
            catalog.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='review_targets'"
            ).fetchone()[0]
            == 0
        )
        assert catalog.connection.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
        assert catalog.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert (
            catalog.connection.execute(
                "SELECT event FROM face_identity_assertion_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()[0]
            == "retired"
        )
