# Table 3 ingest-export schema v2

The green **EXPORT** button in Table 3 writes a SQLite handoff database and SQL dump under:

```text
~/Combined/export-for-ingest/
```

The handoff is designed for a future fourth mode in `ingest-advanced.py` that accepts one or many exported tables.

## Core contract

The GUI export owns source facts:

- Exact absolute local paths for the work directory and every direct file.
- The primary CBZ/ZIP and paired metadata JSON.
- The cover source and all sibling files.
- The title, normalized title, suggested slug, and original source URL.
- The complete source metadata JSON.
- The complete nested `tags.json` manifest when present, plus derived tag paths and flat searchable tags.
- Proposed per-file R2 keys and URL templates.

Ingest owns final facts:

- Full archive and file hashes.
- Duplicate checks across selected exports, the master catalog, and remote R2.
- Overwrite decisions.
- Canonical slug and master work ID.
- Final R2 keys and public URLs.
- Materialization of `thumb.webp`, `item.json`, `tags.json`, and `details.json`.
- Upload verification, with `details.json` uploaded last.

## Tables

### `ingest_work_queue`

One row per work. Important columns include:

- `work_directory`
- `archive_path`
- `metadata_json_path`
- `cover_path`
- `source_url`
- `readiness_status`
- `readiness_issues_json`
- `source_metadata_json`
- `flat_tags_json`
- `tags_manifest_json`
- `tag_paths_json`
- `r2_relative_root`
- `r2_archive_key`
- `r2_details_key`
- `r2_tags_key`
- `r2_thumb_key`
- `r2_item_key`
- duplicate, overwrite, ingest-status, and final-result fields

`source_url` is required before upload and is intended to become `details.json["url"]`.

### `ingest_work_files`

One row per source or generated object. Each row records:

- `absolute_path`
- `role`
- `source_exists`
- `action`
- `planned_relative_path`
- `planned_r2_key`
- `planned_cdn_url_template`
- `upload_order`
- `upload_enabled`
- `generated`
- `materialization_status`
- `details_last`
- final path, URL, upload, and verification result fields

Generated artifact rows are created for:

- `chapter_1/thumb.webp`
- `chapter_1/item.json`
- `tags.json`
- `chapter_1/details.json`

### `ingest_chapter_plan`

Provides the initial `chapter_1` extraction and `item.json` plan. Ingest fills in page count, padding, extension, and inspection results after archive extraction.

### `ingest_graph`

Stores explicit relationships such as:

- work directory contains file
- work directory contains archive
- archive paired with metadata JSON
- work directory has cover source
- source file has planned R2 key
- generated object must be materialized

### `ingest_results`

Keeps final canonical slug, master ID, final R2/CDN URLs, duplicate resolution, overwrite result, completion status, and errors separate from immutable source facts.

## Final generated JSON contract

Mode 4 ingest should generate:

- `item.json` using the resolved chapter base URL, pages, padding, extension, and parent work identity.
- `tags.json` by preserving an existing nested node manifest or generating the standard empty/minimal node manifest when missing.
- `details.json` containing the original source `url`, final public URL map, storage projection, chapter/page structure, archive metadata, fingerprints, tags, and Table 3 provenance.
