---
name: photo-person-review
description: Resume and operate this repository's local-first CLI workflow for importing recurring photo batches, learning named people from face references, generating temporary review crops, and recording explicit review results. Use for photo-person-review catalog work or conversational photo identity review in this project.
---

# Photo Person Review

Operate the repository through `uv run ppr`. Read `README.md` before changing the
implementation, then recover live state from the CLI rather than hard-coding
batch, run, face, or assertion IDs.

## Resume safely

1. Run `uv run ppr status`, `uv run ppr batches`, and
   `uv run ppr person list`. The default catalog is intentionally outside the
   repository; pass `--workspace` only when the user names a different catalog.
   If multiple people exist and the conversation does not establish the active
   person, ask which person to review before ranking or generating questions.
2. Check `git status --short` and preserve unrelated changes.
3. Confirm models with `uv run ppr models status` before local analysis.
4. Read [references/operations.md](references/operations.md) for exact command
   shapes when importing, analyzing, ranking, reviewing, or recording evidence.

## Conversational review protocol

- Build derivatives only in a fresh private temporary directory. Never put
  review JPEGs, source photos, databases, embeddings, model output, manifests,
  or credentials in Git.
- Generate a fresh packet for the live catalog and current analysis run. Do not
  reuse packet labels or run-prefixed face IDs from an earlier session; temporary
  files may be gone and later analysis can replace the latest face observations.
- Present one full-size face crop per question. Use contact/face sheets only as
  indexes. Show the annotated context photo separately when the crop is
  ambiguous. Always give the crop's packet label or exact path.
- Translate a named face into identity evidence, not a whole-photo decision:
  create or label that person and assign the face as a positive identity
  assertion. Use an explicit negative assertion only for a deliberate hard
  negative; another person's positive identity is not a blanket negative.
- If identity conflicts are reported, inspect `uv run ppr identity conflicts`
  and resolve them explicitly by retiring the incorrect assertion. Ranking is
  intentionally blocked for a person with unresolved identity ambiguity.
- Record `accept`, `reject`, or `unsure` photo decisions only when the user is
  answering the photo-presence question. When the user provides qualitative
  context, pass their exact wording through `ppr decide --note` so it remains
  append-only photo-level evidence. Do not summarize, classify, or promote the
  note into face or appearance evidence. Never infer authoritative decisions
  from model scores, provider tags, or VLM output.
- If a face is missed, try a lower YuNet `--face-threshold` and regenerate the
  packet. The default favors recall because false detections are cheap to
  reject; detector threshold is part of the analyzer version.
- Keep temporary artifact paths out of SQLite. The growing catalog stores only
  metadata, tags, geometry, numeric features, evidence, and append-only events;
  original photo bytes remain in their source archive.

## Implementation boundaries

- The product remains CLI-only; Codex is the visual/conversational interface.
- Imports are incremental. Re-imports append observations while SHA-256 photo
  records deduplicate content. Persistent identity assertions carry across batches;
  appearance evidence is batch-local.
- OpenRouter VLM support is a future optional evidence backend. Do not upload
  images without explicit remote-upload consent, provider/privacy approval, and
  a cost limit. Remote results are advisory.
- For substantial implementation work, use bounded Luna subagents when
  available. Keep identity interpretation and final database mutations in the
  primary session.
- After code changes, run formatting, lint, type checks, tests, the privacy
  checker, and `git diff --check`. Commit focused checkpoints and push them as
  work progresses when the user has requested continuous pushes.
