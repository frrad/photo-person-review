import csv
import errno
import json
import os
from pathlib import Path

from photo_person_review.db import Catalog
from photo_person_review.exporters import (
    catalog_rows,
    write_csv,
    write_json,
    write_person_hardlinks,
    write_person_symlinks,
)


def _photo(catalog: Catalog, source_id: str, run_id: str, photo_id: str, path: Path) -> None:
    catalog.upsert_photo(photo_id, capture_time="2026-08-26T09:23:28")
    catalog.observe_source_file(source_id, photo_id, str(path), import_run_id=run_id)


def test_person_export_contains_metadata_tags_and_decision_without_photo_bytes(tmp_path: Path) -> None:
    source_file = tmp_path / "source.jpg"
    source_file.write_bytes(b"placeholder")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source = catalog.create_source("folder")
        run = catalog.create_import_run(source)
        photo_id = catalog.upsert_photo("a" * 64, width=20, height=10)
        catalog.observe_source_file(source, photo_id, str(source_file), import_run_id=run)
        catalog.add_metadata(photo_id, "camera", "old", "test", run)
        catalog.add_metadata(photo_id, "camera", "new", "test", run)
        person_id = catalog.create_person("person")
        tag_id = catalog.create_tag("contains-person")
        catalog.assign_tag(photo_id, tag_id, provenance="user", person_id=person_id)
        catalog.record_decision(person_id, photo_id, "accept")
        rows = catalog_rows(catalog.connection, person_id=person_id)

    assert rows[0]["metadata"]["camera"]["value"] == "new"
    assert rows[0]["tags"][0]["name"] == "contains-person"
    assert rows[0]["decision"]["decision"] == "accept"
    assert rows[0]["person_id"] == person_id
    assert "target_id" not in rows[0]
    assert "bytes" not in json.dumps(rows[0]).lower()
    assert json.loads(write_json(tmp_path / "out.json", rows).read_text())[0]["person_id"] == person_id
    with write_csv(tmp_path / "out.csv", rows).open(encoding="utf-8", newline="") as stream:
        assert next(csv.DictReader(stream))["person_id"] == person_id


def test_person_symlink_export_unions_assertions_and_accepts(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    first_source = source_dir / "first.jpg"
    accepted_source = source_dir / "accepted.PNG"
    rejected_source = source_dir / "rejected.jpg"
    for source in (first_source, accepted_source, rejected_source):
        source.write_bytes(b"placeholder")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        first_id = "a" * 64
        accepted_id = "b" * 64
        rejected_id = "c" * 64
        for photo_id, path in (
            (first_id, first_source),
            (accepted_id, accepted_source),
            (rejected_id, rejected_source),
        ):
            _photo(catalog, source_id, run_id, photo_id, path)
        person_id = catalog.create_person("chloe", label="Chloé Vidigami")
        catalog.add_identity_assertion(person_id, first_id, kind="positive")
        catalog.add_identity_assertion(person_id, rejected_id, kind="positive")
        catalog.record_decision(person_id, accepted_id, "accept")
        catalog.record_decision(person_id, rejected_id, "reject")
        result = write_person_symlinks(catalog.connection, person_id, tmp_path / "export")

    assert result["row_count"] == 2 and result["person_id"] == person_id
    first_name = f"chlo_vidigami_2026-08-26_092328_{first_id}.jpg"
    accepted_name = f"chlo_vidigami_2026-08-26_092328_{accepted_id}.png"
    destination = tmp_path / "export"
    assert (destination / first_name).resolve() == first_source.resolve()
    assert (destination / accepted_name).resolve() == accepted_source.resolve()
    manifest = json.loads((destination / ".ppr-symlink-export.json").read_text())
    assert manifest["person_id"] == person_id and manifest["version"] == 4
    assert "target_id" not in manifest


def test_overlapping_person_exports_are_independent(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"placeholder")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = "d" * 64
        _photo(catalog, source_id, run_id, photo_id, source)
        chloe = catalog.create_person("chloe", label="Chloe")
        isabella = catalog.create_person("isabella", label="Isabella")
        catalog.record_decision(chloe, photo_id, "accept")
        catalog.record_decision(isabella, photo_id, "accept")
        chloe_result = write_person_hardlinks(catalog.connection, chloe, tmp_path / "chloe")
        isabella_result = write_person_hardlinks(catalog.connection, isabella, tmp_path / "isabella")

    assert chloe_result["person_id"] == chloe and isabella_result["person_id"] == isabella
    assert len(list((tmp_path / "chloe").glob("*.jpg"))) == 1
    assert len(list((tmp_path / "isabella").glob("*.jpg"))) == 1
    assert (tmp_path / "chloe" / ".ppr-symlink-export.json").read_text() != (
        tmp_path / "isabella" / ".ppr-symlink-export.json"
    ).read_text()


def test_hardlink_export_is_idempotent_and_records_v4_ownership(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo bytes")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = "e" * 64
        _photo(catalog, source_id, run_id, photo_id, source)
        person_id = catalog.create_person("chloe", label="Chloe")
        catalog.record_decision(person_id, photo_id, "accept")
        first = write_person_hardlinks(catalog.connection, person_id, tmp_path / "export")
        second = write_person_hardlinks(catalog.connection, person_id, tmp_path / "export")

    name = f"chloe_2026-08-26_092328_{photo_id}.jpg"
    exported = tmp_path / "export" / name
    assert exported.is_file() and not exported.is_symlink() and os.path.samefile(exported, source)
    assert first["created_count"] == 1 and second["unchanged_count"] == 1
    manifest = json.loads((tmp_path / "export" / ".ppr-symlink-export.json").read_text())
    assert manifest["version"] == 4 and manifest["person_id"] == person_id
    assert manifest["managed"][name]["kind"] == "hardlink"


def test_reconciliation_only_removes_managed_links_and_preserves_collisions(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo")
    png_source = tmp_path / "source.png"
    png_source.write_bytes(b"photo")
    unmanaged_source = tmp_path / "unmanaged.jpg"
    unmanaged_source.write_bytes(b"unmanaged")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        current_id, stale_id, collision_id = ("a" * 64, "b" * 64, "c" * 64)
        _photo(catalog, source_id, run_id, current_id, source)
        _photo(catalog, source_id, run_id, stale_id, source)
        _photo(catalog, source_id, run_id, collision_id, png_source)
        person_id = catalog.create_person("chloe")
        for photo_id in (current_id, stale_id, collision_id):
            catalog.record_decision(person_id, photo_id, "accept")
        destination = tmp_path / "export"
        write_person_symlinks(catalog.connection, person_id, destination)
        # Preserve arbitrary entries and introduce both a regular-file and an
        # unmanaged-symlink collision at desired managed names.
        arbitrary = destination / "keep-link"
        os.symlink(source, arbitrary)
        unknown = destination / ("f" * 64 + ".jpg")
        os.symlink(source, unknown)
        current_name = f"chloe_2026-08-26_092328_{current_id}.jpg"
        stale_name = f"chloe_2026-08-26_092328_{stale_id}.jpg"
        collision_name = f"chloe_2026-08-26_092328_{collision_id}.png"
        unmanaged_desired = destination / current_name
        os.unlink(unmanaged_desired)
        os.symlink(unmanaged_source, unmanaged_desired)
        regular_collision = destination / collision_name
        regular_collision.unlink()
        regular_collision.write_bytes(b"keep")
        catalog.record_decision(person_id, stale_id, "reject")
        result = write_person_symlinks(catalog.connection, person_id, destination)

    assert not (destination / stale_name).exists()
    assert arbitrary.is_symlink() and unknown.is_symlink()
    # A name already owned by the manifest is safe to reconcile to the
    # current source; arbitrary names remain untouched above.
    assert unmanaged_desired.resolve() == source.resolve()
    assert regular_collision.read_bytes() == b"keep"
    assert result["removed_count"] == 1
    assert result["conflict_count"] == 1
    assert {item["reason"] for item in result["conflicts"]} == {
        "regular_file_collision",
    }


def test_stale_hardlink_cannot_delete_replaced_user_file(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo bytes")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = "d" * 64
        _photo(catalog, source_id, run_id, photo_id, source)
        person_id = catalog.create_person("chloe")
        catalog.record_decision(person_id, photo_id, "accept")
        destination = tmp_path / "export"
        write_person_hardlinks(catalog.connection, person_id, destination)
        name = f"chloe_2026-08-26_092328_{photo_id}.jpg"
        exported = destination / name
        exported.unlink()
        exported.write_bytes(b"user-owned")
        catalog.record_decision(person_id, photo_id, "reject")
        result = write_person_hardlinks(catalog.connection, person_id, destination)

    assert result["removed_count"] == 0 and result["conflict_count"] == 0
    assert exported.read_bytes() == b"user-owned"


def test_hardlink_export_reports_cross_device_without_copy(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo bytes")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = "f" * 64
        _photo(catalog, source_id, run_id, photo_id, source)
        person_id = catalog.create_person("chloe")
        catalog.record_decision(person_id, photo_id, "accept")

        def fail_link(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        monkeypatch.setattr(os, "link", fail_link)
        result = write_person_hardlinks(catalog.connection, person_id, tmp_path / "export")

    assert result["created_count"] == 0 and result["skipped"][0]["reason"] == "cross_device"
