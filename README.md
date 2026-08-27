# Photo Person Review

Photo Person Review is a local-first, CLI-only catalog and review tool for
finding photos that contain a configured person. It is intended for recurring
daily or event batches: approved face references carry forward, while outfit,
burst, and context evidence remains scoped to the batch where it was observed.

The reviewer interface can be an agent such as Codex. The CLI produces
machine-readable review packets plus temporary annotated contact sheets and
crops; the agent shows those artifacts to a person and records their explicit
accept, reject, or unsure decisions.

This project is source-agnostic and is not affiliated with a photo provider,
downloader, school, or cloud vision service.

## Data model

The SQLite catalog is designed to grow over time. It records stable content
hashes, source-path observations, batches, normalized metadata, provider hints,
detected regions, numeric features, evidence, tags, and append-only decisions.
Re-importing a batch adds an observation without duplicating an existing photo.

The catalog never stores encoded photos, thumbnails, or crop bytes. Source
photos remain in their original archive and are opened read-only. Annotated
images, contact sheets, and crops are regenerated into caller-selected temporary
directories when review is needed.

For conversational review, present one full-size face crop per question. Use
the contact and face sheets only as packet indexes, and show the annotated
context photo separately when the crop alone is ambiguous.

## Intended workflow

```console
ppr init --workspace /private/path/to/catalog
ppr import /private/path/to/archive --manifest /private/path/to/media.json
ppr analyze --batch BATCH_ID --new
# Lower the default 0.80 threshold if a review packet misses faces.
ppr analyze --batch BATCH_ID --new --face-threshold 0.75
ppr target create target-1
ppr rank --target target-1 --batch BATCH_ID
# Later audit tiny/distant detections that the primary queue defers.
ppr rank --target target-1 --batch BATCH_ID --min-face-area-ratio 0
ppr review packet --target target-1 --strategy reference-seeding --output "$TMPDIR"
ppr decide --target target-1 --accept PHOTO_ID --actor user
ppr export --target target-1 --format json
```

The exact command surface is under active implementation. Commands intended for
agent use emit stable JSON on standard output and diagnostics on standard error.

## Evidence and decisions

- User-approved face references and hard negatives persist across batches.
- Outfit and person-crop evidence is batch-local by default.
- Local face, appearance, time, burst, and context signals rank candidates.
- Imported provider tags and future VLM results are non-authoritative evidence.
- Suggestions never silently become user tags.
- Decisions retain their actor, evidence, timestamp, and supersession history.

## Optional remote vision

The first milestone is fully local. A backend-neutral vision-evidence contract
allows an explicitly enabled OpenRouter VLM integration later. Remote analysis
must require affirmative upload consent, strip image metadata, send the minimum
necessary crops, enforce configured provider privacy constraints, and remain
advisory. API keys and photo bytes are never stored in the catalog.

## Generic and Vidigami inputs

Ordinary folders are the primary input. Optional generic manifests may attach
opaque external IDs and tags. The Vidigami adapter reads its JSON report and
matches `SHA-256(media_id)` to the downloader's hash-named archive files. It
does not import the downloader package, read its credentials, contact its API,
or modify its state.

## Privacy

Never commit personal photos, generated review images, embeddings, databases,
credentials, local manifests, or model outputs. Before committing, run:

```console
python scripts/privacy_check.py
```

For additional protection, create an ignored `.privacy-patterns` file with one
regular expression per private value or pattern to reject.

## Status

Initial implementation.

## License

MIT. See [LICENSE](LICENSE).
