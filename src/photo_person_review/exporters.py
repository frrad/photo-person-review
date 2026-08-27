"""Deterministic metadata/tag exports; source photo bytes are never opened."""

from __future__ import annotations

import csv
import errno
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

_MANAGED_LINK_NAME = re.compile(
    r"^[a-z0-9]+(?:_[a-z0-9]+)*_(?:\d{4}-\d{2}-\d{2}_\d{6}|undated)_[0-9a-f]{64}(?:\.[a-z0-9]{1,10})?$"
)
_SAFE_PREFIX_COMPONENT = re.compile(r"[a-z0-9]+")
_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,10}$")
# The ownership manifest has a historical filename shared by both link formats.
_EXPORT_MANIFEST_NAME = ".ppr-symlink-export.json"


def catalog_rows(
    connection: sqlite3.Connection,
    *,
    person_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return one current catalog row per photo with append-only history summarized."""

    selected_id = person_id

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
            """SELECT td.name,ta.value,ta.provenance,ta.confidence,ta.person_id,ta.created_at
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
        if selected_id is not None:
            decision_row = connection.execute(
                """SELECT decision,actor,evidence_json,created_at FROM decisions
                   WHERE person_id=? AND photo_id=? ORDER BY decision_id DESC LIMIT 1""",
                (selected_id, photo_id),
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
                "person_id": selected_id,
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
        "person_id",
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


def current_positive_sources(
    connection: sqlite3.Connection,
    person_id: str,
) -> list[dict[str, Any]]:
    """Return current positive photos and their newest available source paths.

    A photo is current when it has an active positive face reference or its
    latest decision is ``accept``.  A later reject supersedes both forms of
    evidence.  Source observations are append-only, so the newest present or
    replaced observation is selected without opening the source file.
    """

    selected_id = person_id
    rows = connection.execute(
        """WITH latest_reference_events AS (
                   SELECT assertion_id,event,
                          ROW_NUMBER() OVER (
                              PARTITION BY assertion_id ORDER BY event_id DESC
                          ) AS row_number
                   FROM face_identity_assertion_events
               ), active_positive AS (
                   SELECT DISTINCT r.photo_id
                   FROM face_identity_assertions r
                   JOIN latest_reference_events e ON e.assertion_id=r.assertion_id
                       AND e.row_number=1 AND e.event='active'
                   WHERE r.person_id=? AND r.assertion_kind='positive'
               ), latest_decisions AS (
                   SELECT photo_id,decision,
                          ROW_NUMBER() OVER (
                              PARTITION BY photo_id ORDER BY decision_id DESC
                          ) AS row_number
                   FROM decisions
                   WHERE person_id=?
               ), selected AS (
                   SELECT photo_id FROM active_positive
                   UNION
                   SELECT photo_id FROM latest_decisions
                   WHERE row_number=1 AND decision='accept'
               )
               SELECT p.photo_id,
                      t.label AS person_label,
                      p.capture_time,
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
               JOIN people t ON t.person_id=?
               WHERE ld.decision IS NULL OR ld.decision <> 'reject'
               ORDER BY p.photo_id""",
        (selected_id, selected_id, selected_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _managed_link_name(name: str) -> bool:
    """Whether *name* matches the current managed-link filename format."""

    return _MANAGED_LINK_NAME.fullmatch(name) is not None


def _safe_person_prefix(label: object, person_id: str, override: object = None) -> str:
    """Return a filesystem-safe, human-readable person or override prefix."""

    values = (override, person_id, "photo") if override is not None else (label, person_id, "photo")
    for value in values:
        components = _SAFE_PREFIX_COMPONENT.findall(str(value or "").lower())
        if components:
            return "_".join(components)
    return "photo"


def _capture_stamp(value: object) -> str:
    """Format a catalog capture time, using a stable marker when unusable."""

    if value is None:
        return "undated"
    raw = str(value).strip()
    if not raw:
        return "undated"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "undated"
    return parsed.strftime("%Y-%m-%d_%H%M%S")


def _safe_extension(source_path: Path | None) -> str:
    """Return a conservative, stable extension for an exported link name."""

    if source_path is None:
        return ""
    suffix = source_path.suffix.lower()
    return suffix if _SAFE_EXTENSION.fullmatch(suffix) else ""


def _read_export_manifest(
    path: Path, person_id: str
) -> tuple[set[str], dict[str, dict[str, Any]], str | None, str | None, bool]:
    """Read a prior manifest, returning names, ownership, prefix, and status."""

    if not os.path.lexists(path):
        return set(), {}, None, None, True
    if path.is_symlink() or not path.is_file():
        return set(), {}, None, "manifest path is not a regular file", False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest must be an object")
        if payload.get("version") != 4:
            raise ValueError("unsupported manifest version")
        if payload.get("person_id") != person_id:
            raise ValueError("manifest person does not match requested person")
        names = payload.get("managed_names")
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("managed_names must be a list of strings")
        if any(not _managed_link_name(name) for name in names):
            raise ValueError("managed_names contains an invalid link name")
        managed: dict[str, dict[str, Any]] = {}
        payload_managed = payload.get("managed", {})
        if not isinstance(payload_managed, dict):
            raise ValueError("managed must be an object")
        for name, record in payload_managed.items():
            if not isinstance(name, str) or name not in names or not isinstance(record, dict):
                raise ValueError("managed contains an invalid record")
            kind = record.get("kind")
            if kind not in ("symlink", "hardlink"):
                raise ValueError("managed record has an invalid kind")
            identity = record.get("source_identity")
            if (
                not isinstance(identity, dict)
                or not isinstance(identity.get("dev"), int)
                or not isinstance(identity.get("ino"), int)
            ):
                raise ValueError("managed record has an invalid source identity")
            source_path = record.get("source_path")
            if not isinstance(source_path, str):
                raise ValueError("managed record has an invalid source path")
            managed[name] = {
                "kind": kind,
                "source_path": source_path,
                "source_identity": {"dev": identity["dev"], "ino": identity["ino"]},
            }
        prefix = payload.get("filename_prefix")
        if prefix is not None and not isinstance(prefix, str):
            raise ValueError("filename_prefix must be a string")
        return set(names), managed, prefix, None, True
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return set(), {}, None, f"invalid manifest: {exc}", False


def _identity(path: Path) -> dict[str, int]:
    stat = os.stat(path)
    return {"dev": int(stat.st_dev), "ino": int(stat.st_ino)}


def _identity_matches(path: Path, identity: object) -> bool:
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("dev"), int)
        or not isinstance(identity.get("ino"), int)
    ):
        return False
    try:
        current = os.stat(path)
    except OSError:
        return False
    return bool(current.st_dev == identity["dev"] and current.st_ino == identity["ino"])


def _record(kind: str, source_path: Path, identity: dict[str, int]) -> dict[str, Any]:
    return {"kind": kind, "source_path": str(source_path), "source_identity": identity}


def _replace_with_hardlink(source_path: Path, destination_path: Path, destination: Path) -> None:
    """Atomically replace an already-owned destination with a hard link."""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination_path.name}.ppr-link-", dir=destination)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        os.link(source_path, temporary)
        os.replace(temporary, destination_path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_with_symlink(source_path: Path, destination_path: Path, destination: Path) -> None:
    """Atomically replace an already-owned destination with a symlink."""

    temporary = destination / f".{destination_path.name}.ppr-link"
    temporary.unlink(missing_ok=True)
    try:
        os.symlink(source_path, temporary)
        os.replace(temporary, destination_path)
    finally:
        temporary.unlink(missing_ok=True)


def write_links(
    connection: sqlite3.Connection,
    person_id: str,
    output: str | Path,
    format: str = "hardlinks",
    filename_prefix: str | None = None,
) -> dict[str, Any]:
    """Reconcile a durable link export for a selected person.

    Hard links never copy bytes and require the source and destination to share
    a filesystem. Existing directories, regular files, and symlinks not listed
    in the matching manifest are left untouched. The manifest records source
    inode identity so stale hard links can only be removed when ownership is
    still provable.
    """

    if format not in ("symlinks", "hardlinks"):
        raise ValueError("format must be symlinks or hardlinks")
    kind = "symlink" if format == "symlinks" else "hardlink"

    destination = Path(output).expanduser()
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError(f"link export output must be a real directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    rows = current_positive_sources(connection, person_id)
    person_label = rows[0].get("person_label") if rows else None
    if person_label is None:
        person_row = connection.execute("SELECT label FROM people WHERE person_id=?", (person_id,)).fetchone()
        person_label = person_row["label"] if person_row is not None else None
    manifest_path = destination / _EXPORT_MANIFEST_NAME
    previous_names, previous_records, stored_prefix, manifest_error, manifest_writable = _read_export_manifest(
        manifest_path, person_id
    )
    # A prefix is part of the export's identity.  Preserve it when a caller
    # omits --filename-prefix, which makes a format migration truly in-place.
    prefix_override = filename_prefix if filename_prefix is not None else stored_prefix
    effective_prefix = _safe_person_prefix(person_label, person_id, prefix_override)
    desired: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_value = row.get("source_path")
        source_path = Path(str(source_value)).expanduser() if source_value else None
        suffix = _safe_extension(source_path)
        stamp = _capture_stamp(row.get("capture_time"))
        name = f"{effective_prefix}_{stamp}_{str(row['photo_id']).lower()}{suffix}"
        # A photo should have one newest source row, but keep reconciliation
        # deterministic if a malformed catalog yields duplicate names.
        desired.setdefault(name, {**row, "destination_name": name})

    created = 0
    updated = 0
    unchanged = 0
    managed_names: set[str] = set()
    managed_records: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for name, row in desired.items():
        destination_path = destination / name
        prior_record = previous_records.get(name)
        source_value = row.get("source_path")
        if not source_value:
            if name in previous_names and (
                destination_path.is_symlink()
                or (
                    prior_record is not None
                    and prior_record.get("kind") == "hardlink"
                    and destination_path.is_file()
                    and _identity_matches(destination_path, prior_record.get("source_identity"))
                )
            ):
                managed_names.add(name)
                if prior_record is not None:
                    managed_records[name] = prior_record
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
            if name in previous_names and (
                destination_path.is_symlink()
                or (
                    prior_record is not None
                    and prior_record.get("kind") == "hardlink"
                    and destination_path.is_file()
                    and _identity_matches(destination_path, prior_record.get("source_identity"))
                )
            ):
                managed_names.add(name)
                if prior_record is not None:
                    managed_records[name] = prior_record
            skipped.append(
                {
                    "photo_id": str(row["photo_id"]),
                    "destination": str(destination_path),
                    "source_path": str(source_path),
                    "reason": "source_missing",
                }
            )
            continue

        try:
            source_identity = _identity(source_path)
        except OSError:
            skipped.append(
                {
                    "photo_id": str(row["photo_id"]),
                    "destination": str(destination_path),
                    "source_path": str(source_path),
                    "reason": "source_missing",
                }
            )
            continue

        destination_exists = os.path.lexists(destination_path)
        destination_is_symlink = destination_path.is_symlink()
        destination_is_regular = destination_exists and not destination_is_symlink and destination_path.is_file()
        destination_is_directory = destination_exists and not destination_is_symlink and destination_path.is_dir()

        def preserve_existing_on_failure() -> None:
            """Keep an owned entry when a replacement cannot be made."""

            if name not in previous_names:
                return
            if prior_record is not None:
                managed_names.add(name)
                managed_records[name] = prior_record
            elif destination_is_symlink:
                # A surviving symlink can be re-recorded using its current source.
                existing_source = destination_path.resolve(strict=False)
                if existing_source.is_file():
                    try:
                        managed_names.add(name)
                        managed_records[name] = _record("symlink", existing_source, _identity(existing_source))
                    except OSError:
                        pass

        # A manifest hardlink record proves that a regular destination is ours.
        # It is the only case in which a regular file may be replaced or removed.
        owned_regular = (
            destination_is_regular
            and prior_record is not None
            and prior_record.get("kind") == "hardlink"
            and _identity_matches(destination_path, prior_record.get("source_identity"))
        )
        if destination_is_directory or (
            destination_exists and not destination_is_symlink and not destination_is_regular
        ):
            conflict = {
                "photo_id": str(row["photo_id"]),
                "destination": str(destination_path),
                "source_path": str(source_path),
                "reason": "directory_collision",
            }
            conflicts.append(conflict)
            skipped.append(conflict)
            continue
        if destination_is_regular and not owned_regular:
            conflict = {
                "photo_id": str(row["photo_id"]),
                "destination": str(destination_path),
                "source_path": str(source_path),
                "reason": "regular_file_collision",
            }
            conflicts.append(conflict)
            skipped.append(conflict)
            continue

        if destination_is_symlink:
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
            if kind == "symlink" and destination_path.resolve(strict=False) == source_path:
                unchanged += 1
            else:
                try:
                    if kind == "hardlink":
                        _replace_with_hardlink(source_path, destination_path, destination)
                    else:
                        _replace_with_symlink(source_path, destination_path, destination)
                except OSError as exc:
                    reason = "cross_device" if format == "hardlinks" and exc.errno == errno.EXDEV else "link_error"
                    failure = {
                        "photo_id": str(row["photo_id"]),
                        "destination": str(destination_path),
                        "source_path": str(source_path),
                        "reason": reason,
                    }
                    skipped.append(failure)
                    if reason == "cross_device":
                        conflicts.append(failure)
                    preserve_existing_on_failure()
                    continue
                updated += 1
            managed_names.add(name)
            managed_records[name] = _record(kind, source_path, source_identity)
            continue

        if destination_is_regular:
            # This is an existing managed hardlink.  samefile is the robust
            # idempotency check; the manifest identity check above prevents
            # treating an arbitrary regular collision as ours.
            if kind == "hardlink" and os.path.samefile(destination_path, source_path):
                unchanged += 1
                managed_names.add(name)
                managed_records[name] = _record(kind, source_path, source_identity)
                continue
            try:
                if kind == "hardlink":
                    _replace_with_hardlink(source_path, destination_path, destination)
                else:
                    _replace_with_symlink(source_path, destination_path, destination)
            except OSError as exc:
                reason = "cross_device" if format == "hardlinks" and exc.errno == errno.EXDEV else "link_error"
                failure = {
                    "photo_id": str(row["photo_id"]),
                    "destination": str(destination_path),
                    "source_path": str(source_path),
                    "reason": reason,
                }
                skipped.append(failure)
                if reason == "cross_device":
                    conflicts.append(failure)
                preserve_existing_on_failure()
                continue
            updated += 1
            managed_names.add(name)
            managed_records[name] = _record(kind, source_path, source_identity)
            continue

        try:
            if kind == "hardlink":
                # os.link is deliberately used instead of copyfile/copy2.
                os.link(source_path, destination_path)
            else:
                os.symlink(source_path, destination_path)
        except OSError as exc:
            reason = "cross_device" if format == "hardlinks" and exc.errno == errno.EXDEV else "link_error"
            failure = {
                "photo_id": str(row["photo_id"]),
                "destination": str(destination_path),
                "source_path": str(source_path),
                "reason": reason,
            }
            skipped.append(failure)
            if reason == "cross_device":
                conflicts.append(failure)
            preserve_existing_on_failure()
            continue
        created += 1
        managed_names.add(name)
        managed_records[name] = _record(kind, source_path, source_identity)

    removed = 0
    for candidate in destination.iterdir():
        if candidate.name not in previous_names:
            continue
        if candidate.name in desired:
            continue
        prior_record = previous_records.get(candidate.name)
        if candidate.is_symlink():
            # A hardlink record does not authorize deleting a symlink that
            # replaced that hardlink.
            if prior_record is not None and prior_record.get("kind") == "hardlink":
                continue
            candidate.unlink()
            removed += 1
        elif (
            prior_record is not None
            and prior_record.get("kind") == "hardlink"
            and _identity_matches(candidate, prior_record.get("source_identity"))
        ):
            candidate.unlink()
            removed += 1

    if manifest_writable:
        # A source that disappeared cannot be upgraded with a trustworthy inode
        # record. Leave it in place but do not claim ownership in the manifest.
        managed_names.intersection_update(managed_records)
        manifest_payload = {
            "version": 4,
            "person_id": person_id,
            "filename_prefix": effective_prefix,
            "managed_names": sorted(managed_names),
        }
        manifest_payload["managed"] = {name: managed_records[name] for name in sorted(managed_records)}

        def write_manifest(stream: Any) -> None:
            json.dump(manifest_payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")

        _atomic_text(manifest_path, write_manifest)

    return {
        "path": str(destination.resolve()),
        "format": format,
        "person_id": person_id,
        "filename_prefix": effective_prefix,
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


def write_person_symlinks(
    connection: sqlite3.Connection,
    person_id: str,
    output: str | Path,
    filename_prefix: str | None = None,
) -> dict[str, Any]:
    """Reconcile a person-centric symlink export."""
    return write_links(
        connection,
        person_id,
        output,
        format="symlinks",
        filename_prefix=filename_prefix,
    )


def write_person_hardlinks(
    connection: sqlite3.Connection,
    person_id: str,
    output: str | Path,
    filename_prefix: str | None = None,
) -> dict[str, Any]:
    """Reconcile an ordinary-file person export made from hard links."""

    return write_links(
        connection,
        person_id,
        output,
        format="hardlinks",
        filename_prefix=filename_prefix,
    )
