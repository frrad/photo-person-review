"""Deterministic metadata/tag exports; source photo bytes are never opened."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any


def catalog_rows(
    connection: sqlite3.Connection,
    *,
    target_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return one current catalog row per photo with append-only history summarized."""

    photos = connection.execute("SELECT * FROM photos ORDER BY photo_id").fetchall()
    result: list[dict[str, Any]] = []
    for photo in photos:
        photo_id = str(photo["photo_id"])
        metadata_rows = connection.execute(
            """SELECT pm.key,pm.value_json,pm.provenance,pm.observed_at
               FROM photo_metadata pm
               WHERE pm.photo_id=? AND pm.metadata_id=(
                   SELECT MAX(pm2.metadata_id) FROM photo_metadata pm2
                   WHERE pm2.photo_id=pm.photo_id AND pm2.key=pm.key
               ) ORDER BY pm.key""",
            (photo_id,),
        ).fetchall()
        tag_rows = connection.execute(
            """SELECT td.name,ta.value,ta.provenance,ta.confidence,ta.target_id,ta.created_at
               FROM tag_assignments ta JOIN tag_definitions td ON td.tag_id=ta.tag_id
               WHERE ta.photo_id=? ORDER BY ta.assignment_id""",
            (photo_id,),
        ).fetchall()
        source_rows = connection.execute(
            """SELECT sf.source_id,sf.path,sf.relative_path,sf.observation_state,sf.observed_at
               FROM source_files sf WHERE sf.photo_id=? AND sf.source_file_id=(
                   SELECT MAX(sf2.source_file_id) FROM source_files sf2
                   WHERE sf2.source_id=sf.source_id AND sf2.relative_path=sf.relative_path
               ) ORDER BY sf.source_id,sf.relative_path""",
            (photo_id,),
        ).fetchall()
        decision = None
        if target_id is not None:
            decision_row = connection.execute(
                """SELECT decision,actor,evidence_json,created_at FROM decisions
                   WHERE target_id=? AND photo_id=? ORDER BY decision_id DESC LIMIT 1""",
                (target_id, photo_id),
            ).fetchone()
            if decision_row is not None:
                decision = {
                    "decision": decision_row["decision"],
                    "actor": decision_row["actor"],
                    "evidence": json.loads(decision_row["evidence_json"]),
                    "created_at": decision_row["created_at"],
                }
        result.append(
            {
                "photo_id": photo_id,
                "sha256": photo["sha256"],
                "width": photo["width"],
                "height": photo["height"],
                "mime_type": photo["mime_type"],
                "capture_time": photo["capture_time"],
                "metadata": {
                    row["key"]: {
                        "value": json.loads(row["value_json"]),
                        "provenance": row["provenance"],
                        "observed_at": row["observed_at"],
                    }
                    for row in metadata_rows
                },
                "tags": [dict(row) for row in tag_rows],
                "sources": [dict(row) for row in source_rows],
                "target_id": target_id,
                "decision": decision,
            }
        )
    return result


def _atomic_text(path: Path, writer: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def write_json(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    destination = Path(path)

    def writer(stream: Any) -> None:
        json.dump(rows, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")

    return _atomic_text(destination, writer)


def write_csv(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    destination = Path(path)
    columns = (
        "photo_id",
        "sha256",
        "width",
        "height",
        "mime_type",
        "capture_time",
        "metadata",
        "tags",
        "sources",
        "target_id",
        "decision",
    )

    def writer(stream: Any) -> None:
        output = csv.DictWriter(stream, fieldnames=columns)
        output.writeheader()
        for row in rows:
            output.writerow(
                {
                    key: json.dumps(row[key], sort_keys=True, separators=(",", ":"))
                    if isinstance(row[key], (dict, list))
                    else row[key]
                    for key in columns
                }
            )

    return _atomic_text(destination, writer)
