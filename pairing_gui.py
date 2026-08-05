#!/usr/bin/env python3
"""
Pair CBZ/ZIP archives with JSON metadata using the `title` field.

Titles are Unicode-aware, `naming.title` is preferred, and only text before the first `|` is matched.

Usage
-----
GUI mode (opens automatically when no paths are supplied):
    python3 pair_original_page_name.py

CLI mode:
    python3 pair_original_page_name.py ARCHIVE_INPUT JSON_INPUT

Each input may be either a directory or one file:
  * Archive directory: reads direct-child .cbz and .zip files (non-recursive).
  * Archive file: accepts one .cbz or .zip file.
  * JSON directory: searches recursively for .json files.
  * JSON file: reads that one .json file.

The GUI opens as a side-by-side pairing gallery with a far-right archive-cover
column. Candidate JSON cards can be checkmarked one-per-archive and promoted into
table 2. REASSIGN JSON mode lets a candidate card be dragged to a different archive
block without recalculating its score. DEFAULT CLOSEST MATCH preselects the strongest
available candidate; SELECT MODE preselects only 100% matches and permits manual
choices. Source-URL (preferred) or url is shown directly under each JSON and is
clickable. Dragging supports continuous top/bottom edge auto-scroll, and DESELECT ALL
clears every current candidate checkmark. JSON paths and Source-URL values activate on mouse press so canvas movement cannot cancel them,
so they remain clickable and cannot be mistaken for a drag gesture. A current-run search bar finds any JSON by title or filename and shows the CBZ block it is presently associated with. Select a result and press Enter to return to the main gallery at that exact CBZ block and JSON card. Archive and JSON cards are directly clickable.
When table 2 is active, COMBINE AND STRUCTURE first validates a complete,
duplicate-free move plan, then moves every selected pairing into
~/Combined/<timestamp>/<json-name>/ together with its direct sibling files.
USE LAST LOCATION appends a later table-2 batch to the same combined run and
merges its manifest. File moves and SQL updates roll back together on failure.
Table 3 lists every direct run directory under ~/Combined. The user may display
the existing manifest unchanged or rebuild/overwrite the visible Table 3 by
scanning each pairing directory. By default the safety filter is OFF, so every
direct pairing directory containing at least one CBZ/ZIP and one JSON is scanned.
An optional red/green switch enables direct-child limits (for example, ignore a
directory with more than one direct image or more than one direct CBZ/ZIP).
Nested folders are never counted. Extra direct JSON/sibling files are allowed;
the primary JSON and archive are recovered from the move ledger, old manifest,
directory-name relation, metadata, and deterministic filename ranking.
Every run still creates a timestamped
SQLite database and SQL dump under:
    ~/doku-doujins-pairings/sql-outputs/
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import re
import shutil
import shlex
import threading
import time
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import webbrowser
import uuid
from urllib.parse import urlparse
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_OUTPUT_DIR = Path.home() / "doku-doujins-pairings" / "sql-outputs"
DEFAULT_THRESHOLD = 80.0
SUPPORTED_ARCHIVE_SUFFIXES = {".cbz", ".zip"}
SUPPORTED_IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".webp",
    ".gif", ".bmp", ".dib", ".tif", ".tiff", ".avif", ".jxl",
    ".heic", ".heif", ".svg", ".svgz", ".ico", ".jp2", ".j2k",
}
THUMBNAIL_CACHE_DIR = Path.home() / ".cache" / "doku-doujins-pairings" / "archive-thumbnails"
APP_VERSION = "29.0"
SCHEMA_VERSION = "15"
DEFAULT_COMBINED_ROOT = Path.home() / "Combined"
LAST_COMBINED_POINTER = Path.home() / ".cache" / "doku-doujins-pairings" / "last-combined-location.txt"


def terminal_file_link(path: Path) -> str:
    """Return a file URI that supporting terminals can recognize."""
    return path.expanduser().resolve().as_uri()


@dataclass(frozen=True)
class JsonRecord:
    path: Path
    title: str
    normalized_name: str
    source_url: str


@dataclass(frozen=True)
class ScoredJson:
    record: JsonRecord
    score: float


@dataclass(frozen=True)
class RunResult:
    archive_count: int
    json_count: int
    rejected_json: tuple[tuple[Path, str], ...]
    candidate_rows: int
    exact_rows: int
    output_dir: Path
    database_path: Path
    sql_dump_path: Path


@dataclass(frozen=True)
class CombineResult:
    destination_dir: Path
    pairing_count: int
    moved_file_count: int
    shared_file_count: int
    manifest_database: Path
    manifest_sql: Path
    used_existing_location: bool
    batch_id: str = ""


@dataclass(frozen=True)
class CombinedRebuildResult:
    manifest_database: Path
    manifest_sql: Path
    paired_directories: int
    ignored_directories: int
    ignored_reasons: tuple[tuple[str, str], ...]
    backup_database: Path | None


def unicode_words(value: str) -> str:
    """Normalize text while preserving letters and numbers from every language."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []

    for character in normalized:
        category = unicodedata.category(character)
        if category[0] in {"L", "N", "M"}:
            characters.append(character)
        else:
            characters.append(" ")

    return " ".join("".join(characters).split())


def clean_title(value: str) -> str:
    """Keep only the title portion before the first vertical bar."""
    return value.split("|", 1)[0].strip()


def normalize_name(value: str) -> str:
    """Normalize a filename or title for language-independent comparison."""
    value = value.strip().replace("\\", "/")
    value = value.rsplit("/", 1)[-1]
    return unicode_words(Path(value).stem)


def compact_name(value: str) -> str:
    """Return a Unicode-aware compact form for sibling-name containment."""
    return unicode_words(value).replace(" ", "")


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    return round(
        SequenceMatcher(None, left, right, autojunk=False).ratio() * 100.0,
        2,
    )


def find_naming_title(value: Any) -> str | None:
    """Prefer the explicit `naming.title` field at any nesting level."""
    if isinstance(value, dict):
        naming = value.get("naming")
        if isinstance(naming, dict):
            candidate = naming.get("title")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = find_naming_title(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_naming_title(child)
            if found is not None:
                return found
    return None


def find_any_title(value: Any) -> str | None:
    """Fallback: find any non-empty string field named `title`."""
    if isinstance(value, dict):
        direct = value.get("title")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for child in value.values():
            found = find_any_title(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_any_title(child)
            if found is not None:
                return found
    return None


def find_title(value: Any) -> str | None:
    """Read `naming.title` first, then fall back to any nested `title` field."""
    return find_naming_title(value) or find_any_title(value)



def find_string_field(value: Any, field_name: str) -> str | None:
    """Find the first non-empty string field with an exact key, recursively."""
    if isinstance(value, dict):
        direct = value.get(field_name)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for child in value.values():
            found = find_string_field(child, field_name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_string_field(child, field_name)
            if found is not None:
                return found
    return None


def find_source_url(value: Any) -> str | None:
    """Prefer `Source-URL`; use `url` only when Source-URL is absent."""
    return find_string_field(value, "Source-URL") or find_string_field(value, "url")


@lru_cache(maxsize=4096)
def source_url_from_json_file(path_text: str) -> str:
    """Read Source-URL/url from a JSON file for old databases lacking the field."""
    if not path_text:
        return ""
    try:
        with Path(path_text).expanduser().open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    return find_source_url(payload) or ""

def canonical_existing_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.exists():
        raise ValueError(f"Input does not exist: {expanded}")
    return expanded.resolve()


def deduplicate_paths(paths: Iterable[Path]) -> list[Path]:
    unique: dict[str, Path] = {}
    for path in paths:
        resolved = path.resolve()
        unique[str(resolved)] = resolved
    return sorted(unique.values(), key=lambda item: str(item).casefold())


def discover_archive_files(sources: Sequence[Path]) -> list[Path]:
    """
    Discover archives from files and/or directories.

    Directories are intentionally non-recursive. Explicit files must be .cbz or
    .zip; unsupported files are reported as input errors rather than ignored.
    """
    found: list[Path] = []

    for raw_source in sources:
        source = canonical_existing_path(raw_source)
        if source.is_file():
            if source.suffix.casefold() not in SUPPORTED_ARCHIVE_SUFFIXES:
                raise ValueError(f"Archive input is not a .cbz or .zip file: {source}")
            found.append(source)
            continue

        if not source.is_dir():
            raise ValueError(f"Archive input is neither a file nor directory: {source}")

        try:
            children = source.iterdir()
            found.extend(
                child.resolve()
                for child in children
                if child.is_file()
                and child.suffix.casefold() in SUPPORTED_ARCHIVE_SUFFIXES
            )
        except OSError as exc:
            raise OSError(f"Could not read archive directory {source}: {exc}") from exc

    return deduplicate_paths(found)


def discover_json_paths(sources: Sequence[Path]) -> list[Path]:
    """Discover explicit JSON files and recursively search selected directories."""
    found: list[Path] = []

    for raw_source in sources:
        source = canonical_existing_path(raw_source)
        if source.is_file():
            if source.suffix.casefold() != ".json":
                raise ValueError(f"JSON input is not a .json file: {source}")
            found.append(source)
            continue

        if not source.is_dir():
            raise ValueError(f"JSON input is neither a file nor directory: {source}")

        try:
            found.extend(
                path.resolve()
                for path in source.rglob("*")
                if path.is_file() and path.suffix.casefold() == ".json"
            )
        except OSError as exc:
            raise OSError(f"Could not search JSON directory {source}: {exc}") from exc

    return deduplicate_paths(found)


def discover_json_records(
    sources: Sequence[Path],
) -> tuple[list[JsonRecord], list[tuple[Path, str]]]:
    records: list[JsonRecord] = []
    rejected: list[tuple[Path, str]] = []

    for path in discover_json_paths(sources):
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            rejected.append((path, f"invalid JSON: {exc}"))
            continue

        raw_title = find_title(payload)
        if raw_title is None:
            rejected.append((path, "missing string field title"))
            continue

        title = clean_title(raw_title)
        if not title:
            rejected.append((path, "title has no text before |"))
            continue

        normalized = normalize_name(title)
        if not normalized:
            rejected.append((path, "title normalizes to an empty value"))
            continue

        records.append(
            JsonRecord(
                path=path,
                title=title,
                normalized_name=normalized,
                source_url=find_source_url(payload) or "",
            )
        )

    return records, rejected


def related_files_for_json(json_path: Path) -> list[str]:
    """
    Return files beside the matched JSON whose names contain its basename.

    Example:
      work.json -> work.jpg, work-thumb.webp, backup-work.txt

    The matched JSON itself is excluded.
    """
    json_stem_raw = json_path.stem.casefold()
    json_stem_compact = compact_name(json_path.stem)
    related: list[str] = []

    try:
        siblings: Iterable[Path] = json_path.parent.iterdir()
    except OSError:
        return related

    for sibling in siblings:
        if not sibling.is_file() or sibling.resolve() == json_path.resolve():
            continue

        sibling_stem_raw = sibling.stem.casefold()
        sibling_stem_compact = compact_name(sibling.stem)

        raw_match = bool(json_stem_raw) and json_stem_raw in sibling_stem_raw
        compact_match = (
            bool(json_stem_compact)
            and json_stem_compact in sibling_stem_compact
        )

        if raw_match or compact_match:
            related.append(str(sibling.resolve()))

    related.sort(key=str.casefold)
    return related


def unique_output_paths(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = output_dir / f"pairings-{timestamp}"

    counter = 1
    while base.with_suffix(".sqlite3").exists() or base.with_suffix(".sql").exists():
        base = output_dir / f"pairings-{timestamp}-{counter:02d}"
        counter += 1

    return base.with_suffix(".sqlite3"), base.with_suffix(".sql")


def build_database(
    db_path: Path,
    sql_dump_path: Path,
    archive_files: list[Path],
    json_records: list[JsonRecord],
    threshold: float,
    *,
    auto_promote_exact: bool = True,
) -> tuple[int, int]:
    candidate_row_count = 0
    exact_row_count = 0

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")

        connection.execute(
            """
            CREATE TABLE candidate_pairings (
                archive_file TEXT PRIMARY KEY NOT NULL,
                matching_json_files TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE exact_pairings (
                matched_archive TEXT PRIMARY KEY NOT NULL,
                matched_title TEXT NOT NULL,
                matched_json TEXT UNIQUE NOT NULL,
                related_files TEXT NOT NULL,
                matched_source_url TEXT NOT NULL DEFAULT '',
                match_percent REAL NOT NULL DEFAULT 100.0,
                selection_method TEXT NOT NULL DEFAULT 'automatic_100',
                promoted_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE run_metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO run_metadata (key, value) VALUES (?, ?)",
            [
                ("schema_version", SCHEMA_VERSION),
                ("app_version", APP_VERSION),
                ("threshold", f"{threshold:.2f}"),
                ("created_at", datetime.now().astimezone().isoformat()),
            ],
        )

        auto_promoted_jsons: set[Path] = set()

        for archive_path in archive_files:
            archive_normalized = normalize_name(archive_path.name)
            scored: list[ScoredJson] = []

            for record in json_records:
                score = similarity(archive_normalized, record.normalized_name)
                if score >= threshold:
                    scored.append(ScoredJson(record=record, score=score))

            scored.sort(
                key=lambda item: (-item.score, str(item.record.path).casefold())
            )

            candidate_payload = [
                {
                    "json_file": str(item.record.path),
                    "title": item.record.title,
                    "source_url": item.record.source_url,
                    "match_percent": item.score,
                }
                for item in scored
            ]

            connection.execute(
                """
                INSERT INTO candidate_pairings
                    (archive_file, matching_json_files)
                VALUES (?, ?)
                """,
                (
                    str(archive_path),
                    json.dumps(candidate_payload, ensure_ascii=False),
                ),
            )
            candidate_row_count += 1

            if auto_promote_exact:
                # Table 2 represents one physical pairing per archive. When several
                # JSON files tie at 100%, preselect only the first unused candidate;
                # the others remain visible in Table 1 and can be chosen manually.
                exact_item = next(
                    (
                        item
                        for item in scored
                        if item.score == 100.0
                        and item.record.path.resolve() not in auto_promoted_jsons
                    ),
                    None,
                )
                if exact_item is not None:
                    related_files = related_files_for_json(exact_item.record.path)
                    connection.execute(
                        """
                        INSERT INTO exact_pairings
                            (matched_archive, matched_title, matched_json, related_files,
                             matched_source_url, match_percent, selection_method, promoted_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(archive_path),
                            exact_item.record.title,
                            str(exact_item.record.path),
                            json.dumps(related_files, ensure_ascii=False),
                            exact_item.record.source_url,
                            exact_item.score,
                            "automatic_100",
                            datetime.now().astimezone().isoformat(),
                        ),
                    )
                    auto_promoted_jsons.add(exact_item.record.path.resolve())
                    exact_row_count += 1

        connection.commit()

        with sql_dump_path.open("w", encoding="utf-8") as handle:
            handle.write("-- Generated by pair_original_page_name.py (title-field mode)\n")
            handle.write(f"-- Threshold: {threshold:.2f}%\n")
            for line in connection.iterdump():
                handle.write(line)
                handle.write("\n")
    finally:
        connection.close()

    return candidate_row_count, exact_row_count


def run_pairing(
    archive_sources: Sequence[Path],
    json_sources: Sequence[Path],
    threshold: float = DEFAULT_THRESHOLD,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    auto_promote_exact: bool = True,
) -> RunResult:
    if not archive_sources:
        raise ValueError("Choose at least one archive directory or archive file")
    if not json_sources:
        raise ValueError("Choose at least one JSON directory or JSON file")
    if not 0.0 <= threshold <= 100.0:
        raise ValueError("Threshold must be between 0 and 100")

    archive_files = discover_archive_files(archive_sources)
    json_records, rejected_json = discover_json_records(json_sources)

    if not archive_files:
        raise ValueError("No .cbz or .zip archives were found in the selected input")
    if not json_records:
        raise ValueError(
            "No usable JSON files containing a string field named title were found"
        )

    resolved_output_dir = output_dir.expanduser().resolve()
    db_path, sql_dump_path = unique_output_paths(resolved_output_dir)

    try:
        candidate_rows, exact_rows = build_database(
            db_path=db_path,
            sql_dump_path=sql_dump_path,
            archive_files=archive_files,
            json_records=json_records,
            threshold=threshold,
            auto_promote_exact=auto_promote_exact,
        )
    except Exception:
        # Avoid leaving misleading partial output after a failed run.
        db_path.unlink(missing_ok=True)
        sql_dump_path.unlink(missing_ok=True)
        raise

    return RunResult(
        archive_count=len(archive_files),
        json_count=len(json_records),
        rejected_json=tuple(rejected_json),
        candidate_rows=candidate_rows,
        exact_rows=exact_rows,
        output_dir=resolved_output_dir,
        database_path=db_path,
        sql_dump_path=sql_dump_path,
    )


def print_summary(result: RunResult) -> None:
    print(f"CBZ/ZIP archives read:       {result.archive_count}")
    print(f"usable JSON files read:      {result.json_count}")
    print(f"ignored/rejected JSON files: {len(result.rejected_json)}")
    print(f"candidate table rows:        {result.candidate_rows}")
    print(f"exact pairing rows:          {result.exact_rows}")
    print(f"Output directory:            {terminal_file_link(result.output_dir)}")
    print(f"SQLite database:             {terminal_file_link(result.database_path)}")
    print(f"SQL dump:                    {terminal_file_link(result.sql_dump_path)}")

    if result.rejected_json:
        print("\nIgnored JSON examples:")
        for path, reason in result.rejected_json[:10]:
            print(f"  - {path}: {reason}")
        if len(result.rejected_json) > 10:
            print(f"  ... and {len(result.rejected_json) - 10} more")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pair CBZ/ZIP archives with JSON metadata by title. "
            "Run without paths to open the GUI."
        )
    )
    parser.add_argument(
        "archive_input",
        nargs="?",
        type=Path,
        help="Archive directory (direct children) or one .cbz/.zip file",
    )
    parser.add_argument(
        "json_input",
        nargs="?",
        type=Path,
        help="JSON directory (recursive) or one .json file",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="Minimum candidate similarity percentage (default: 80)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open the graphical chooser even if other options are present",
    )
    parser.add_argument(
        "--combined-root",
        type=Path,
        default=DEFAULT_COMBINED_ROOT,
        help=f"Combined-run root used by Table 3 (default: {DEFAULT_COMBINED_ROOT})",
    )
    parser.add_argument(
        "--combined-max-images",
        type=int,
        default=1,
        help="Table 3 rebuild ignores a pairing directory with more images than this (default: 1)",
    )
    parser.add_argument(
        "--combined-max-archives",
        type=int,
        default=1,
        help="Table 3 rebuild ignores a pairing directory with more CBZ/ZIP files than this (default: 1)",
    )
    parser.add_argument(
        "--combined-enable-limits",
        action="store_true",
        help=(
            "Enable Table 3 direct-child image/archive limits. "
            "Limits are OFF by default, so all pairable direct directories are scanned."
        ),
    )
    args = parser.parse_args(argv)

    if not args.gui and ((args.archive_input is None) != (args.json_input is None)):
        parser.error("supply both ARCHIVE_INPUT and JSON_INPUT, or neither for GUI mode")

    return args


def _launch_first_available(commands: list[list[str]], label: str) -> None:
    """Launch through the first available desktop opener without detaching from DBus."""
    attempted: list[str] = []
    last_error: OSError | None = None

    for command in commands:
        executable = command[0]
        if not shutil.which(executable):
            continue
        attempted.append(executable)
        try:
            print(f"[{label}] {' '.join(command)}", flush=True)
            # Do not use start_new_session=True here. KDE launchers rely on the
            # inherited desktop/DBus environment and should remain in the same
            # login session as this Tk application.
            subprocess.Popen(command)
            return
        except OSError as exc:
            last_error = exc

    if last_error is not None:
        raise OSError(f"Desktop launcher failed after trying {', '.join(attempted)}: {last_error}")
    raise OSError("No compatible desktop launcher was found.")


def _desktop_open_commands(target: str) -> list[list[str]]:
    """Return KDE-first opener commands for a URL or file URI."""
    return [
        ["kioclient6", "exec", target],
        ["kde-open6", target],
        ["kioclient5", "exec", target],
        ["kde-open5", target],
        ["gio", "open", target],
        ["xdg-open", target],
    ]


def open_with_desktop(path: Path) -> None:
    """Open a non-JSON file or directory with KDE first."""
    resolved = path.expanduser().resolve()
    _launch_first_available(_desktop_open_commands(resolved.as_uri()), "OPEN FILE")


def _query_default_browser_desktop_id() -> str:
    """Return the desktop-file ID for the user's configured default browser."""
    queries = [
        ["xdg-settings", "get", "default-web-browser"],
        ["xdg-mime", "query", "default", "x-scheme-handler/https"],
        ["xdg-mime", "query", "default", "x-scheme-handler/http"],
    ]
    for command in queries:
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = result.stdout.strip().splitlines()
        if value and value[0].strip():
            return value[0].strip()
    return ""


def _find_desktop_file(desktop_id: str) -> Path | None:
    if not desktop_id:
        return None
    candidates = [
        Path.home() / ".local/share/applications" / desktop_id,
        Path("/usr/local/share/applications") / desktop_id,
        Path("/usr/share/applications") / desktop_id,
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _desktop_exec_command(desktop_file: Path, target: str) -> list[str] | None:
    """Build an executable command from a browser .desktop file's Exec entry."""
    try:
        lines = desktop_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    exec_line = next((line[5:] for line in lines if line.startswith("Exec=")), "")
    if not exec_line:
        return None

    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        return None

    command: list[str] = []
    inserted_target = False
    target_codes = {"%u", "%U", "%f", "%F"}
    ignored_codes = {"%i", "%c", "%k"}
    for token in tokens:
        if token in target_codes:
            if not inserted_target:
                command.append(target)
                inserted_target = True
            continue
        if token in ignored_codes:
            continue
        token = token.replace("%%", "%")
        if "%" in token:
            # Drop unsupported freedesktop field-code fragments rather than
            # accidentally handing them to the browser.
            token = re.sub(r"%[A-Za-z]", "", token)
        if token:
            command.append(token)

    if not command:
        return None
    if not inserted_target:
        command.append(target)
    return command


def _default_browser_command(target: str) -> list[str] | None:
    """Resolve an explicit command for the configured default web browser."""
    desktop_id = _query_default_browser_desktop_id()
    desktop_file = _find_desktop_file(desktop_id)
    if desktop_file is not None:
        command = _desktop_exec_command(desktop_file, target)
        if command and shutil.which(command[0]):
            return command

    # Practical fallbacks for systems whose desktop association cannot be read.
    for executable in (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "firefox",
        "opera",
        "brave-browser",
    ):
        if shutil.which(executable):
            return [executable, target]
    return None


def _browser_class_hints(command: Sequence[str]) -> list[str]:
    executable = Path(command[0]).name.casefold() if command else ""
    hints: list[str] = []
    if "chrome" in executable:
        hints.extend(["google-chrome", "chrome"])
    elif "chromium" in executable:
        hints.extend(["chromium", "chromium-browser"])
    elif "firefox" in executable:
        hints.append("firefox")
    elif "opera" in executable:
        hints.append("opera")
    elif "brave" in executable:
        hints.extend(["brave-browser", "brave"])
    if executable:
        hints.append(executable)
    return list(dict.fromkeys(hints))


def _activate_browser_later(command: Sequence[str]) -> None:
    """Best-effort KWin activation after the browser has opened/reused a window."""
    if not shutil.which("kdotool"):
        return

    hints = _browser_class_hints(command)

    def activate() -> None:
        time.sleep(0.9)
        for hint in hints:
            try:
                result = subprocess.run(
                    ["kdotool", "search", "--class", hint],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            window_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if not window_ids:
                continue
            window_id = window_ids[-1]
            subprocess.run(["kdotool", "windowactivate", window_id], check=False)
            return

    threading.Thread(target=activate, daemon=True).start()


def open_in_default_browser(target: str, label: str) -> None:
    """Open a web URL or local file URI explicitly in the default browser."""
    command = _default_browser_command(target)
    if command is not None:
        print(f"[{label}] {' '.join(command)}", flush=True)
        subprocess.Popen(command)
        _activate_browser_later(command)
        return

    # Python's browser controller still requests that the browser raise itself.
    if webbrowser.open(target, new=2, autoraise=True):
        print(f"[{label}] Python webbrowser {target}", flush=True)
        return

    raise OSError("No default web browser could be resolved.")


def open_json_in_browser(path: Path) -> None:
    """Always display JSON as a local file URL in the user's default browser."""
    resolved = path.expanduser().resolve()
    open_in_default_browser(resolved.as_uri(), "OPEN JSON IN BROWSER")


def normalize_external_url(url: str) -> str:
    """Return a desktop-openable URL, adding https:// to bare web addresses."""
    value = url.strip()
    if value.startswith("//"):
        return f"https:{value}"
    parsed = urlparse(value)
    if parsed.scheme:
        return value
    if re.match(r"^[^\s/]+\.[^\s/]+(?:[/:?#].*)?$", value):
        return f"https://{value}"
    return value


def open_external_url(url: str) -> None:
    """Open a Source-URL explicitly in the configured default browser."""
    target = normalize_external_url(url)
    if not target:
        raise ValueError("The Source-URL is empty.")
    open_in_default_browser(target, "OPEN URL IN BROWSER")


def reveal_in_file_manager(path: Path) -> None:
    """Reveal a file in Dolphin, preserving the current KDE/DBus session."""
    resolved = path.expanduser().resolve()
    if shutil.which("dolphin"):
        command = ["dolphin", "--select", str(resolved)]
        print(f"[REVEAL FILE] {' '.join(command)}", flush=True)
        subprocess.Popen(command)
        return

    _launch_first_available(
        _desktop_open_commands(resolved.parent.as_uri()),
        "OPEN FOLDER",
    )


def safe_combined_directory_name(value: str) -> str:
    """Return a Unicode-preserving directory name safe on ordinary filesystems."""
    normalized = unicodedata.normalize("NFKC", value).strip()
    cleaned: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if character in {"/", "\\", "\0"} or category.startswith("C"):
            cleaned.append("_")
        else:
            cleaned.append(character)
    result = re.sub(r"\s+", " ", "".join(cleaned)).strip(" .")
    if not result:
        result = "pairing"
    # Leave room for collision suffixes and avoid unwieldy path components.
    return result[:180].rstrip(" .") or "pairing"


def unique_combined_directory(root: Path = DEFAULT_COMBINED_ROOT) -> Path:
    """Create and return a fresh ~/Combined/<timestamp> directory."""
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = root / timestamp
    counter = 1
    while candidate.exists():
        candidate = root / f"{timestamp}-{counter:02d}"
        counter += 1
    candidate.mkdir(parents=True)
    return candidate


def remember_last_combined_directory(path: Path) -> None:
    resolved = path.expanduser().resolve()
    LAST_COMBINED_POINTER.parent.mkdir(parents=True, exist_ok=True)
    LAST_COMBINED_POINTER.write_text(str(resolved) + "\n", encoding="utf-8")


def last_combined_directory(root: Path = DEFAULT_COMBINED_ROOT) -> Path | None:
    """Return the remembered combined run, falling back to the newest directory."""
    try:
        stored = Path(LAST_COMBINED_POINTER.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        stored = None
    if stored is not None and stored.is_dir():
        return stored.resolve()

    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        return None
    directories = [entry.resolve() for entry in resolved_root.iterdir() if entry.is_dir()]
    if not directories:
        return None
    directories.sort(key=lambda item: (item.stat().st_mtime_ns, item.name.casefold()))
    return directories[-1]


def unique_child_directory(parent: Path, requested_name: str, reserved: set[Path]) -> Path:
    base_name = safe_combined_directory_name(requested_name)
    candidate = parent / base_name
    counter = 2
    while candidate.exists() or candidate in reserved:
        candidate = parent / f"{base_name} ({counter})"
        counter += 1
    reserved.add(candidate)
    return candidate


def unique_destination_file(parent: Path, filename: str) -> Path:
    candidate = parent / filename
    if not candidate.exists():
        return candidate
    source = Path(filename)
    stem = source.stem or "file"
    suffix = source.suffix
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def direct_sibling_files(json_path: Path) -> list[Path]:
    """Return every direct file beside a JSON, including the JSON itself."""
    parent = json_path.parent
    files = [entry.resolve() for entry in parent.iterdir() if entry.is_file()]
    files.sort(key=lambda item: item.name.casefold())
    return files


def rewrite_combined_manifest_sql(manifest_database: Path) -> Path:
    sql_path = manifest_database.with_suffix(".sql")
    connection = sqlite3.connect(manifest_database)
    try:
        with sql_path.open("w", encoding="utf-8") as handle:
            handle.write("-- Combined pairing manifest generated by pair_original_page_name.py\n")
            for line in connection.iterdump():
                handle.write(line)
                handle.write("\n")
    finally:
        connection.close()
    return sql_path


def _rollback_combined_moves(
    moved: list[tuple[Path, Path]],
    created_directories: list[Path],
    newly_created_root: Path | None,
) -> list[str]:
    errors: list[str] = []
    for destination, source in reversed(moved):
        try:
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
        except OSError as exc:
            errors.append(f"{destination} -> {source}: {exc}")
    for directory in reversed(created_directories):
        try:
            directory.rmdir()
        except OSError:
            pass
    if newly_created_root is not None:
        try:
            newly_created_root.rmdir()
        except OSError:
            pass
    return errors


def _load_combine_rows(database_path: Path) -> list[dict[str, Any]]:
    """Load Table 2 with enough metadata to resolve legacy duplicate rows."""
    connection = sqlite3.connect(database_path)
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(exact_pairings)")
        }
        title_expr = "matched_title" if "matched_title" in columns else "''"
        source_expr = "matched_source_url" if "matched_source_url" in columns else "''"
        score_expr = "match_percent" if "match_percent" in columns else "100.0"
        method_expr = (
            "selection_method" if "selection_method" in columns else "'automatic_100'"
        )
        promoted_expr = "promoted_at" if "promoted_at" in columns else "''"
        query = (
            f"SELECT rowid, matched_archive, {title_expr}, matched_json, related_files, "
            f"{source_expr}, {score_expr}, {method_expr}, {promoted_expr} "
            "FROM exact_pairings ORDER BY rowid"
        )
        result: list[dict[str, Any]] = []
        for row in connection.execute(query):
            (
                rowid,
                archive_text,
                title,
                json_text,
                raw_related,
                source_url,
                match_percent,
                selection_method,
                promoted_at,
            ) = row
            try:
                related = json.loads(raw_related)
            except (TypeError, json.JSONDecodeError):
                related = []
            try:
                score = float(match_percent)
            except (TypeError, ValueError):
                score = 100.0
            json_string = str(json_text)
            resolved_url = str(source_url or "") or source_url_from_json_file(json_string)
            result.append(
                {
                    "rowid": int(rowid),
                    "archive_text": str(archive_text),
                    "archive_path": Path(str(archive_text)).expanduser().resolve(),
                    "title": str(title),
                    "json_text": json_string,
                    "json_path": Path(json_string).expanduser().resolve(),
                    "related_files": [str(item) for item in related if isinstance(item, str)],
                    "source_url": resolved_url,
                    "match_percent": score,
                    "selection_method": str(selection_method or ""),
                    "promoted_at": str(promoted_at or ""),
                }
            )
        return result
    finally:
        connection.close()



def _validate_combine_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require Table 2 to be an exact, one-row-per-CBZ approved snapshot.

    v21 silently collapsed duplicate archives, which made Table 3 smaller than the
    approved Table 2. v22 never discards approved rows. Duplicate physical sources
    are reported before any move so the user can correct Table 2 explicitly.
    """
    by_archive: dict[Path, list[dict[str, Any]]] = {}
    by_json: dict[Path, list[dict[str, Any]]] = {}
    for row in rows:
        by_archive.setdefault(row["archive_path"], []).append(row)
        by_json.setdefault(row["json_path"], []).append(row)

    conflicts: list[str] = []
    for archive_path, group in by_archive.items():
        if len(group) > 1:
            conflicts.append(f"CBZ/ZIP selected {len(group)} times: {archive_path}")
            for row in group:
                conflicts.append(f"  -> {row['json_path']}")
    for json_path, group in by_json.items():
        if len(group) > 1:
            conflicts.append(f"JSON selected {len(group)} times: {json_path}")
            for row in group:
                conflicts.append(f"  -> {row['archive_path']}")

    if conflicts:
        raise ValueError(
            "Table 2 contains duplicate physical assignments. Nothing was moved.\n\n"
            "v23 will not silently discard approved rows. Keep exactly one JSON per "
            "CBZ and one CBZ per JSON, then try again:\n\n" + "\n".join(conflicts)
        )
    return list(rows)


def _combined_table_columns(connection: sqlite3.Connection, table: str, schema: str = "main") -> set[str]:
    pragma = f"PRAGMA {schema}.table_info({table})"
    return {str(row[1]) for row in connection.execute(pragma)}


def ensure_combined_manifest_schema(connection: sqlite3.Connection, schema: str = "main") -> None:
    """Create/migrate the durable Table 3, batch, and move-ledger schema."""
    prefix = "" if schema == "main" else f"{schema}."
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}combined_pairings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            combined_at TEXT NOT NULL,
            source_database TEXT NOT NULL,
            source_archive TEXT NOT NULL,
            source_json TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            match_percent REAL NOT NULL,
            selection_method TEXT NOT NULL,
            destination_directory TEXT NOT NULL,
            destination_archive TEXT NOT NULL,
            destination_json TEXT NOT NULL,
            moved_files TEXT NOT NULL,
            batch_id TEXT NOT NULL DEFAULT '',
            pair_order INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'combined',
            candidate_snapshot TEXT NOT NULL DEFAULT '',
            exact_snapshot TEXT NOT NULL DEFAULT '',
            recovered INTEGER NOT NULL DEFAULT 0,
            reverse_ready INTEGER NOT NULL DEFAULT 0,
            reversed_at TEXT NOT NULL DEFAULT '',
            recovery_note TEXT NOT NULL DEFAULT '',
            UNIQUE(source_database, source_archive, source_json)
        )
        """
    )
    pair_columns = _combined_table_columns(connection, "combined_pairings", schema)
    additions = {
        "batch_id": "TEXT NOT NULL DEFAULT ''",
        "pair_order": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'combined'",
        "candidate_snapshot": "TEXT NOT NULL DEFAULT ''",
        "exact_snapshot": "TEXT NOT NULL DEFAULT ''",
        "recovered": "INTEGER NOT NULL DEFAULT 0",
        "reverse_ready": "INTEGER NOT NULL DEFAULT 0",
        "reversed_at": "TEXT NOT NULL DEFAULT ''",
        "recovery_note": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in pair_columns:
            connection.execute(
                f"ALTER TABLE {prefix}combined_pairings ADD COLUMN {name} {ddl}"
            )

    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}combined_batches (
            batch_id TEXT PRIMARY KEY NOT NULL,
            combined_at TEXT NOT NULL,
            source_database TEXT NOT NULL,
            destination_root TEXT NOT NULL,
            approved_pairing_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            used_existing_location INTEGER NOT NULL DEFAULT 0,
            reversed_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}combined_file_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            pairing_id INTEGER,
            role TEXT NOT NULL,
            source_path TEXT NOT NULL,
            destination_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'moved',
            moved_at TEXT NOT NULL,
            reversed_at TEXT NOT NULL DEFAULT '',
            UNIQUE(batch_id, source_path)
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}combined_shared_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            combined_at TEXT NOT NULL,
            source_database TEXT NOT NULL,
            source_directory TEXT NOT NULL,
            destination_directory TEXT NOT NULL,
            moved_files TEXT NOT NULL,
            batch_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'combined',
            reversed_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    shared_columns = _combined_table_columns(connection, "combined_shared_files", schema)
    for name, ddl in {
        "batch_id": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'combined'",
        "reversed_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in shared_columns:
            connection.execute(
                f"ALTER TABLE {prefix}combined_shared_files ADD COLUMN {name} {ddl}"
            )
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {prefix}run_metadata (
            key TEXT PRIMARY KEY NOT NULL,
            value TEXT NOT NULL
        )
        """
    )


def _candidate_snapshot_map(database_path: Path, archives: Iterable[str]) -> dict[str, str]:
    wanted = set(archives)
    result: dict[str, str] = {}
    connection = sqlite3.connect(database_path)
    try:
        for archive_file, raw in connection.execute(
            "SELECT archive_file, matching_json_files FROM candidate_pairings"
        ):
            archive_text = str(archive_file)
            if archive_text in wanted:
                result[archive_text] = str(raw or "[]")
    finally:
        connection.close()
    return result


def _exact_snapshot(row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "matched_archive": row["archive_text"],
            "matched_title": row.get("title", ""),
            "matched_json": row["json_text"],
            "related_files": row.get("related_files", []),
            "matched_source_url": row.get("source_url", ""),
            "match_percent": row.get("match_percent", 0.0),
            "selection_method": row.get("selection_method", ""),
            "promoted_at": row.get("promoted_at", ""),
        },
        ensure_ascii=False,
    )


def _read_json_title_url(path: Path) -> tuple[str, str]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return path.stem, ""
    title = clean_title(find_title(payload) or path.stem)
    return title, find_source_url(payload) or ""


def _historical_pair_for_files(archive_path: Path | None, json_path: Path) -> dict[str, Any] | None:
    """Find original paths/metadata in older pairing databases by filenames."""
    archive_name = archive_path.name.casefold() if archive_path else ""
    json_name = json_path.name.casefold()
    databases = list_output_databases(DEFAULT_OUTPUT_DIR)
    for database in reversed(databases):
        try:
            rows = load_exact_table(database)
        except (OSError, sqlite3.Error):
            continue
        for archive, title, json_file, related, source_url, score, method in rows:
            if Path(json_file).name.casefold() != json_name:
                continue
            if archive_name and Path(archive).name.casefold() != archive_name:
                continue
            return {
                "source_database": str(database),
                "source_archive": archive,
                "source_json": json_file,
                "title": title,
                "source_url": source_url,
                "match_percent": score,
                "selection_method": method,
                "related_files": related,
            }
    # Candidate tables often survive in another timestamped run even when Table 2
    # was cleared in the run that performed the combine.
    for database in reversed(databases):
        try:
            candidate_rows = load_candidate_table(database)
        except (OSError, sqlite3.Error):
            continue
        for archive, matches in candidate_rows:
            if archive_name and Path(archive).name.casefold() != archive_name:
                continue
            for item in matches:
                candidate_json = str(item.get("json_file", ""))
                if Path(candidate_json).name.casefold() == json_name:
                    return {
                        "source_database": str(database),
                        "source_archive": archive,
                        "source_json": candidate_json,
                        "title": str(item.get("title", "")),
                        "source_url": str(item.get("source_url", "")),
                        "match_percent": float(item.get("match_percent", 0.0) or 0.0),
                        "selection_method": "recovered_from_candidate_history",
                        "related_files": [],
                    }
    return None


def repair_combined_manifest_from_disk(manifest_database: Path) -> int:
    """Repair incomplete v19-v21 Table 3 manifests from actual combined folders.

    Every direct pairing directory containing JSON files becomes visible in Table 3.
    Historical pairing databases are consulted to recover original source paths. The
    files are never moved by this repair operation.
    """
    manifest_database = manifest_database.expanduser().resolve()
    root = manifest_database.parent
    if not root.is_dir():
        return 0
    connection = sqlite3.connect(manifest_database)
    inserted = 0
    try:
        ensure_combined_manifest_schema(connection)
        existing_jsons = {
            str(Path(str(row[0])).expanduser().resolve())
            for row in connection.execute(
                "SELECT destination_json FROM combined_pairings WHERE status != 'reversed'"
            )
            if row[0]
        }
        existing_dirs = {
            str(Path(str(row[0])).expanduser().resolve())
            for row in connection.execute(
                "SELECT destination_directory FROM combined_pairings WHERE status != 'reversed'"
            )
            if row[0]
        }
        recovered_batch = f"recovered-{root.name}"
        now = datetime.now().astimezone().isoformat()
        connection.execute(
            """
            INSERT OR IGNORE INTO combined_batches
                (batch_id, combined_at, source_database, destination_root,
                 approved_pairing_count, status, used_existing_location)
            VALUES (?, ?, ?, ?, 0, 'combined', 0)
            """,
            (recovered_batch, now, "recovered-from-disk", str(root)),
        )
        pair_order_row = connection.execute(
            "SELECT COALESCE(MAX(pair_order), 0) FROM combined_pairings"
        ).fetchone()
        pair_order = int(pair_order_row[0] or 0)

        # Adopt any legacy manifest rows that v19-v21 wrote before the durable
        # batch/ledger schema existed. This makes the already-visible row part of
        # the same recovered batch as rows discovered from the filesystem.
        legacy_rows = list(
            connection.execute(
                """
                SELECT id, source_database, source_archive, source_json, title,
                       source_url, match_percent, selection_method,
                       destination_directory, destination_archive, destination_json,
                       moved_files, candidate_snapshot, exact_snapshot
                FROM combined_pairings
                WHERE COALESCE(batch_id, '') = ''
                  AND COALESCE(status, 'combined') != 'reversed'
                ORDER BY id
                """
            )
        )
        for legacy in legacy_rows:
            (
                pairing_id,
                source_database,
                source_archive,
                source_json,
                title,
                source_url,
                match_percent,
                selection_method,
                destination_directory,
                destination_archive,
                destination_json,
                raw_moved_files,
                candidate_snapshot,
                exact_snapshot,
            ) = legacy
            pair_order += 1
            if not candidate_snapshot and source_database and Path(str(source_database)).is_file():
                candidate_snapshot = _candidate_snapshot_map(
                    Path(str(source_database)), [str(source_archive)]
                ).get(str(source_archive), "[]")
            if not exact_snapshot:
                exact_snapshot = json.dumps(
                    {
                        "matched_archive": str(source_archive or ""),
                        "matched_title": str(title or ""),
                        "matched_json": str(source_json or ""),
                        "related_files": [],
                        "matched_source_url": str(source_url or ""),
                        "match_percent": float(match_percent or 0.0),
                        "selection_method": str(selection_method or "recovered_legacy_manifest"),
                        "promoted_at": "",
                    },
                    ensure_ascii=False,
                )
            destination_archive_path = Path(str(destination_archive)).expanduser() if destination_archive else None
            destination_json_path = Path(str(destination_json)).expanduser() if destination_json else None
            reverse_ready = int(
                bool(source_archive)
                and bool(source_json)
                and destination_archive_path is not None
                and destination_json_path is not None
                and Path(str(source_archive)).expanduser().resolve() != destination_archive_path.resolve()
                and Path(str(source_json)).expanduser().resolve() != destination_json_path.resolve()
            )
            connection.execute(
                """
                UPDATE combined_pairings
                SET batch_id=?, pair_order=?, status='combined',
                    candidate_snapshot=?, exact_snapshot=?, recovered=1,
                    reverse_ready=?, recovery_note=?
                WHERE id=?
                """,
                (
                    recovered_batch,
                    pair_order,
                    str(candidate_snapshot or "[]"),
                    str(exact_snapshot or ""),
                    reverse_ready,
                    "Adopted from the legacy manifest and reconciled with Combined files",
                    int(pairing_id),
                ),
            )
            try:
                moved_files = json.loads(raw_moved_files or "[]")
            except (TypeError, json.JSONDecodeError):
                moved_files = []
            directory_path = Path(str(destination_directory)).expanduser()
            if directory_path.is_dir():
                actual_files = [
                    str(item.resolve())
                    for item in sorted(directory_path.iterdir(), key=lambda item: item.name.casefold())
                    if item.is_file()
                ]
                if actual_files:
                    moved_files = actual_files
                    connection.execute(
                        "UPDATE combined_pairings SET moved_files=? WHERE id=?",
                        (json.dumps(moved_files, ensure_ascii=False), int(pairing_id)),
                    )
            for moved_text in moved_files:
                moved = Path(str(moved_text)).expanduser().resolve()
                if destination_archive_path and moved == destination_archive_path.resolve():
                    role = "archive"
                    source = str(source_archive or "")
                elif destination_json_path and moved == destination_json_path.resolve():
                    role = "json"
                    source = str(source_json or "")
                else:
                    role = "sibling"
                    source = (
                        str(Path(str(source_json)).expanduser().parent / moved.name)
                        if source_json
                        else ""
                    )
                if source:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO combined_file_moves
                            (batch_id, pairing_id, role, source_path,
                             destination_path, status, moved_at)
                        VALUES (?, ?, ?, ?, ?, 'moved', ?)
                        """,
                        (recovered_batch, int(pairing_id), role, source, str(moved), now),
                    )

        for directory in sorted(
            (item for item in root.iterdir() if item.is_dir() and not item.name.startswith("_shared_")),
            key=lambda item: item.name.casefold(),
        ):
            files = sorted(
                (item.resolve() for item in directory.iterdir() if item.is_file()),
                key=lambda item: item.name.casefold(),
            )
            archives = [item for item in files if item.suffix.casefold() in SUPPORTED_ARCHIVE_SUFFIXES]
            jsons = [item for item in files if item.suffix.casefold() == ".json"]
            if not jsons:
                continue
            archive = archives[0] if archives else None
            for json_path in jsons:
                destination_json = str(json_path.resolve())
                if destination_json in existing_jsons:
                    continue
                # If an old row points at the directory but at a now-missing JSON,
                # do not let that single stale row hide the other actual JSON files.
                history = _historical_pair_for_files(archive, json_path)
                title, source_url = _read_json_title_url(json_path)
                source_database = str(history.get("source_database")) if history else "recovered-from-disk"
                source_archive = str(history.get("source_archive")) if history else (str(archive) if archive else "")
                source_json = str(history.get("source_json")) if history else destination_json
                if history:
                    title = str(history.get("title") or title)
                    source_url = str(history.get("source_url") or source_url)
                reverse_ready = int(
                    bool(history)
                    and bool(source_archive)
                    and Path(source_archive).expanduser().resolve() != (archive.resolve() if archive else Path(source_archive).expanduser().resolve())
                    and Path(source_json).expanduser().resolve() != json_path.resolve()
                )
                pair_order += 1
                moved_files = [str(item) for item in files]
                exact_snapshot = json.dumps(
                    {
                        "matched_archive": source_archive,
                        "matched_title": title,
                        "matched_json": source_json,
                        "related_files": history.get("related_files", []) if history else [],
                        "matched_source_url": source_url,
                        "match_percent": float(history.get("match_percent", 0.0)) if history else 0.0,
                        "selection_method": str(history.get("selection_method", "recovered_from_disk")) if history else "recovered_from_disk",
                        "promoted_at": "",
                    },
                    ensure_ascii=False,
                )
                candidate_snapshot = "[]"
                if source_database and Path(source_database).is_file() and source_archive:
                    candidate_snapshot = _candidate_snapshot_map(
                        Path(source_database), [source_archive]
                    ).get(source_archive, "[]")
                cursor = connection.execute(
                    """
                    INSERT INTO combined_pairings
                        (combined_at, source_database, source_archive, source_json,
                         title, source_url, match_percent, selection_method,
                         destination_directory, destination_archive, destination_json,
                         moved_files, batch_id, pair_order, status,
                         candidate_snapshot, exact_snapshot, recovered,
                         reverse_ready, recovery_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'combined',
                            ?, ?, 1, ?, ?)
                    """,
                    (
                        now,
                        source_database,
                        source_archive,
                        source_json,
                        title,
                        source_url,
                        float(history.get("match_percent", 0.0)) if history else 0.0,
                        str(history.get("selection_method", "recovered_from_disk")) if history else "recovered_from_disk",
                        str(directory.resolve()),
                        str(archive.resolve()) if archive else "",
                        destination_json,
                        json.dumps(moved_files, ensure_ascii=False),
                        recovered_batch,
                        pair_order,
                        candidate_snapshot,
                        exact_snapshot,
                        reverse_ready,
                        "Recovered from files already present in the Combined directory",
                    ),
                )
                pairing_id = int(cursor.lastrowid)
                for moved in files:
                    role = "archive" if archive and moved == archive else "json" if moved == json_path else "sibling"
                    if history:
                        if role == "archive":
                            source = source_archive
                        elif role == "json":
                            source = source_json
                        else:
                            source = str(Path(source_json).expanduser().parent / moved.name)
                    else:
                        source = ""
                    if source:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO combined_file_moves
                                (batch_id, pairing_id, role, source_path,
                                 destination_path, status, moved_at)
                            VALUES (?, ?, ?, ?, ?, 'moved', ?)
                            """,
                            (recovered_batch, pairing_id, role, source, str(moved), now),
                        )
                existing_jsons.add(destination_json)
                inserted += 1

        recovered_source_databases = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source_database FROM combined_pairings "
                "WHERE batch_id=? AND source_database NOT IN ('', 'recovered-from-disk')",
                (recovered_batch,),
            )
            if row[0] and Path(str(row[0])).expanduser().is_file()
        }
        if len(recovered_source_databases) == 1:
            connection.execute(
                "UPDATE combined_batches SET source_database=? WHERE batch_id=?",
                (next(iter(recovered_source_databases)), recovered_batch),
            )

        connection.execute(
            "UPDATE combined_batches SET approved_pairing_count = "
            "(SELECT COUNT(*) FROM combined_pairings WHERE batch_id = ?) "
            "WHERE batch_id = ?",
            (recovered_batch, recovered_batch),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO run_metadata (key, value) VALUES (?, ?)",
            [
                ("app_version", APP_VERSION),
                ("destination", str(root)),
                ("updated_at", now),
                ("last_repair_added", str(inserted)),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    if inserted:
        try:
            rewrite_combined_manifest_sql(manifest_database)
        except OSError:
            pass
        print(f"[TABLE 3 REPAIR] Added {inserted} missing pairing row(s) from {root}", flush=True)
    return inserted


def reverse_latest_combined_batch(manifest_database: Path) -> tuple[str, int]:
    """Reverse the newest fully-ledgered batch and restore its Table 2 rows."""
    manifest_database = manifest_database.expanduser().resolve()
    connection = sqlite3.connect(manifest_database)
    try:
        ensure_combined_manifest_schema(connection)
        batch = connection.execute(
            """
            SELECT batch_id, source_database
            FROM combined_batches
            WHERE status = 'combined'
            ORDER BY combined_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if not batch:
            raise ValueError("This combined output has no active batch to reverse.")
        batch_id, source_database = str(batch[0]), str(batch[1])
        moves = list(
            connection.execute(
                """
                SELECT id, source_path, destination_path
                FROM combined_file_moves
                WHERE batch_id = ? AND status = 'moved'
                ORDER BY id DESC
                """,
                (batch_id,),
            )
        )
        if not moves:
            raise ValueError(
                "The newest batch has no complete source→destination move ledger. "
                "It can be displayed, but it cannot be safely reversed automatically."
            )
        unresolved = [row for row in moves if not str(row[1]).strip()]
        if unresolved:
            raise ValueError("The newest batch contains unresolved original paths and is not safely reversible.")
        conflicts: list[str] = []
        for _move_id, source_text, destination_text in moves:
            source = Path(str(source_text)).expanduser()
            destination = Path(str(destination_text)).expanduser()
            if not destination.is_file():
                conflicts.append(f"Missing combined file: {destination}")
            if source.exists():
                conflicts.append(f"Original location is occupied: {source}")
        if conflicts:
            raise ValueError("Reverse preflight failed; nothing was moved:\n\n" + "\n".join(conflicts))

        pairing_rows = list(
            connection.execute(
                """
                SELECT candidate_snapshot, exact_snapshot
                FROM combined_pairings
                WHERE batch_id = ? AND status = 'combined'
                ORDER BY pair_order, id
                """,
                (batch_id,),
            )
        )
    finally:
        connection.close()

    source_db = Path(source_database).expanduser().resolve()
    if not source_db.is_file():
        raise ValueError(f"The original pairing database is missing:\n{source_db}")

    completed: list[tuple[Path, Path]] = []
    db_connection: sqlite3.Connection | None = None
    try:
        for _move_id, source_text, destination_text in moves:
            source = Path(str(source_text)).expanduser().resolve()
            destination = Path(str(destination_text)).expanduser().resolve()
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
            completed.append((source, destination))

        db_connection = sqlite3.connect(manifest_database)
        db_connection.execute("BEGIN IMMEDIATE")
        ensure_combined_manifest_schema(db_connection)
        db_connection.execute("ATTACH DATABASE ? AS source_pairings", (str(source_db),))
        now = datetime.now().astimezone().isoformat()
        for candidate_raw, exact_raw in pairing_rows:
            try:
                exact = json.loads(exact_raw or "{}")
            except (TypeError, json.JSONDecodeError):
                exact = {}
            archive = str(exact.get("matched_archive", ""))
            json_file = str(exact.get("matched_json", ""))
            if not archive or not json_file:
                raise ValueError("A Table 2 snapshot is incomplete; reversal was canceled.")
            db_connection.execute(
                "INSERT OR REPLACE INTO source_pairings.candidate_pairings "
                "(archive_file, matching_json_files) VALUES (?, ?)",
                (archive, str(candidate_raw or "[]")),
            )
            db_connection.execute(
                "DELETE FROM source_pairings.exact_pairings WHERE matched_archive = ? OR matched_json = ?",
                (archive, json_file),
            )
            db_connection.execute(
                """
                INSERT INTO source_pairings.exact_pairings
                    (matched_archive, matched_title, matched_json, related_files,
                     matched_source_url, match_percent, selection_method, promoted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive,
                    str(exact.get("matched_title", "")),
                    json_file,
                    json.dumps(exact.get("related_files", []), ensure_ascii=False),
                    str(exact.get("matched_source_url", "")),
                    float(exact.get("match_percent", 0.0) or 0.0),
                    str(exact.get("selection_method", "restored_from_combined")),
                    str(exact.get("promoted_at", "")),
                ),
            )
        db_connection.execute(
            "UPDATE combined_file_moves SET status='reversed', reversed_at=? WHERE batch_id=?",
            (now, batch_id),
        )
        db_connection.execute(
            "UPDATE combined_pairings SET status='reversed', reversed_at=? WHERE batch_id=?",
            (now, batch_id),
        )
        db_connection.execute(
            "UPDATE combined_shared_files SET status='reversed', reversed_at=? WHERE batch_id=?",
            (now, batch_id),
        )
        db_connection.execute(
            "UPDATE combined_batches SET status='reversed', reversed_at=? WHERE batch_id=?",
            (now, batch_id),
        )
        db_connection.commit()
        try:
            db_connection.execute("DETACH DATABASE source_pairings")
        except sqlite3.Error:
            pass
        db_connection.close()
        db_connection = None
    except Exception:
        if db_connection is not None:
            try:
                db_connection.rollback()
                db_connection.close()
            except sqlite3.Error:
                pass
        # Put every file back into Combined if DB restoration fails.
        for source, destination in reversed(completed):
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.exists() and not destination.exists():
                    shutil.move(str(source), str(destination))
            except OSError:
                pass
        raise

    for directory in sorted(
        (item for item in manifest_database.parent.iterdir() if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    rewrite_sql_dump(source_db)
    rewrite_combined_manifest_sql(manifest_database)
    return batch_id, len(moves)


def _combine_row_priority(row: dict[str, Any]) -> tuple[int, str, float, int]:
    """Prefer an explicit/manual choice, then the newest and strongest row."""
    method = str(row.get("selection_method", "")).casefold()
    explicit = 0 if method in {"", "automatic_100", "legacy"} else 1
    return (
        explicit,
        str(row.get("promoted_at", "")),
        float(row.get("match_percent", 0.0)),
        int(row.get("rowid", 0)),
    )


def _deduplicate_combine_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse legacy duplicate rows so one archive is moved exactly once."""
    by_archive: dict[Path, list[dict[str, Any]]] = {}
    for row in rows:
        by_archive.setdefault(row["archive_path"], []).append(row)

    winners: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    for archive_path in sorted(by_archive, key=lambda item: str(item).casefold()):
        group = by_archive[archive_path]
        group.sort(key=_combine_row_priority, reverse=True)
        winners.append(group[0])
        discarded.extend(group[1:])

    # One JSON is also one physical source file. Unlike duplicate archive rows,
    # assigning the same JSON to two different archives is ambiguous and must be
    # corrected before any move begins.
    by_json: dict[Path, list[dict[str, Any]]] = {}
    for row in winners:
        by_json.setdefault(row["json_path"], []).append(row)
    conflicts = {path: group for path, group in by_json.items() if len(group) > 1}
    if conflicts:
        lines = [
            "The same JSON is selected for more than one CBZ/ZIP.",
            "Nothing was moved. Reassign or replace one of these Table 2 rows:",
            "",
        ]
        for json_path, group in conflicts.items():
            lines.append(str(json_path))
            for row in group:
                lines.append(f"  -> {row['archive_path']}")
            lines.append("")
        raise ValueError("\n".join(lines).rstrip())

    return winners, discarded


def _planned_unique_destination(
    parent: Path,
    filename: str,
    reserved: set[Path],
) -> Path:
    """Choose a collision-free destination without creating or moving anything."""
    candidate = parent / filename
    source_name = Path(filename)
    stem = source_name.stem or "file"
    suffix = source_name.suffix
    counter = 2
    while candidate.exists() or candidate in reserved:
        candidate = parent / f"{stem} ({counter}){suffix}"
        counter += 1
    reserved.add(candidate)
    return candidate


def _next_combined_directory_path(root: Path = DEFAULT_COMBINED_ROOT) -> Path:
    """Return a fresh timestamp destination path without creating it yet."""
    resolved_root = root.expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = resolved_root / timestamp
    counter = 1
    while candidate.exists():
        candidate = resolved_root / f"{timestamp}-{counter:02d}"
        counter += 1
    return candidate


def _remove_failed_manifest_artifacts(manifest_database: Path, manifest_sql: Path) -> None:
    for path in (
        manifest_sql,
        manifest_database,
        Path(str(manifest_database) + "-journal"),
        Path(str(manifest_database) + "-wal"),
        Path(str(manifest_database) + "-shm"),
    ):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def combine_and_structure_pairings(
    database_path: Path,
    *,
    destination_dir: Path | None = None,
    combined_root: Path = DEFAULT_COMBINED_ROOT,
) -> CombineResult:
    """Validate the entire batch, then move files and update SQL transactionally.

    Legacy databases may contain several Table 2 rows for one archive because older
    versions auto-promoted every 100% tie. This function deterministically keeps the
    newest explicit choice (or strongest/newest automatic choice) and moves that
    physical archive once. Every source and destination is validated before the first
    file is touched. On any move or SQL failure, completed moves are reversed and
    Table 2 remains intact.
    """
    database_path = database_path.expanduser().resolve()
    raw_rows = _load_combine_rows(database_path)
    if not raw_rows:
        raise ValueError("Table 2 is empty. Promote at least one pairing first.")

    rows = _validate_combine_rows(raw_rows)
    discarded_duplicate_archive_rows: list[dict[str, Any]] = []
    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    candidate_snapshots = _candidate_snapshot_map(
        database_path, (row["archive_text"] for row in rows)
    )

    used_existing = destination_dir is not None
    if destination_dir is None:
        target_root = _next_combined_directory_path(combined_root)
        newly_created_root: Path | None = target_root
    else:
        target_root = destination_dir.expanduser().resolve()
        newly_created_root = None
        if not target_root.is_dir():
            raise ValueError(f"The last combined location no longer exists:\n{target_root}")

    # ----------------------------- PRE-FLIGHT -----------------------------
    # No source file is moved and no output directory is created above this line.
    missing: list[Path] = []
    for row in rows:
        if not row["archive_path"].is_file():
            missing.append(row["archive_path"])
        if not row["json_path"].is_file():
            missing.append(row["json_path"])
    if missing:
        unique_missing = sorted(set(missing), key=lambda item: str(item).casefold())
        raise FileNotFoundError(
            "Preflight found missing source files. Nothing was moved:\n\n"
            + "\n".join(str(path) for path in unique_missing)
        )

    reserved_directories: set[Path] = set()
    plans: list[dict[str, Any]] = []
    plans_by_parent: dict[Path, list[dict[str, Any]]] = {}
    selected_archive_paths = {row["archive_path"] for row in rows}

    for row in rows:
        pairing_directory = unique_child_directory(
            target_root,
            row["json_path"].stem,
            reserved_directories,
        )
        plan = dict(row)
        plan.update(
            {
                "pairing_directory": pairing_directory,
                "source_paths": [row["archive_path"], row["json_path"]],
                "candidate_snapshot": candidate_snapshots.get(row["archive_text"], "[]"),
                "exact_snapshot": _exact_snapshot(row),
                "batch_id": batch_id,
                "pair_order": len(plans) + 1,
            }
        )
        plans.append(plan)
        plans_by_parent.setdefault(row["json_path"].parent.resolve(), []).append(plan)

    shared_plans: list[dict[str, Any]] = []
    globally_assigned: set[Path] = {
        source.resolve()
        for plan in plans
        for source in plan["source_paths"]
    }

    for source_parent, parent_plans in plans_by_parent.items():
        try:
            all_files = sorted(
                (entry.resolve() for entry in source_parent.iterdir() if entry.is_file()),
                key=lambda item: item.name.casefold(),
            )
        except OSError as exc:
            raise OSError(
                f"Could not inspect JSON sibling directory during preflight:\n{source_parent}\n\n{exc}"
            ) from exc

        if len(parent_plans) == 1:
            plan = parent_plans[0]
            for source in all_files:
                if source in selected_archive_paths and source != plan["archive_path"]:
                    continue
                if source not in globally_assigned:
                    plan["source_paths"].append(source)
                    globally_assigned.add(source)
            continue

        ambiguous_sources: list[Path] = []
        selected_jsons = {plan["json_path"] for plan in parent_plans}
        plan_keys: list[tuple[dict[str, Any], tuple[str, ...]]] = []
        for plan in parent_plans:
            keys = tuple(
                key
                for key in {
                    compact_name(plan["json_path"].stem),
                    compact_name(str(plan.get("title", ""))),
                }
                if key
            )
            plan_keys.append((plan, keys))

        for source in all_files:
            if source in selected_jsons or source in globally_assigned:
                continue
            if source in selected_archive_paths:
                continue
            source_key = compact_name(source.stem)
            matching_plans: list[dict[str, Any]] = []
            if source_key:
                for plan, keys in plan_keys:
                    if any(
                        key in source_key or source_key in key
                        for key in keys
                        if key
                    ):
                        matching_plans.append(plan)
            if len(matching_plans) == 1:
                matching_plans[0]["source_paths"].append(source)
            else:
                ambiguous_sources.append(source)
            globally_assigned.add(source)

        if ambiguous_sources:
            shared_directory = unique_child_directory(
                target_root,
                f"_shared_{source_parent.name or 'files'}",
                reserved_directories,
            )
            shared_plans.append(
                {
                    "source_directory": source_parent,
                    "shared_directory": shared_directory,
                    "source_paths": ambiguous_sources,
                }
            )

    # Construct one immutable source->destination move plan. A physical source may
    # appear exactly once; this is the invariant v20 violated.
    reserved_destinations: set[Path] = set()
    source_owners: dict[Path, str] = {}
    move_entries: list[dict[str, Any]] = []

    def add_source(plan: dict[str, Any], source: Path, kind: str, role: str) -> None:
        resolved_source = source.expanduser().resolve()
        owner = str(plan.get("archive_path") or plan.get("source_directory") or "shared")
        previous_owner = source_owners.get(resolved_source)
        if previous_owner is not None:
            raise ValueError(
                "Preflight detected one physical file scheduled for two moves. "
                "Nothing was moved:\n\n"
                f"{resolved_source}\n\nFirst owner: {previous_owner}\nSecond owner: {owner}"
            )
        if not resolved_source.is_file():
            raise FileNotFoundError(
                f"Source disappeared during preflight. Nothing was moved:\n{resolved_source}"
            )
        source_owners[resolved_source] = owner
        destination_parent = (
            plan["pairing_directory"] if kind == "pair" else plan["shared_directory"]
        )
        destination = _planned_unique_destination(
            destination_parent,
            resolved_source.name,
            reserved_destinations,
        )
        move_entries.append(
            {
                "source": resolved_source,
                "destination": destination,
                "plan": plan,
                "kind": kind,
                "role": role,
            }
        )

    for plan in plans:
        # Preserve order while removing accidental duplicates inside one plan.
        seen_inside: set[Path] = set()
        for source in plan["source_paths"]:
            resolved = source.resolve()
            if resolved in seen_inside:
                continue
            seen_inside.add(resolved)
            if resolved == plan["archive_path"]:
                role = "archive"
            elif resolved == plan["json_path"]:
                role = "json"
            else:
                role = "sibling"
            add_source(plan, resolved, "pair", role)

    for shared_plan in shared_plans:
        for source in shared_plan["source_paths"]:
            add_source(shared_plan, source, "shared", "shared")

    # Ensure output parents can be created and no source sits inside the destination.
    for source in source_owners:
        try:
            source.relative_to(target_root)
        except ValueError:
            continue
        raise ValueError(
            "A source file is already inside the selected Combined destination. "
            "Nothing was moved:\n" + str(source)
        )

    manifest_database = target_root / "combined-pairings.sqlite3"
    manifest_sql = target_root / "combined-pairings.sql"
    manifest_preexisted = manifest_database.exists()
    manifest_sql_preexisted = manifest_sql.exists()

    # ------------------------------- APPLY -------------------------------
    moved_pairs: list[tuple[Path, Path]] = []
    created_directories: list[Path] = []
    source_connection: sqlite3.Connection | None = None
    try:
        if newly_created_root is not None:
            target_root.parent.mkdir(parents=True, exist_ok=True)
            target_root.mkdir(parents=False, exist_ok=False)

        for directory in [
            *(plan["pairing_directory"] for plan in plans),
            *(plan["shared_directory"] for plan in shared_plans),
        ]:
            directory.mkdir(parents=False, exist_ok=False)
            created_directories.append(directory)

        for plan in plans:
            plan["moved_files"] = []
            plan["destination_archive"] = ""
            plan["destination_json"] = ""
        for shared_plan in shared_plans:
            shared_plan["moved_files"] = []

        for entry in move_entries:
            source: Path = entry["source"]
            destination: Path = entry["destination"]
            plan: dict[str, Any] = entry["plan"]
            if not source.is_file():
                raise FileNotFoundError(
                    "A source changed after preflight and before its move:\n" + str(source)
                )
            shutil.move(str(source), str(destination))
            entry["destination_actual"] = destination.resolve()
            moved_pairs.append((destination, source))
            plan["moved_files"].append(str(destination.resolve()))
            if entry["kind"] == "pair":
                if source == plan["archive_path"]:
                    plan["destination_archive"] = str(destination.resolve())
                if source == plan["json_path"]:
                    plan["destination_json"] = str(destination.resolve())

        # One transaction spans the source pairing DB and attached combined manifest.
        source_connection = sqlite3.connect(database_path)
        source_connection.execute("PRAGMA foreign_keys = ON")
        source_connection.execute("BEGIN IMMEDIATE")
        ensure_promotion_columns(source_connection)
        source_connection.execute(
            "ATTACH DATABASE ? AS combined_manifest",
            (str(manifest_database),),
        )
        ensure_combined_manifest_schema(source_connection, "combined_manifest")

        now = datetime.now().astimezone().isoformat()
        source_connection.execute(
            """
            INSERT INTO combined_manifest.combined_batches
                (batch_id, combined_at, source_database, destination_root,
                 approved_pairing_count, status, used_existing_location)
            VALUES (?, ?, ?, ?, ?, 'combined', ?)
            """,
            (
                batch_id,
                now,
                str(database_path),
                str(target_root.resolve()),
                len(plans),
                int(used_existing),
            ),
        )
        for plan in plans:
            cursor = source_connection.execute(
                """
                INSERT INTO combined_manifest.combined_pairings
                    (combined_at, source_database, source_archive, source_json,
                     title, source_url, match_percent, selection_method,
                     destination_directory, destination_archive, destination_json,
                     moved_files, batch_id, pair_order, status,
                     candidate_snapshot, exact_snapshot, recovered,
                     reverse_ready, recovery_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'combined',
                        ?, ?, 0, 1, '')
                """,
                (
                    now,
                    str(database_path),
                    plan["archive_text"],
                    plan["json_text"],
                    plan["title"],
                    plan["source_url"],
                    plan["match_percent"],
                    plan["selection_method"],
                    str(plan["pairing_directory"].resolve()),
                    plan["destination_archive"],
                    plan["destination_json"],
                    json.dumps(plan["moved_files"], ensure_ascii=False),
                    batch_id,
                    plan["pair_order"],
                    plan["candidate_snapshot"],
                    plan["exact_snapshot"],
                ),
            )
            plan["manifest_pairing_id"] = int(cursor.lastrowid)
            # Clear the exact approved row only after its complete Table 3 snapshot
            # and physical destination have been written.
            source_connection.execute(
                "DELETE FROM exact_pairings WHERE matched_archive = ? AND matched_json = ?",
                (plan["archive_text"], plan["json_text"]),
            )
            source_connection.execute(
                "DELETE FROM candidate_pairings WHERE archive_file = ?",
                (plan["archive_text"],),
            )

        for shared_plan in shared_plans:
            source_connection.execute(
                """
                INSERT INTO combined_manifest.combined_shared_files
                    (combined_at, source_database, source_directory,
                     destination_directory, moved_files, batch_id, status)
                VALUES (?, ?, ?, ?, ?, ?, 'combined')
                """,
                (
                    now,
                    str(database_path),
                    str(shared_plan["source_directory"]),
                    str(shared_plan["shared_directory"].resolve()),
                    json.dumps(shared_plan["moved_files"], ensure_ascii=False),
                    batch_id,
                ),
            )

        for entry in move_entries:
            plan = entry["plan"]
            pairing_id = (
                int(plan.get("manifest_pairing_id"))
                if entry["kind"] == "pair" and plan.get("manifest_pairing_id")
                else None
            )
            source_connection.execute(
                """
                INSERT INTO combined_manifest.combined_file_moves
                    (batch_id, pairing_id, role, source_path,
                     destination_path, status, moved_at)
                VALUES (?, ?, ?, ?, ?, 'moved', ?)
                """,
                (
                    batch_id,
                    pairing_id,
                    str(entry.get("role", "file")),
                    str(entry["source"]),
                    str(entry.get("destination_actual") or entry["destination"]),
                    now,
                ),
            )

        consumed_jsons = {
            str(source.resolve())
            for source in source_owners
            if source.suffix.casefold() == ".json"
        }
        remaining_rows = list(
            source_connection.execute(
                "SELECT archive_file, matching_json_files FROM candidate_pairings"
            )
        )
        updates: list[tuple[str, str]] = []
        for archive_file, raw_matches in remaining_rows:
            try:
                decoded = json.loads(raw_matches)
            except (TypeError, json.JSONDecodeError):
                decoded = []
            filtered = [
                item
                for item in decoded
                if isinstance(item, dict)
                and str(Path(str(item.get("json_file", ""))).expanduser().resolve())
                not in consumed_jsons
            ]
            if filtered != decoded:
                updates.append((json.dumps(filtered, ensure_ascii=False), str(archive_file)))
        if updates:
            source_connection.executemany(
                "UPDATE candidate_pairings SET matching_json_files = ? WHERE archive_file = ?",
                updates,
            )

        source_tables = {
            str(row[0])
            for row in source_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "run_metadata" in source_tables:
            source_connection.executemany(
                "INSERT OR REPLACE INTO run_metadata (key, value) VALUES (?, ?)",
                [
                    ("last_combined_location", str(target_root.resolve())),
                    ("last_combined_at", now),
                ],
            )
        source_connection.executemany(
            "INSERT OR REPLACE INTO combined_manifest.run_metadata (key, value) VALUES (?, ?)",
            [
                ("app_version", APP_VERSION),
                ("destination", str(target_root.resolve())),
                ("updated_at", now),
            ],
        )
        source_connection.commit()
        try:
            source_connection.execute("DETACH DATABASE combined_manifest")
        except sqlite3.Error:
            pass
        source_connection.close()
        source_connection = None

    except Exception as exc:
        if source_connection is not None:
            try:
                source_connection.rollback()
            except sqlite3.Error:
                pass
            try:
                source_connection.close()
            except sqlite3.Error:
                pass

        rollback_errors = _rollback_combined_moves(
            moved_pairs,
            created_directories,
            None,
        )
        if not manifest_preexisted and not manifest_sql_preexisted:
            _remove_failed_manifest_artifacts(manifest_database, manifest_sql)
        # Retry directory cleanup after removing a newly-created manifest file.
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        if newly_created_root is not None:
            try:
                newly_created_root.rmdir()
            except OSError:
                pass

        detail = str(exc)
        if rollback_errors:
            detail += "\n\nRollback warnings:\n" + "\n".join(rollback_errors)
        raise OSError(detail) from exc

    # SQL dump generation is post-commit: a dump failure must not undo a valid,
    # already-committed file/DB transaction. The SQLite manifests remain authoritative.
    try:
        rewrite_sql_dump(database_path)
    except OSError as exc:
        print(f"[COMBINED WARNING] Could not rewrite source SQL dump: {exc}", flush=True)
    try:
        manifest_sql = rewrite_combined_manifest_sql(manifest_database)
    except OSError as exc:
        print(f"[COMBINED WARNING] Could not rewrite combined SQL dump: {exc}", flush=True)

    remember_last_combined_directory(target_root)
    pair_file_count = sum(len(plan["moved_files"]) for plan in plans)
    shared_file_count = sum(len(plan["moved_files"]) for plan in shared_plans)
    moved_file_count = pair_file_count + shared_file_count
    if discarded_duplicate_archive_rows:
        print(
            f"[COMBINED PREFLIGHT] Collapsed {len(discarded_duplicate_archive_rows)} "
            "legacy duplicate Table 2 row(s); each archive moved once.",
            flush=True,
        )
    print(f"[COMBINED] batch {batch_id}: {len(plans)} approved pairing(s) -> {target_root}", flush=True)
    print(f"[COMBINED] {moved_file_count} file(s) moved", flush=True)
    if shared_file_count:
        print(
            f"[COMBINED] {shared_file_count} ambiguous/shared file(s) preserved in _shared_*",
            flush=True,
        )
    print(f"[COMBINED] Manifest: {manifest_database}", flush=True)
    return CombineResult(
        destination_dir=target_root.resolve(),
        pairing_count=len(plans),
        moved_file_count=moved_file_count,
        shared_file_count=shared_file_count,
        manifest_database=manifest_database.resolve(),
        manifest_sql=manifest_sql.resolve(),
        used_existing_location=used_existing,
        batch_id=batch_id,
    )

def list_output_databases(output_dir: Path) -> list[Path]:
    """Return generated SQLite databases from oldest to newest."""
    directory = output_dir.expanduser().resolve()
    if not directory.exists():
        return []
    databases = [
        path.resolve()
        for path in directory.glob("pairings-*.sqlite3")
        if path.is_file()
    ]
    databases.sort(key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()))
    return databases




def list_combined_databases(root: Path = DEFAULT_COMBINED_ROOT) -> list[Path]:
    """Return one expected manifest path for every direct ~/Combined child.

    A run directory is visible in Table 3 even when it has no old manifest yet;
    CLEAR OLD TABLE + RESCAN can create that manifest from its contents.
    """
    directory = root.expanduser().resolve()
    if not directory.is_dir():
        return []
    run_directories = [
        path.resolve()
        for path in directory.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    run_directories.sort(
        key=lambda path: (
            path.stat().st_mtime_ns,
            path.name.casefold(),
        )
    )
    return [path / "combined-pairings.sqlite3" for path in run_directories]


def _resolved_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def _combined_directory_base_name(name: str) -> str:
    """Remove only the collision suffix added by unique_child_directory()."""
    return re.sub(r" \(\d+\)$", "", name).strip()


def _is_direct_image_file(path: Path) -> bool:
    """Return True for a direct-child image file without reading nested folders."""
    suffix = path.suffix.casefold()
    if suffix in SUPPORTED_IMAGE_SUFFIXES:
        return True
    guessed, _encoding = mimetypes.guess_type(path.name, strict=False)
    return bool(guessed and guessed.casefold().startswith("image/"))


def _json_has_pairing_metadata(path: Path) -> tuple[bool, bool]:
    """Return whether a JSON has a title and Source-URL/url field."""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, False
    return bool(find_title(payload)), bool(find_source_url(payload))


def _choose_primary_json(
    directory: Path,
    jsons: list[Path],
    old_rows: list[dict[str, Any]],
    ledger_jsons: list[Path] | None = None,
    *,
    allow_deterministic_fallback: bool = False,
) -> Path | None:
    """Choose the one JSON that represents this direct pairing directory.

    Priority is: durable move ledger, old Table 3 row, directory-name relation,
    single JSON, then a unique metadata/name winner. Extra sibling JSON files are
    retained as related files and do not by themselves invalidate the directory.
    """
    directory = directory.resolve()
    jsons = [path.resolve() for path in jsons]

    # The move ledger is the strongest source of truth for runs made by v21+.
    ledger_candidates = [
        path.resolve()
        for path in (ledger_jsons or [])
        if path.is_file() and path.resolve().parent == directory
    ]
    if len(ledger_candidates) == 1:
        return ledger_candidates[0]

    # Preserve an existing manifest's explicit destination when it still exists.
    old_candidates: list[Path] = []
    for row in old_rows:
        old_json = _resolved_text(row.get("destination_json"))
        if not old_json:
            continue
        candidate = Path(old_json).expanduser()
        if candidate.is_file() and candidate.resolve().parent == directory:
            old_candidates.append(candidate.resolve())
    old_unique = list(dict.fromkeys(old_candidates))
    if len(old_unique) == 1:
        return old_unique[0]

    directory_name = _combined_directory_base_name(directory.name)
    directory_key = unicode_words(directory_name)
    safe_directory_name = safe_combined_directory_name(directory_name)

    exact = [
        path for path in jsons
        if unicode_words(path.stem) == directory_key
        or safe_combined_directory_name(path.stem) == safe_directory_name
    ]
    if len(exact) == 1:
        return exact[0]

    # Long names may have been truncated to 180 characters by the combine step.
    truncated = [
        path for path in jsons
        if safe_combined_directory_name(path.stem).startswith(safe_directory_name)
        or safe_directory_name.startswith(safe_combined_directory_name(path.stem))
    ]
    if len(truncated) == 1:
        return truncated[0]

    if len(jsons) == 1:
        return jsons[0]

    # In old/hand-built runs, prefer a unique JSON carrying both the title and URL.
    metadata: dict[Path, tuple[bool, bool]] = {
        path: _json_has_pairing_metadata(path) for path in jsons
    }
    both = [path for path, flags in metadata.items() if flags == (True, True)]
    if len(both) == 1:
        return both[0]
    titled = [path for path, flags in metadata.items() if flags[0]]
    if len(titled) == 1:
        return titled[0]

    # Last safe fallback: accept only a unique strongest filename match.
    ranked = sorted(
        ((similarity(directory_key, unicode_words(path.stem)), path) for path in jsons),
        key=lambda item: (item[0], item[1].name.casefold()),
        reverse=True,
    )
    if ranked and ranked[0][0] > 0:
        if len(ranked) == 1 or ranked[0][0] > ranked[1][0]:
            return ranked[0][1]
    if allow_deterministic_fallback and ranked:
        # FILTER OFF means “scan every pairable direct directory.” When old
        # metadata cannot disambiguate several sibling JSON files, keep the scan
        # moving with a stable filename choice rather than hiding the directory.
        return ranked[0][1]
    return None


def _choose_primary_archive(
    directory: Path,
    archives: list[Path],
    old_rows: list[dict[str, Any]],
    ledger_archives: list[Path] | None = None,
    primary_json: Path | None = None,
) -> Path | None:
    """Choose one direct archive without discarding a multi-archive directory.

    The safety filter, when enabled, can reject multi-archive directories before
    this function is reached. With the filter OFF, the scanner still needs one
    archive for the Table 3 row, so it uses durable history first and then a
    deterministic name ranking. Every non-selected archive remains listed among
    the row's related/moved files; no physical file is changed.
    """
    directory = directory.resolve()
    archives = [path.resolve() for path in archives]
    if not archives:
        return None

    ledger_candidates = [
        path.resolve()
        for path in (ledger_archives or [])
        if path.is_file() and path.resolve().parent == directory
    ]
    ledger_unique = list(dict.fromkeys(ledger_candidates))
    if len(ledger_unique) == 1:
        return ledger_unique[0]

    old_candidates: list[Path] = []
    for row in old_rows:
        old_archive = _resolved_text(row.get("destination_archive"))
        if not old_archive:
            continue
        candidate = Path(old_archive).expanduser()
        if candidate.is_file() and candidate.resolve().parent == directory:
            old_candidates.append(candidate.resolve())
    old_unique = list(dict.fromkeys(old_candidates))
    if len(old_unique) == 1:
        return old_unique[0]

    if len(archives) == 1:
        return archives[0]

    directory_key = unicode_words(_combined_directory_base_name(directory.name))
    json_key = unicode_words(primary_json.stem) if primary_json is not None else ""
    title_key = ""
    if primary_json is not None:
        title, _source_url = _read_json_title_url(primary_json)
        title_key = unicode_words(title)

    def rank(path: Path) -> tuple[float, float, float, str]:
        stem = unicode_words(path.stem)
        return (
            similarity(directory_key, stem),
            similarity(title_key, stem) if title_key else 0.0,
            similarity(json_key, stem) if json_key else 0.0,
            path.name.casefold(),
        )

    return max(archives, key=rank)


def _backup_combined_manifest(manifest_database: Path) -> Path | None:
    if not manifest_database.is_file():
        return None
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = manifest_database.parent / "manifest-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"combined-pairings-before-overwrite-{stamp}.sqlite3"
    shutil.copy2(manifest_database, backup)
    sql = manifest_database.with_suffix(".sql")
    if sql.is_file():
        shutil.copy2(sql, backup.with_suffix(".sql"))
    return backup


def rebuild_combined_manifest_from_directories(
    manifest_database: Path,
    *,
    enforce_limits: bool = False,
    max_images: int = 1,
    max_archives: int = 1,
) -> CombinedRebuildResult:
    """Hard-rebuild Table 3 from every pairable directory beneath the run.

    A pairable directory directly contains at least one CBZ/ZIP and one JSON.
    The physical files are never moved. Existing source→destination ledgers,
    batches, and reversed history stay in the database. Active Table 3 rows are
    replaced from disk, while matching historical metadata/snapshots are carried
    forward so already-ledgered batches remain reversible. Direct-child image and
    archive count limits are applied only when ``enforce_limits`` is True.
    """
    if max_images < 0 or max_archives < 0:
        raise ValueError("Maximum image/archive limits must be zero or greater.")

    manifest_database = manifest_database.expanduser().resolve()
    run_dir = manifest_database.parent
    if not run_dir.is_dir():
        raise ValueError(f"Combined run directory does not exist:\n{run_dir}")

    old_rows: list[dict[str, Any]] = []
    ledger_jsons_by_directory: dict[str, list[Path]] = {}
    ledger_archives_by_directory: dict[str, list[Path]] = {}
    if manifest_database.is_file():
        connection = sqlite3.connect(manifest_database)
        connection.row_factory = sqlite3.Row
        try:
            ensure_combined_manifest_schema(connection)
            old_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM combined_pairings "
                    "WHERE COALESCE(status, 'combined') != 'reversed' ORDER BY id"
                )
            ]
            for row in connection.execute(
                "SELECT destination_path FROM combined_file_moves "
                "WHERE role='json' AND COALESCE(status, 'moved') != 'reversed'"
            ):
                destination = Path(str(row[0])).expanduser()
                if destination.is_file():
                    resolved = destination.resolve()
                    ledger_jsons_by_directory.setdefault(str(resolved.parent), []).append(resolved)
            for row in connection.execute(
                "SELECT destination_path FROM combined_file_moves "
                "WHERE role='archive' AND COALESCE(status, 'moved') != 'reversed'"
            ):
                destination = Path(str(row[0])).expanduser()
                if destination.is_file():
                    resolved = destination.resolve()
                    ledger_archives_by_directory.setdefault(str(resolved.parent), []).append(resolved)
        finally:
            connection.close()

    by_directory: dict[str, list[dict[str, Any]]] = {}
    by_json: dict[str, dict[str, Any]] = {}
    for row in old_rows:
        directory_text = _resolved_text(row.get("destination_directory"))
        if directory_text:
            by_directory.setdefault(directory_text, []).append(row)
        json_text = _resolved_text(row.get("destination_json"))
        if json_text:
            by_json[json_text] = row

    ignored: list[tuple[str, str]] = []
    scanned: list[dict[str, Any]] = []
    # Scan every descendant directory, not merely the first level.  Older
    # Combine runs and hand-arranged runs can contain an extra grouping level;
    # limiting discovery to run_dir.iterdir() made a perfectly full run appear
    # to contain only one pairing.  A directory qualifies independently when it
    # directly contains at least one archive and at least one JSON.
    pairing_directories: list[Path] = []
    for item in run_dir.rglob("*"):
        if not item.is_dir():
            continue
        try:
            relative = item.relative_to(run_dir)
        except ValueError:
            continue
        if any(
            part.startswith(".")
            or part == "manifest-backups"
            or part.startswith("_shared_")
            for part in relative.parts
        ):
            continue
        pairing_directories.append(item.resolve())
    pairing_directories.sort(
        key=lambda item: str(item.relative_to(run_dir)).casefold()
    )

    for directory in pairing_directories:
        files = sorted(
            (item.resolve() for item in directory.iterdir() if item.is_file()),
            key=lambda item: item.name.casefold(),
        )
        archives = [item for item in files if item.suffix.casefold() in SUPPORTED_ARCHIVE_SUFFIXES]
        # These limits apply ONLY to files directly inside this pairing directory.
        # Nested folders and every file below them are deliberately ignored.
        images = [item for item in files if _is_direct_image_file(item)]
        jsons = [item for item in files if item.suffix.casefold() == ".json"]

        if enforce_limits and len(images) > max_images:
            ignored.append((str(directory), f"{len(images)} direct images exceeds maximum {max_images}"))
            continue
        if enforce_limits and len(archives) > max_archives:
            ignored.append((str(directory), f"{len(archives)} direct CBZ/ZIP files exceeds maximum {max_archives}"))
            continue
        if not archives:
            ignored.append((str(directory), "no direct CBZ/ZIP files found"))
            continue
        if not jsons:
            ignored.append((str(directory), "no direct JSON files found"))
            continue

        directory_old_rows = by_directory.get(str(directory), [])
        primary_json = _choose_primary_json(
            directory,
            jsons,
            directory_old_rows,
            ledger_jsons_by_directory.get(str(directory), []),
            allow_deterministic_fallback=not enforce_limits,
        )
        if primary_json is None:
            ignored.append((
                str(directory),
                f"could not identify the primary JSON among {len(jsons)} direct JSON files",
            ))
            continue

        archive = _choose_primary_archive(
            directory,
            archives,
            directory_old_rows,
            ledger_archives_by_directory.get(str(directory), []),
            primary_json,
        )
        if archive is None:
            ignored.append((str(directory), "could not identify a direct CBZ/ZIP"))
            continue
        old = by_json.get(str(primary_json))
        if old is None and directory_old_rows:
            old = directory_old_rows[0]
        history = _historical_pair_for_files(archive, primary_json)
        title, source_url = _read_json_title_url(primary_json)

        source_database = str((old or {}).get("source_database") or (history or {}).get("source_database") or "reconstructed-from-combined")
        source_archive = str((old or {}).get("source_archive") or (history or {}).get("source_archive") or archive)
        source_json = str((old or {}).get("source_json") or (history or {}).get("source_json") or primary_json)
        title = str((old or {}).get("title") or (history or {}).get("title") or title or primary_json.stem)
        source_url = str((old or {}).get("source_url") or (history or {}).get("source_url") or source_url)
        match_percent = float((old or {}).get("match_percent") or (history or {}).get("match_percent") or 0.0)
        selection_method = str((old or {}).get("selection_method") or (history or {}).get("selection_method") or "directory_rescan")
        batch_id = str((old or {}).get("batch_id") or f"rescan-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}")
        candidate_snapshot = str((old or {}).get("candidate_snapshot") or "[]")
        exact_snapshot = str((old or {}).get("exact_snapshot") or "")
        if not exact_snapshot:
            exact_snapshot = json.dumps(
                {
                    "matched_archive": source_archive,
                    "matched_title": title,
                    "matched_json": source_json,
                    "related_files": [str(item) for item in files if item not in {archive, primary_json}],
                    "matched_source_url": source_url,
                    "match_percent": match_percent,
                    "selection_method": selection_method,
                    "promoted_at": "",
                },
                ensure_ascii=False,
            )

        scanned.append(
            {
                "combined_at": str((old or {}).get("combined_at") or datetime.now().astimezone().isoformat()),
                "source_database": source_database,
                "source_archive": source_archive,
                "source_json": source_json,
                "title": title,
                "source_url": source_url,
                "match_percent": match_percent,
                "selection_method": selection_method,
                "destination_directory": str(directory),
                "destination_archive": str(archive),
                "destination_json": str(primary_json),
                "moved_files": json.dumps([str(item) for item in files], ensure_ascii=False),
                "batch_id": batch_id,
                "candidate_snapshot": candidate_snapshot,
                "exact_snapshot": exact_snapshot,
                "recovered": int((old or {}).get("recovered") or 1),
                "reverse_ready": int((old or {}).get("reverse_ready") or 0),
                "recovery_note": "Table 3 overwritten from the actual Combined directory structure",
            }
        )

    backup = _backup_combined_manifest(manifest_database)
    manifest_database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(manifest_database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        ensure_combined_manifest_schema(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS combined_scan_ignored (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                directory TEXT NOT NULL,
                reason TEXT NOT NULL,
                max_images INTEGER NOT NULL,
                max_archives INTEGER NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM combined_scan_ignored")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS combined_scan_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                directory TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        connection.execute("DELETE FROM combined_scan_audit")
        now = datetime.now().astimezone().isoformat()
        connection.executemany(
            "INSERT INTO combined_scan_ignored "
            "(scanned_at, directory, reason, max_images, max_archives) VALUES (?, ?, ?, ?, ?)",
            [(now, directory, reason, max_images, max_archives) for directory, reason in ignored],
        )
        connection.executemany(
            "INSERT INTO combined_scan_audit (scanned_at, directory, outcome, detail) "
            "VALUES (?, ?, 'ADDED', ?)",
            [
                (
                    now,
                    str(item["destination_directory"]),
                    f"archive={Path(item['destination_archive']).name}; "
                    f"json={Path(item['destination_json']).name}",
                )
                for item in scanned
            ],
        )
        connection.executemany(
            "INSERT INTO combined_scan_audit (scanned_at, directory, outcome, detail) "
            "VALUES (?, ?, 'IGNORED', ?)",
            [(now, directory, reason) for directory, reason in ignored],
        )

        # HARD REBUILD: the old display table is deliberately discarded after
        # the backup has been written.  The durable batch and file-move ledgers
        # remain in their own tables, so reversibility metadata is preserved,
        # while stale rows and old UNIQUE constraints cannot leak into the new
        # Table 3 result.
        connection.execute("DROP TABLE IF EXISTS combined_pairings")
        ensure_combined_manifest_schema(connection)
        for order, item in enumerate(scanned, start=1):
            connection.execute(
                """
                INSERT INTO combined_pairings
                    (combined_at, source_database, source_archive, source_json,
                     title, source_url, match_percent, selection_method,
                     destination_directory, destination_archive, destination_json,
                     moved_files, batch_id, pair_order, status,
                     candidate_snapshot, exact_snapshot, recovered,
                     reverse_ready, recovery_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'combined',
                        ?, ?, ?, ?, ?)
                """,
                (
                    item["combined_at"], item["source_database"], item["source_archive"],
                    item["source_json"], item["title"], item["source_url"],
                    item["match_percent"], item["selection_method"],
                    item["destination_directory"], item["destination_archive"],
                    item["destination_json"], item["moved_files"], item["batch_id"],
                    order, item["candidate_snapshot"], item["exact_snapshot"],
                    item["recovered"], item["reverse_ready"], item["recovery_note"],
                ),
            )
            if not connection.execute(
                "SELECT 1 FROM combined_batches WHERE batch_id=?", (item["batch_id"],)
            ).fetchone():
                connection.execute(
                    """
                    INSERT INTO combined_batches
                        (batch_id, combined_at, source_database, destination_root,
                         approved_pairing_count, status, used_existing_location)
                    VALUES (?, ?, ?, ?, 1, 'scanned', 1)
                    """,
                    (
                        item["batch_id"], item["combined_at"], item["source_database"],
                        str(run_dir),
                    ),
                )

        connection.executemany(
            "INSERT OR REPLACE INTO run_metadata (key, value) VALUES (?, ?)",
            [
                ("app_version", APP_VERSION),
                ("destination", str(run_dir)),
                ("updated_at", now),
                ("table3_source", "directory-overwrite"),
                ("scan_limits_enabled", "1" if enforce_limits else "0"),
                ("scan_max_images", str(max_images)),
                ("scan_max_archives", str(max_archives)),
                ("scan_pairings", str(len(scanned))),
                ("scan_ignored", str(len(ignored))),
            ],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        f"[TABLE 3 HARD RESCAN] discovered={len(pairing_directories)} "
        f"added={len(scanned)} ignored={len(ignored)} root={run_dir}"
    )
    for item in scanned:
        print(
            "[TABLE 3 ADDED] "
            f"{item['destination_directory']} :: "
            f"{Path(item['destination_archive']).name} + "
            f"{Path(item['destination_json']).name}"
        )
    for directory, reason in ignored:
        print(f"[TABLE 3 IGNORED] {directory} :: {reason}")

    manifest_sql = rewrite_combined_manifest_sql(manifest_database)
    return CombinedRebuildResult(
        manifest_database=manifest_database,
        manifest_sql=manifest_sql,
        paired_directories=len(scanned),
        ignored_directories=len(ignored),
        ignored_reasons=tuple(ignored),
        backup_database=backup,
    )


def load_combined_table(database_path: Path) -> list[dict[str, Any]]:
    """Read the merged Table 3 manifest as display-ready dictionaries."""
    rows: list[dict[str, Any]] = []
    connection = sqlite3.connect(database_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "combined_pairings" not in tables:
            return []
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(combined_pairings)")
        }
        wanted = [
            "id",
            "combined_at",
            "source_database",
            "source_archive",
            "source_json",
            "title",
            "source_url",
            "match_percent",
            "selection_method",
            "destination_directory",
            "destination_archive",
            "destination_json",
            "moved_files",
            "batch_id",
            "pair_order",
            "status",
            "recovered",
            "reverse_ready",
            "recovery_note",
        ]
        expressions = [name if name in columns else "''" for name in wanted]
        query = (
            "SELECT " + ", ".join(expressions) +
            " FROM combined_pairings WHERE COALESCE(status, 'combined') != 'reversed' "
            "ORDER BY combined_at, pair_order, id"
        )
        for raw in connection.execute(query):
            item = dict(zip(wanted, raw))
            try:
                moved = json.loads(item.get("moved_files") or "[]")
            except (TypeError, json.JSONDecodeError):
                moved = []
            item["moved_files"] = [str(value) for value in moved if isinstance(value, str)]
            try:
                item["match_percent"] = float(item.get("match_percent") or 0.0)
            except (TypeError, ValueError):
                item["match_percent"] = 0.0
            rows.append(item)
    finally:
        connection.close()
    return rows



def load_live_combined_inventory(database_path: Path) -> tuple[list[dict[str, Any]], int]:
    """Build Table 3 from the files that exist on disk right now.

    The SQLite manifest is used only to enrich rows with original paths, scores,
    and reversal metadata.  It is never allowed to decide how many cards appear.
    This deliberately prevents a stale/partially rebuilt SQL table from hiding
    pairing directories that visibly exist under ~/Combined/<run>/.

    Returns ``(live_rows, stored_sql_row_count)``.
    """
    database_path = database_path.expanduser().resolve()
    run_directory = database_path.parent
    stored_rows: list[dict[str, Any]] = []
    if database_path.is_file():
        try:
            stored_rows = load_combined_table(database_path)
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            stored_rows = []

    rows_by_directory: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in stored_rows:
        destinations = [
            _resolved_text(row.get("destination_directory")),
            str(Path(_resolved_text(row.get("destination_archive"))).parent)
            if _resolved_text(row.get("destination_archive")) else "",
            str(Path(_resolved_text(row.get("destination_json"))).parent)
            if _resolved_text(row.get("destination_json")) else "",
        ]
        for value in destinations:
            if value:
                rows_by_directory[str(Path(value).expanduser().resolve())].append(row)

    ledger_by_directory: dict[str, dict[str, list[Path]]] = defaultdict(
        lambda: {"archive": [], "json": []}
    )
    ledger_source_by_destination: dict[str, str] = {}
    if database_path.is_file():
        connection = sqlite3.connect(database_path)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "combined_file_moves" in tables:
                for role, source_path, destination_path, status in connection.execute(
                    "SELECT role, source_path, destination_path, status "
                    "FROM combined_file_moves"
                ):
                    if str(status or "moved") == "reversed":
                        continue
                    destination = Path(str(destination_path)).expanduser()
                    try:
                        destination = destination.resolve()
                    except OSError:
                        pass
                    ledger_source_by_destination[str(destination)] = str(source_path or "")
                    role_text = str(role or "").casefold()
                    if role_text in {"archive", "json"}:
                        ledger_by_directory[str(destination.parent)][role_text].append(destination)
        finally:
            connection.close()

    live_rows: list[dict[str, Any]] = []
    if not run_directory.is_dir():
        return live_rows, len(stored_rows)

    pairing_directories = sorted(
        (
            path.resolve()
            for path in run_directory.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and path.name != "manifest-backups"
        ),
        key=lambda path: path.name.casefold(),
    )

    for directory in pairing_directories:
        try:
            direct_files = sorted(
                (path.resolve() for path in directory.iterdir() if path.is_file()),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            continue
        archives = [
            path for path in direct_files
            if path.suffix.casefold() in SUPPORTED_ARCHIVE_SUFFIXES
        ]
        jsons = [path for path in direct_files if path.suffix.casefold() == ".json"]
        if not archives or not jsons:
            continue

        directory_key = str(directory)
        old_rows = rows_by_directory.get(directory_key, [])
        ledger = ledger_by_directory.get(directory_key, {"archive": [], "json": []})
        primary_json = _choose_primary_json(
            directory,
            jsons,
            old_rows,
            ledger.get("json", []),
            allow_deterministic_fallback=True,
        )
        if primary_json is None:
            primary_json = jsons[0]
        primary_archive = _choose_primary_archive(
            directory,
            archives,
            old_rows,
            ledger.get("archive", []),
            primary_json,
        )
        if primary_archive is None:
            primary_archive = archives[0]

        chosen_old: dict[str, Any] = {}
        for row in old_rows:
            old_archive = _resolved_text(row.get("destination_archive"))
            old_json = _resolved_text(row.get("destination_json"))
            if (
                old_archive and Path(old_archive).expanduser().resolve() == primary_archive
            ) or (
                old_json and Path(old_json).expanduser().resolve() == primary_json
            ):
                chosen_old = row
                break
        if not chosen_old and old_rows:
            chosen_old = old_rows[0]

        title, source_url = _read_json_title_url(primary_json)
        related_files = [
            str(path)
            for path in direct_files
            if path not in {primary_archive, primary_json}
        ]
        moved_files = [str(primary_archive), str(primary_json), *related_files]
        source_archive = str(
            chosen_old.get("source_archive")
            or ledger_source_by_destination.get(str(primary_archive), "")
        )
        source_json = str(
            chosen_old.get("source_json")
            or ledger_source_by_destination.get(str(primary_json), "")
        )
        try:
            match_percent = float(chosen_old.get("match_percent") or 0.0)
        except (TypeError, ValueError):
            match_percent = 0.0

        live_rows.append(
            {
                "id": chosen_old.get("id", ""),
                "combined_at": chosen_old.get("combined_at", ""),
                "source_database": chosen_old.get("source_database", ""),
                "source_archive": source_archive,
                "source_json": source_json,
                "title": title or str(chosen_old.get("title") or primary_json.stem),
                "source_url": source_url or str(chosen_old.get("source_url") or ""),
                "match_percent": match_percent,
                "selection_method": str(
                    chosen_old.get("selection_method") or "LIVE DIRECTORY"
                ),
                "destination_directory": str(directory),
                "destination_archive": str(primary_archive),
                "destination_json": str(primary_json),
                "moved_files": moved_files,
                "batch_id": chosen_old.get("batch_id", ""),
                "pair_order": chosen_old.get("pair_order", 0),
                "status": "combined",
                "recovered": chosen_old.get("recovered", 1),
                "reverse_ready": chosen_old.get("reverse_ready", 0),
                "recovery_note": chosen_old.get(
                    "recovery_note", "Live Table 3 inventory from files on disk"
                ),
            }
        )

    return live_rows, len(stored_rows)


def load_combined_shared_rows(database_path: Path) -> list[dict[str, Any]]:
    """Read shared-file groups created when selected JSONs used one source folder."""
    rows: list[dict[str, Any]] = []
    connection = sqlite3.connect(database_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "combined_shared_files" not in tables:
            return []
        for row in connection.execute(
            "SELECT combined_at, source_database, source_directory, "
            "destination_directory, moved_files "
            "FROM combined_shared_files "
            "WHERE COALESCE(status, 'combined') != 'reversed' ORDER BY id"
        ):
            try:
                moved = json.loads(row[4] or "[]")
            except (TypeError, json.JSONDecodeError):
                moved = []
            rows.append(
                {
                    "combined_at": str(row[0] or ""),
                    "source_database": str(row[1] or ""),
                    "source_directory": str(row[2] or ""),
                    "destination_directory": str(row[3] or ""),
                    "moved_files": [str(value) for value in moved if isinstance(value, str)],
                }
            )
    finally:
        connection.close()
    return rows


def load_combined_version(database_path: Path) -> str:
    connection = sqlite3.connect(database_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "run_metadata" not in tables:
            return "combined output"
        row = connection.execute(
            "SELECT value FROM run_metadata WHERE key='app_version'"
        ).fetchone()
        return f"v{row[0]} combined output" if row and row[0] else "combined output"
    finally:
        connection.close()

def load_candidate_table(database_path: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """Read candidate rows and decode the stored JSON list."""
    rows: list[tuple[str, list[dict[str, Any]]]] = []
    connection = sqlite3.connect(database_path)
    try:
        result = connection.execute(
            "SELECT archive_file, matching_json_files "
            "FROM candidate_pairings ORDER BY archive_file COLLATE NOCASE"
        )
        for archive_file, raw_matches in result:
            try:
                decoded = json.loads(raw_matches)
            except (TypeError, json.JSONDecodeError):
                decoded = []
            matches = [item for item in decoded if isinstance(item, dict)]
            rows.append((str(archive_file), matches))
    finally:
        connection.close()
    return rows


def load_exact_table(
    database_path: Path,
) -> list[tuple[str, str, str, list[str], str, float, str]]:
    """Read promoted rows while remaining compatible with older databases."""
    rows: list[tuple[str, str, str, list[str], str, float, str]] = []
    connection = sqlite3.connect(database_path)
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(exact_pairings)")
        }
        title_expr = "matched_title" if "matched_title" in columns else "''"
        source_expr = (
            "matched_source_url" if "matched_source_url" in columns else "''"
        )
        score_expr = "match_percent" if "match_percent" in columns else "100.0"
        method_expr = (
            "selection_method" if "selection_method" in columns else "'automatic_100'"
        )
        result = connection.execute(
            f"SELECT matched_archive, {title_expr}, matched_json, related_files, "
            f"{source_expr}, {score_expr}, {method_expr} FROM exact_pairings "
            "ORDER BY matched_archive COLLATE NOCASE, matched_json COLLATE NOCASE"
        )
        for (
            matched_archive,
            matched_title,
            matched_json,
            raw_related,
            matched_source_url,
            match_percent,
            selection_method,
        ) in result:
            try:
                decoded = json.loads(raw_related)
            except (TypeError, json.JSONDecodeError):
                decoded = []
            related = [str(item) for item in decoded if isinstance(item, str)]
            try:
                score = float(match_percent)
            except (TypeError, ValueError):
                score = 100.0
            json_path_text = str(matched_json)
            source_url = str(matched_source_url or "")
            if not source_url:
                source_url = source_url_from_json_file(json_path_text)
            rows.append(
                (
                    str(matched_archive),
                    str(matched_title),
                    json_path_text,
                    related,
                    source_url,
                    score,
                    str(selection_method),
                )
            )
    finally:
        connection.close()
    return rows


def exact_jsons_by_archive(database_path: Path) -> dict[str, set[str]]:
    """Return JSON files already present in table 2 for each archive."""
    grouped: dict[str, set[str]] = {}
    for archive, _title, json_file, _related, _source, _score, _method in load_exact_table(
        database_path
    ):
        grouped.setdefault(archive, set()).add(json_file)
    return grouped


def ensure_promotion_columns(connection: sqlite3.Connection) -> None:
    """Upgrade an older database in place so manual promotions can store scores."""
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(exact_pairings)")
    }
    if "matched_title" not in columns:
        connection.execute(
            "ALTER TABLE exact_pairings ADD COLUMN matched_title TEXT NOT NULL DEFAULT ''"
        )
    if "matched_source_url" not in columns:
        connection.execute(
            "ALTER TABLE exact_pairings ADD COLUMN matched_source_url TEXT NOT NULL DEFAULT ''"
        )
    if "match_percent" not in columns:
        connection.execute(
            "ALTER TABLE exact_pairings ADD COLUMN match_percent REAL NOT NULL DEFAULT 100.0"
        )
    if "selection_method" not in columns:
        connection.execute(
            "ALTER TABLE exact_pairings ADD COLUMN selection_method TEXT NOT NULL DEFAULT 'automatic_100'"
        )
    if "promoted_at" not in columns:
        connection.execute(
            "ALTER TABLE exact_pairings ADD COLUMN promoted_at TEXT NOT NULL DEFAULT ''"
        )


def rewrite_sql_dump(database_path: Path) -> Path:
    """Rewrite the sibling SQL dump so it mirrors GUI edits and promotions."""
    sql_path = database_path.with_suffix(".sql")
    connection = sqlite3.connect(database_path)
    try:
        with sql_path.open("w", encoding="utf-8") as handle:
            handle.write("-- Generated by pair_original_page_name.py (title-field mode)\n")
            handle.write("-- Includes GUI selections and manual JSON reassignments\n")
            for line in connection.iterdump():
                handle.write(line)
                handle.write("\n")
    finally:
        connection.close()
    return sql_path


def reassign_candidate_json(
    database_path: Path,
    source_archive: str,
    target_archive: str,
    item: dict[str, Any],
) -> bool:
    """Move one displayed candidate from its source block to a target block.

    The stored score is deliberately preserved. Only the source block loses the
    candidate; identical candidate appearances under unrelated archive blocks are
    left untouched. The moved item is inserted first in the target block so it is
    immediately visible and easy to select.
    """
    json_file = str(item.get("json_file", ""))
    if not json_file or not target_archive or source_archive == target_archive:
        return False

    moved_item = dict(item)
    moved_item["manual_reassigned"] = True
    moved_item["reassigned_from"] = source_archive
    if not moved_item.get("source_url"):
        moved_item["source_url"] = source_url_from_json_file(json_file)

    connection = sqlite3.connect(database_path)
    try:
        rows = list(
            connection.execute(
                "SELECT archive_file, matching_json_files FROM candidate_pairings"
            )
        )
        source_found = False
        target_found = False
        updates: list[tuple[str, str]] = []

        for archive_file, raw_matches in rows:
            archive_text = str(archive_file)
            try:
                decoded = json.loads(raw_matches)
            except (TypeError, json.JSONDecodeError):
                decoded = []
            matches = [entry for entry in decoded if isinstance(entry, dict)]

            if archive_text == source_archive:
                before = len(matches)
                matches = [
                    entry
                    for entry in matches
                    if str(entry.get("json_file", "")) != json_file
                ]
                source_found = source_found or len(matches) != before

            if archive_text == target_archive:
                # Avoid a duplicate in the destination, then place the manually
                # moved candidate first so it cannot appear to have vanished.
                matches = [
                    entry
                    for entry in matches
                    if str(entry.get("json_file", "")) != json_file
                ]
                matches.insert(0, moved_item)
                target_found = True

            if archive_text in {source_archive, target_archive}:
                updates.append((json.dumps(matches, ensure_ascii=False), archive_text))

        if not source_found or not target_found:
            connection.rollback()
            return False

        connection.executemany(
            "UPDATE candidate_pairings SET matching_json_files = ? WHERE archive_file = ?",
            updates,
        )
        connection.commit()
    finally:
        connection.close()

    rewrite_sql_dump(database_path)
    return True


def promote_pairings(
    database_path: Path,
    selections: dict[str, dict[str, Any]],
    *,
    selection_method: str,
) -> int:
    """Move one selected JSON per archive into table 2, replacing prior choices."""
    if not selections:
        return 0

    connection = sqlite3.connect(database_path)
    try:
        ensure_promotion_columns(connection)
        now = datetime.now().astimezone().isoformat()
        for archive_file, item in selections.items():
            json_file = str(item.get("json_file", ""))
            title = str(item.get("title", ""))
            source_url = str(item.get("source_url", "")) or source_url_from_json_file(
                json_file
            )
            try:
                score = float(item.get("match_percent", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            if not json_file:
                continue

            connection.execute(
                "DELETE FROM exact_pairings WHERE matched_archive = ? OR matched_json = ?",
                (archive_file, json_file),
            )
            related = related_files_for_json(Path(json_file))
            connection.execute(
                """
                INSERT INTO exact_pairings
                    (matched_archive, matched_title, matched_json, related_files,
                     matched_source_url, match_percent, selection_method, promoted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive_file,
                    title,
                    json_file,
                    json.dumps(related, ensure_ascii=False),
                    source_url,
                    score,
                    selection_method,
                    now,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    rewrite_sql_dump(database_path)
    return len(selections)


def load_run_version(database_path: Path) -> str:
    """Return the generator version, or 'legacy' for an older database."""
    connection = sqlite3.connect(database_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "run_metadata" not in tables:
            return "legacy output"
        row = connection.execute(
            "SELECT value FROM run_metadata WHERE key='app_version'"
        ).fetchone()
        return f"v{row[0]} output" if row else "unknown output"
    finally:
        connection.close()



def natural_sort_key(value: str) -> list[object]:
    """Sort archive members so page 2 comes before page 10."""
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def find_dolphin_thumbnail(archive_path: Path) -> Path | None:
    """Locate the freedesktop/KDE thumbnail Dolphin generated for an archive."""
    try:
        uri = archive_path.expanduser().resolve().as_uri()
    except ValueError:
        return None
    digest = hashlib.md5(uri.encode("utf-8"), usedforsecurity=False).hexdigest()
    thumbnail_root = Path.home() / ".cache" / "thumbnails"
    for size_name in ("xx-large", "x-large", "large", "normal", "fail"):
        candidate = thumbnail_root / size_name / f"{digest}.png"
        if candidate.is_file() and size_name != "fail":
            return candidate
    return None


def first_image_member(archive_path: Path) -> str | None:
    """Return the naturally first image member inside a CBZ/ZIP archive."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = [
                info.filename
                for info in archive.infolist()
                if not info.is_dir()
                and Path(info.filename).suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES
            ]
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    return min(names, key=natural_sort_key) if names else None


def archive_thumbnail_cache_path(archive_path: Path) -> Path:
    """Return an invalidating cache filename based on path, size, and mtime."""
    resolved = archive_path.expanduser().resolve()
    try:
        stat = resolved.stat()
        identity = f"{resolved.as_uri()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        identity = str(resolved)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return THUMBNAIL_CACHE_DIR / f"{digest}.png"


def build_archive_thumbnail_png(
    archive_path: Path,
    *,
    max_width: int = 210,
    max_height: int = 280,
) -> Path | None:
    """
    Build a reusable PNG cover thumbnail.

    Dolphin's cached thumbnail is preferred. When it is unavailable, the first
    image is read directly from the CBZ/ZIP. Pillow is used when installed;
    ImageMagick is the dependency-free desktop fallback.
    """
    archive_path = archive_path.expanduser().resolve()
    destination = archive_thumbnail_cache_path(archive_path)
    if destination.is_file():
        return destination

    THUMBNAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dolphin_thumbnail = find_dolphin_thumbnail(archive_path)
    source_bytes: bytes | None = None
    source_suffix = ".png"

    if dolphin_thumbnail is None:
        member = first_image_member(archive_path)
        if member is None:
            return None
        source_suffix = Path(member).suffix.casefold() or ".img"
        try:
            with zipfile.ZipFile(archive_path) as archive:
                source_bytes = archive.read(member)
        except (OSError, KeyError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
            return None

    # Preferred path: Pillow handles JPEG, PNG, WebP, EXIF rotation, and alpha.
    try:
        from PIL import Image, ImageOps  # type: ignore[import-not-found]

        if dolphin_thumbnail is not None:
            image = Image.open(dolphin_thumbnail)
        else:
            assert source_bytes is not None
            image = Image.open(io.BytesIO(source_bytes))
        with image:
            image = ImageOps.exif_transpose(image)
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image.thumbnail((max_width, max_height), resampling)
            if image.mode in {"RGBA", "LA"}:
                rgba = image.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, "white")
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")
            temporary = destination.with_suffix(".tmp.png")
            image.save(temporary, "PNG", optimize=True)
            temporary.replace(destination)
            return destination
    except (ImportError, OSError, ValueError, SyntaxError):
        pass

    # Desktop fallback: ask ImageMagick to make a PNG Tk can display.
    converter = shutil.which("magick") or shutil.which("convert")
    temporary_source: Path | None = None
    try:
        if dolphin_thumbnail is not None:
            source_path = dolphin_thumbnail
        else:
            assert source_bytes is not None
            with tempfile.NamedTemporaryFile(
                prefix="doku-cover-", suffix=source_suffix, delete=False
            ) as handle:
                handle.write(source_bytes)
                temporary_source = Path(handle.name)
            source_path = temporary_source

        if converter:
            temporary_output = destination.with_suffix(".tmp.png")
            command = [
                converter,
                str(source_path),
                "-auto-orient",
                "-thumbnail",
                f"{max_width}x{max_height}>",
                str(temporary_output),
            ]
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if completed.returncode == 0 and temporary_output.is_file():
                temporary_output.replace(destination)
                return destination

        # A cached Dolphin thumbnail is already PNG, so it remains usable even
        # without Pillow or ImageMagick; Tk will subsample it in the GUI.
        if dolphin_thumbnail is not None:
            shutil.copy2(dolphin_thumbnail, destination)
            return destination
    finally:
        if temporary_source is not None:
            temporary_source.unlink(missing_ok=True)

    return None


def choose_gui_font_family() -> str:
    """Choose a fontconfig family with Japanese glyphs when available."""
    try:
        completed = subprocess.run(
            ["fc-match", "-f", "%{family[0]}", "sans-serif:lang=ja"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        family = completed.stdout.strip()
        if family:
            return family
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    return "DejaVu Sans"


def launch_gui(
    initial_threshold: float = DEFAULT_THRESHOLD,
    initial_output_dir: Path = DEFAULT_OUTPUT_DIR,
    combined_root: Path = DEFAULT_COMBINED_ROOT,
    combined_max_images: int = 1,
    combined_max_archives: int = 1,
    combined_enable_limits: bool = False,
) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print(
            "error: Tkinter is not installed. Install your distribution's Tk/Python "
            "Tkinter package, or supply both input paths on the command line.",
            file=sys.stderr,
        )
        return 1

    root = tk.Tk()
    root.title(f"Doku Doujins Pairing Gallery v{APP_VERSION}")
    root.minsize(1280, 720)

    # Cover extraction can require opening a CBZ from slow/removable storage and
    # decoding its first image. Never do that on Tk's UI thread. A single worker
    # avoids saturating a USB drive while allowing all text rows to appear
    # immediately; covers fill in quietly afterward.
    thumbnail_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="doku-cover"
    )

    def close_gui() -> None:
        thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_gui)

    gui_font_family = choose_gui_font_family()
    root.option_add("*Font", (gui_font_family, 10))
    style = ttk.Style(root)
    style.configure(".", font=(gui_font_family, 10))
    style.configure("TNotebook.Tab", padding=(14, 7))

    # The gallery deliberately uses a white page with restrained borders so the
    # archive and candidate remain the visual focus.
    PAGE_BG = "#ffffff"
    CARD_BORDER = "#d6d9de"
    TEXT_MAIN = "#17191c"
    TEXT_MUTED = "#68707a"
    SCORE_BG = "#f2f3f5"
    SCORE_TEXT = "#30343a"
    LINK_TEXT = "#245f9e"

    output_dir = initial_output_dir.expanduser().resolve()
    combined_root = combined_root.expanduser().resolve()
    databases: list[Path] = []
    current_index = -1
    combined_databases: list[Path] = []
    combined_index = -1

    filename_var = tk.StringVar(value="No generated tables yet")
    run_counter_var = tk.StringVar(value="")
    table_status_var = tk.StringVar(value="")
    pairing_mode_var = tk.StringVar(value="closest")
    reassign_mode_var = tk.BooleanVar(value=False)
    search_var = tk.StringVar(value="")
    combined_directory_var = tk.StringVar(value="")
    combined_max_images_var = tk.IntVar(value=max(0, combined_max_images))
    combined_max_archives_var = tk.IntVar(value=max(0, combined_max_archives))
    combined_limits_enabled_var = tk.BooleanVar(value=bool(combined_enable_limits))
    combined_limits_label_var = tk.StringVar(value="")
    combined_loaded_var = tk.StringVar(value="")

    # Rebuilt whenever table 1 is rendered. Each archive owns a set of Boolean
    # variables, one per candidate; commands enforce at most one checkmark.
    candidate_selection_vars: dict[str, dict[str, "tk.BooleanVar"]] = {}
    candidate_selection_items: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_group_frames: dict[str, "tk.Frame"] = {}
    exact_group_frames: dict[str, "tk.Frame"] = {}
    candidate_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {}
    exact_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {}
    drag_state: dict[str, Any] = {
        "source_archive": "",
        "item": None,
        "ghost": None,
        "highlighted": None,
        "dragged": False,
        "pointer_x_root": 0,
        "pointer_y_root": 0,
        "autoscroll_job": None,
    }

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(1, weight=1)

    toolbar = ttk.Frame(outer)
    toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
    toolbar.columnconfigure(3, weight=1)

    older_button = ttk.Button(toolbar, text="◀ Older")
    older_button.grid(row=0, column=0, padx=(0, 6))

    newer_button = ttk.Button(toolbar, text="Newer ▶")
    newer_button.grid(row=0, column=1, padx=(0, 12))

    ttk.Label(toolbar, textvariable=run_counter_var).grid(
        row=0, column=2, sticky="w", padx=(0, 12)
    )
    ttk.Label(toolbar, textvariable=filename_var, font=(gui_font_family, 10, "bold")).grid(
        row=0, column=3, sticky="w"
    )

    mode_frame = ttk.Frame(toolbar)
    mode_frame.grid(row=0, column=4, padx=(12, 8))
    ttk.Radiobutton(
        mode_frame,
        text="DEFAULT CLOSEST MATCH",
        value="closest",
        variable=pairing_mode_var,
    ).pack(side="left", padx=(0, 8))
    ttk.Radiobutton(
        mode_frame,
        text="SELECT MODE",
        value="select",
        variable=pairing_mode_var,
    ).pack(side="left")

    reassign_button = ttk.Checkbutton(
        toolbar,
        text="REASSIGN JSON (DRAG)",
        variable=reassign_mode_var,
    )
    reassign_button.grid(row=0, column=5, padx=(4, 8))

    deselect_all_button = ttk.Button(toolbar, text="DESELECT ALL")
    deselect_all_button.grid(row=0, column=6, padx=(0, 8))

    new_pairing_button = ttk.Button(toolbar, text="New pairing…")
    new_pairing_button.grid(row=0, column=7, padx=(4, 6))

    open_database_button = ttk.Button(toolbar, text="Open database")
    open_database_button.grid(row=0, column=8, padx=(0, 6))

    open_folder_button = ttk.Button(toolbar, text="Open output folder")
    open_folder_button.grid(row=0, column=9, padx=(0, 8))

    submit_button = ttk.Button(toolbar, text="SUBMIT SELECTED →")
    submit_button.grid(row=0, column=10)

    search_frame = ttk.Frame(toolbar)
    search_frame.grid(row=1, column=0, columnspan=11, sticky="ew", pady=(9, 0))
    search_frame.columnconfigure(1, weight=1)
    ttk.Label(search_frame, text="FIND JSON:", font=(gui_font_family, 9, "bold")).grid(
        row=0, column=0, sticky="w", padx=(0, 8)
    )
    search_entry = ttk.Entry(search_frame, textvariable=search_var)
    search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
    search_button = ttk.Button(search_frame, text="SEARCH CURRENT RUN")
    search_button.grid(row=0, column=2, padx=(0, 6))
    clear_search_button = ttk.Button(
        search_frame, text="CLEAR", command=lambda: search_var.set("")
    )
    clear_search_button.grid(row=0, column=3)

    # These controls exist only while table 2 is active. They physically move
    # completed pairings into a structured ~/Combined run and then clear the
    # processed rows so the same SQL run can accept another selection batch.
    table2_actions = ttk.Frame(toolbar)
    table2_actions.grid(
        row=2, column=0, columnspan=11, sticky="ew", pady=(9, 0)
    )
    table2_actions.columnconfigure(3, weight=1)
    ttk.Label(
        table2_actions,
        text="TABLE 2 WORKFLOW:",
        font=(gui_font_family, 9, "bold"),
    ).grid(row=0, column=0, sticky="w", padx=(0, 8))
    combine_structure_button = ttk.Button(
        table2_actions, text="COMBINE AND STRUCTURE"
    )
    combine_structure_button.grid(row=0, column=1, padx=(0, 8))
    use_last_location_button = ttk.Button(
        table2_actions, text="USE LAST LOCATION"
    )
    use_last_location_button.grid(row=0, column=2, padx=(0, 12))
    last_combined_var = tk.StringVar(value="No previous combined location")
    ttk.Label(table2_actions, textvariable=last_combined_var).grid(
        row=0, column=3, sticky="w"
    )
    table2_actions.grid_remove()

    table3_actions = ttk.Frame(toolbar)
    table3_actions.grid(
        row=2, column=0, columnspan=11, sticky="ew", pady=(9, 0)
    )
    table3_actions.columnconfigure(1, weight=1)
    ttk.Label(
        table3_actions,
        text="TABLE 3 DIRECTORY:",
        font=(gui_font_family, 9, "bold"),
    ).grid(row=0, column=0, sticky="w", padx=(0, 8))
    combined_directory_combo = ttk.Combobox(
        table3_actions,
        textvariable=combined_directory_var,
        state="readonly",
        width=38,
    )
    combined_directory_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
    use_old_table_button = ttk.Button(table3_actions, text="REFRESH LIVE DIRECTORY")
    use_old_table_button.grid(row=0, column=2, padx=(0, 8))
    overwrite_table3_button = ttk.Button(
        table3_actions, text="CLEAR OLD TABLE + RESCAN"
    )
    overwrite_table3_button.grid(row=0, column=3, padx=(0, 8))
    reverse_batch_button = ttk.Button(table3_actions, text="REVERSE LAST BATCH")
    reverse_batch_button.grid(row=0, column=4, padx=(0, 10))
    ttk.Label(
        table3_actions,
        textvariable=combined_loaded_var,
        font=(gui_font_family, 8, "bold"),
    ).grid(row=0, column=5, sticky="e")

    limits_row = ttk.Frame(table3_actions)
    limits_row.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))

    limit_toggle = tk.Checkbutton(
        limits_row,
        textvariable=combined_limits_label_var,
        variable=combined_limits_enabled_var,
        indicatoron=False,
        relief="raised",
        borderwidth=2,
        padx=10,
        pady=4,
        font=(gui_font_family, 8, "bold"),
        cursor="hand2",
    )
    limit_toggle.pack(side="left", padx=(0, 10))

    limits_caption = ttk.Label(
        limits_row,
        text="IGNORE ONLY WHEN FILTER IS ON: images >",
        font=(gui_font_family, 8, "bold"),
    )
    limits_caption.pack(side="left", padx=(0, 5))
    max_images_spin = ttk.Spinbox(
        limits_row,
        from_=0,
        to=999999,
        textvariable=combined_max_images_var,
        width=5,
    )
    max_images_spin.pack(side="left", padx=(0, 8))
    ttk.Label(limits_row, text="OR CBZ/ZIP >").pack(side="left")
    max_archives_spin = ttk.Spinbox(
        limits_row,
        from_=0,
        to=999999,
        textvariable=combined_max_archives_var,
        width=5,
    )
    max_archives_spin.pack(side="left", padx=(4, 12))
    table3_note_var = tk.StringVar(
        value="FILTER OFF: scan every pairable direct directory under the selected Combined run"
    )
    ttk.Label(limits_row, textvariable=table3_note_var).pack(side="left", fill="x", expand=True)

    def refresh_combined_limit_controls(*_args: object) -> None:
        enabled = bool(combined_limits_enabled_var.get())
        if enabled:
            combined_limits_label_var.set("SAFETY FILTER ON")
            limit_toggle.configure(
                background="#2f6f44", foreground="white",
                activebackground="#285f3a", activeforeground="white",
                selectcolor="#2f6f44",
            )
            max_images_spin.state(["!disabled"])
            max_archives_spin.state(["!disabled"])
            table3_note_var.set(
                "FILTER ON: skip direct directories exceeding the image/archive limits"
            )
        else:
            combined_limits_label_var.set("SAFETY FILTER OFF — SCAN ALL")
            limit_toggle.configure(
                background="#b3261e", foreground="white",
                activebackground="#8f1f19", activeforeground="white",
                selectcolor="#b3261e",
            )
            max_images_spin.state(["disabled"])
            max_archives_spin.state(["disabled"])
            table3_note_var.set(
                "FILTER OFF: scan every pairable direct directory; counts are ignored"
            )

    limit_toggle.configure(command=refresh_combined_limit_controls)
    refresh_combined_limit_controls()
    table3_actions.grid_remove()

    notebook = ttk.Notebook(outer)
    notebook.grid(row=1, column=0, sticky="nsew")

    candidate_tab = ttk.Frame(notebook, padding=0)
    exact_tab = ttk.Frame(notebook, padding=0)
    combined_tab = ttk.Frame(notebook, padding=0)
    notebook.add(candidate_tab, text="1  Candidate pairings")
    notebook.add(exact_tab, text="2  Selected pairings")
    notebook.add(combined_tab, text="3  Combined output")

    def make_scroll_gallery(parent: "tk.Misc") -> tuple["tk.Canvas", "tk.Frame"]:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            parent,
            background=PAGE_BG,
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(canvas, background=PAGE_BG, padx=14, pady=14)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def refresh_scrollregion(_event: object | None = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_width(event: "tk.Event") -> None:
            canvas.itemconfigure(window_id, width=max(event.width, 1))

        def wheel(event: "tk.Event") -> str:
            if getattr(event, "num", None) == 4:
                canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(3, "units")
            elif getattr(event, "delta", 0):
                canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        inner.bind("<Configure>", refresh_scrollregion)
        canvas.bind("<Configure>", fit_width)
        for widget in (canvas, inner):
            widget.bind("<MouseWheel>", wheel)
            widget.bind("<Button-4>", wheel)
            widget.bind("<Button-5>", wheel)
        return canvas, inner

    candidate_canvas, candidate_gallery = make_scroll_gallery(candidate_tab)
    exact_canvas, exact_gallery = make_scroll_gallery(exact_tab)
    combined_canvas, combined_gallery = make_scroll_gallery(combined_tab)

    def widget_is_inside(widget: "tk.Misc | None", ancestor: "tk.Misc") -> bool:
        """Return True when widget is ancestor or one of its descendants."""
        current = widget
        while current is not None:
            if current == ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    def route_gallery_wheel(event: "tk.Event") -> str | None:
        """Scroll the gallery under the pointer, even when it is over a card/button.

        Tk mouse-wheel events do not bubble from deeply nested child widgets.  The
        old Table 3 therefore rendered every row but appeared to contain only its
        first card because wheel events over that card never reached the canvas.
        """
        try:
            target = root.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            return None
        chosen: "tk.Canvas | None" = None
        for canvas, inner in (
            (candidate_canvas, candidate_gallery),
            (exact_canvas, exact_gallery),
            (combined_canvas, combined_gallery),
        ):
            if widget_is_inside(target, canvas) or widget_is_inside(target, inner):
                chosen = canvas
                break
        if chosen is None:
            return None
        if getattr(event, "num", None) == 4:
            chosen.yview_scroll(-3, "units")
        elif getattr(event, "num", None) == 5:
            chosen.yview_scroll(3, "units")
        else:
            delta = int(getattr(event, "delta", 0) or 0)
            if delta:
                chosen.yview_scroll(-max(-8, min(8, int(delta / 120) or (1 if delta > 0 else -1))), "units")
        return "break"

    root.bind_all("<MouseWheel>", route_gallery_wheel, add="+")
    root.bind_all("<Button-4>", route_gallery_wheel, add="+")
    root.bind_all("<Button-5>", route_gallery_wheel, add="+")

    status_bar = ttk.Frame(outer)
    status_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
    status_bar.columnconfigure(0, weight=1)
    ttk.Label(status_bar, textvariable=table_status_var).grid(row=0, column=0, sticky="w")
    ttk.Label(
        status_bar,
        text=f"Click a card to open it · REASSIGN JSON lets you drag a JSON to another archive · Font: {gui_font_family}",
    ).grid(row=0, column=1, sticky="e")

    def clear_gallery(frame: "tk.Frame") -> None:
        for child in frame.winfo_children():
            child.destroy()

    def shortened(path_text: str) -> str:
        path = Path(path_text)
        try:
            return str(path.relative_to(Path.home()))
        except ValueError:
            return str(path)

    def open_existing(path_text: str) -> None:
        if not path_text:
            return
        path = Path(path_text).expanduser()
        if path.exists():
            if path.is_file() and path.suffix.casefold() == ".json":
                open_json_in_browser(path)
            else:
                open_with_desktop(path)
        else:
            messagebox.showwarning("File not found", str(path), parent=root)

    def reveal_existing(path_text: str) -> None:
        if not path_text:
            return
        path = Path(path_text).expanduser()
        if path.exists():
            reveal_in_file_manager(path)
        else:
            messagebox.showwarning("File not found", str(path), parent=root)

    def open_source_url(url: str) -> None:
        if not url:
            return
        try:
            open_external_url(url)
        except (OSError, ValueError, webbrowser.Error) as exc:
            messagebox.showwarning("Could not open Source-URL", str(exc), parent=root)

    def bind_press_action(widget: "tk.Widget", action: Callable[[], None]) -> None:
        """Run a link action on mouse press, before canvas movement can cancel release."""
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass

        def activate(_event: "tk.Event | None" = None) -> str:
            action()
            return "break"

        widget.bind("<ButtonPress-1>", activate)
        widget.bind("<Return>", activate)
        widget.bind("<space>", activate)

    def bind_file_link(widget: "tk.Widget", path_text: str) -> None:
        """Make a file-path label an unconditional link, separate from drag logic."""
        if not path_text:
            return
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass

        def activate(_event: "tk.Event | None" = None, p: str = path_text) -> str:
            open_existing(p)
            return "break"

        widget.bind("<ButtonRelease-1>", activate)
        widget.bind("<Return>", activate)
        widget.bind("<space>", activate)
        widget.bind(
            "<Button-3>",
            lambda _event, p=path_text: (reveal_existing(p), "break")[1],
        )

    def bind_clickable(widget: "tk.Widget", path_text: str) -> None:
        """Make a card open its file. Dragging is restricted to its own handle."""
        if not path_text:
            return
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass

        def open_on_release(_event: "tk.Event", p: str = path_text) -> str:
            open_existing(p)
            return "break"

        widget.bind("<ButtonRelease-1>", open_on_release)
        widget.bind(
            "<Button-3>",
            lambda _event, p=path_text: reveal_existing(p),
            add="+",
        )

    def bind_clickable_tree(widget: "tk.Widget", path_text: str) -> None:
        bind_clickable(widget, path_text)
        for child in widget.winfo_children():
            bind_clickable_tree(child, path_text)

    def archive_at_pointer(x_root: int, y_root: int) -> str:
        widget = root.winfo_containing(x_root, y_root)
        while widget is not None:
            for archive_file, group in candidate_group_frames.items():
                if widget == group:
                    return archive_file
            widget = getattr(widget, "master", None)
        return ""

    def clear_drag_highlight() -> None:
        highlighted = drag_state.get("highlighted")
        if highlighted is not None:
            try:
                highlighted.configure(
                    highlightbackground=CARD_BORDER, highlightthickness=1
                )
            except tk.TclError:
                pass
        drag_state["highlighted"] = None

    def highlight_drop_target(archive_file: str) -> None:
        clear_drag_highlight()
        group = candidate_group_frames.get(archive_file)
        if group is None:
            return
        try:
            group.configure(highlightbackground=LINK_TEXT, highlightthickness=3)
            drag_state["highlighted"] = group
        except tk.TclError:
            pass

    def stop_drag_autoscroll() -> None:
        job = drag_state.get("autoscroll_job")
        if job is not None:
            try:
                root.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        drag_state["autoscroll_job"] = None

    def update_drop_target_from_pointer() -> None:
        x_root = int(drag_state.get("pointer_x_root", 0))
        y_root = int(drag_state.get("pointer_y_root", 0))
        target_archive = archive_at_pointer(x_root, y_root)
        if target_archive and target_archive != drag_state.get("source_archive"):
            highlight_drop_target(target_archive)
        else:
            clear_drag_highlight()

    def drag_autoscroll_tick() -> None:
        drag_state["autoscroll_job"] = None
        if not reassign_mode_var.get() or drag_state.get("item") is None:
            return

        x_root = int(drag_state.get("pointer_x_root", 0))
        y_root = int(drag_state.get("pointer_y_root", 0))
        canvas_left = candidate_canvas.winfo_rootx()
        canvas_top = candidate_canvas.winfo_rooty()
        canvas_right = canvas_left + candidate_canvas.winfo_width()
        canvas_bottom = canvas_top + candidate_canvas.winfo_height()
        edge = 82
        direction = 0

        # Permit the pointer to sit just outside the canvas as well as directly
        # on its top/bottom edge. Scrolling continues without further mouse motion.
        if canvas_left - 24 <= x_root <= canvas_right + 24:
            if canvas_top - 24 <= y_root <= canvas_top + edge:
                depth = max(0, canvas_top + edge - y_root)
                direction = -max(1, min(7, 1 + depth // 16))
            elif canvas_bottom - edge <= y_root <= canvas_bottom + 24:
                depth = max(0, y_root - (canvas_bottom - edge))
                direction = max(1, min(7, 1 + depth // 16))

        if direction:
            candidate_canvas.yview_scroll(direction, "units")
            candidate_canvas.update_idletasks()
            update_drop_target_from_pointer()
            drag_state["autoscroll_job"] = root.after(45, drag_autoscroll_tick)

    def ensure_drag_autoscroll() -> None:
        if drag_state.get("autoscroll_job") is None:
            drag_state["autoscroll_job"] = root.after(45, drag_autoscroll_tick)

    def scroll_archive_into_view(archive_file: str) -> None:
        """Bring a reassignment destination into view and briefly outline it."""
        def perform() -> None:
            group = candidate_group_frames.get(archive_file)
            if group is None:
                return
            candidate_gallery.update_idletasks()
            content_height = max(candidate_gallery.winfo_height(), 1)
            viewport_height = max(candidate_canvas.winfo_height(), 1)
            maximum = max(content_height - viewport_height, 1)
            desired_y = max(0, group.winfo_y() - 34)
            candidate_canvas.yview_moveto(min(1.0, desired_y / maximum))
            try:
                group.configure(highlightbackground=LINK_TEXT, highlightthickness=4)
            except tk.TclError:
                return

            def restore() -> None:
                current = candidate_group_frames.get(archive_file)
                if current is group:
                    try:
                        group.configure(
                            highlightbackground=CARD_BORDER,
                            highlightthickness=1,
                        )
                    except tk.TclError:
                        pass

            root.after(1800, restore)

        root.after_idle(perform)

    def jump_to_pairing_location(
        *,
        table_number: int,
        archive_file: str,
        json_file: str,
    ) -> None:
        """Switch tabs, scroll to the owning CBZ block, and highlight the JSON card."""
        if table_number == 2:
            notebook.select(exact_tab)
            canvas = exact_canvas
            gallery = exact_gallery
            group = exact_group_frames.get(archive_file)
            card = exact_json_card_frames.get((archive_file, json_file))
        else:
            notebook.select(candidate_tab)
            canvas = candidate_canvas
            gallery = candidate_gallery
            group = candidate_group_frames.get(archive_file)
            card = candidate_json_card_frames.get((archive_file, json_file))

        if group is None:
            table_status_var.set(
                "The pairing exists in SQL but is not currently visible in this gallery table."
            )
            return

        def perform() -> None:
            root.deiconify()
            root.lift()
            try:
                root.focus_force()
            except tk.TclError:
                pass
            gallery.update_idletasks()
            canvas.update_idletasks()

            content_height = max(gallery.winfo_height(), 1)
            viewport_height = max(canvas.winfo_height(), 1)
            maximum = max(content_height - viewport_height, 1)
            target_y = group.winfo_y()
            if card is not None:
                target_y += card.winfo_y()
            desired_y = max(0, target_y - 52)
            canvas.yview_moveto(min(1.0, desired_y / maximum))
            canvas.update_idletasks()

            highlighted: list[tuple["tk.Frame", str, int]] = []
            for widget, thickness in ((group, 4), (card, 5)):
                if widget is None:
                    continue
                try:
                    old_colour = str(widget.cget("highlightbackground"))
                    old_thickness = int(widget.cget("highlightthickness"))
                    highlighted.append((widget, old_colour, old_thickness))
                    widget.configure(
                        highlightbackground=LINK_TEXT,
                        highlightcolor=LINK_TEXT,
                        highlightthickness=thickness,
                    )
                except (tk.TclError, TypeError, ValueError):
                    pass

            def restore() -> None:
                for widget, colour, thickness in highlighted:
                    try:
                        widget.configure(
                            highlightbackground=colour,
                            highlightthickness=thickness,
                        )
                    except tk.TclError:
                        pass

            root.after(2400, restore)
            table_status_var.set(
                f"Located {Path(json_file).name} inside {Path(archive_file).name}."
            )

        root.after_idle(perform)

    def begin_json_drag(
        event: "tk.Event",
        source_archive: str,
        item: dict[str, Any],
    ) -> str | None:
        if not reassign_mode_var.get():
            return None
        stop_drag_autoscroll()
        drag_state["source_archive"] = source_archive
        drag_state["item"] = dict(item)
        drag_state["dragged"] = False
        drag_state["pointer_x_root"] = event.x_root
        drag_state["pointer_y_root"] = event.y_root

        ghost = tk.Toplevel(root)
        ghost.overrideredirect(True)
        ghost.attributes("-topmost", True)
        ghost.configure(background=LINK_TEXT, padx=1, pady=1)
        tk.Label(
            ghost,
            text=f"DRAG JSON  {item.get('title') or Path(str(item.get('json_file', ''))).name}",
            background=PAGE_BG,
            foreground=TEXT_MAIN,
            font=(gui_font_family, 10, "bold"),
            padx=12,
            pady=8,
            wraplength=420,
        ).pack()
        ghost.geometry(f"+{event.x_root + 14}+{event.y_root + 14}")
        drag_state["ghost"] = ghost
        root.configure(cursor="fleur")
        return "break"

    def move_json_drag(event: "tk.Event") -> str | None:
        if not reassign_mode_var.get() or drag_state.get("item") is None:
            return None
        drag_state["dragged"] = True
        drag_state["pointer_x_root"] = event.x_root
        drag_state["pointer_y_root"] = event.y_root
        ensure_drag_autoscroll()
        ghost = drag_state.get("ghost")
        if ghost is not None:
            try:
                ghost.geometry(f"+{event.x_root + 14}+{event.y_root + 14}")
            except tk.TclError:
                pass
        update_drop_target_from_pointer()
        return "break"

    def finish_json_drag(event: "tk.Event") -> str | None:
        if not reassign_mode_var.get() or drag_state.get("item") is None:
            return None
        drag_state["pointer_x_root"] = event.x_root
        drag_state["pointer_y_root"] = event.y_root
        stop_drag_autoscroll()
        target_archive = archive_at_pointer(event.x_root, event.y_root)
        source_archive = str(drag_state.get("source_archive", ""))
        item = drag_state.get("item")
        ghost = drag_state.get("ghost")
        if ghost is not None:
            try:
                ghost.destroy()
            except tk.TclError:
                pass
        clear_drag_highlight()
        root.configure(cursor="")
        drag_state.update(
            {
                "source_archive": "",
                "item": None,
                "ghost": None,
                "dragged": False,
                "pointer_x_root": 0,
                "pointer_y_root": 0,
                "autoscroll_job": None,
            }
        )

        if not target_archive or target_archive == source_archive or not isinstance(item, dict):
            table_status_var.set(
                "Reassignment cancelled. Drag a JSON card onto a different archive block."
            )
            return "break"

        database_path = current_database()
        if database_path is None:
            return "break"
        try:
            moved = reassign_candidate_json(
                database_path, source_archive, target_archive, item
            )
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("Could not reassign JSON", str(exc), parent=root)
            return "break"

        if moved:
            reassign_mode_var.set(False)
            pairing_mode_var.set("select")
            show_database(current_index)
            scroll_archive_into_view(target_archive)
            table_status_var.set(
                "JSON moved to the new archive block without recalculating its score. "
                "The destination is outlined; SELECT MODE is active so you can check it."
            )
        return "break"

    def bind_json_drag(
        widget: "tk.Widget",
        source_archive: str,
        item: dict[str, Any],
    ) -> None:
        widget.bind(
            "<ButtonPress-1>",
            lambda event, a=source_archive, i=item: begin_json_drag(event, a, i),
            add="+",
        )
        widget.bind("<B1-Motion>", move_json_drag, add="+")
        widget.bind("<ButtonRelease-1>", finish_json_drag, add="+")

    def make_work_card(
        parent: "tk.Misc",
        *,
        title: str,
        path_text: str,
        role: str,
        related_files: Sequence[str] = (),
        source_url: str = "",
        selection_var: "tk.BooleanVar | None" = None,
        selection_command: Any = None,
        drag_source_archive: str = "",
        drag_item: dict[str, Any] | None = None,
    ) -> "tk.Frame":
        is_json_card = "json" in role.casefold()
        card = tk.Frame(
            parent,
            background=PAGE_BG,
            highlightbackground=CARD_BORDER,
            highlightcolor=LINK_TEXT if path_text else CARD_BORDER,
            highlightthickness=1,
            padx=13,
            pady=10,
        )
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=0)

        role_text = role.upper()
        if drag_item is not None:
            role_text += "  ·  DRAG ONLY FROM THE HANDLE"
        role_label = tk.Label(
            card,
            text=role_text,
            background=PAGE_BG,
            foreground=TEXT_MUTED,
            font=(gui_font_family, 8, "bold"),
            anchor="w",
        )
        role_label.grid(row=0, column=0, sticky="ew")

        selector: "tk.Checkbutton | None" = None
        if selection_var is not None:
            selector = tk.Checkbutton(
                card,
                text="SELECT",
                variable=selection_var,
                command=selection_command,
                onvalue=True,
                offvalue=False,
                background=PAGE_BG,
                activebackground=PAGE_BG,
                foreground=TEXT_MAIN,
                activeforeground=TEXT_MAIN,
                selectcolor=PAGE_BG,
                font=(gui_font_family, 8, "bold"),
                cursor="hand2",
                padx=0,
                pady=0,
            )
            selector.grid(row=0, column=1, sticky="e", padx=(10, 0))

        visible_title = title or (Path(path_text).name if path_text else "")
        if is_json_card and path_text:
            # A genuine button is used here deliberately. No ButtonRelease binding,
            # no card-wide drag binding, and therefore no chance for a normal click
            # to be interpreted as movement.
            title_label = tk.Button(
                card,
                text=visible_title,
                command=lambda p=path_text: reveal_existing(p),
                background=PAGE_BG,
                activebackground=PAGE_BG,
                foreground=TEXT_MAIN,
                activeforeground=TEXT_MAIN,
                font=(gui_font_family, 11, "bold"),
                anchor="w",
                justify="left",
                wraplength=430,
                cursor="hand2",
                relief="flat",
                overrelief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=0,
                pady=0,
                takefocus=True,
            )
        else:
            title_label = tk.Label(
                card,
                text=visible_title,
                background=PAGE_BG,
                foreground=TEXT_MAIN,
                font=(gui_font_family, 11, "bold"),
                anchor="w",
                justify="left",
                wraplength=430,
            )
        title_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(3, 2))
        if is_json_card and path_text:
            bind_press_action(title_label, lambda p=path_text: reveal_existing(p))

        if is_json_card and path_text:
            path_label = tk.Button(
                card,
                text=shortened(path_text),
                command=lambda p=path_text: reveal_existing(p),
                background=PAGE_BG,
                activebackground=PAGE_BG,
                foreground=LINK_TEXT,
                activeforeground=LINK_TEXT,
                font=(gui_font_family, 9, "underline"),
                anchor="w",
                justify="left",
                wraplength=430,
                cursor="hand2",
                relief="flat",
                overrelief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=0,
                pady=0,
                takefocus=True,
            )
        else:
            path_label = tk.Label(
                card,
                text=shortened(path_text) if path_text else "",
                background=PAGE_BG,
                foreground=LINK_TEXT,
                font=(gui_font_family, 9, "underline"),
                anchor="w",
                justify="left",
                wraplength=430,
                cursor="hand2" if path_text else "",
                takefocus=bool(path_text),
            )
        path_label.grid(row=2, column=0, columnspan=2, sticky="ew")
        if is_json_card and path_text:
            bind_press_action(path_label, lambda p=path_text: reveal_existing(p))

        next_row = 3
        source_button_inline: "tk.Button | None" = None
        drag_handle: "tk.Label | None" = None
        if source_url and is_json_card:
            source_button_inline = tk.Button(
                card,
                text=f"SOURCE-URL: {source_url}",
                command=lambda url=source_url: open_source_url(url),
                background=PAGE_BG,
                activebackground=PAGE_BG,
                foreground=LINK_TEXT,
                activeforeground=LINK_TEXT,
                font=(gui_font_family, 9, "underline"),
                anchor="w",
                justify="left",
                wraplength=430,
                cursor="hand2",
                relief="flat",
                overrelief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=0,
                pady=0,
                takefocus=True,
            )
            source_button_inline.grid(
                row=next_row, column=0, columnspan=2, sticky="ew", pady=(5, 0)
            )
            bind_press_action(
                source_button_inline,
                lambda url=source_url: open_source_url(url),
            )
            next_row += 1

        if path_text:
            actions = tk.Frame(card, background=PAGE_BG)
            actions.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(7, 0))
            action_word = "JSON" if is_json_card else "ARCHIVE"
            open_button = tk.Button(
                actions,
                text=f"OPEN {action_word} ↗",
                command=lambda p=path_text: open_existing(p),
                background=PAGE_BG,
                activebackground=PAGE_BG,
                foreground=LINK_TEXT,
                activeforeground=LINK_TEXT,
                font=(gui_font_family, 8, "bold", "underline"),
                cursor="hand2",
                relief="flat",
                overrelief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=0,
                pady=0,
                takefocus=True,
            )
            open_button.pack(side="left")
            if is_json_card:
                bind_press_action(open_button, lambda p=path_text: open_existing(p))
            reveal_button = tk.Button(
                actions,
                text="SHOW IN FOLDER",
                command=lambda p=path_text: reveal_existing(p),
                background=PAGE_BG,
                activebackground=PAGE_BG,
                foreground=TEXT_MUTED,
                activeforeground=TEXT_MUTED,
                font=(gui_font_family, 8, "underline"),
                cursor="hand2",
                relief="flat",
                overrelief="flat",
                borderwidth=0,
                highlightthickness=0,
                padx=14,
                pady=0,
                takefocus=True,
            )
            reveal_button.pack(side="left")
            if is_json_card:
                bind_press_action(reveal_button, lambda p=path_text: reveal_existing(p))
            if source_url and is_json_card:
                source_button = tk.Button(
                    actions,
                    text="OPEN SOURCE-URL ↗",
                    command=lambda url=source_url: open_source_url(url),
                    background=PAGE_BG,
                    activebackground=PAGE_BG,
                    foreground=LINK_TEXT,
                    activeforeground=LINK_TEXT,
                    font=(gui_font_family, 8, "bold", "underline"),
                    cursor="hand2",
                    relief="flat",
                    overrelief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    padx=14,
                    pady=0,
                    takefocus=True,
                )
                source_button.pack(side="left")
                bind_press_action(
                    source_button,
                    lambda url=source_url: open_source_url(url),
                )
            if drag_item is not None and drag_source_archive:
                drag_handle = tk.Label(
                    actions,
                    text="DRAG JSON ↕",
                    background=PAGE_BG,
                    foreground=TEXT_MUTED,
                    font=(gui_font_family, 8, "bold"),
                    cursor="fleur",
                    padx=14,
                    pady=0,
                )
                drag_handle.pack(side="left")
            next_row += 1

        if related_files:
            separator = tk.Frame(card, background=CARD_BORDER, height=1)
            separator.grid(row=next_row, column=0, columnspan=2, sticky="ew", pady=(8, 6))
            next_row += 1
            related_heading = tk.Label(
                card,
                text=f"RELATED FILES ({len(related_files)})",
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 8, "bold"),
                anchor="w",
            )
            related_heading.grid(row=next_row, column=0, columnspan=2, sticky="ew")
            next_row += 1
            for related in related_files[:8]:
                related_button = tk.Button(
                    card,
                    text=Path(related).name,
                    command=lambda p=related: open_existing(p),
                    background=PAGE_BG,
                    activebackground=PAGE_BG,
                    foreground=LINK_TEXT,
                    activeforeground=LINK_TEXT,
                    font=(gui_font_family, 9, "underline"),
                    anchor="w",
                    justify="left",
                    wraplength=430,
                    cursor="hand2",
                    relief="flat",
                    overrelief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    padx=0,
                    pady=0,
                )
                related_button.grid(
                    row=next_row, column=0, columnspan=2, sticky="ew", pady=(2, 0)
                )
                related_button.bind(
                    "<Button-3>", lambda _event, p=related: reveal_existing(p)
                )
                next_row += 1
            if len(related_files) > 8:
                tk.Label(
                    card,
                    text=f"…and {len(related_files) - 8} more",
                    background=PAGE_BG,
                    foreground=TEXT_MUTED,
                    font=(gui_font_family, 9),
                    anchor="w",
                ).grid(
                    row=next_row,
                    column=0,
                    columnspan=2,
                    sticky="ew",
                    pady=(2, 0),
                )

        # Archive cards retain their broad clickable surface. JSON cards do not
        # receive any card-wide ButtonRelease or drag binding: only their real
        # buttons act on clicks, and only DRAG JSON starts a drag.
        if not is_json_card:
            bind_clickable(card, path_text)
            for widget in (role_label, title_label):
                bind_clickable(widget, path_text)
            bind_file_link(path_label, path_text)
        if drag_handle is not None and drag_item is not None and drag_source_archive:
            bind_json_drag(drag_handle, drag_source_archive, drag_item)
        if selector is not None:
            selector.unbind("<ButtonRelease-1>")
        return card

    def make_thumbnail_card(parent: "tk.Misc", archive_file: str) -> "tk.Frame":
        card = tk.Frame(
            parent,
            background=PAGE_BG,
            highlightbackground=CARD_BORDER,
            highlightcolor=LINK_TEXT,
            highlightthickness=1,
            padx=8,
            pady=8,
        )
        card.columnconfigure(0, weight=1)
        archive_path = Path(archive_file).expanduser()

        # Draw the row now. The old implementation generated the cover here,
        # synchronously, which meant Table 3 stopped after the first card while
        # Python opened/decompressed CBZ files on the USB drive.
        image_label = tk.Label(
            card,
            text="Loading cover…",
            background=PAGE_BG,
            foreground=TEXT_MUTED,
            wraplength=170,
            padx=10,
            pady=30,
        )
        image_label.grid(row=0, column=0)

        def apply_thumbnail(thumbnail_path: Path | None) -> None:
            try:
                if not image_label.winfo_exists():
                    return
            except tk.TclError:
                return

            if thumbnail_path is None:
                image_label.configure(text="No archive cover found", image="")
                image_label.image = None  # type: ignore[attr-defined]
                return

            try:
                photo = tk.PhotoImage(file=str(thumbnail_path))
                factor = max(
                    1,
                    (photo.width() + 189) // 190,
                    (photo.height() + 249) // 250,
                )
                if factor > 1:
                    photo = photo.subsample(factor, factor)
                image_label.configure(image=photo, text="", cursor="hand2")
                image_label.image = photo  # type: ignore[attr-defined]
            except (tk.TclError, OSError):
                image_label.configure(text="Cover could not be displayed", image="")
                image_label.image = None  # type: ignore[attr-defined]

        def thumbnail_finished(future: object) -> None:
            try:
                thumbnail_path = future.result()  # type: ignore[attr-defined]
            except Exception:
                thumbnail_path = None
            try:
                root.after(0, apply_thumbnail, thumbnail_path)
            except (RuntimeError, tk.TclError):
                pass

        future = thumbnail_executor.submit(build_archive_thumbnail_png, archive_path)
        future.add_done_callback(thumbnail_finished)

        hint = tk.Label(
            card,
            text="OPEN ARCHIVE ↗",
            background=PAGE_BG,
            foreground=LINK_TEXT,
            font=(gui_font_family, 8, "bold", "underline"),
            cursor="hand2",
        )
        hint.grid(row=1, column=0, pady=(7, 0))
        bind_clickable_tree(card, archive_file)
        return card

    def make_blank_left(parent: "tk.Misc") -> "tk.Frame":
        # This is intentional: extra candidates align beneath the first archive,
        # leaving calm white space on the left rather than repeating the archive.
        blank = tk.Frame(parent, background=PAGE_BG, height=76)
        blank.grid_propagate(False)
        return blank

    def make_score(parent: "tk.Misc", score_text: str) -> "tk.Label":
        return tk.Label(
            parent,
            text=score_text,
            background=SCORE_BG,
            foreground=SCORE_TEXT,
            font=(gui_font_family, 10, "bold"),
            padx=10,
            pady=6,
        )

    def add_section_headers(frame: "tk.Frame", *, selectable: bool = False) -> None:
        header = tk.Frame(frame, background=PAGE_BG)
        header.pack(fill="x", pady=(0, 7))
        header.columnconfigure(0, weight=34)
        header.columnconfigure(1, weight=8)
        header.columnconfigure(2, weight=40)
        header.columnconfigure(3, weight=18)
        tk.Label(
            header,
            text="ARCHIVE WORK",
            background=PAGE_BG,
            foreground=TEXT_MUTED,
            font=(gui_font_family, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(2, 12))
        tk.Label(
            header,
            text="MATCH",
            background=PAGE_BG,
            foreground=TEXT_MUTED,
            font=(gui_font_family, 9, "bold"),
        ).grid(row=0, column=1)
        tk.Label(
            header,
            text="JSON WORK · CHECK ONE" if selectable else "JSON WORK",
            background=PAGE_BG,
            foreground=TEXT_MUTED,
            font=(gui_font_family, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=2, sticky="ew", padx=(12, 12))
        tk.Label(
            header,
            text="ARCHIVE COVER",
            background=PAGE_BG,
            foreground=TEXT_MUTED,
            font=(gui_font_family, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=3, sticky="ew", padx=(2, 2))

    def add_pair_group(
        gallery: "tk.Frame",
        archive_file: str,
        matches: Sequence[dict[str, Any]],
        *,
        exact_mode: bool = False,
        selection_vars: dict[str, "tk.BooleanVar"] | None = None,
        empty_message: str = "No match at or above the threshold",
    ) -> int:
        group = tk.Frame(
            gallery,
            background=PAGE_BG,
            highlightbackground=CARD_BORDER,
            highlightthickness=1,
            padx=10,
            pady=10,
        )
        group.pack(fill="x", pady=(0, 10))
        if exact_mode:
            exact_group_frames[archive_file] = group
        else:
            candidate_group_frames[archive_file] = group
        group.columnconfigure(0, weight=34, uniform="pair")
        group.columnconfigure(1, weight=8)
        group.columnconfigure(2, weight=40, uniform="pair")
        group.columnconfigure(3, weight=18)

        archive_title = Path(archive_file).stem
        usable_matches = list(matches)
        if not usable_matches:
            usable_matches = [
                {
                    "title": empty_message,
                    "json_file": "",
                    "match_percent": "",
                    "related_files": [],
                }
            ]

        for index, item in enumerate(usable_matches):
            if index == 0:
                left = make_work_card(
                    group,
                    title=archive_title,
                    path_text=archive_file,
                    role="Archive",
                )
            else:
                left = make_blank_left(group)
            left.grid(row=index, column=0, sticky="nsew", padx=(0, 12), pady=(0, 7))

            score = item.get("match_percent", "")
            try:
                score_text = f"{float(score):.2f}%"
            except (TypeError, ValueError):
                score_text = str(score)
            if item.get("manual_reassigned"):
                score_text = f"MANUAL\n{score_text}"
            score_label = make_score(group, score_text)
            score_label.grid(row=index, column=1, padx=4, pady=(10, 7), sticky="n")

            json_file = str(item.get("json_file", ""))
            title = str(item.get("title", item.get("original_page_name", "")))
            related = item.get("related_files", [])
            related_files = [str(value) for value in related if isinstance(value, str)]
            source_url = str(item.get("source_url", ""))
            if json_file and not source_url:
                source_url = source_url_from_json_file(json_file)

            selector_var: "tk.BooleanVar | None" = None
            selector_command: Any = None
            if selection_vars is not None and json_file:
                selector_var = selection_vars.get(json_file)

                def choose_one(
                    a: str = archive_file,
                    chosen_json: str = json_file,
                ) -> None:
                    chosen_var = candidate_selection_vars[a][chosen_json]
                    if chosen_var.get():
                        for other_json, other_var in candidate_selection_vars[a].items():
                            if other_json != chosen_json:
                                other_var.set(False)

                selector_command = choose_one

            right = make_work_card(
                group,
                title=title,
                path_text=json_file,
                role=("Selected JSON" if exact_mode else "JSON title") if json_file else "Result",
                related_files=related_files,
                source_url=source_url,
                selection_var=selector_var,
                selection_command=selector_command,
                drag_source_archive=archive_file if not exact_mode and json_file else "",
                drag_item=item if not exact_mode and json_file else None,
            )
            right.grid(row=index, column=2, sticky="nsew", padx=(12, 12), pady=(0, 7))
            if json_file:
                if exact_mode:
                    exact_json_card_frames[(archive_file, json_file)] = right
                else:
                    candidate_json_card_frames[(archive_file, json_file)] = right

        cover = make_thumbnail_card(group, archive_file)
        cover.grid(
            row=0,
            column=3,
            rowspan=max(1, len(usable_matches)),
            sticky="n",
            padx=(0, 0),
            pady=(0, 7),
        )
        return len(matches)

    def initial_selected_json(
        matches: Sequence[dict[str, Any]],
        *,
        archive_already_paired: bool,
    ) -> str:
        """Choose the initial checkmark according to the toolbar mode."""
        if archive_already_paired or not matches:
            return ""
        if pairing_mode_var.get() == "closest":
            return str(matches[0].get("json_file", ""))
        for item in matches:
            try:
                score = float(item.get("match_percent", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            if score == 100.0:
                return str(item.get("json_file", ""))
        return ""

    def populate_candidate_gallery(database_path: Path) -> tuple[int, int]:
        clear_gallery(candidate_gallery)
        candidate_selection_vars.clear()
        candidate_selection_items.clear()
        candidate_group_frames.clear()
        candidate_json_card_frames.clear()
        add_section_headers(candidate_gallery, selectable=True)

        already_selected = exact_jsons_by_archive(database_path)
        archive_count = 0
        match_count = 0
        for archive_file, all_matches in load_candidate_table(database_path):
            archive_count += 1
            selected_jsons = already_selected.get(archive_file, set())
            # Table 1 contains candidate JSONs not currently present in table 2.
            matches = [
                item
                for item in all_matches
                if str(item.get("json_file", "")) not in selected_jsons
            ]
            match_count += len(matches)

            vars_for_archive: dict[str, "tk.BooleanVar"] = {}
            for item in matches:
                json_file = str(item.get("json_file", ""))
                if not json_file:
                    continue
                vars_for_archive[json_file] = tk.BooleanVar(value=False)
                candidate_selection_items[(archive_file, json_file)] = item
            candidate_selection_vars[archive_file] = vars_for_archive

            selected = initial_selected_json(
                matches,
                archive_already_paired=bool(selected_jsons),
            )
            if selected in vars_for_archive:
                vars_for_archive[selected].set(True)

            if selected_jsons and not matches:
                empty_message = "Already paired in table 2 · no other candidates"
            elif selected_jsons:
                empty_message = "Already paired in table 2"
            else:
                empty_message = "No match at or above the threshold"

            add_pair_group(
                candidate_gallery,
                archive_file,
                matches,
                selection_vars=vars_for_archive,
                empty_message=empty_message,
            )
        candidate_canvas.yview_moveto(0)
        return archive_count, match_count

    def populate_exact_gallery(database_path: Path) -> tuple[int, int]:
        clear_gallery(exact_gallery)
        exact_group_frames.clear()
        exact_json_card_frames.clear()
        add_section_headers(exact_gallery, selectable=False)
        grouped: dict[str, list[dict[str, Any]]] = {}
        related_count = 0
        for (
            archive_file,
            title,
            json_file,
            related_files,
            source_url,
            match_percent,
            selection_method,
        ) in load_exact_table(database_path):
            grouped.setdefault(archive_file, []).append(
                {
                    "title": title,
                    "json_file": json_file,
                    "match_percent": match_percent,
                    "selection_method": selection_method,
                    "related_files": related_files,
                    "source_url": source_url,
                }
            )
            related_count += len(related_files)

        for archive_file, matches in grouped.items():
            add_pair_group(exact_gallery, archive_file, matches, exact_mode=True)

        if not grouped:
            tk.Label(
                exact_gallery,
                text="No selected pairings in this run. Check candidates in table 1, then press SUBMIT SELECTED.",
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 11),
                pady=24,
            ).pack(anchor="w")
        exact_canvas.yview_moveto(0)
        return sum(len(values) for values in grouped.values()), related_count


    def populate_combined_gallery(database_path: Path) -> tuple[int, int, int]:
        """Render Table 3 from one ~/Combined/<run>/combined-pairings.sqlite3."""
        clear_gallery(combined_gallery)
        add_section_headers(combined_gallery, selectable=False)
        rows, stored_sql_row_count = load_live_combined_inventory(database_path)
        moved_file_count = 0
        for item in rows:
            archive_file = str(item.get("destination_archive") or item.get("source_archive") or "")
            json_file = str(item.get("destination_json") or item.get("source_json") or "")
            moved_files = [str(value) for value in item.get("moved_files", [])]
            moved_file_count += len(moved_files)
            related_files = [
                path
                for path in moved_files
                if path not in {archive_file, json_file}
            ]
            add_pair_group(
                combined_gallery,
                archive_file,
                [
                    {
                        "title": str(item.get("title") or Path(json_file).stem),
                        "json_file": json_file,
                        "match_percent": item.get("match_percent", 0.0),
                        "selection_method": str(item.get("selection_method", "")),
                        "related_files": related_files,
                        "source_url": str(item.get("source_url", "")),
                    }
                ],
                exact_mode=True,
            )

            destination_directory = str(item.get("destination_directory", ""))
            if destination_directory:
                folder_row = tk.Frame(combined_gallery, background=PAGE_BG)
                folder_row.pack(fill="x", padx=12, pady=(-8, 10))
                folder_button = tk.Button(
                    folder_row,
                    text=f"OPEN COMBINED DIRECTORY ↗  {shortened(destination_directory)}",
                    command=lambda p=destination_directory: open_existing(p),
                    background=PAGE_BG,
                    activebackground=PAGE_BG,
                    foreground=LINK_TEXT,
                    activeforeground=LINK_TEXT,
                    font=(gui_font_family, 8, "bold", "underline"),
                    cursor="hand2",
                    relief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    anchor="w",
                )
                folder_button.pack(anchor="e")
                bind_press_action(
                    folder_button,
                    lambda p=destination_directory: open_existing(p),
                )

        shared_rows = load_combined_shared_rows(database_path)
        shared_count = sum(len(row.get("moved_files", [])) for row in shared_rows)
        if shared_rows:
            heading = tk.Label(
                combined_gallery,
                text="SHARED SOURCE-DIRECTORY FILES",
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 10, "bold"),
                anchor="w",
                pady=10,
            )
            heading.pack(fill="x")
            for row in shared_rows:
                destination = str(row.get("destination_directory", ""))
                files = [str(value) for value in row.get("moved_files", [])]
                card = tk.Frame(
                    combined_gallery,
                    background=PAGE_BG,
                    highlightbackground=CARD_BORDER,
                    highlightthickness=1,
                    padx=12,
                    pady=10,
                )
                card.pack(fill="x", pady=(0, 10))
                tk.Label(
                    card,
                    text=Path(destination).name or "Shared files",
                    background=PAGE_BG,
                    foreground=TEXT_MAIN,
                    font=(gui_font_family, 10, "bold"),
                    anchor="w",
                ).pack(fill="x")
                destination_button = tk.Button(
                    card,
                    text=shortened(destination),
                    command=lambda p=destination: open_existing(p),
                    background=PAGE_BG,
                    activebackground=PAGE_BG,
                    foreground=LINK_TEXT,
                    activeforeground=LINK_TEXT,
                    font=(gui_font_family, 9, "underline"),
                    cursor="hand2",
                    relief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    anchor="w",
                )
                destination_button.pack(fill="x", pady=(3, 6))
                bind_press_action(
                    destination_button,
                    lambda p=destination: open_existing(p),
                )
                tk.Label(
                    card,
                    text=f"{len(files)} file{'s' if len(files) != 1 else ''} preserved here",
                    background=PAGE_BG,
                    foreground=TEXT_MUTED,
                    font=(gui_font_family, 9),
                    anchor="w",
                ).pack(fill="x")

        if not rows and not shared_rows:
            tk.Label(
                combined_gallery,
                text="No combined pairings are stored in this manifest.",
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 11),
                pady=24,
            ).pack(anchor="w")
        # Force geometry and scrollregion calculation after all cards exist.  This
        # makes the scrollbar reflect every Table 3 row immediately instead of
        # retaining the height of the first rendered card.
        combined_gallery.update_idletasks()
        bbox = combined_canvas.bbox("all")
        if bbox is not None:
            combined_canvas.configure(scrollregion=bbox)
        combined_canvas.yview_moveto(0)
        combined_loaded_var.set(
            f"{len(rows)} LIVE DIRECTORIES LOADED · SQL LEDGER ROWS: {stored_sql_row_count}"
        )
        return len(rows), moved_file_count, shared_count

    def current_combined_database() -> Path | None:
        selected_name = combined_directory_var.get().strip()
        if selected_name:
            for path in combined_databases:
                if path.parent.name == selected_name:
                    return path
        if 0 <= combined_index < len(combined_databases):
            return combined_databases[combined_index]
        return None

    def current_database() -> Path | None:
        if 0 <= current_index < len(databases):
            return databases[current_index]
        return None

    def refresh_table2_actions() -> None:
        """Show combine controls only on table 2 and keep their state honest."""
        try:
            on_table_two = notebook.index(notebook.select()) == 1
        except tk.TclError:
            on_table_two = False

        if not on_table_two:
            table2_actions.grid_remove()
            return

        table2_actions.grid()
        database_path = current_database()
        exact_count = 0
        if database_path is not None:
            try:
                exact_count = len(load_exact_table(database_path))
            except (OSError, sqlite3.Error):
                exact_count = 0

        if exact_count:
            combine_structure_button.state(["!disabled"])
        else:
            combine_structure_button.state(["disabled"])

        previous = last_combined_directory()
        if previous is None:
            last_combined_var.set("No previous combined location")
            use_last_location_button.state(["disabled"])
        else:
            last_combined_var.set(f"Last: {shortened(str(previous))}")
            if exact_count:
                use_last_location_button.state(["!disabled"])
            else:
                use_last_location_button.state(["disabled"])

    def refresh_table3_actions() -> None:
        try:
            on_table_three = notebook.index(notebook.select()) == 2
        except tk.TclError:
            on_table_three = False
        if not on_table_three:
            table3_actions.grid_remove()
            return
        table3_actions.grid()
        database_path = current_combined_database()
        if database_path is None:
            use_old_table_button.state(["disabled"])
            overwrite_table3_button.state(["disabled"])
            reverse_batch_button.state(["disabled"])
            return

        overwrite_table3_button.state(["!disabled"])
        if database_path.is_file():
            use_old_table_button.state(["!disabled"])
        else:
            use_old_table_button.state(["disabled"])

        reversible = False
        reversible_pairs = 0
        reversible_moves = 0
        if database_path.is_file():
            try:
                connection = sqlite3.connect(database_path)
                ensure_combined_manifest_schema(connection)
                batch = connection.execute(
                    """
                    SELECT batch_id
                    FROM combined_batches
                    WHERE status='combined'
                    ORDER BY combined_at DESC, rowid DESC
                    LIMIT 1
                    """
                ).fetchone()
                if batch:
                    batch_id = str(batch[0])
                    pair_row = connection.execute(
                        "SELECT COUNT(*) FROM combined_pairings "
                        "WHERE batch_id=? AND status='combined'",
                        (batch_id,),
                    ).fetchone()
                    move_row = connection.execute(
                        "SELECT COUNT(*) FROM combined_file_moves "
                        "WHERE batch_id=? AND status='moved'",
                        (batch_id,),
                    ).fetchone()
                    reversible_pairs = int(pair_row[0] or 0) if pair_row else 0
                    reversible_moves = int(move_row[0] or 0) if move_row else 0
                    reversible = reversible_pairs > 0 and reversible_moves > 0
                connection.close()
            except sqlite3.Error:
                reversible = False
        if reversible:
            reverse_batch_button.configure(
                text=f"REVERSE LAST BATCH ({reversible_pairs} PAIRS / {reversible_moves} FILES)"
            )
            reverse_batch_button.state(["!disabled"])
        else:
            reverse_batch_button.configure(text="REVERSE LAST BATCH")
            reverse_batch_button.state(["disabled"])

    def use_old_current_table3() -> None:
        database_path = current_combined_database()
        if database_path is None:
            return
        if not database_path.is_file():
            messagebox.showinfo(
                "No old table",
                "This Combined directory has no combined-pairings.sqlite3 yet.\n\n"
                "Choose CLEAR OLD TABLE + RESCAN to rebuild it from its folders.",
                parent=root,
            )
            return
        show_combined_database(combined_index)

    def overwrite_current_table3() -> None:
        database_path = current_combined_database()
        if database_path is None:
            return
        try:
            max_images = int(combined_max_images_var.get())
            max_archives = int(combined_max_archives_var.get())
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror(
                "Invalid limits",
                "Image and CBZ/ZIP limits must be whole numbers.",
                parent=root,
            )
            return
        if max_images < 0 or max_archives < 0:
            messagebox.showerror(
                "Invalid limits",
                "Image and CBZ/ZIP limits cannot be negative.",
                parent=root,
            )
            return

        enforce_limits = bool(combined_limits_enabled_var.get())
        filter_text = (
            f"SAFETY FILTER ON: ignore a direct directory when images > {max_images} "
            f"OR CBZ/ZIP > {max_archives}."
            if enforce_limits
            else "SAFETY FILTER OFF: scan every direct directory containing at least one CBZ/ZIP and one JSON."
        )
        prompt = (
            f"Delete the old Table 3 display rows and rebuild from every pairable\n"
            f"directory anywhere beneath:\n\n{database_path.parent}\n\n"
            f"{filter_text}\n"
            "When the filter is OFF, multiple archives/JSON files are resolved from "
            "the ledger, old table, metadata, and stable filename ranking.\n\n"
            "The physical files will NOT move. The old SQLite/SQL manifest is backed "
            "up first. The display table is recreated from scratch; batch and file-move "
            "ledgers remain available for reversibility."
        )
        if not messagebox.askyesno(
            "Clear old Table 3 and rescan?", prompt, parent=root
        ):
            return

        root.configure(cursor="watch")
        root.update_idletasks()
        try:
            result = rebuild_combined_manifest_from_directories(
                database_path,
                enforce_limits=enforce_limits,
                max_images=max_images,
                max_archives=max_archives,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            root.configure(cursor="")
            messagebox.showerror("Table 3 overwrite failed", str(exc), parent=root)
            return
        root.configure(cursor="")
        reload_combined_databases(select=result.manifest_database, display=True)

        ignored_preview = ""
        if result.ignored_reasons:
            preview = "\n".join(
                f"• {Path(directory).name}: {reason}"
                for directory, reason in result.ignored_reasons[:8]
            )
            if len(result.ignored_reasons) > 8:
                preview += f"\n• …and {len(result.ignored_reasons) - 8} more"
            ignored_preview = f"\n\nIgnored:\n{preview}"
        backup_text = (
            f"\n\nOld manifest backup:\n{result.backup_database}"
            if result.backup_database is not None
            else ""
        )
        messagebox.showinfo(
            "Table 3 rebuilt",
            f"Pairing directories added: {result.paired_directories}\n"
            f"Directories ignored: {result.ignored_directories}\n"
            f"Manifest:\n{result.manifest_database}"
            f"{backup_text}{ignored_preview}",
            parent=root,
        )

    def combined_directory_selected(_event: object | None = None) -> None:
        selected_name = combined_directory_var.get().strip()
        if not selected_name:
            return
        for index, path in enumerate(combined_databases):
            if path.parent.name == selected_name:
                show_combined_database(index)
                return

    def repair_current_table3() -> None:
        database_path = current_combined_database()
        if database_path is None:
            return
        root.configure(cursor="watch")
        root.update_idletasks()
        try:
            added = repair_combined_manifest_from_disk(database_path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            messagebox.showerror("Table 3 repair failed", str(exc), parent=root)
        else:
            show_combined_database(combined_index)
            messagebox.showinfo(
                "Table 3 repaired",
                f"Added {added} missing pairing row{'s' if added != 1 else ''}.\n\n"
                "Table 3 now mirrors the files currently present in this Combined run.",
                parent=root,
            )
        finally:
            root.configure(cursor="")
            refresh_table3_actions()

    def reverse_current_combined_batch() -> None:
        database_path = current_combined_database()
        if database_path is None:
            return
        confirmed = messagebox.askyesno(
            "Reverse last combined batch",
            "This will move the newest fully-ledgered batch back to its exact original "
            "paths and restore its approved rows to Table 2.\n\nContinue?",
            icon="warning",
            parent=root,
        )
        if not confirmed:
            return
        root.configure(cursor="watch")
        root.update_idletasks()
        try:
            batch_id, moved_count = reverse_latest_combined_batch(database_path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            messagebox.showerror("Reverse failed", str(exc), parent=root)
            root.configure(cursor="")
            refresh_table3_actions()
            return
        root.configure(cursor="")
        reload_combined_databases(select=database_path, display=False)
        reload_databases()
        notebook.select(exact_tab)
        show_database(current_index)
        messagebox.showinfo(
            "Combined batch reversed",
            f"Batch: {batch_id}\nFiles restored: {moved_count}\n\n"
            "The approved pairings are back in Table 2.",
            parent=root,
        )

    def run_combine_workflow(*, use_last_location: bool) -> None:
        database_path = current_database()
        if database_path is None:
            return
        try:
            selected_rows = load_exact_table(database_path)
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("Could not read table 2", str(exc), parent=root)
            return
        if not selected_rows:
            messagebox.showinfo(
                "Table 2 is empty",
                "Promote at least one pairing into table 2 first.",
                parent=root,
            )
            return

        destination = last_combined_directory() if use_last_location else None
        if use_last_location and destination is None:
            messagebox.showinfo(
                "No previous location",
                "Use COMBINE AND STRUCTURE once before using the last location.",
                parent=root,
            )
            refresh_table2_actions()
            return

        destination_text = (
            str(destination)
            if destination is not None
            else str(DEFAULT_COMBINED_ROOT / "<new timestamp>")
        )
        verb = "append to" if use_last_location else "create"
        confirmed = messagebox.askyesno(
            "Combine and structure table 2",
            f"This will {verb}:\n\n{destination_text}\n\n"
            f"Pairings: {len(selected_rows)}\n\n"
            "Table 3 will first snapshot every approved Table 2 row exactly as shown. "
            "The entire batch is validated before any file moves. For every "
            "pairing, the CBZ/ZIP and matched JSON will then be physically MOVED "
            "into its own directory. Direct sibling files move with their "
            "clear filename match; ambiguous files from a shared source folder "
            "are preserved once in a _shared_* directory. The processed archive "
            "rows will leave table 1, "
            "table 2 will clear, and a combined SQLite/SQL manifest will be "
            "written in the destination. If any move or SQL update fails, the "
            "batch is rolled back.\n\nContinue?",
            icon="warning",
            parent=root,
        )
        if not confirmed:
            return

        combine_structure_button.state(["disabled"])
        use_last_location_button.state(["disabled"])
        root.configure(cursor="watch")
        table_status_var.set("Combining table 2 and moving files…")
        root.update_idletasks()
        try:
            result = combine_and_structure_pairings(
                database_path,
                destination_dir=destination,
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror(
                "Combine and Structure failed",
                str(exc),
                parent=root,
            )
            table_status_var.set("Combine and Structure failed; no completed batch was cleared.")
            root.configure(cursor="")
            refresh_table2_actions()
            return

        root.configure(cursor="")
        # Refresh Tables 1/2 in memory, then take the user directly to the new
        # combined manifest in Table 3. Its Older/Newer history is independent.
        show_database(current_index)
        reload_combined_databases(select=result.manifest_database, display=False)
        notebook.select(combined_tab)
        show_combined_database(combined_index)
        messagebox.showinfo(
            "Combined batch complete",
            f"Destination:\n{result.destination_dir}\n\n"
            f"Pairings combined: {result.pairing_count}\n"
            f"Files moved: {result.moved_file_count}\n"
            f"Shared/ambiguous files: {result.shared_file_count}\n\n"
            f"Merged manifest:\n{result.manifest_database}\n\n"
            "Table 3 is now showing this combined output.",
            parent=root,
        )

    def notebook_tab_changed(_event: object | None = None) -> None:
        if active_tab_index() == 2:
            table2_actions.grid_remove()
            table3_actions.grid()
            reload_combined_databases(display=True)
            refresh_table3_actions()
        else:
            table3_actions.grid_remove()
            refresh_pairing_header_only()
            refresh_table2_actions()

    def search_current_run() -> None:
        """Search JSONs, then Enter jumps to the exact CBZ block in the main gallery."""
        database_path = current_database()
        if database_path is None:
            messagebox.showinfo("No run selected", "Create or select a run first.", parent=root)
            return
        raw_term = search_var.get().strip()
        if not raw_term:
            search_entry.focus_set()
            return

        folded = raw_term.casefold()
        normalized = unicode_words(raw_term)
        results: list[dict[str, str]] = []

        def matches_text(*values: object) -> bool:
            joined = " ".join(str(value or "") for value in values)
            if folded in joined.casefold():
                return True
            return bool(normalized and normalized in unicode_words(joined))

        # Search only candidate rows that are actually visible in table 1. A JSON
        # already promoted for the same archive belongs to table 2, not both tables.
        selected_by_archive = exact_jsons_by_archive(database_path)
        for archive_file, candidates in load_candidate_table(database_path):
            hidden_selected = selected_by_archive.get(archive_file, set())
            for candidate in candidates:
                json_file = str(candidate.get("json_file", ""))
                if not json_file or json_file in hidden_selected:
                    continue
                title = str(candidate.get("title", candidate.get("original_page_name", "")))
                source_url = str(candidate.get("source_url", "")) or source_url_from_json_file(json_file)
                if matches_text(
                    title,
                    Path(json_file).name,
                    json_file,
                    Path(archive_file).name,
                    archive_file,
                    source_url,
                ):
                    results.append(
                        {
                            "table": "1 Candidate",
                            "table_number": "1",
                            "title": title,
                            "json_file": json_file,
                            "archive_file": archive_file,
                            "score": str(candidate.get("match_percent", "")),
                            "source_url": source_url,
                        }
                    )

        for (
            archive_file,
            title,
            json_file,
            _related_files,
            source_url,
            match_percent,
            _selection_method,
        ) in load_exact_table(database_path):
            if matches_text(
                title,
                Path(json_file).name,
                json_file,
                Path(archive_file).name,
                archive_file,
                source_url,
            ):
                results.append(
                    {
                        "table": "2 Selected",
                        "table_number": "2",
                        "title": title,
                        "json_file": json_file,
                        "archive_file": archive_file,
                        "score": str(match_percent),
                        "source_url": source_url,
                    }
                )

        if not results:
            messagebox.showinfo(
                "No matches",
                f"No JSON title, filename, URL, or associated CBZ matched:\n{raw_term}",
                parent=root,
            )
            return

        dialog = tk.Toplevel(root)
        dialog.title(f"Find JSON · {raw_term}")
        dialog.geometry("1180x520")
        dialog.minsize(900, 380)
        dialog.transient(root)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(
            body,
            text=(
                f"{len(results)} result{'s' if len(results) != 1 else ''} · "
                "Select one and press Enter to jump to its CBZ block in the main window"
            ),
            font=(gui_font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        columns = ("table", "json", "json_file", "score", "cbz")
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        tree.heading("table", text="TABLE")
        tree.heading("json", text="JSON TITLE")
        tree.heading("json_file", text="JSON FILE")
        tree.heading("score", text="MATCH")
        tree.heading("cbz", text="CURRENT CBZ / ZIP BLOCK")
        tree.column("table", width=95, stretch=False)
        tree.column("json", width=260, stretch=True)
        tree.column("json_file", width=290, stretch=True)
        tree.column("score", width=75, anchor="center", stretch=False)
        tree.column("cbz", width=340, stretch=True)
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        result_by_id: dict[str, dict[str, str]] = {}
        for result in results:
            score = result["score"]
            try:
                score = f"{float(score):.2f}%"
            except (TypeError, ValueError):
                pass
            item_id = tree.insert(
                "",
                "end",
                values=(
                    result["table"],
                    result["title"],
                    Path(result["json_file"]).name,
                    score,
                    Path(result["archive_file"]).name,
                ),
            )
            result_by_id[item_id] = result

        actions = ttk.Frame(body)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        def selected_result() -> dict[str, str] | None:
            selected = tree.selection()
            return result_by_id.get(selected[0]) if selected else None

        def jump_selected(_event: object | None = None) -> str:
            result = selected_result()
            if result is None:
                return "break"
            table_number = int(result.get("table_number", "1"))
            archive_file = result["archive_file"]
            json_file = result["json_file"]
            dialog.grab_release()
            dialog.destroy()
            jump_to_pairing_location(
                table_number=table_number,
                archive_file=archive_file,
                json_file=json_file,
            )
            return "break"

        def open_selected_json() -> None:
            result = selected_result()
            if result:
                open_existing(result["json_file"])

        def reveal_selected_json() -> None:
            result = selected_result()
            if result:
                reveal_existing(result["json_file"])

        def open_selected_source() -> None:
            result = selected_result()
            if result and result.get("source_url"):
                open_source_url(result["source_url"])
            elif result:
                messagebox.showinfo(
                    "No Source-URL",
                    "That JSON has no Source-URL or url field.",
                    parent=dialog,
                )

        ttk.Button(
            actions,
            text="GO TO CBZ BLOCK ↵",
            command=jump_selected,
        ).pack(side="left")
        ttk.Button(
            actions,
            text="OPEN JSON ↗",
            command=open_selected_json,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions,
            text="SHOW JSON IN FOLDER",
            command=reveal_selected_json,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions,
            text="OPEN SOURCE-URL ↗",
            command=open_selected_source,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Close", command=dialog.destroy).pack(side="right")

        if tree.get_children():
            first = tree.get_children()[0]
            tree.selection_set(first)
            tree.focus(first)
            tree.see(first)
        tree.bind("<Double-1>", jump_selected)
        tree.bind("<Return>", jump_selected)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        tree.focus_set()

    def show_database(index: int) -> None:
        nonlocal current_index
        if not databases:
            current_index = -1
            filename_var.set("No generated tables yet")
            run_counter_var.set("")
            table_status_var.set(
                "Press ‘New pairing…’ to create the first candidate and exact tables."
            )
            clear_gallery(candidate_gallery)
            clear_gallery(exact_gallery)
            older_button.state(["disabled"])
            newer_button.state(["disabled"])
            open_database_button.state(["disabled"])
            submit_button.state(["disabled"])
            root.after_idle(refresh_table2_actions)
            return

        current_index = max(0, min(index, len(databases) - 1))
        database_path = databases[current_index]
        try:
            run_version = load_run_version(database_path)
        except sqlite3.Error:
            run_version = "unreadable output"
        filename_var.set(f"{database_path.name}  ·  {run_version}")
        run_counter_var.set(f"Run {current_index + 1} of {len(databases)}")

        try:
            archive_count, candidate_match_count = populate_candidate_gallery(database_path)
            exact_count, related_count = populate_exact_gallery(database_path)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            messagebox.showerror("Could not read tables", f"{database_path}\n\n{exc}")
            clear_gallery(candidate_gallery)
            clear_gallery(exact_gallery)
            table_status_var.set("This database could not be displayed.")
        else:
            table_status_var.set(
                f"{archive_count} archives · {candidate_match_count} available candidates · "
                f"{exact_count} selected pairings · {related_count} related files"
            )

        if current_index <= 0:
            older_button.state(["disabled"])
        else:
            older_button.state(["!disabled"])
        if current_index >= len(databases) - 1:
            newer_button.state(["disabled"])
        else:
            newer_button.state(["!disabled"])
        open_database_button.state(["!disabled"])
        submit_button.state(["!disabled"])
        root.after_idle(refresh_table2_actions)


    def active_tab_index() -> int:
        try:
            return int(notebook.index(notebook.select()))
        except tk.TclError:
            return 0

    def refresh_pairing_header_only() -> None:
        """Restore Table 1/2 navigation without rebuilding either gallery."""
        if not databases or not (0 <= current_index < len(databases)):
            filename_var.set("No generated tables yet")
            run_counter_var.set("")
            older_button.state(["disabled"])
            newer_button.state(["disabled"])
            open_database_button.state(["disabled"])
            submit_button.state(["disabled"])
            search_button.state(["disabled"])
            return
        database_path = databases[current_index]
        try:
            run_version = load_run_version(database_path)
        except sqlite3.Error:
            run_version = "unreadable output"
        filename_var.set(f"{database_path.name}  ·  {run_version}")
        run_counter_var.set(f"Run {current_index + 1} of {len(databases)}")
        if current_index <= 0:
            older_button.state(["disabled"])
        else:
            older_button.state(["!disabled"])
        if current_index >= len(databases) - 1:
            newer_button.state(["disabled"])
        else:
            newer_button.state(["!disabled"])
        open_database_button.state(["!disabled"])
        submit_button.state(["!disabled"])
        search_button.state(["!disabled"])
        search_entry.state(["!disabled"])
        deselect_all_button.state(["!disabled"])

    def show_combined_database(index: int) -> None:
        nonlocal combined_index
        if not combined_databases:
            combined_index = -1
            filename_var.set("No combined outputs yet")
            run_counter_var.set("")
            clear_gallery(combined_gallery)
            tk.Label(
                combined_gallery,
                text=f"No directories exist under {combined_root}. Use COMBINE AND STRUCTURE in table 2 first.",
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 11),
                pady=24,
            ).pack(anchor="w")
            table_status_var.set("Table 3 has no Combined run directories yet.")
            combined_loaded_var.set("")
            older_button.state(["disabled"])
            newer_button.state(["disabled"])
            open_database_button.state(["disabled"])
            submit_button.state(["disabled"])
            search_button.state(["disabled"])
            search_entry.state(["disabled"])
            deselect_all_button.state(["disabled"])
            return

        combined_index = max(0, min(index, len(combined_databases) - 1))
        database_path = combined_databases[combined_index]
        combined_loaded_var.set("LOADING LIVE DIRECTORY…")
        table_status_var.set(f"Loading live Combined directory: {database_path.parent}")
        combined_directory_var.set(database_path.parent.name)
        if not database_path.is_file():
            filename_var.set(f"{database_path.parent.name}  ·  no old table")
            run_counter_var.set(
                f"Combined directory {combined_index + 1} of {len(combined_databases)}"
            )
            clear_gallery(combined_gallery)
            tk.Label(
                combined_gallery,
                text=(
                    "This directory exists under ~/Combined but has no stored Table 3 manifest.\n\n"
                    "Choose CLEAR OLD TABLE + RESCAN to scan its pairing directories."
                ),
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 11),
                pady=24,
                justify="left",
            ).pack(anchor="w")
            table_status_var.set(f"Selected Combined directory: {database_path.parent}")
            combined_loaded_var.set("NO TABLE YET")
            older_button.state(["disabled"] if combined_index <= 0 else ["!disabled"])
            newer_button.state(["disabled"] if combined_index >= len(combined_databases) - 1 else ["!disabled"])
            open_database_button.state(["disabled"])
            submit_button.state(["disabled"])
            search_button.state(["disabled"])
            search_entry.state(["disabled"])
            deselect_all_button.state(["disabled"])
            refresh_table3_actions()
            return
        try:
            version = load_combined_version(database_path)
        except sqlite3.Error:
            version = "unreadable combined output"
        filename_var.set(
            f"{database_path.parent.name}/{database_path.name}  ·  {version}"
        )
        run_counter_var.set(
            f"Combined run {combined_index + 1} of {len(combined_databases)}"
        )
        try:
            pairing_count, moved_count, shared_count = populate_combined_gallery(database_path)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            messagebox.showerror(
                "Could not read combined table",
                f"{database_path}\n\n{exc}",
                parent=root,
            )
            clear_gallery(combined_gallery)
            table_status_var.set("This combined manifest could not be displayed.")
        else:
            shared_text = (
                f" · {shared_count} shared files"
                if shared_count
                else ""
            )
            table_status_var.set(
                f"{pairing_count} live pairing directories · {moved_count} direct files"
                f"{shared_text} · LIVE: {database_path.parent}"
            )

        if combined_index <= 0:
            older_button.state(["disabled"])
        else:
            older_button.state(["!disabled"])
        if combined_index >= len(combined_databases) - 1:
            newer_button.state(["disabled"])
        else:
            newer_button.state(["!disabled"])
        open_database_button.state(["!disabled"])
        submit_button.state(["disabled"])
        search_button.state(["disabled"])
        search_entry.state(["disabled"])
        deselect_all_button.state(["disabled"])
        refresh_table3_actions()

    def navigate_runs(delta: int) -> None:
        if active_tab_index() == 2:
            show_combined_database(combined_index + delta)
        else:
            show_database(current_index + delta)

    def current_visible_database() -> Path | None:
        if active_tab_index() == 2:
            return current_combined_database()
        return current_database()

    def open_current_database() -> None:
        database_path = current_visible_database()
        if database_path is not None:
            open_with_desktop(database_path)

    def open_current_output_folder() -> None:
        database_path = current_visible_database()
        if active_tab_index() == 2 and database_path is not None:
            open_with_desktop(database_path.parent)
        else:
            open_with_desktop(output_dir)

    def deselect_all_candidates() -> None:
        """Clear every visible candidate checkmark in one action."""
        if pairing_mode_var.get() != "select":
            # The trace rebuilds the gallery first; the loop below then clears the
            # newly created SELECT MODE variables, including automatic 100% checks.
            pairing_mode_var.set("select")
        cleared = 0
        for vars_for_archive in candidate_selection_vars.values():
            for selected_var in vars_for_archive.values():
                if selected_var.get():
                    cleared += 1
                selected_var.set(False)
        table_status_var.set(
            f"Deselected all candidates ({cleared} checkmark{'s' if cleared != 1 else ''} cleared)."
        )

    def selected_candidate_items() -> dict[str, dict[str, Any]]:
        selections: dict[str, dict[str, Any]] = {}
        for archive_file, vars_for_archive in candidate_selection_vars.items():
            for json_file, selected_var in vars_for_archive.items():
                if selected_var.get():
                    item = candidate_selection_items.get((archive_file, json_file))
                    if item is not None:
                        selections[archive_file] = item
                    break
        return selections

    def submit_selected_pairings() -> None:
        database_path = current_database()
        if database_path is None:
            return
        selections = selected_candidate_items()
        if not selections:
            messagebox.showinfo(
                "Nothing selected",
                "Check one JSON beside any archive, then press SUBMIT SELECTED.",
                parent=root,
            )
            return

        method = (
            "default_closest"
            if pairing_mode_var.get() == "closest"
            else "manual_selection"
        )
        try:
            promoted = promote_pairings(
                database_path,
                selections,
                selection_method=method,
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror("Could not promote pairings", str(exc), parent=root)
            return

        # Re-render both tables: promoted rows disappear from table 1 and appear
        # immediately in table 2. Existing choices for those archives are replaced.
        show_database(current_index)
        notebook.select(exact_tab)
        table_status_var.set(
            f"Promoted {promoted} pairing{'s' if promoted != 1 else ''} into table 2."
        )

    def reassign_mode_changed(*_args: object) -> None:
        if reassign_mode_var.get():
            notebook.select(candidate_tab)
            table_status_var.set(
                "REASSIGN JSON is active: drag a JSON card onto a different archive block. "
                "Its score will not be recalculated."
            )

    def pairing_mode_changed(*_args: object) -> None:
        database_path = current_database()
        if database_path is None:
            return
        try:
            archive_count, candidate_match_count = populate_candidate_gallery(database_path)
            exact_count, related_count = populate_exact_gallery(database_path)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            messagebox.showerror("Could not refresh selections", str(exc), parent=root)
            return
        mode_text = (
            "closest candidate preselected for every unpaired archive"
            if pairing_mode_var.get() == "closest"
            else "only 100% candidates preselected; choose the rest manually"
        )
        table_status_var.set(
            f"{archive_count} archives · {candidate_match_count} available candidates · "
            f"{exact_count} selected pairings · {mode_text}"
        )

    def reload_databases(select: Path | None = None) -> None:
        nonlocal databases
        databases = list_output_databases(output_dir)
        if not databases:
            show_database(-1)
            return
        if select is not None:
            resolved = select.resolve()
            for index, path in enumerate(databases):
                if path == resolved:
                    show_database(index)
                    return
        show_database(len(databases) - 1)


    def reload_combined_databases(
        select: Path | None = None,
        *,
        display: bool = False,
    ) -> None:
        nonlocal combined_databases, combined_index
        previous = current_combined_database()
        combined_databases = list_combined_databases(combined_root)
        combined_directory_combo.configure(
            values=[path.parent.name for path in combined_databases]
        )
        if not combined_databases:
            combined_index = -1
            if display:
                show_combined_database(-1)
            return

        target: Path | None = None
        if select is not None:
            target = select.expanduser().resolve()
        elif previous is not None:
            target = previous.resolve()

        if target is not None:
            for index, path in enumerate(combined_databases):
                if path == target:
                    combined_index = index
                    break
            else:
                combined_index = len(combined_databases) - 1
        elif combined_index < 0 or combined_index >= len(combined_databases):
            combined_index = len(combined_databases) - 1

        if display:
            show_combined_database(combined_index)

    def open_new_pairing_dialog() -> None:
        nonlocal output_dir

        dialog = tk.Toplevel(root)
        dialog.title("New pairing")
        dialog.minsize(850, 650)
        dialog.transient(root)
        dialog.grab_set()

        archive_sources: list[Path] = []
        json_sources: list[Path] = []
        threshold_var = tk.StringVar(value=f"{initial_threshold:g}")
        output_var = tk.StringVar(value=str(output_dir))
        dialog_status_var = tk.StringVar(value="Choose sources, then create the tables.")

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)

        def refresh_listbox(listbox: "tk.Listbox", values: Sequence[Path]) -> None:
            listbox.delete(0, tk.END)
            for value in values:
                listbox.insert(tk.END, str(value))

        def add_unique(target: list[Path], values: Iterable[str]) -> None:
            known = {str(path.expanduser().resolve()) for path in target}
            for raw_value in values:
                path = Path(raw_value).expanduser().resolve()
                if str(path) not in known:
                    target.append(path)
                    known.add(str(path))
            target.sort(key=lambda item: str(item).casefold())

        def source_section(
            row: int,
            title: str,
            target: list[Path],
            directory_title: str,
            file_title: str,
            filetypes: list[tuple[str, str]],
        ) -> "tk.Listbox":
            ttk.Label(body, text=title, font=(gui_font_family, 11, "bold")).grid(
                row=row, column=0, sticky="w", pady=(0 if row == 0 else 12, 4)
            )
            frame = ttk.Frame(body)
            frame.grid(row=row + 1, column=0, sticky="nsew")
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)

            listbox = tk.Listbox(frame, selectmode=tk.EXTENDED, height=7)
            listbox.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            listbox.configure(yscrollcommand=scrollbar.set)

            buttons = ttk.Frame(frame)
            buttons.grid(row=0, column=2, sticky="ns", padx=(10, 0))

            def choose_directory() -> None:
                selected = filedialog.askdirectory(title=directory_title, parent=dialog)
                if selected:
                    add_unique(target, [selected])
                    refresh_listbox(listbox, target)

            def choose_files() -> None:
                selected = filedialog.askopenfilenames(
                    title=file_title, filetypes=filetypes, parent=dialog
                )
                if selected:
                    add_unique(target, selected)
                    refresh_listbox(listbox, target)

            def remove_selected() -> None:
                for index in reversed(listbox.curselection()):
                    del target[index]
                refresh_listbox(listbox, target)

            ttk.Button(buttons, text="Add directory…", command=choose_directory).pack(
                fill="x", pady=(0, 6)
            )
            ttk.Button(buttons, text="Add files…", command=choose_files).pack(
                fill="x", pady=(0, 6)
            )
            ttk.Button(buttons, text="Remove selected", command=remove_selected).pack(
                fill="x", pady=(0, 6)
            )
            ttk.Button(
                buttons,
                text="Clear",
                command=lambda: (target.clear(), refresh_listbox(listbox, target)),
            ).pack(fill="x")
            return listbox

        source_section(
            0,
            "1. CBZ / ZIP sources",
            archive_sources,
            "Choose a directory containing CBZ/ZIP files",
            "Choose CBZ/ZIP files",
            [("Comic/ZIP archives", "*.cbz *.zip"), ("All files", "*")],
        )
        source_section(
            2,
            "2. JSON sources (directories are recursive)",
            json_sources,
            "Choose a directory containing JSON files",
            "Choose JSON files",
            [("JSON files", "*.json"), ("All files", "*")],
        )

        settings = ttk.LabelFrame(body, text="Settings", padding=10)
        settings.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Candidate threshold:").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            settings,
            from_=0,
            to=100,
            increment=1,
            textvariable=threshold_var,
            width=8,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(settings, text="%").grid(row=0, column=1, sticky="w", padx=(72, 0))

        ttk.Label(settings, text="Output directory:").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Entry(settings, textvariable=output_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 8), pady=(10, 0)
        )

        def choose_output() -> None:
            selected = filedialog.askdirectory(
                title="Choose output directory",
                initialdir=output_var.get() or str(Path.home()),
                parent=dialog,
            )
            if selected:
                output_var.set(selected)

        ttk.Button(settings, text="Browse…", command=choose_output).grid(
            row=1, column=2, pady=(10, 0)
        )

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Label(actions, textvariable=dialog_status_var).grid(row=0, column=0, sticky="w")

        def create_tables() -> None:
            nonlocal output_dir
            try:
                threshold = float(threshold_var.get())
                selected_output_dir = Path(output_var.get()).expanduser().resolve()
            except ValueError:
                messagebox.showerror(
                    "Invalid threshold", "Threshold must be a number.", parent=dialog
                )
                return

            dialog_status_var.set("Creating tables…")
            dialog.configure(cursor="watch")
            create_button.state(["disabled"])
            dialog.update_idletasks()

            try:
                result = run_pairing(
                    archive_sources=archive_sources,
                    json_sources=json_sources,
                    threshold=threshold,
                    output_dir=selected_output_dir,
                    auto_promote_exact=False,
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                messagebox.showerror("Pairing failed", str(exc), parent=dialog)
                dialog_status_var.set("Pairing failed.")
                create_button.state(["!disabled"])
                dialog.configure(cursor="")
                return

            print_summary(result)
            output_dir = result.output_dir
            dialog.destroy()
            reload_databases(select=result.database_path)
            notebook.select(candidate_tab)

        create_button = ttk.Button(actions, text="Create tables", command=create_tables)
        create_button.grid(row=0, column=1, padx=(12, 6))
        ttk.Button(actions, text="Cancel", command=dialog.destroy).grid(row=0, column=2)

        dialog.bind("<Control-Return>", lambda _event: create_tables())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    older_button.configure(command=lambda: navigate_runs(-1))
    newer_button.configure(command=lambda: navigate_runs(1))
    new_pairing_button.configure(command=open_new_pairing_dialog)
    open_database_button.configure(command=open_current_database)
    open_folder_button.configure(command=open_current_output_folder)
    submit_button.configure(command=submit_selected_pairings)
    deselect_all_button.configure(command=deselect_all_candidates)
    search_button.configure(command=search_current_run)
    search_entry.bind("<Return>", lambda _event: search_current_run())
    combine_structure_button.configure(
        command=lambda: run_combine_workflow(use_last_location=False)
    )
    use_last_location_button.configure(
        command=lambda: run_combine_workflow(use_last_location=True)
    )
    use_old_table_button.configure(command=use_old_current_table3)
    overwrite_table3_button.configure(command=overwrite_current_table3)
    combined_directory_combo.bind("<<ComboboxSelected>>", combined_directory_selected)
    reverse_batch_button.configure(command=reverse_current_combined_batch)
    notebook.bind("<<NotebookTabChanged>>", notebook_tab_changed)
    pairing_mode_var.trace_add("write", pairing_mode_changed)
    reassign_mode_var.trace_add("write", reassign_mode_changed)

    root.bind("<Left>", lambda _event: navigate_runs(-1))
    root.bind("<Right>", lambda _event: navigate_runs(1))
    root.bind("<Control-n>", lambda _event: open_new_pairing_dialog())

    def scroll_active_gallery(amount: int, units: str = "pages") -> str:
        try:
            tab = notebook.index(notebook.select())
        except tk.TclError:
            return "break"
        canvas = (candidate_canvas, exact_canvas, combined_canvas)[tab]
        canvas.yview_scroll(amount, units)
        return "break"

    root.bind("<Prior>", lambda _event: scroll_active_gallery(-1, "pages"))
    root.bind("<Next>", lambda _event: scroll_active_gallery(1, "pages"))
    root.bind("<Home>", lambda _event: ((candidate_canvas, exact_canvas, combined_canvas)[notebook.index(notebook.select())].yview_moveto(0), "break")[1])
    root.bind("<End>", lambda _event: ((candidate_canvas, exact_canvas, combined_canvas)[notebook.index(notebook.select())].yview_moveto(1), "break")[1])
    def handle_escape(_event: "tk.Event") -> None:
        if drag_state.get("item") is not None:
            stop_drag_autoscroll()
            ghost = drag_state.get("ghost")
            if ghost is not None:
                try:
                    ghost.destroy()
                except tk.TclError:
                    pass
            clear_drag_highlight()
            root.configure(cursor="")
            drag_state.update(
                {
                    "source_archive": "",
                    "item": None,
                    "ghost": None,
                    "highlighted": None,
                    "dragged": False,
                    "pointer_x_root": 0,
                    "pointer_y_root": 0,
                    "autoscroll_job": None,
                }
            )
            table_status_var.set("Reassignment cancelled; the JSON remains in its original block.")
            return
        close_gui()

    root.bind("<Escape>", handle_escape)

    reload_databases()
    reload_combined_databases(display=False)
    root.mainloop()
    return 0

def main(argv: list[str] | None = None) -> int:
    raw_argv = argv if argv is not None else sys.argv[1:]
    args = parse_args(raw_argv)

    if args.gui or (args.archive_input is None and args.json_input is None):
        return launch_gui(
            initial_threshold=args.threshold,
            initial_output_dir=args.output_dir,
            combined_root=args.combined_root,
            combined_max_images=args.combined_max_images,
            combined_max_archives=args.combined_max_archives,
            combined_enable_limits=args.combined_enable_limits,
        )

    try:
        result = run_pairing(
            archive_sources=[args.archive_input],
            json_sources=[args.json_input],
            threshold=args.threshold,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
