# Table 3 BulkOCR Export Studio

GUI v33 adds a reversible pre-export workflow to the green **EXPORT** button in Table 3.

## Purpose

The selected live Table 3 run is enriched with recursive BulkOCR output before the schema-v2 SQLite handoff is finalized. Every major stage writes durable JSON and JSONL checkpoints so an interrupted or mistaken run can resume without starting from zero.

## BulkOCR source

The GUI calls `batch_extract.py` from the `EXTRACTED-DATA` repository. It searches these locations in order:

1. `$EXTRACTED_DATA_HOME/batch_extract.py`
2. `$BULKOCR_HOME/batch_extract.py`
3. `~/dev/EXTRACTED-DATA/batch_extract.py`
4. `~/EXTRACTED-DATA/batch_extract.py`
5. `~/dev/BulkOCR/batch_extract.py`
6. `~/BulkOCR/batch_extract.py`
7. an `EXTRACTED-DATA` directory beside the GUI checkout or its parent

The repository's own virtual environment is preferred when present.

## Workflow

1. **Prepare** — choose recursive BulkOCR or reuse existing sidecars; optionally allow overwrite after backup.
2. **Discover** — recursively inventory supported source images.
3. **BulkOCR** — run `batch_extract.py --recursive --ocr-engine auto --no-extract-thumbnail --non-interactive`.
4. **Collect** — load every `*-EXTRACTED-DATA.json` and freeze the records.
5. **Merge** — match OCR records to Table 3 works by the longest containing work directory.
6. **Export** — augment the schema-v2 SQLite/SQL handoff and refresh the stable `latest` copies.

BulkOCR exit status `2` is treated as a completed audited batch because the upstream tool uses it when one or more images fail while still writing machine-readable failure JSON.

## Reversible crumbs

Sessions are written under:

```text
~/Combined/export-for-ingest/sessions/<table3-run>/<timestamp>-<table3-run>/
```

A session may contain:

```text
session.json
current-stage.json
00-pre-ocr-backup.json
00-pre-ocr-backup.jsonl
pre-ocr-backup/...
01-discovered-images.json
01-discovered-images.jsonl
01-bulkocr-command.json
01-bulkocr-output.log
02-bulkocr-results.json
02-bulkocr-results.jsonl
03-merged-works.json
03-merged-works.jsonl
04-export-plan.json
04-export-plan.jsonl
```

The **Back** button returns to an earlier checkpoint without deleting later crumbs. Closing an unfinished studio marks the session paused; opening EXPORT again offers to resume it.

## SQLite enrichment

The workflow adds an `ingest_ocr_records` table with the source image path, OCR JSON path, status, confidence, title, creator, tags, characters, fields, thumbnail evidence, warnings, errors, and complete raw BulkOCR payload.

`ingest_work_queue` gains summary fields including:

```text
ocr_session_id
ocr_record_count
ocr_status
ocr_confidence
ocr_source_images_json
ocr_json_paths_json
ocr_tags_json
ocr_characters_json
ocr_fields_json
ocr_warnings_json
ocr_errors_json
ocr_metadata_json
combined_flat_tags_json
```

The original Table 3 metadata remains intact. OCR tags are preserved separately and also merged into `combined_flat_tags_json` for future ingest mode 4. Final `details.json`, `tags.json`, `item.json`, canonical slugs, master IDs, and final R2/CDN URLs remain the responsibility of ingest.

## Stable outputs

After OCR enrichment, these are replaced with the enriched versions:

```text
~/Combined/export-for-ingest/latest.sqlite3
~/Combined/export-for-ingest/latest.sql
~/Combined/export-for-ingest/latest.txt
~/Combined/export-for-ingest/latest-session.txt
```

## Validation

The GitHub workflow `.github/workflows/validate-gui.yml` compiles the launcher, dumps the complete standalone source, compiles that source, and verifies that v33, the Export Studio, and `ingest_ocr_records` are present.
