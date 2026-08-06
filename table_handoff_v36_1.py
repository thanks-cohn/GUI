"""Source patch for the default BulkOCR SQL handoff directories in GUI v36.1."""
from __future__ import annotations


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"v36.1 handoff patch target not found: {label}")
    return source.replace(old, new, 1)


_HANDOFF_HELPERS = r'''
import json as _table_handoff_json
import shutil as _table_handoff_shutil
from datetime import datetime as _table_handoff_datetime

TABLE_HANDOFF_ROOT = Path.home() / "tables"
TABLE_HANDOFF_MOST_RECENT = TABLE_HANDOFF_ROOT / "most_recent"
TABLE_HANDOFF_ARCHIVED = TABLE_HANDOFF_ROOT / "archived"


def _table_handoff_unique_destination(parent: Path, name: str) -> Path:
    candidate = parent / name
    if not candidate.exists():
        return candidate
    source = Path(name)
    stem = source.stem or "artifact"
    suffix = source.suffix
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _table_handoff_copy(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"BulkOCR handoff artifact does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        _table_handoff_shutil.copytree(source, destination)
    else:
        _table_handoff_shutil.copy2(source, destination)
    return destination


def _table_handoff_alias(source: Path, alias: Path) -> None:
    if alias == source or alias.exists():
        return
    try:
        alias.hardlink_to(source)
    except OSError:
        _table_handoff_shutil.copy2(source, alias)


def _table_handoff_unique_archive_directory() -> Path:
    stamp = _table_handoff_datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    candidate = TABLE_HANDOFF_ARCHIVED / f"run-{stamp}"
    counter = 2
    while candidate.exists():
        candidate = TABLE_HANDOFF_ARCHIVED / f"run-{stamp}-{counter:02d}"
        counter += 1
    return candidate


def publish_bulkocr_table_handoff(
    sqlite_path: Path,
    sql_path: Path,
    session_path: Path,
    work_count: int,
) -> tuple[Path, Path | None]:
    """Publish one complete handoff and archive the previous handoff directory.

    The new handoff is assembled in a staging directory first. Only after every
    returned BulkOCR artifact has copied successfully is the prior most_recent
    directory moved wholesale into its own timestamped archived/run-* directory.
    """
    sqlite_path = Path(sqlite_path).expanduser().resolve()
    sql_path = Path(sql_path).expanduser().resolve()
    session_path = Path(session_path).expanduser().resolve()

    TABLE_HANDOFF_ROOT.mkdir(parents=True, exist_ok=True)
    TABLE_HANDOFF_ARCHIVED.mkdir(parents=True, exist_ok=True)

    stamp = _table_handoff_datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stage = TABLE_HANDOFF_ROOT / f".most_recent-stage-{stamp}"
    counter = 2
    while stage.exists():
        stage = TABLE_HANDOFF_ROOT / f".most_recent-stage-{stamp}-{counter:02d}"
        counter += 1
    stage.mkdir(parents=False)

    archived_previous: Path | None = None
    restore_source: Path | None = None
    previous_was_file = False
    try:
        copied_sqlite = _table_handoff_copy(
            sqlite_path,
            _table_handoff_unique_destination(stage, sqlite_path.name),
        )
        copied_sql = _table_handoff_copy(
            sql_path,
            _table_handoff_unique_destination(stage, sql_path.name),
        )
        copied_session = _table_handoff_copy(
            session_path,
            _table_handoff_unique_destination(stage, session_path.name or "session"),
        )

        _table_handoff_alias(copied_sqlite, stage / "latest.sqlite3")
        _table_handoff_alias(copied_sql, stage / "latest.sql")

        manifest = {
            "schema": "doku-doujins-table-handoff/v1",
            "published_at": _table_handoff_datetime.now().astimezone().isoformat(),
            "work_count": int(work_count),
            "handoff_directory": str(TABLE_HANDOFF_MOST_RECENT),
            "artifacts": {
                "sqlite": copied_sqlite.name,
                "sql": copied_sql.name,
                "session": copied_session.name,
                "stable_sqlite": "latest.sqlite3",
                "stable_sql": "latest.sql",
            },
            "source_paths": {
                "sqlite": str(sqlite_path),
                "sql": str(sql_path),
                "session": str(session_path),
            },
            "previous_handoff_archived_to": None,
        }

        if TABLE_HANDOFF_MOST_RECENT.exists():
            if TABLE_HANDOFF_MOST_RECENT.is_dir():
                try:
                    has_previous_content = next(TABLE_HANDOFF_MOST_RECENT.iterdir(), None) is not None
                except OSError:
                    has_previous_content = True
                if has_previous_content:
                    archived_previous = _table_handoff_unique_archive_directory()
                    TABLE_HANDOFF_MOST_RECENT.replace(archived_previous)
                    restore_source = archived_previous
                else:
                    TABLE_HANDOFF_MOST_RECENT.rmdir()
            else:
                archived_previous = _table_handoff_unique_archive_directory()
                archived_previous.mkdir(parents=False)
                restore_source = archived_previous / "most_recent"
                TABLE_HANDOFF_MOST_RECENT.replace(restore_source)
                previous_was_file = True

        if archived_previous is not None:
            manifest["previous_handoff_archived_to"] = str(archived_previous)
        (stage / "handoff.json").write_text(
            _table_handoff_json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        stage.replace(TABLE_HANDOFF_MOST_RECENT)
    except Exception:
        if stage.exists():
            if stage.is_dir():
                _table_handoff_shutil.rmtree(stage, ignore_errors=True)
            else:
                try:
                    stage.unlink()
                except OSError:
                    pass
        if restore_source is not None and restore_source.exists() and not TABLE_HANDOFF_MOST_RECENT.exists():
            try:
                restore_source.replace(TABLE_HANDOFF_MOST_RECENT)
                if previous_was_file and archived_previous is not None:
                    archived_previous.rmdir()
            except OSError:
                pass
        raise

    return TABLE_HANDOFF_MOST_RECENT, archived_previous

'''


def apply(source: str) -> str:
    source = _replace_once(
        source,
        "\ndef choose_gui_font_family() -> str:\n",
        _HANDOFF_HELPERS + "\ndef choose_gui_font_family() -> str:\n",
        "table handoff helpers",
    )

    old = r'''                sqlite_path, sql_path, work_count, session_path = result
                combined_loaded_var.set(
                    f"EXPORTED {work_count} WORK{'S' if work_count != 1 else ''} · OCR ENRICHED"
                )
                table_status_var.set(
                    f"Enriched {work_count} ingest-ready work rows: {sqlite_path}"
                )
                messagebox.showinfo(
                    "Table 3 enriched export complete",
                    f"Schema: v2 + BulkOCR evidence\n"
                    f"Works exported: {work_count}\n\n"
                    f"SQLite:\n{sqlite_path}\n\n"
                    f"SQL dump:\n{sql_path}\n\n"
                    f"Resumable session:\n{session_path}\n\n"
                    f"Stable latest copy:\n{EXPORT_FOR_INGEST_ROOT / 'latest.sqlite3'}",
                    parent=root,
                )
'''
    new = r'''                sqlite_path, sql_path, work_count, session_path = result
                try:
                    handoff_path, archived_path = publish_bulkocr_table_handoff(
                        sqlite_path=sqlite_path,
                        sql_path=sql_path,
                        session_path=session_path,
                        work_count=work_count,
                    )
                except Exception as exc:
                    combined_loaded_var.set("EXPORT COMPLETE · HANDOFF ERROR")
                    table_status_var.set(
                        f"BulkOCR completed, but ~/tables handoff failed: {exc}"
                    )
                    messagebox.showerror(
                        "BulkOCR complete, table handoff failed",
                        f"The BulkOCR export itself completed and remains at:\n"
                        f"{sqlite_path.parent}\n\n"
                        f"The automatic handoff to ~/tables/most_recent failed:\n"
                        f"{exc}",
                        parent=root,
                    )
                    return

                combined_loaded_var.set(
                    f"EXPORTED {work_count} WORK{'S' if work_count != 1 else ''} · HANDOFF READY"
                )
                table_status_var.set(
                    f"Published {work_count} ingest-ready work rows to {handoff_path}"
                )
                archived_text = (
                    str(archived_path)
                    if archived_path is not None
                    else "No previous handoff existed."
                )
                messagebox.showinfo(
                    "Table 3 enriched export and handoff complete",
                    f"Schema: v2 + BulkOCR evidence\n"
                    f"Works exported: {work_count}\n\n"
                    f"Current handoff directory:\n{handoff_path}\n\n"
                    f"Stable SQLite:\n{handoff_path / 'latest.sqlite3'}\n\n"
                    f"Stable SQL:\n{handoff_path / 'latest.sql'}\n\n"
                    f"Previous handoff archive:\n{archived_text}\n\n"
                    f"Original export remains at:\n{sqlite_path.parent}",
                    parent=root,
                )
'''
    return _replace_once(
        source,
        old,
        new,
        "successful BulkOCR handoff",
    )
