# The Master SQL Manifest Architecture

## Architectural Charter

This document defines how the GUI repository, the Table 3 handoff, `4-ingest.py`, `manifest.py`, `catalog_db.py`, R2, `details.json`, `item.json`, and the future per-directory `tags.json` system fit together.

It also separates:

1. what already works;
2. what is structurally compatible today;
3. what remains to be standardized;
4. how the system can survive old and new formats;
5. how the archive can evolve for decades without rebuilding the entire cathedral.

---

# 1. The central principle

> **The master SQL manifest is the heart of the entire system.**

The durable operational center is:

```text
catalog.sqlite3
```

Everything else has a distinct responsibility around that heart:

```text
GUI repository
    creates a structured intake and pairing product

Table 3 schema-v2 SQLite handoff
    carries selected works into ingestion

4-ingest.py
    prepares, enriches, uploads, verifies, and records works

manifest.py
    observes storage, creates skeletal records, reconciles, and browses

catalog_db.py
    defines the one shared SQL schema and API

details.json
    portable work-level identity, provenance, storage, and recovery knowledge

item.json
    portable chapter-level reader and reconstruction knowledge

tags.json
    redundant, lightweight classification and discovery reference in every directory

local JSON mirror
    a fast offline copy of remote JSON artifacts for inference and traversal

CBZ / ZIP / PDF
    compact source assets

R2 or another object store
    one replaceable physical warehouse
```

R2 is not the brain.

The GUI is not the brain.

`4-ingest.py` is not the brain.

`manifest.py` is not the brain.

They are producers, consumers, or maintainers of the same durable SQL knowledge layer.

---

# 2. The complete flow

The intended end-to-end pipeline is:

```text
CBZ files + source metadata JSON
            ↓
Pairing GUI
            ↓
Table 3 schema-v2 SQLite handoff
            ↓
4-ingest.py Mode 4 validates the handoff
            ↓
4-ingest.py materializes each work
            ↓
4-ingest.py generates or enriches:
    item.json
    details.json
    tags.json
    thumbnails
    chapter pages
            ↓
4-ingest.py creates a complete file plan in catalog.sqlite3
            ↓
4-ingest.py uploads and verifies the planned objects
            ↓
4-ingest.py records final URLs, hashes, states, and operation history
            ↓
manifest.py later observes the warehouse
            ↓
manifest.py confirms or repairs skeletal presence facts
            ↓
future search, curation, migration, and recovery tools query SQL
```

The Table 3 handoff and the master SQL manifest are **not the same database**.

They have different jobs:

```text
Table 3 handoff
    temporary intake and workflow database

catalog.sqlite3
    permanent operational knowledge database
```

The handoff is allowed to evolve through versions and adapters.

The master SQL contract must remain boring, stable, migration-safe, and backward compatible.

---

# 3. The GUI handoff

The GUI does not need to import `catalog_db.py` directly.

Its compatibility boundary is the exported Table 3 SQLite product.

That product should contain:

- a declared schema version;
- a declared purpose;
- one queue row per selected work;
- paths to the chosen CBZ/ZIP and metadata JSON;
- title and source URL;
- normalized authors, languages, characters, groups, type, and date when known;
- tags and OCR-derived facts;
- fingerprints and readiness information;
- enough information for `4-ingest.py` to materialize the work safely.

The current Mode 4 code validates that the handoff identifies itself as:

```text
schema_version = 2
purpose = table3-ingest-handoff
```

It also checks its required tables and columns before processing.

Therefore:

> **The GUI and `4-ingest.py` are connected through a strict, versioned handoff contract.**

The GUI can evolve internally without threatening the master manifest, provided it continues to emit a supported handoff version or supplies a versioned adapter.

---

# 4. What `4-ingest.py` contributes

`4-ingest.py` is the bridge from intake knowledge to durable operational knowledge.

For each accepted work it can:

- validate the Table 3 handoff;
- locate the selected archive and metadata source;
- extract or materialize pages;
- generate a thumbnail;
- generate chapter `item.json`;
- create or enrich `details.json`;
- create `tags.json`;
- calculate file plans, paths, R2 keys, URLs, sizes, and hashes;
- upsert the logical work into the master SQL manifest;
- replace or update the work's file plan;
- upload files;
- verify uploaded objects;
- upload `details.json` last;
- preserve operation and error history.

The important boundary is:

```text
GUI knowledge
    ↓ versioned adapter
4-ingest.py
    ↓ CatalogDB API
master SQL manifest
```

---

# 5. What `manifest.py` contributes

`manifest.py` is the storage observer and surgical catalog maintainer.

It does not need to recreate all rich ingest knowledge.

Its first responsibility is to establish and maintain the skeleton:

- this work directory exists;
- an archive appears to exist;
- `details.json` appears to exist;
- `item.json` appears to exist;
- `tags.json` appears to exist;
- a thumbnail appears to exist;
- how many chapters are visible;
- which remote root and prefix were observed;
- when the observation happened;
- whether a fact is present, absent, or unknown.

The tri-state rule is essential:

```text
1 / present
    positively observed

0 / absent
    positively checked and not found

NULL / unknown
    not checked, interrupted, outside the scan scope, or errored
```

`manifest.py` must act like a surgeon:

```text
update one work
update one prefix
update one week
update one month
update objects newer than a checkpoint
reconcile one selected storage root
```

It must not behave like a conqueror that recomputes or invalidates everything outside the selected operation.

---

# 6. What `catalog_db.py` contributes

`catalog_db.py` is the only supported doorway into the master SQL manifest.

Both `4-ingest.py` and `manifest.py` should import the same physical module and point to the same physical `catalog.sqlite3`.

Its responsibilities include:

- schema creation;
- schema migrations;
- transactions;
- work UPSERT operations;
- file planning and file-state updates;
- inventory facts;
- tags;
- operations and reconciliation history;
- read views used by browsers;
- compatibility adapters for older callers.

The current shared module declares schema version 3 and contains the inventory-fact structures used by the current `manifest.py`.

The safest repository structure is:

```text
~/new-scripts/
├── catalog_db.py
├── 4-ingest.py
├── manifest.py
└── catalog.sqlite3
```

Both tools then import the same module naturally.

The critical operational rule is:

> **There must be one canonical `catalog_db.py` and one configured master database path.**

Copies may exist for backup, distribution, or testing, but ordinary tools must not quietly create competing schemas.

---

# 7. The portable JSON pillars

The JSON files are not competitors to SQL.

They are portable records that let the system survive:

- database loss;
- provider changes;
- bucket changes;
- path changes;
- repository changes;
- application rewrites;
- language changes;
- search-engine changes;
- sharding;
- infrastructure replacement.

They are the portable DNA of the archive.

---

## 7.1 `details.json`: work-level portable DNA

`details.json` should carry enough rich data to identify and differentiate a work.

It does **not** need every possible field to be valid.

The contract must distinguish:

```text
required identity-bearing fields
optional enrichment fields
```

### New-standard identity requirements

The exact Details v2 schema still needs to be committed, but it should require an identity quorum such as:

- `schema_version`;
- canonical `url`, containing the original source URL;
- a title or display name;
- a canonical slug or stable work identifier;
- enough source or provenance information to distinguish the work;
- generation or observation metadata.

The governing rule is:

> **A new-standard `details.json` without the canonical `url` field is incomplete.**

`source_url` may remain as a compatibility alias, but `url` is the canonical required field.

### Optional rich fields

A valid record may also contain:

- authors;
- groups;
- languages;
- characters;
- work type;
- published date;
- tags and tag sources;
- OCR results;
- chapter and page counts;
- archive metadata;
- archive and page-tree fingerprints;
- storage provider;
- bucket and prefix;
- exact object keys and URLs;
- thumbnail information;
- ingest-handoff provenance;
- custom future fields.

Missing optional fields do not make the record invalid.

Unknown fields must be preserved rather than discarded.

### Current versioning issue

The current ingest code can write the richer payload while still defaulting its top-level `schema_version` to `1`.

This should be formalized deliberately:

```text
Details v1
    legacy records, including records without a required URL

Details v2
    identity-bearing format with required canonical URL
```

Old v1 records remain readable through an adapter.

New controlled writers should emit v2 after its schema and validator are committed.

---

## 7.2 `item.json`: chapter-level portable DNA

`item.json` describes how a chapter is read and reconstructed.

The current generated shape includes facts such as:

- chapter ID;
- parent work slug;
- parent work ID when available;
- chapter slug;
- type;
- title and subtitle;
- base URL;
- page count;
- numeric padding;
- page extension.

This can describe an entire page sequence without listing every image:

```text
base_url
pages = 73
padding = 3
extension = webp

→ 001.webp through 073.webp
```

A future Item v2 may add:

- stable chapter UUID;
- page-list exceptions;
- page hashes;
- alternate storage locations;
- language variants;
- revision history.

Old readers should ignore unknown fields.

New readers should accept older versions through adapters.

---

## 7.3 `tags.json`: redundant by design

Every managed remote directory should eventually contain a lightweight `tags.json`.

That includes:

- work directories;
- chapter directories;
- collection directories;
- selected administrative or shard roots.

The duplication is intentional.

`tags.json` becomes a cheap reference point for:

- scripts;
- web applications;
- static deployments;
- search-index generators;
- curation tools;
- migration tools;
- recovery tools;
- systems that cannot or should not open the master SQL manifest.

A directory-level `tags.json` can contain:

```json
{
  "schema_version": 1,
  "scope": "work",
  "work_uid": "stable-id",
  "slug": "Current_Slug",
  "tags": ["english", "full-color"],
  "namespaces": {
    "language": ["english"],
    "format": ["full-color"]
  },
  "inherits": [],
  "source_revision": "manifest-revision-id",
  "generated_at": "..."
}
```

A chapter-level copy may contain:

```json
{
  "schema_version": 1,
  "scope": "chapter",
  "work_uid": "stable-id",
  "chapter_uid": "stable-chapter-id",
  "tags": ["chapter-1"],
  "inherits": ["../tags.json"]
}
```

The intended division is:

```text
SQL
    canonical normalized tag relationships and provenance

tags.json
    portable, local, easy-to-read tag projection
```

`tags.json` should be cheap to regenerate.

It should also be safe to re-import without blindly replacing richer or newer SQL facts.

---

# 8. A local mirror of remote JSON artifacts

The system should eventually maintain a local mirror of remote JSON documents:

```text
~/manifest/json-mirror/
└── provider/
    └── bucket/
        └── prefix/
            └── Work_A/
                ├── details.json
                ├── tags.json
                └── chapter_1/
                    ├── item.json
                    └── tags.json
```

This mirror should preserve:

- the original remote relative path;
- the original JSON bytes;
- a content checksum;
- object modification time;
- object version or ETag when available;
- retrieval time;
- detected document type;
- detected schema version;
- detected format era;
- parsing and validation result.

Benefits:

- fast offline inference;
- fast traversal;
- no repeated remote download for unchanged JSON;
- easier debugging;
- search-index construction without touching R2;
- migration planning;
- recovery after provider failure;
- comparing document generations;
- reproducing historical behavior.

The SQL manifest should index the mirror, but the mirror should not replace SQL.

---

# 9. Protecting the pillars

Ordinary scripts should not casually mutate `details.json` or `item.json`.

Responsibilities must be separated:

```text
readers
    may read any supported version

observers
    write observations to SQL, not into identity JSON

tag tools
    primarily update SQL and regenerate tags.json

ingest/reprojection tools
    are controlled writers of details.json and item.json

migration tools
    preserve raw JSON byte-for-byte unless an explicit conversion is requested
```

A controlled writer should:

1. load the existing document;
2. preserve unknown fields;
3. update only its owned namespace;
4. validate required fields;
5. write atomically;
6. record the previous revision;
7. update SQL with the resulting checksum and schema version.

This prevents convenience scripts from corrupting the archive's identity and reconstruction pillars.

---

# 10. SQL and JSON form a two-way recovery system

The resilience model is:

```text
SQL
    normalized, indexed, operational truth

details.json + item.json + tags.json
    portable identity and reconstruction truth

CBZ / ZIP / PDF
    compact source assets
```

The system must support both directions.

## Rebuild SQL from assets and JSON

After total database loss:

```text
scan work directories
    ↓
read details.json
read item.json
read tags.json
observe CBZ/ZIP/PDF and thumbnail
    ↓
recreate works, chapters, tags, files, locations, and fingerprints
    ↓
mark uncertain facts unknown
```

## Regenerate JSON from SQL

After JSON loss or a controlled format migration:

```text
query the master SQL manifest
    ↓
generate details.json
generate item.json
generate tags.json
    ↓
validate and publish projections
```

Neither SQL nor JSON should become the only surviving copy of irreplaceable knowledge.

---

# 11. Era-aware traversal

The archive must be able to contain documents from many generations without forcing a global rewrite.

Examples:

```text
details-v1-pre-url
details-v1-rich-but-unversioned
details-v2-url-required
item-v1
item-v2
tags-v1
future unknown formats
```

The master manifest should record for every document:

- document type;
- declared schema version;
- detected era;
- validation status;
- raw checksum;
- parsed canonical identity;
- adapter used;
- observed time;
- current remote location.

A future script should be able to ask:

```text
show all works from the details-v1-pre-url era
show all documents that need a URL upgrade
show all item-v1 chapters
search only documents validated under adapter X
```

This is how the system can traverse an era instead of demolishing and rebuilding the cathedral.

---

# 12. Schema adapters, not global rewrites

Each historical format gets a reader adapter.

Example:

```text
raw details-v1
    ↓ details_v1_adapter
canonical work facts

raw details-v2
    ↓ details_v2_adapter
canonical work facts
```

Both adapters produce the same internal facts:

```text
work identity
title
source URL
authors
groups
languages
tags
storage hints
fingerprints
```

The raw document remains preserved.

The normalized SQL representation remains queryable.

Conversion to a newer JSON version is optional and explicit.

The system should never require billions of remote objects to be rewritten merely because a new field was invented.

---

# 13. The boring core and evolving edges

The permanent schema must be intentionally boring.

Stable core concepts:

```text
work identity
chapter identity
asset identity
aliases
locations
documents
facts
tags
observations
operations
schema versions
timestamps
checksums
```

Future scripts may discover endless new information.

They should not require rebuilding the `works` table every time.

New knowledge can initially enter through extensible structures:

```text
document_revisions
fact_definitions
work_facts
chapter_facts
asset_facts
raw_documents
observations
```

Frequently queried facts can later be promoted into indexed columns or materialized search tables without changing permanent identity.

This permits endless evolution while preserving old readers.

---

# 14. Recommended future master schema

The current schema is a strong operational beginning.

The long-term schema should grow additively toward:

```text
schema_migrations
    ordered, checksummed migration history

works
    stable logical works

work_aliases
    old slugs, titles, source IDs, former paths

chapters
    stable logical chapters

assets
    logical files independent of one provider

asset_locations
    provider, bucket, key, URL, active/historical state

documents
    logical details/item/tags documents

document_revisions
    raw payload, checksum, era, schema version, source, timestamp

observations
    append-only statements about storage and files

fact_definitions
    typed extensible fact registry

work_facts
chapter_facts
asset_facts
    extensible knowledge without endless schema churn

tags
tag_assignments
    normalized tags with provenance and scope

scan_runs
scan_checkpoints
change_journal
    surgical incremental manifest updates

operations
    uploads, moves, verification, curation, errors

archive_snapshots
    local and remote master-manifest snapshots

search_documents
search_shards
    derived advanced-search structures
```

Existing tables and IDs should be preserved through migrations and compatibility views.

---

# 15. Archiving the master manifest

The master manifest must be archivable as a self-describing package.

A complete snapshot should contain:

```text
manifest-snapshot-2026-08-06T120000Z/
├── snapshot.json
├── catalog.sqlite3
├── catalog.sqlite3.sha256
├── root-manifest.json
├── schema-registry/
├── migration-ledger.jsonl
├── shard-map.json
├── json-mirror-index.jsonl
├── change-checkpoint.json
└── checksums.txt
```

Before snapshotting SQLite:

1. finish or pause write transactions;
2. checkpoint WAL;
3. run an integrity check;
4. create the snapshot copy;
5. calculate checksums;
6. record schema version and code versions;
7. archive locally;
8. optionally archive remotely.

Default local archive location:

```text
~/manifest/archive/<timestamp>-master/
```

Default remote archive location:

```text
<configured-remote>/manifest/archive/<timestamp>-master/
```

The active master should never be replaced until a verified recovery snapshot exists.

---

# 16. Snapshot plus delta

At very large scale, complete snapshots are periodic.

Between snapshots, changes are recorded as immutable deltas:

```text
baseline snapshot
    +
delta 000001
delta 000002
delta 000003
```

A recovery operation can:

1. restore the latest valid baseline;
2. replay later deltas;
3. verify the resulting checksum and counts;
4. open the recovered manifest.

This makes archiving efficient without repeatedly copying an enormous database.

---

# 17. Search vision

The SQL manifest lets future search avoid repeatedly opening billions of JSON files or listing billions of R2 objects.

Searchable facts may include:

- titles;
- aliases;
- authors;
- groups;
- languages;
- characters;
- types;
- dates;
- source URLs;
- tags;
- OCR fields;
- archive hashes;
- chapter counts;
- page counts;
- file roles;
- storage state;
- upload time;
- verification time;
- document era;
- schema version.

The query path becomes:

```text
advanced query
    ↓
indexed SQL/search shard
    ↓
stable work identity
    ↓
exact chapter or asset identity
    ↓
exact active object location
    ↓
retrieve only the requested asset
```

At large scale, the logical contract remains the same while the physical implementation evolves:

```text
small library
    one SQLite database

large library
    read replicas and specialized indexes

enormous library
    root routing index + independent SQL/search shards
```

A small computer opens only the root index and the relevant shard.

---

# 18. Migration vision

The same manifest becomes the migration planner.

A migration can select exact assets from SQL:

```text
verified CBZ/ZIP/PDF source assets
details.json
item.json
tags.json
selected thumbnails
other non-reconstructable documents
```

Derived page images may be omitted only when reconstruction from the source archive is proven and desired.

Every copy job records:

```text
planned
copying
copied
verified
committed
failed
retryable
```

Because the JSONs preserve identity and reconstruction knowledge, a new infrastructure can:

1. copy the compact seed;
2. create a new database;
3. import the portable JSONs;
4. upload or reconstruct required assets;
5. update active location records;
6. rebuild search shards;
7. verify the new warehouse;
8. cut over without changing logical identities.

---

# 19. Backward-compatibility laws

## Law 1: Never reuse an identity

A permanent work, chapter, document, or asset identity is never reassigned.

## Law 2: Never silently reinterpret an old field

When meaning changes, create a new field or a new version.

## Law 3: Additive evolution first

Prefer adding tables, columns, fields, views, and adapters over destructive replacement.

## Law 4: Unknown fields survive

JSON readers and writers preserve fields they do not understand.

## Law 5: Old records remain readable

New code reads all supported historical versions.

## Law 6: New output uses the newest standard

Adapters accept legacy input; controlled writers emit the current canonical version.

## Law 7: Absence is not unknown

A field omitted by an older producer must not become a false negative.

## Law 8: Every migration is recorded

Schema and document conversions record source version, destination version, timestamp, tool, and result.

## Law 9: No full-library rewrite for a local change

A work-level change updates only that work and its affected indexes or shards.

## Law 10: The master is never replaced without recovery

Before complete synchronization or replacement, archive and validate the previous master locally and remotely.

## Law 11: Raw source documents remain available

Normalization never destroys the raw JSON from which facts were inferred.

## Law 12: Derived indexes are disposable

Search indexes, caches, and projections can be rebuilt from the master SQL and portable JSON pillars.

---

# 20. Honest compatibility appraisal

## Green: already connected

### GUI → `4-ingest.py`

The Table 3 schema-v2 SQLite export is a real, validated handoff boundary.

### `4-ingest.py` → `CatalogDB`

`4-ingest.py` imports `CatalogDB`, `FilePlan`, storage projection helpers, tag helpers, and hashing helpers.

### `manifest.py` → `CatalogDB`

`manifest.py` imports `CatalogDB` and uses shared work, file, inventory, and reconciliation structures.

### Current `catalog_db.py` → `manifest.py`

The current supplied catalog module declares schema version 3 and contains `work_inventory_facts`, tri-state inventory support, and the methods used by the current manifest program.

### Shared JSON generation

`4-ingest.py` already generates `item.json`, produces rich `details.json`, records URLs, and creates tag information.

---

## Yellow: aligned but requires formalization

### One configured master path

Both programs are compatible only when they are deliberately pointed at the same master `catalog.sqlite3`.

### Details schema version

The rich new `details.json` format still needs a committed Details v2 schema and validator.

### Stable global identity

Current code often relies on SQLite row IDs, slugs, or deterministic parent IDs.

A billion-scale multi-shard system should add immutable globally unique work, chapter, document, and asset IDs while preserving every existing identifier as an alias.

### Raw JSON revision preservation

JSON payloads are retained today, but formal document-revision tables and merge ownership rules still need to be added.

### Per-directory `tags.json`

The architecture is defined here, but generation, inheritance, import precedence, and conflict resolution still need implementation.

### Local JSON mirror

The purpose and layout are defined here, but incremental synchronization and mirror indexing still need implementation.

---

## Red: not yet built

- formal Details v2 JSON Schema;
- formal Item v2 JSON Schema;
- formal Tags v1 JSON Schema;
- versioned JSON adapters and era registry;
- immutable global identity layer;
- generic typed fact tables;
- document revision and raw-payload history;
- incremental Cloudflare change-journal ingestion;
- checkpoint-based “objects added since last update” processing;
- root manifest and SQL/search shard routing;
- remote master synchronization and archive/restore UI;
- manifest-driven migration worker engine;
- billion-object simulation and failure testing.

---

# 21. The next build sequence

## Phase 1: Freeze the shared heart

1. Commit one canonical `catalog_db.py`.
2. Confirm `4-ingest.py` and `manifest.py` import it.
3. Configure one default master database path.
4. Add migration tests from every known older catalog.
5. Add compatibility views for old readers.
6. Add immutable global work IDs without removing current numeric IDs or slugs.

## Phase 2: Formalize the portable DNA

1. Define Details v2.
2. Require canonical `url`.
3. Define the minimum identity quorum.
4. Preserve all optional and unknown fields.
5. Define Item v2.
6. Define Tags v1.
7. Add validators and old-format adapters.
8. Record document versions and checksums in SQL.

## Phase 3: Prove the GUI handoff

Create an automated fixture test:

```text
GUI-style Table 3 v2 database
    ↓
4-ingest.py dry-run
    ↓
generated package
    ↓
master SQL rows
    ↓
details/item/tags validation
```

The test should confirm:

- source URL survives;
- title and identifying metadata survive;
- tags survive;
- item URLs and page rules are correct;
- expected work and files enter the master SQL manifest;
- rerunning is safe and idempotent.

## Phase 4: Build the JSON mirror

1. Mirror only JSON artifacts initially.
2. Preserve exact paths and bytes.
3. Skip unchanged objects by version, timestamp, size, or checksum.
4. Detect schema era.
5. Parse normalized facts through adapters.
6. Index mirror documents in SQL.
7. Permit completely offline traversal.

## Phase 5: Surgical manifest updates

Add:

- last successful checkpoint;
- changed-object journal;
- `--since`;
- `--month`;
- `--week`;
- `--only-work`;
- `--prefix`;
- scoped reconciliation;
- conservative missing confirmation;
- append-only observation history.

## Phase 6: Archive and synchronization

Add:

- local master snapshots;
- remote master snapshots;
- checksums and integrity validation;
- snapshot plus delta recovery;
- non-destructive sync-to-existing;
- archive-before-complete-sync;
- atomic active-master replacement;
- archive browser and restore testing.

## Phase 7: Search and sharding

Add:

- normalized aliases and entities;
- full-text search;
- advanced filters;
- root shard map;
- immutable shard IDs;
- incremental index updates;
- compact read-only distribution shards.

## Phase 8: Migration engine

Add:

- immutable migration manifests;
- independent worker shards;
- resumable copy state;
- hash-based deduplication;
- baseline plus live delta migration;
- atomic location cutover;
- complete verification and rollback.

---

# 22. The final vision

The framework is not merely a collection of upload scripts.

It is a durable knowledge system in which:

```text
R2
    stores current physical objects

SQL
    provides fast operational memory and advanced search

details.json
    preserves portable work identity and provenance

item.json
    preserves portable chapter reconstruction rules

tags.json
    provides redundant, lightweight directory-level reference

local JSON mirror
    enables offline inference and traversal

schema adapters
    allow old and new eras to coexist

snapshots and deltas
    make the master archivable and recoverable
```

The goal is not to rebuild the cathedral every time a new field, script, provider, or search feature appears.

The goal is:

> **Build a stable foundation once, preserve every era, add knowledge surgically, and let the system evolve indefinitely around a dependable and boringly successful core.**

That is the architecture.
