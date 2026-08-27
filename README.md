# Photo Person Review

Photo Person Review is a local-first tool for semi-automatically finding photos that contain a chosen person. It is designed for large batches of photos from the same day: a reviewer confirms a few clear examples, and the tool ranks the remaining images using face evidence, visual similarity, clothing, burst proximity, and the reviewer's decisions.

This repository is intentionally generic and currently at the planning stage. It is not affiliated with any photo provider, downloader, school, or cloud photo service.

## Goals

- Reduce a 100-photo day to a small, fast review queue.
- Keep photos, face crops, embeddings, annotations, and model outputs local by default.
- Preserve human approval and the evidence behind each suggestion.
- Accept ordinary folders and optional generic media manifests.
- Make cloud vision an explicit, optional fallback rather than a requirement.

## Planned workflow

1. Import a folder or manifest and group media by capture date.
2. Confirm one or two obvious reference images for the person.
3. Rank candidates using local face and visual embeddings, same-day appearance, and near-duplicate relationships.
4. Review suggested, rejected, and uncertain results with keyboard-friendly controls.
5. Export confirmed annotations or sidecars without modifying source media.

The first milestone will be a small local review gallery plus a durable annotation format. Model and UI choices are deliberately not locked in yet; see [the design notes](docs/design.md).

## Privacy and safety

Do not commit personal photos, face crops, embeddings, databases, credentials, or model outputs. The repository includes conservative ignore rules, but review `git status` before every commit. Any remote model integration must be opt-in, clearly labeled, and documented with its data-handling implications.

## Status

Early planning. Contributions and implementation proposals are welcome.

## License

MIT. See [LICENSE](LICENSE).
