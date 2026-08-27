import sqlite3

import pytest

import photo_person_review.schema as schema
from photo_person_review.analysis.models import AnalysisResult, FaceObservation
from photo_person_review.analysis.ranking import rank_for_person
from photo_person_review.db import Catalog
from photo_person_review.review.store import ReviewStore
from photo_person_review.schema import MIGRATIONS, apply_migrations


def _v1_fixture(path):
    connection = sqlite3.connect(path)
    connection.executescript(MIGRATIONS[1])
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (1)")
    connection.execute("INSERT INTO sources VALUES ('s','folder',NULL,NULL,'{}','now')")
    connection.execute("INSERT INTO batches VALUES ('b','s','batch',NULL,'now','{}')")
    connection.execute("INSERT INTO photos VALUES ('p',?,'now',NULL,NULL,NULL,NULL,'{}')", ("a" * 64,))
    connection.execute("INSERT INTO targets VALUES ('a','Alice','now','{}')")
    connection.execute("INSERT INTO targets VALUES ('b','Bob','now','{}')")
    connection.execute("INSERT INTO analysis_runs VALUES ('r','b',NULL,'test',NULL,'now',NULL,'complete','{}','{}')")
    connection.execute("INSERT INTO tag_definitions VALUES ('tag','school',NULL,'now','{}')")
    connection.execute("INSERT INTO tag_assignments VALUES (7,'p','tag','true','import',0.9,'a','r','now','{}')")
    connection.execute(
        "INSERT INTO appearance_references VALUES ('appearance-a','a','p','detector-7','b','[0.2,0.3]','now','{}')"
    )
    connection.execute("INSERT INTO appearance_reference_events VALUES (9,'appearance-a','active','now','{}')")
    connection.execute("INSERT INTO candidate_scores VALUES (11,'score-run','a','p','b',0.75,'{\"score\":0.75}','now')")
    connection.execute("INSERT INTO decisions VALUES (13,'a','p','b','accept','user','{}','r','now')")
    connection.execute("INSERT INTO artifact_manifests VALUES ('manifest','b','b','now',NULL,'/tmp','{}','{}')")
    connection.execute("INSERT INTO faces VALUES ('face','p','r',0,0,1,1,NULL,NULL,'{}')")
    connection.execute(
        "INSERT INTO target_references VALUES ('assert-a','a','p','face','positive','b',NULL,'[1,0]','old','{}')"
    )
    connection.execute(
        "INSERT INTO target_references VALUES ('assert-b','b','p','face','positive','b',NULL,'[1,0]','old','{}')"
    )
    connection.execute(
        """INSERT INTO target_reference_events(
           reference_id,event,created_at,metadata_json)
           VALUES ('assert-a','active','old','{}')"""
    )
    connection.execute(
        """INSERT INTO target_reference_events(
           reference_id,event,created_at,metadata_json)
           VALUES ('assert-b','active','old','{}')"""
    )
    connection.commit()
    return connection


def test_v1_to_v2_preserves_rows_ids_events_and_surfaces_conflicts(tmp_path):
    connection = _v1_fixture(tmp_path / "v1.sqlite3")
    apply_migrations(connection)
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 2
    assert connection.execute("SELECT person_id,label FROM people ORDER BY person_id").fetchall() == [
        ("a", "Alice"),
        ("b", "Bob"),
    ]
    assert connection.execute(
        "SELECT assertion_id,person_id,assertion_kind FROM face_identity_assertions ORDER BY assertion_id"
    ).fetchall() == [("assert-a", "a", "positive"), ("assert-b", "b", "positive")]
    assert connection.execute(
        "SELECT assertion_id,event FROM face_identity_assertion_events ORDER BY event_id"
    ).fetchall() == [
        ("assert-a", "active"),
        ("assert-b", "active"),
    ]
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('targets','target_references','target_reference_events')"
        ).fetchall()
        == []
    )
    assert connection.execute("SELECT assignment_id,person_id FROM tag_assignments").fetchall() == [(7, "a")]
    assert connection.execute("SELECT reference_id,person_id,person_box_id FROM appearance_references").fetchall() == [
        ("appearance-a", "a", "detector-7")
    ]
    assert connection.execute("SELECT event_id,reference_id,event FROM appearance_reference_events").fetchall() == [
        (9, "appearance-a", "active")
    ]
    assert connection.execute("SELECT score_id,person_id FROM candidate_scores").fetchall() == [(11, "a")]
    assert connection.execute("SELECT decision_id,person_id FROM decisions").fetchall() == [(13, "a")]
    assert connection.execute("SELECT manifest_id,person_id FROM artifact_manifests").fetchall() == [("manifest", "b")]
    conflicts = connection.execute(
        "SELECT conflict_kind,face_id FROM identity_conflicts ORDER BY conflict_id"
    ).fetchall()
    assert ("multiple_positive_identities", "face") in conflicts
    # Reapplying v2 is a no-op and cannot duplicate conflict records.
    apply_migrations(connection)
    assert connection.execute("SELECT COUNT(*) FROM face_identity_assertions").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0] == 1


def test_retiring_one_assertion_removes_contradiction_from_report(tmp_path):
    connection = _v1_fixture(tmp_path / "v1.sqlite3")
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO face_identity_assertion_events(assertion_id,event,created_at,metadata_json) "
        "VALUES ('assert-b','retired','later','{}')"
    )
    connection.commit()
    with ReviewStore(connection) as review:
        assert review.identity_conflicts() == []


def test_ranking_refuses_active_identity_conflicts(tmp_path):
    connection = _v1_fixture(tmp_path / "v1.sqlite3")
    apply_migrations(connection)
    result = AnalysisResult("p", "b", faces=(FaceObservation("p", "face", (0, 0, 1, 1), embedding=(1, 0)),))
    with ReviewStore(connection) as review:
        with pytest.raises(ValueError, match="identity conflicts"):
            rank_for_person("a", [result], review)


def test_identity_assertion_creation_is_idempotent_and_blocks_cross_person_collision(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        photo_id = catalog.upsert_photo("b" * 64)
        catalog.create_person("alice")
        catalog.create_person("bob")
        with ReviewStore(catalog.connection) as review:
            assertion_id = review.add_identity_assertion("alice", media_id=photo_id, face_id="face", embedding=(1, 0))
            assert (
                review.add_identity_assertion("alice", media_id=photo_id, face_id="face", embedding=(0, 1))
                == assertion_id
            )
            try:
                review.add_identity_assertion("bob", media_id=photo_id, face_id="face", embedding=(1, 0))
            except ValueError as exc:
                assert "identity conflict" in str(exc)
            else:
                raise AssertionError("a face cannot be actively assigned to two people")
            review.retire_identity_assertion(assertion_id)
            assert review.add_identity_assertion("bob", media_id=photo_id, face_id="face", embedding=(1, 0))


def test_assertion_and_score_apis_require_explicit_person_creation(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        photo_id = catalog.upsert_photo("c" * 64)
        with ReviewStore(catalog.connection) as review:
            try:
                review.add_identity_assertion("missing", media_id=photo_id)
            except ValueError as exc:
                assert "unknown person" in str(exc)
            else:
                raise AssertionError("assertion API must not auto-create people")


def test_foreign_key_failure_rolls_back_v2_and_version_marker(tmp_path):
    path = tmp_path / "invalid-v1.sqlite3"
    connection = _v1_fixture(path)
    connection.execute(
        """INSERT INTO target_references VALUES
           ('orphan','a','missing-photo','missing-face','positive','b',NULL,'[1,0]','old','{}')"""
    )
    connection.execute(
        "INSERT INTO target_reference_events(reference_id,event,created_at,metadata_json) "
        "VALUES ('orphan','active','old','{}')"
    )
    connection.commit()
    try:
        apply_migrations(connection)
    except RuntimeError as exc:
        assert "foreign-key violations" in str(exc)
    else:
        raise AssertionError("invalid v1 fixture must fail migration")
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    assert connection.execute("SELECT type FROM sqlite_master WHERE name='targets'").fetchone()[0] == "table"


def test_event_order_preserves_retired_latest_state_during_migration(tmp_path):
    connection = _v1_fixture(tmp_path / "v1.sqlite3")
    connection.execute(
        "INSERT INTO target_reference_events(reference_id,event,created_at,metadata_json) "
        "VALUES ('assert-b','retired','later','{}')"
    )
    connection.commit()
    apply_migrations(connection)
    latest = connection.execute(
        "SELECT event FROM face_identity_assertion_events WHERE assertion_id='assert-b' ORDER BY event_id DESC LIMIT 1"
    ).fetchone()[0]
    assert latest == "retired"
    assert connection.execute("SELECT COUNT(*) FROM identity_conflicts").fetchone()[0] == 0


def test_mid_script_failure_rolls_back_and_restores_foreign_keys(tmp_path, monkeypatch):
    connection = _v1_fixture(tmp_path / "v1.sqlite3")
    broken = """
        PRAGMA foreign_keys=OFF;
        BEGIN;
        CREATE TABLE migration_fault (id INTEGER);
        DROP TABLE targets;
        THIS IS NOT VALID SQL;
    """
    monkeypatch.setitem(schema.MIGRATIONS, 2, broken)
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(connection)
    assert connection.in_transaction is False
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    assert connection.execute("SELECT type FROM sqlite_master WHERE name='targets'").fetchone()[0] == "table"
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='migration_fault'").fetchone() is None


def test_post_v2_surface_is_person_only(tmp_path):
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        assert hasattr(catalog, "create_person")
        assert not hasattr(catalog, "create_target")
        assert not hasattr(catalog, "record_person_decision")
        with ReviewStore(catalog.connection) as review:
            assert hasattr(review, "add_identity_assertion")
            assert not hasattr(review, "add_reference")
            assert not hasattr(review, "list_references")


def test_ranking_derives_other_people_positives_as_face_scoped_negatives():
    class Store:
        def identity_conflicts(self, person_id=None):
            return []

        def list_people(self):
            return [{"person_id": "alice"}, {"person_id": "bob"}]

        def list_identity_assertions(self, person_id, *, assertion_kind=None):
            rows = {
                ("alice", "positive"): [{"assertion_id": "alice-face", "embedding": (1.0, 0.0)}],
                ("bob", "positive"): [{"assertion_id": "bob-face", "embedding": (0.9, 0.1)}],
                ("alice", "negative"): [],
                ("bob", "negative"): [],
            }
            return rows[(person_id, assertion_kind)]

        def list_appearance_references(self, person_id, *, batch_id):
            return []

    result = AnalysisResult(
        "photo", "batch", faces=(FaceObservation("photo", "face", (0, 0, 1, 1), embedding=(1.0, 0.0)),)
    )
    alice = rank_for_person("alice", [result], Store())[0]
    bob = rank_for_person("bob", [result], Store())[0]
    assert alice.supporting_reference_id == "alice-face"
    assert bob.supporting_reference_id == "bob-face"
    assert bob.components.face < alice.components.face
