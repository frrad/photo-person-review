# Design notes

## Evidence model

Suggestions should retain their provenance instead of collapsing to a single opaque score. A future annotation record may include:

```text
media_id
person_id
region_id or bounding_box
decision: suggested | confirmed | rejected | unsure
source: user | face_similarity | visual_similarity | burst_match | remote_model
confidence
reference_media_ids
model_name and model_version
created_at and reviewed_at
```

Scores should help sort a review queue, not silently make irreversible decisions. Near-duplicate burst photos are the safest place to begin propagation; clothing and appearance are useful same-day signals but can change or be shared by multiple people.

## Repository boundary

The tool should work with any local photo source. Source-specific downloaders can export stable media identifiers and capture metadata through a small generic manifest, while this project owns review state and derived local artifacts.

## Possible implementation layers

- Local gallery and keyboard-driven review.
- Date/burst grouping and perceptual duplicate detection.
- Local face detection and face clustering.
- Whole-image and person-crop embeddings for similarity search.
- Optional, explicitly enabled remote vision adjudication for ambiguous cases.

No particular model or framework is required by this planning document.
