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
uv run ppr analyze --batch BATCH_ID --new --face-threshold 0.75
```

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

## Verify implementation changes

```console
uv run ruff format .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv run python scripts/privacy_check.py
git diff --check
```
