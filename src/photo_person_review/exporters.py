"""Deterministic metadata/tag exports; source photo bytes are never opened."""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

_MANAGED_SYMLINK_NAME = re.compile(r"^[0-9a-f]{64}(?:\.[^/]+)?$")
_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,10}$")
_SYMLINK_MANIFEST_NAME = ".ppr-symlink-export.json"


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


def current_positive_sources(connection: sqlite3.Connection, target_id: str) -> list[dict[str, Any]]:
    """Return current positive photos and their newest available source paths.

    A photo is current when it has an active positive face reference or its
    latest decision is ``accept``.  A later reject supersedes both forms of
    evidence.  Source observations are append-only, so the newest present or
    replaced observation is selected without opening the source file.
    """

    rows = connection.execute(
        """WITH latest_reference_events AS (
                   SELECT reference_id,event,
                          ROW_NUMBER() OVER (
                              PARTITION BY reference_id ORDER BY event_id DESC
                          ) AS row_number
                   FROM target_reference_events
               ), active_positive AS (
                   SELECT DISTINCT r.photo_id
                   FROM target_references r
                   JOIN latest_reference_events e ON e.reference_id=r.reference_id
                       AND e.row_number=1 AND e.event='active'
                   WHERE r.target_id=? AND r.kind='positive'
               ), latest_decisions AS (
                   SELECT photo_id,decision,
                          ROW_NUMBER() OVER (
                              PARTITION BY photo_id ORDER BY decision_id DESC
                          ) AS row_number
                   FROM decisions
                   WHERE target_id=?
               ), selected AS (
                   SELECT photo_id FROM active_positive
                   UNION
                   SELECT photo_id FROM latest_decisions
                   WHERE row_number=1 AND decision='accept'
               )
               SELECT p.photo_id,
                      sf.path AS source_path,
                      sf.relative_path,
                      sf.source_id,
                      sf.observation_state,
                      ld.decision AS latest_decision
               FROM selected s
               JOIN photos p ON p.photo_id=s.photo_id
               LEFT JOIN latest_decisions ld ON ld.photo_id=p.photo_id AND ld.row_number=1
               LEFT JOIN source_files sf ON sf.source_file_id=(
                   SELECT MAX(sf2.source_file_id)
                   FROM source_files sf2
                   WHERE sf2.photo_id=p.photo_id
                     AND sf2.observation_state IN ('present','replaced')
               )
               WHERE ld.decision IS NULL OR ld.decision <> 'reject'
               ORDER BY p.photo_id""",
        (target_id, target_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _managed_symlink_name(name: str) -> bool:
    """Whether *name* is one of the hash-plus-extension names we manage."""

    return _MANAGED_SYMLINK_NAME.fullmatch(name) is not None


def _safe_extension(source_path: Path | None) -> str:
    """Return a conservative, stable extension for an exported link name."""

    if source_path is None:
        return ""
    suffix = source_path.suffix.lower()
    return suffix if _SAFE_EXTENSION.fullmatch(suffix) else ""


def _read_symlink_manifest(path: Path, target_id: str) -> tuple[set[str], str | None, bool]:
    """Read a prior manifest, returning names, an error, and write permission."""

    if not os.path.lexists(path):
        return set(), None, True
    if path.is_symlink() or not path.is_file():
        return set(), "manifest path is not a regular file", False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("unsupported manifest version")
        if payload.get("target_id") != target_id:
            raise ValueError("manifest target does not match requested target")
        names = payload.get("managed_names")
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("managed_names must be a list of strings")
        if any(not _managed_symlink_name(name) for name in names):
            raise ValueError("managed_names contains an invalid link name")
        return set(names), None, True
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return set(), f"invalid manifest: {exc}", False


def write_symlinks(
    connection: sqlite3.Connection,
    target_id: str,
    output: str | Path,
) -> dict[str, Any]:
    """Reconcile a durable symlink export for *target_id*.

    The export contains symlinks named ``<full-photo-id><extension>`` plus a
    small hidden ownership manifest. Existing directories, regular files, and
    symlinks not listed in that manifest are left untouched. Managed links can
    be updated atomically, while stale managed links are removed. Neither
    source metadata nor photo bytes are read by this operation.
    """

    destination = Path(output).expanduser()
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError(f"symlink export output must be a real directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    rows = current_positive_sources(connection, target_id)
    manifest_path = destination / _SYMLINK_MANIFEST_NAME
    previous_names, manifest_error, manifest_writable = _read_symlink_manifest(manifest_path, target_id)
    desired: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_value = row.get("source_path")
        source_path = Path(str(source_value)).expanduser() if source_value else None
        suffix = _safe_extension(source_path)
        name = f"{row['photo_id']}{suffix}"
        # A photo should have one newest source row, but keep reconciliation
        # deterministic if a malformed catalog yields duplicate names.
        desired.setdefault(name, {**row, "destination_name": name})

    created = 0
    updated = 0
    unchanged = 0
    managed_names: set[str] = set()
    skipped: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for name, row in desired.items():
        destination_path = destination / name
        source_value = row.get("source_path")
        if not source_value:
            if name in previous_names and destination_path.is_symlink():
                managed_names.add(name)
            skipped.append(
                {
                    "photo_id": str(row["photo_id"]),
                    "destination": str(destination_path),
                    "reason": "source_missing",
                }
            )
            continue
        source_path = Path(str(source_value)).expanduser().resolve(strict=False)
        if not source_path.is_file():
            if name in previous_names and destination_path.is_symlink():
                managed_names.add(name)
            skipped.append(
                {
                    "photo_id": str(row["photo_id"]),
                    "destination": str(destination_path),
                    "source_path": str(source_path),
                    "reason": "source_missing",
                }
            )
            continue

        if os.path.lexists(destination_path) and not destination_path.is_symlink():
            reason = "directory_collision" if destination_path.is_dir() else "regular_file_collision"
            conflict = {
                "photo_id": str(row["photo_id"]),
                "destination": str(destination_path),
                "source_path": str(source_path),
                "reason": reason,
            }
            conflicts.append(conflict)
            skipped.append(conflict)
            continue

        if destination_path.is_symlink():
            if name not in previous_names:
                conflict = {
                    "photo_id": str(row["photo_id"]),
                    "destination": str(destination_path),
                    "source_path": str(source_path),
                    "reason": "unmanaged_symlink_collision",
                }
                conflicts.append(conflict)
                skipped.append(conflict)
                continue
            existing_target = destination_path.resolve(strict=False)
            if existing_target == source_path:
                unchanged += 1
                managed_names.add(name)
                continue
            temporary = destination / f".{name}.ppr-link"
            temporary.unlink(missing_ok=True)
            os.symlink(source_path, temporary)
            os.replace(temporary, destination_path)
            updated += 1
            managed_names.add(name)
            continue

        os.symlink(source_path, destination_path)
        created += 1
        managed_names.add(name)

    removed = 0
    for candidate in destination.iterdir():
        if not candidate.is_symlink() or candidate.name not in previous_names:
            continue
        if candidate.name in desired:
            continue
        candidate.unlink()
        removed += 1

    if manifest_writable:
        manifest_payload = {
            "version": 1,
            "target_id": target_id,
            "managed_names": sorted(managed_names),
        }

        def write_manifest(stream: Any) -> None:
            json.dump(manifest_payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")

        _atomic_text(manifest_path, write_manifest)

    return {
        "path": str(destination.resolve()),
        "format": "symlinks",
        "target_id": target_id,
        "row_count": len(rows),
        "desired_count": len(desired),
        "managed_count": len(managed_names),
        "created_count": created,
        "updated_count": updated,
        "unchanged_count": unchanged,
        "removed_count": removed,
        "skipped_count": len(skipped),
        "conflict_count": len(conflicts),
        "manifest": {
            "path": str(manifest_path),
            "updated": manifest_writable,
            "error": manifest_error,
        },
        "skipped": skipped,
        "conflicts": conflicts,
    }
