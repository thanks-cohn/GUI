# Table 3 -> ingest handoff support, injected into the standalone GUI source.
EXPORT_FOR_INGEST_ROOT = DEFAULT_COMBINED_ROOT / "export-for-ingest"
EXPORT_FOR_INGEST_SCHEMA_VERSION = 1


def _export_slugify(value: str) -> str:
    original = unicodedata.normalize("NFKC", str(value or "")).strip()
    cleaned = re.sub(r"[’']", "", original)
    cleaned = re.sub(r"[^A-Za-z0-9.-]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_.")
    if cleaned:
        return cleaned
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return f"Untitled_Work_{digest}"


def _export_json_strings(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, str):
        if value.strip():
            output.append(value.strip())
    elif isinstance(value, (int, float)):
        output.append(str(value))
    elif isinstance(value, list):
        for child in value:
            output.extend(_export_json_strings(child))
    elif isinstance(value, dict):
        for child in value.values():
            output.extend(_export_json_strings(child))
    seen: set[str] = set()
    unique: list[str] = []
    for item in output:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _export_collect_fields(value: Any, accepted_keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in accepted_keys:
                found.extend(_export_json_strings(child))
            found.extend(_export_collect_fields(child, accepted_keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(_export_collect_fields(child, accepted_keys))
    seen: set[str] = set()
    output: list[str] = []
    for item in found:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def _export_json_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _export_load_old_pairing_hints(database_path: Path) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    if not database_path.is_file():
        return hints
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT destination_directory, destination_archive, destination_json,
                   title, source_url, match_percent, selection_method, batch_id
            FROM combined_pairings
            WHERE COALESCE(status, 'combined') != 'reversed'
            ORDER BY id
            """
        ).fetchall()
    except sqlite3.Error:
        return hints
    finally:
        connection.close()
    for row in rows:
        directory = str(row["destination_directory"] or "").strip()
        if not directory:
            continue
        try:
            directory = str(Path(directory).expanduser().resolve())
        except OSError:
            pass
        hints[directory] = dict(row)
    return hints


def _export_choose_primary_file(
    files: list[Path],
    hinted_path: str,
    *,
    prefer_suffix: str = "",
) -> Path | None:
    if hinted_path:
        candidate = Path(hinted_path).expanduser()
        if candidate.is_file():
            try:
                resolved = candidate.resolve()
                for file_path in files:
                    if file_path == resolved:
                        return file_path
            except OSError:
                pass
    if not files:
        return None
    if prefer_suffix:
        preferred = [
            path for path in files
            if path.name.casefold().endswith(prefer_suffix.casefold())
        ]
        if len(preferred) == 1:
            return preferred[0]
    return sorted(files, key=lambda path: path.name.casefold())[0]


def _export_classify_file(
    path: Path,
    primary_archive: Path,
    primary_json: Path,
    cover: Path | None,
) -> str:
    if path == primary_archive:
        return "archive"
    if path == primary_json:
        return "metadata_json"
    name = path.name.casefold()
    if path == cover:
        return "cover"
    if name == "details.json":
        return "details_json"
    if name == "tags.json":
        return "tags_json"
    if name == "item.json":
        return "item_json"
    if path.suffix.casefold() in SUPPORTED_ARCHIVE_SUFFIXES:
        return "archive_extra"
    if path.suffix.casefold() == ".json":
        return "json_extra"
    if path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES:
        return "image"
    return "other"


def export_table3_for_ingest(
    manifest_database: Path,
    destination_root: Path = EXPORT_FOR_INGEST_ROOT,
) -> tuple[Path, Path, int]:
    """Export the selected Table 3 run as a self-contained ingest handoff database."""
    manifest_database = manifest_database.expanduser().resolve()
    run_directory = manifest_database.parent
    if not run_directory.is_dir():
        raise ValueError(f"Combined run directory does not exist:\n{run_directory}")

    destination_root = destination_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    hints = _export_load_old_pairing_hints(manifest_database)

    directories = [
        path.resolve()
        for path in run_directory.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and path.name != "manifest-backups"
        and not path.name.startswith("_shared_")
        and path.name != "export-for-ingest"
    ]
    directories.sort(key=lambda path: path.name.casefold())

    records: list[dict[str, Any]] = []
    for order, directory in enumerate(directories, start=1):
        files = sorted(
            (path.resolve() for path in directory.iterdir() if path.is_file()),
            key=lambda path: path.name.casefold(),
        )
        archives = [
            path for path in files
            if path.suffix.casefold() in SUPPORTED_ARCHIVE_SUFFIXES
        ]
        jsons = [path for path in files if path.suffix.casefold() == ".json"]
        if not archives or not jsons:
            continue

        hint = hints.get(str(directory), {})
        primary_archive = _export_choose_primary_file(
            archives, str(hint.get("destination_archive") or "")
        )
        primary_json = _export_choose_primary_file(
            jsons,
            str(hint.get("destination_json") or ""),
            prefer_suffix=".provenance.json",
        )
        if primary_archive is None or primary_json is None:
            continue

        try:
            metadata = json.loads(primary_json.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            metadata = {}

        raw_title = find_title(metadata) if isinstance(metadata, (dict, list)) else None
        title = clean_title(raw_title) if raw_title else str(hint.get("title") or "")
        title = title.strip() or primary_archive.stem
        source_url = (
            (find_source_url(metadata) if isinstance(metadata, (dict, list)) else None)
            or str(hint.get("source_url") or "")
        ).strip()

        tags = _export_collect_fields(metadata, {"tags", "tag"})
        authors = _export_collect_fields(
            metadata, {"authors", "author", "artist", "artists"}
        )
        languages = _export_collect_fields(
            metadata, {"languages", "language", "lang"}
        )
        characters = _export_collect_fields(
            metadata, {"characters", "character"}
        )
        groups = _export_collect_fields(metadata, {"groups", "group", "circle"})
        work_types = _export_collect_fields(metadata, {"type", "category", "kind"})
        dates = _export_collect_fields(
            metadata, {"date", "published", "published_at", "created_at"}
        )

        images = [
            path for path in files
            if path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES
        ]
        cover = None
        if images:
            cover = sorted(
                images,
                key=lambda path: (
                    0 if path.stem.casefold() in {"cover", "thumb", "thumbnail"} else 1,
                    0 if path.suffix.casefold() in {".png", ".webp", ".jpg", ".jpeg"} else 1,
                    path.name.casefold(),
                ),
            )[0]

        slug = _export_slugify(title)
        r2_root = f"works/{slug}"
        archive_stat = primary_archive.stat()
        json_sha256 = _export_json_sha256(primary_json)
        related_files = [
            str(path)
            for path in files
            if path not in {primary_archive, primary_json}
        ]
        quick_payload = {
            "title": unicode_words(title),
            "source_url": source_url,
            "archive_filename": primary_archive.name,
            "archive_size_bytes": archive_stat.st_size,
            "json_sha256": json_sha256,
        }
        quick_fingerprint = hashlib.sha256(
            json.dumps(
                quick_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        graph_payload = {
            "work_directory": str(directory),
            "archive": str(primary_archive),
            "metadata_json": str(primary_json),
            "files": [str(path) for path in files],
            "title": title,
            "source_url": source_url,
            "slug": slug,
        }
        graph_sha256 = hashlib.sha256(
            json.dumps(
                graph_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        records.append(
            {
                "work_order": order,
                "work_directory": directory,
                "files": files,
                "archive": primary_archive,
                "metadata_json_path": primary_json,
                "cover": cover,
                "title": title,
                "normalized_title": unicode_words(title),
                "slug": slug,
                "source_url": source_url,
                "metadata": metadata,
                "tags": tags,
                "authors": authors,
                "languages": languages,
                "characters": characters,
                "groups": groups,
                "work_type": work_types[0] if work_types else "",
                "published_date": dates[0] if dates else "",
                "related_files": related_files,
                "archive_size_bytes": archive_stat.st_size,
                "archive_mtime_ns": archive_stat.st_mtime_ns,
                "json_sha256": json_sha256,
                "quick_fingerprint": quick_fingerprint,
                "graph_sha256": graph_sha256,
                "r2_root": r2_root,
            }
        )

    if not records:
        raise ValueError(
            "No direct pairing directories containing both CBZ/ZIP and JSON "
            f"were found in:\n{run_directory}"
        )

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    base = destination_root / f"{run_directory.name}-ingest-export-{stamp}"
    counter = 1
    while base.with_suffix(".sqlite3").exists() or base.with_suffix(".sql").exists():
        base = destination_root / (
            f"{run_directory.name}-ingest-export-{stamp}-{counter:02d}"
        )
        counter += 1
    sqlite_path = base.with_suffix(".sqlite3")
    sql_path = base.with_suffix(".sql")

    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE export_metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            );

            CREATE TABLE ingest_work_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_key TEXT UNIQUE NOT NULL,
                run_name TEXT NOT NULL,
                run_directory TEXT NOT NULL,
                work_order INTEGER NOT NULL,
                work_directory TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                slug_suggestion TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                metadata_json_path TEXT NOT NULL,
                archive_path TEXT NOT NULL,
                archive_filename TEXT NOT NULL,
                archive_suffix TEXT NOT NULL,
                archive_size_bytes INTEGER NOT NULL,
                archive_mtime_ns INTEGER NOT NULL,
                archive_sha256 TEXT,
                archive_hash_status TEXT NOT NULL DEFAULT 'deferred-to-ingest',
                json_sha256 TEXT NOT NULL,
                quick_fingerprint TEXT NOT NULL,
                graph_sha256 TEXT NOT NULL,
                cover_path TEXT,
                related_files_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                languages_json TEXT NOT NULL,
                characters_json TEXT NOT NULL,
                groups_json TEXT NOT NULL,
                work_type TEXT NOT NULL DEFAULT '',
                published_date TEXT NOT NULL DEFAULT '',
                source_metadata_json TEXT NOT NULL,
                r2_relative_root TEXT NOT NULL,
                r2_archive_key TEXT NOT NULL,
                r2_details_key TEXT NOT NULL,
                r2_tags_key TEXT NOT NULL,
                r2_item_prefix TEXT NOT NULL,
                duplicate_status TEXT NOT NULL DEFAULT 'unchecked',
                duplicate_match_count INTEGER NOT NULL DEFAULT 0,
                duplicate_matches_json TEXT NOT NULL DEFAULT '[]',
                requested_action TEXT NOT NULL DEFAULT 'inspect',
                overwrite_allowed INTEGER NOT NULL DEFAULT 0,
                ingest_status TEXT NOT NULL DEFAULT 'pending',
                master_work_id INTEGER,
                final_r2_prefix TEXT,
                final_work_url TEXT,
                final_details_url TEXT,
                final_archive_url TEXT,
                final_tags_url TEXT,
                final_item_urls_json TEXT NOT NULL DEFAULT '[]',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE ingest_work_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                absolute_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                suffix TEXT NOT NULL,
                relative_to_work_directory TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT,
                hash_status TEXT NOT NULL DEFAULT 'deferred-to-ingest',
                is_primary INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(work_id) REFERENCES ingest_work_queue(id) ON DELETE CASCADE,
                UNIQUE(work_id, absolute_path)
            );

            CREATE TABLE ingest_graph (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                FOREIGN KEY(work_id) REFERENCES ingest_work_queue(id) ON DELETE CASCADE
            );

            CREATE INDEX ingest_queue_title_idx
                ON ingest_work_queue(normalized_title);
            CREATE INDEX ingest_queue_slug_idx
                ON ingest_work_queue(slug_suggestion);
            CREATE INDEX ingest_queue_source_url_idx
                ON ingest_work_queue(source_url);
            CREATE INDEX ingest_queue_quick_fingerprint_idx
                ON ingest_work_queue(quick_fingerprint);
            CREATE INDEX ingest_queue_graph_sha256_idx
                ON ingest_work_queue(graph_sha256);
            CREATE INDEX ingest_files_filename_idx
                ON ingest_work_files(filename);
            CREATE INDEX ingest_files_size_idx
                ON ingest_work_files(size_bytes);

            CREATE VIEW ingest_ready AS
            SELECT
                id,
                title,
                slug_suggestion,
                source_url,
                work_directory,
                archive_path,
                metadata_json_path,
                cover_path,
                tags_json,
                authors_json,
                languages_json,
                related_files_json,
                quick_fingerprint,
                graph_sha256,
                r2_relative_root,
                r2_archive_key,
                r2_details_key,
                r2_tags_key,
                r2_item_prefix,
                duplicate_status,
                requested_action,
                overwrite_allowed,
                ingest_status
            FROM ingest_work_queue
            ORDER BY work_order, id;
            """
        )

        exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata_rows = {
            "schema_version": str(EXPORT_FOR_INGEST_SCHEMA_VERSION),
            "generator": "pairing_gui.py",
            "gui_version": APP_VERSION,
            "purpose": "table3-ingest-handoff",
            "run_name": run_directory.name,
            "run_directory": str(run_directory),
            "source_manifest_database": str(manifest_database),
            "exported_at": exported_at,
            "work_count": str(len(records)),
            "archive_hash_policy": "deferred-to-ingest",
            "duplicate_policy": (
                "ingest compares export rows against master catalog and remote "
                "before upload"
            ),
            "overwrite_policy": (
                "inspect duplicates first; overwrite only after explicit "
                "decision or --overwrite"
            ),
        }
        connection.executemany(
            "INSERT INTO export_metadata(key, value) VALUES (?, ?)",
            metadata_rows.items(),
        )

        for record in records:
            cursor = connection.execute(
                """
                INSERT INTO ingest_work_queue (
                    package_key, run_name, run_directory, work_order,
                    work_directory, title, normalized_title, slug_suggestion,
                    source_url, metadata_json_path, archive_path,
                    archive_filename, archive_suffix, archive_size_bytes,
                    archive_mtime_ns, json_sha256, quick_fingerprint,
                    graph_sha256, cover_path, related_files_json, tags_json,
                    authors_json, languages_json, characters_json, groups_json,
                    work_type, published_date, source_metadata_json,
                    r2_relative_root, r2_archive_key, r2_details_key,
                    r2_tags_key, r2_item_prefix, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record["graph_sha256"],
                    run_directory.name,
                    str(run_directory),
                    record["work_order"],
                    str(record["work_directory"]),
                    record["title"],
                    record["normalized_title"],
                    record["slug"],
                    record["source_url"],
                    str(record["metadata_json_path"]),
                    str(record["archive"]),
                    record["archive"].name,
                    record["archive"].suffix.casefold(),
                    record["archive_size_bytes"],
                    record["archive_mtime_ns"],
                    record["json_sha256"],
                    record["quick_fingerprint"],
                    record["graph_sha256"],
                    str(record["cover"]) if record["cover"] else None,
                    json.dumps(record["related_files"], ensure_ascii=False),
                    json.dumps(record["tags"], ensure_ascii=False),
                    json.dumps(record["authors"], ensure_ascii=False),
                    json.dumps(record["languages"], ensure_ascii=False),
                    json.dumps(record["characters"], ensure_ascii=False),
                    json.dumps(record["groups"], ensure_ascii=False),
                    record["work_type"],
                    record["published_date"],
                    json.dumps(record["metadata"], ensure_ascii=False),
                    record["r2_root"],
                    f'{record["r2_root"]}/{record["archive"].name}',
                    f'{record["r2_root"]}/details.json',
                    f'{record["r2_root"]}/tags.json',
                    f'{record["r2_root"]}/',
                    exported_at,
                ),
            )
            work_id = int(cursor.lastrowid)

            for file_path in record["files"]:
                st = file_path.stat()
                role = _export_classify_file(
                    file_path,
                    record["archive"],
                    record["metadata_json_path"],
                    record["cover"],
                )
                connection.execute(
                    """
                    INSERT INTO ingest_work_files (
                        work_id, role, absolute_path, filename, suffix,
                        relative_to_work_directory, size_bytes, mtime_ns,
                        sha256, hash_status, is_primary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        role,
                        str(file_path),
                        file_path.name,
                        file_path.suffix.casefold(),
                        file_path.relative_to(
                            record["work_directory"]
                        ).as_posix(),
                        st.st_size,
                        st.st_mtime_ns,
                        (
                            record["json_sha256"]
                            if file_path == record["metadata_json_path"]
                            else None
                        ),
                        (
                            "computed"
                            if file_path == record["metadata_json_path"]
                            else "deferred-to-ingest"
                        ),
                        int(
                            file_path
                            in {record["archive"], record["metadata_json_path"]}
                        ),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO ingest_graph(work_id, subject, predicate, object)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        str(record["work_directory"]),
                        "contains_file",
                        str(file_path),
                    ),
                )

            connection.executemany(
                """
                INSERT INTO ingest_graph(work_id, subject, predicate, object)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        work_id,
                        str(record["work_directory"]),
                        "contains_archive",
                        str(record["archive"]),
                    ),
                    (
                        work_id,
                        str(record["work_directory"]),
                        "described_by",
                        str(record["metadata_json_path"]),
                    ),
                    (
                        work_id,
                        str(record["archive"]),
                        "paired_with",
                        str(record["metadata_json_path"]),
                    ),
                    (
                        work_id,
                        str(record["work_directory"]),
                        "suggests_r2_root",
                        record["r2_root"],
                    ),
                ],
            )
            if record["cover"]:
                connection.execute(
                    """
                    INSERT INTO ingest_graph(work_id, subject, predicate, object)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        work_id,
                        str(record["work_directory"]),
                        "has_cover",
                        str(record["cover"]),
                    ),
                )

        connection.commit()
        dump_text = "\n".join(connection.iterdump()) + "\n"
    finally:
        connection.close()

    sql_path.write_text(dump_text, encoding="utf-8")
    shutil.copy2(sqlite_path, destination_root / "latest.sqlite3")
    shutil.copy2(sql_path, destination_root / "latest.sql")
    (destination_root / "latest.txt").write_text(
        str(sqlite_path) + "\n", encoding="utf-8"
    )
    return sqlite_path, sql_path, len(records)
