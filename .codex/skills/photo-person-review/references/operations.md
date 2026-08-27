# Operations

Run commands from the repository root. CLI output is JSON and is suitable for
extracting IDs with `jq`.

## Discover the live catalog

```console
uv run ppr status
uv run ppr batches
uv run ppr target list
uv run ppr target references TARGET_ID
uv run ppr models status
```

Use the newest relevant batch returned by `batches`; do not assume that the
last batch from a previous conversation is still current.

## Import and analyze an incremental batch

Generic folder:

```console
uv run ppr import SOURCE_DIRECTORY --source-id SOURCE_ID
```

Vidigami report plus hash-named archive:

```console
uv run ppr import ARCHIVE_DIRECTORY --source-type vidigami \
  --manifest REPORT_JSON --source-id SOURCE_ID
```

Analyze only photos not yet processed by the current model/threshold version:

```console
uv run ppr analyze --batch BATCH_ID --new
```

For missed faces, lower the confidence threshold. This creates a new append-only
analysis version and may analyze the whole batch because the CLI currently has
no single-photo filter:

```console
uv run ppr analyze --batch BATCH_ID --new --face-threshold 0.35
```

Large source photos are resized to a 2000-pixel maximum side before local face
analysis by default. The persisted face boxes and landmarks remain in original
photo coordinates, and no resized bytes are stored. For an exhaustive recall
pass, disable this optimization explicitly:

```console
uv run ppr analyze --batch BATCH_ID --new --face-threshold 0.35 --face-max-side 0
```

Threshold and max-side settings are part of the analyzer version. Changing
either setting therefore makes `--new` select the batch again while preserving
earlier append-only observations.

## Rank and ask questions

Select the active target from the conversation. If it is not established and
`target list` returns more than one person, ask the user which person to review.
Refresh ranking immediately before every `likely` packet so selection never
depends on absent or stale candidate scores:

```console
uv run ppr rank --target TARGET_ID --batch BATCH_ID
review_dir=$(mktemp -d /private/tmp/ppr-review.XXXXXX)
uv run ppr review packet --target TARGET_ID --batch BATCH_ID \
  --strategy likely --limit 8 --output "$review_dir"
```

The primary rank queue defaults to `--min-face-area-ratio 0.0005`, which
defers tiny embedded portraits such as printed cubby labels. This never deletes
detections. After the primary review, run an exhaustive low-resolution audit
with `--min-face-area-ratio 0`; expect more printed faces and false positives.

Read `packet.json`. Each `visible[].faces[]` entry supplies the exact persisted
`face_id`, bounding box, and individual crop path. Display an individual path
such as `faces/01-face-01.jpg`, not the dense face sheet, as the primary prompt.
Always generate this packet in the current session after the latest analysis;
do not carry labels or face IDs forward from an older temporary packet.

## Record identity evidence

Make target creation repeat-safe and keep the human-readable name as the label:

```console
uv run ppr target create TARGET_ID --label PERSON_NAME
uv run ppr target reference-add TARGET_ID PHOTO_ID --face FACE_ID \
  --kind positive --batch BATCH_ID
```

For an explicitly identified different person relative to the active target:

```console
uv run ppr target reference-add ACTIVE_TARGET PHOTO_ID --face FACE_ID \
  --kind negative --batch BATCH_ID
```

Use the full `face_id` from the packet generated against the live catalog. Face
IDs contain an analysis-run prefix; stale packet IDs may not exist in another
catalog or after switching workspaces.

## Record whole-photo decisions

Only after the user answers whether the target is present in the photo:

```console
uv run ppr decide TARGET_ID accept PHOTO_ID [PHOTO_ID ...] --actor user
uv run ppr decide TARGET_ID reject PHOTO_ID [PHOTO_ID ...] --actor user
uv run ppr decide TARGET_ID unsure PHOTO_ID [PHOTO_ID ...] --actor user
```

## Reconcile the durable Chloe export

Review packets and their annotated media are ephemeral batch-review artifacts.
The going-forward handoff to a local photo workflow is a durable hard-link
directory, reconciled after imports and the latest decisions:

```console
uv run ppr export --target TARGET_ID \
  --output "$HOME/Pictures/chloevidigami" \
  --filename-prefix ppr_chloevidigami
```

The destination is created if absent. Managed links are named with the
sanitized filename prefix, capture timestamp, full stable `photo_id`, and
current source extension
(`ppr_chloevidigami_2026-08-26_092328_<photo_id>.jpg`). The current set is the union of active positive face-reference photos and latest `accept`
decisions, except that a latest `reject` excludes the photo even when it has
older acceptance evidence. Re-running follows the newest present/replaced
source observation, adds new links, updates changed links, and removes stale
managed links.

`--filename-prefix` is optional. Its value is lowercased and reduced to
`[a-z0-9]+` components joined by underscores. Without it, the target label is
used, falling back to the target ID or `photo`. Missing or invalid
`capture_time` values use `undated`. The manifest stores the effective prefix;
changing it safely migrates prior managed names on the next sync while
preserving unknown links.

The default command creates hard links plus a small hidden ownership manifest;
it never copies or opens photo bytes. Hard links require the source and output
directory to be on the same filesystem. Use `--format symlinks` only for a
workflow that needs symlinks. Missing source paths, cross-device hard-link
attempts, and regular-file/directory collisions are skipped and reported in
JSON. Arbitrary files, directories, and symlinks not listed in the prior
matching manifest are preserved. Hard-link ownership records include source
device/inode identity; stale hard links are removed only when that identity
still matches. Inspect
`created_count`, `updated_count`, `unchanged_count`, `removed_count`,
`skipped_count`, `conflict_count`, and the `skipped`/`conflicts` detail arrays
before handing the directory to another workflow. If the manifest is absent or
invalid, stale links are not removed and the manifest issue is reported.

## Verify implementation changes

```console
uv run ruff format .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts/privacy_check.py
git diff --check
```
