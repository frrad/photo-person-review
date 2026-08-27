# Photo Person Review

Photo Person Review is a local-first tool for semi-automatically finding photos that contain one configured person. It is designed for recurring large daily or batch downloads where manually reviewing and tagging every image is impractical: a reviewer confirms a few clear examples, and the tool ranks the remaining images using local face evidence plus short-lived same-day appearance signals.

This repository is intentionally generic and currently at the planning stage. It is not affiliated with any photo provider, downloader, school, or cloud photo service.

## Goals

- Reduce a 100-photo day to a small, fast review queue instead of requiring every image to be hand-tagged.
- Keep photos, face crops, embeddings, annotations, and model outputs local by default.
- Preserve human approval and the evidence behind each suggestion.
- Accept ordinary folders and optional generic media manifests.
- Make cloud vision an explicit, optional fallback rather than a requirement.

## Planned workflow

1. Import a folder or manifest and group media by capture date.
2. Confirm a few obvious reference images for the configured person.
3. Rank candidates using local face matching and short-lived same-day signals: outfit, person-crop and context similarity, capture time, and burst/near-duplicate relationships.
4. Human-review the ranked queue with accept, reject, and unsure controls; suggestions never silently become tags.
5. Store the evidence, reference IDs, confidence, and model versions behind each decision.
6. Export confirmed annotations or sidecars without modifying source media.

The first milestone will be a small local review gallery plus a durable annotation format. Model and UI choices are deliberately not locked in yet.

## Privacy and safety

Do not commit personal photos, face crops, embeddings, databases, credentials, or model outputs. The repository includes conservative ignore rules, but review `git status` before every commit. Cloud vision is optional only: any remote model integration must be explicitly enabled, clearly labeled, and documented with its data-handling implications.

## Source-agnostic boundary

The project consumes ordinary local folders and optional generic media manifests. A downloader or photo service remains responsible for acquisition; this companion owns local derived data, review state, ranking, and export. It has no dependency on a particular photo provider.

## Status

Early planning. Contributions and implementation proposals are welcome.

## License

MIT. See [LICENSE](LICENSE).
