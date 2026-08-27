import csv
import json
from pathlib import Path

from photo_person_review.db import Catalog
from photo_person_review.exporters import catalog_rows, write_csv, write_json


def test_export_contains_current_metadata_tags_and_decision_without_photo_bytes(
    tmp_path: Path,
) -> None:
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source = catalog.create_source("folder")
        run = catalog.create_import_run(source)
        photo_id = catalog.upsert_photo("a" * 64, width=20, height=10)
        catalog.observe_source_file(source, photo_id, "/source/a.jpg", import_run_id=run)
        catalog.add_metadata(photo_id, "camera", "old", "test", run)
        catalog.add_metadata(photo_id, "camera", "new", "test", run)
        target_id = catalog.create_target("person")
        tag_id = catalog.create_tag("contains-person")
        catalog.assign_tag(photo_id, tag_id, provenance="user", target_id=target_id)
        catalog.record_decision(target_id, photo_id, "accept")
        rows = catalog_rows(catalog.connection, target_id=target_id)

    assert rows[0]["metadata"]["camera"]["value"] == "new"
    assert rows[0]["tags"][0]["name"] == "contains-person"
    assert rows[0]["decision"]["decision"] == "accept"
    assert "bytes" not in json.dumps(rows[0]).lower()

    json_path = write_json(tmp_path / "out.json", rows)
    csv_path = write_csv(tmp_path / "out.csv", rows)
    assert json.loads(json_path.read_text(encoding="utf-8"))[0]["photo_id"] == photo_id
    with csv_path.open(encoding="utf-8", newline="") as stream:
        assert next(csv.DictReader(stream))["photo_id"] == photo_id
