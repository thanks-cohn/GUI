"""Source-to-source patch for bounded expandable Table 1 and Table 2 galleries.

v35 proved that rendering a whole pairing run at once is unsafe.  v36 keeps the
human model of one growing gallery while imposing a hard ceiling on live Tk
widgets and thumbnails.  The complete logical run remains available for search,
selection, submission, and drag/drop; only a small moving window is rendered.
"""
from __future__ import annotations

import re

BATCH_SIZE = 20
LIVE_WINDOW_LIMIT = 60


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"v36 virtual-gallery patch target not found: {label}")
    return source.replace(old, new, 1)


def _replace_function_block(
    source: str,
    function_name: str,
    next_function_name: str,
    replacement: str,
) -> str:
    pattern = re.compile(
        rf"(?ms)^    def {re.escape(function_name)}\(.*?"
        rf"(?=^    def {re.escape(next_function_name)}\()"
    )
    match = pattern.search(source)
    if match is None:
        raise RuntimeError(
            f"v36 virtual-gallery patch target not found: {function_name} block"
        )
    return source[: match.start()] + replacement + source[match.end() :]


def apply(source: str) -> str:
    """Upgrade the v35 generated source to the bounded v36 gallery model."""

    old_state = '''    candidate_selection_vars: dict[str, dict[str, "tk.BooleanVar"]] = {}
    candidate_selection_items: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_group_frames: dict[str, "tk.Frame"] = {}
    exact_group_frames: dict[str, "tk.Frame"] = {}
    candidate_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {}
    exact_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {}
    PAIRING_PAGE_SIZE = 20
    candidate_page_index = 0
    exact_page_index = 0
    candidate_rows_cache: list[tuple[str, list[dict[str, Any]], str]] = []
    exact_rows_cache: list[tuple[str, list[dict[str, Any]]]] = []
    candidate_page_text_var = tk.StringVar(value="")
    exact_page_text_var = tk.StringVar(value="")
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
'''
    new_state = f'''    # v36 keeps only a bounded moving window of expensive Tk widgets alive.
    # Selection truth lives in ordinary Python data so destroying a window never
    # destroys the user's work.  BooleanVars exist only for the rows on screen.
    candidate_selection_vars: dict[str, dict[str, "tk.BooleanVar"]] = {{}}
    candidate_selection_items: dict[tuple[str, str], dict[str, Any]] = {{}}
    candidate_selected_jsons: dict[str, str] = {{}}
    candidate_group_frames: dict[str, "tk.Frame"] = {{}}
    exact_group_frames: dict[str, "tk.Frame"] = {{}}
    candidate_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {{}}
    exact_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {{}}
    PAIRING_PAGE_SIZE = {BATCH_SIZE}
    PAIRING_LIVE_WINDOW_LIMIT = {LIVE_WINDOW_LIMIT}
    candidate_window_start = 0
    candidate_visible_count = PAIRING_PAGE_SIZE
    exact_window_start = 0
    exact_visible_count = PAIRING_PAGE_SIZE
    candidate_rows_cache: list[tuple[str, list[dict[str, Any]], str]] = []
    exact_rows_cache: list[tuple[str, list[dict[str, Any]]]] = []
    candidate_page_text_var = tk.StringVar(value="")
    exact_page_text_var = tk.StringVar(value="")
    pairing_journal_path: Path | None = None
    drag_state: dict[str, Any] = {{
        "source_archive": "",
        "item": None,
        "ghost": None,
        "highlighted": None,
        "dragged": False,
        "pointer_x_root": 0,
        "pointer_y_root": 0,
        "autoscroll_job": None,
        "window_shift_pending": False,
    }}

    def append_pairing_journal(event_name: str, **payload: Any) -> None:
        """Append one durable interaction crumb without retaining a large log in RAM."""
        if pairing_journal_path is None:
            return
        import json as _pairing_json
        import os as _pairing_os
        from datetime import datetime as _pairing_datetime

        record = {{
            "schema_version": 1,
            "recorded_at": _pairing_datetime.now().astimezone().isoformat(),
            "event": event_name,
            **payload,
        }}
        try:
            pairing_journal_path.parent.mkdir(parents=True, exist_ok=True)
            with pairing_journal_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _pairing_json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\\n"
                )
                handle.flush()
                _pairing_os.fsync(handle.fileno())
        except OSError:
            # Pairing must remain usable even when a removable/output drive briefly
            # disappears.  The database remains the authority after submission.
            pass

    def replay_pairing_journal() -> None:
        """Restore unsubmitted checkbox choices after an interrupted GUI session."""
        if pairing_journal_path is None or not pairing_journal_path.is_file():
            return
        import json as _pairing_json

        try:
            with pairing_journal_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = _pairing_json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    event_name = str(record.get("event", ""))
                    if event_name == "deselect-all":
                        candidate_selected_jsons.clear()
                        continue
                    if event_name != "selection":
                        continue
                    archive_file = str(record.get("archive_file", ""))
                    json_file = str(record.get("json_file", ""))
                    if not archive_file or not json_file:
                        continue
                    if bool(record.get("selected")):
                        candidate_selected_jsons[archive_file] = json_file
                    elif candidate_selected_jsons.get(archive_file) == json_file:
                        candidate_selected_jsons.pop(archive_file, None)
        except OSError:
            pass
'''
    source = _replace_once(source, old_state, new_state, "bounded gallery state")

    old_controls = '''    candidate_pager = ttk.Frame(candidate_tab, padding=(14, 6))
    candidate_pager.grid(row=1, column=0, columnspan=2, sticky="ew")
    candidate_pager.columnconfigure(1, weight=1)
    candidate_previous_page_button = ttk.Button(
        candidate_pager,
        text="◀ PREVIOUS 20",
        command=lambda: change_candidate_page(-1),
    )
    candidate_previous_page_button.grid(row=0, column=0, sticky="w")
    ttk.Label(
        candidate_pager,
        textvariable=candidate_page_text_var,
        anchor="center",
        font=(gui_font_family, 10, "bold"),
    ).grid(row=0, column=1, sticky="ew", padx=12)
    candidate_next_page_button = ttk.Button(
        candidate_pager,
        text="NEXT 20 ▶",
        command=lambda: change_candidate_page(1),
    )
    candidate_next_page_button.grid(row=0, column=2, sticky="e")

    exact_pager = ttk.Frame(exact_tab, padding=(14, 6))
    exact_pager.grid(row=1, column=0, columnspan=2, sticky="ew")
    exact_pager.columnconfigure(1, weight=1)
    exact_previous_page_button = ttk.Button(
        exact_pager,
        text="◀ PREVIOUS 20",
        command=lambda: change_exact_page(-1),
    )
    exact_previous_page_button.grid(row=0, column=0, sticky="w")
    ttk.Label(
        exact_pager,
        textvariable=exact_page_text_var,
        anchor="center",
        font=(gui_font_family, 10, "bold"),
    ).grid(row=0, column=1, sticky="ew", padx=12)
    exact_next_page_button = ttk.Button(
        exact_pager,
        text="NEXT 20 ▶",
        command=lambda: change_exact_page(1),
    )
    exact_next_page_button.grid(row=0, column=2, sticky="e")
'''
    new_controls = '''    candidate_pager = ttk.Frame(candidate_tab, padding=(14, 6))
    candidate_pager.grid(row=1, column=0, columnspan=2, sticky="ew")
    candidate_pager.columnconfigure(1, weight=1)
    candidate_previous_page_button = ttk.Button(
        candidate_pager,
        text="◀ SHOW 20 ABOVE",
        command=lambda: shift_candidate_window(-1),
    )
    candidate_previous_page_button.grid(row=0, column=0, sticky="w")
    ttk.Label(
        candidate_pager,
        textvariable=candidate_page_text_var,
        anchor="center",
        font=(gui_font_family, 10, "bold"),
    ).grid(row=0, column=1, sticky="ew", padx=12)
    candidate_next_page_button = ttk.Button(
        candidate_pager,
        text="SHOW 20 MORE ▼",
        command=lambda: shift_candidate_window(1),
    )
    candidate_next_page_button.grid(row=0, column=2, sticky="e")

    exact_pager = ttk.Frame(exact_tab, padding=(14, 6))
    exact_pager.grid(row=1, column=0, columnspan=2, sticky="ew")
    exact_pager.columnconfigure(1, weight=1)
    exact_previous_page_button = ttk.Button(
        exact_pager,
        text="◀ SHOW 20 ABOVE",
        command=lambda: shift_exact_window(-1),
    )
    exact_previous_page_button.grid(row=0, column=0, sticky="w")
    ttk.Label(
        exact_pager,
        textvariable=exact_page_text_var,
        anchor="center",
        font=(gui_font_family, 10, "bold"),
    ).grid(row=0, column=1, sticky="ew", padx=12)
    exact_next_page_button = ttk.Button(
        exact_pager,
        text="SHOW 20 MORE ▼",
        command=lambda: shift_exact_window(1),
    )
    exact_next_page_button.grid(row=0, column=2, sticky="e")
'''
    source = _replace_once(source, old_controls, new_controls, "expandable controls")

    candidate_window_functions = '''    def sync_visible_candidate_selections() -> None:
        """Copy live BooleanVars into the lightweight selection model."""
        for archive_file, vars_for_archive in candidate_selection_vars.items():
            chosen = next(
                (json_file for json_file, selected_var in vars_for_archive.items() if selected_var.get()),
                "",
            )
            if chosen:
                candidate_selected_jsons[archive_file] = chosen
            else:
                candidate_selected_jsons.pop(archive_file, None)

    def render_candidate_page(
        *,
        restore_y: float | None = None,
        anchor: str = "top",
    ) -> None:
        """Render one bounded continuous window and release all prior widgets."""
        nonlocal candidate_window_start, candidate_visible_count
        sync_visible_candidate_selections()
        candidate_selection_vars.clear()
        clear_gallery(candidate_gallery)
        candidate_group_frames.clear()
        candidate_json_card_frames.clear()
        add_section_headers(candidate_gallery, selectable=True)

        total = len(candidate_rows_cache)
        if total:
            candidate_visible_count = max(
                1,
                min(candidate_visible_count, PAIRING_LIVE_WINDOW_LIMIT, total),
            )
            candidate_window_start = max(
                0,
                min(candidate_window_start, max(0, total - candidate_visible_count)),
            )
        else:
            candidate_window_start = 0
            candidate_visible_count = PAIRING_PAGE_SIZE
        start = candidate_window_start
        end = min(start + candidate_visible_count, total)

        for archive_file, matches, empty_message in candidate_rows_cache[start:end]:
            selected_json = candidate_selected_jsons.get(archive_file, "")
            vars_for_archive: dict[str, "tk.BooleanVar"] = {}
            for item in matches:
                json_file = str(item.get("json_file", ""))
                if json_file:
                    vars_for_archive[json_file] = tk.BooleanVar(
                        value=json_file == selected_json
                    )
            candidate_selection_vars[archive_file] = vars_for_archive
            add_pair_group(
                candidate_gallery,
                archive_file,
                matches,
                selection_vars=vars_for_archive,
                empty_message=empty_message,
            )

        if not total:
            tk.Label(
                candidate_gallery,
                text="No candidate archives in this run.",
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 11),
                pady=24,
            ).pack(anchor="w")
            candidate_page_text_var.set("0 archives")
        else:
            candidate_page_text_var.set(
                f"{start + 1}–{end} of {total} archives · {end - start} live"
            )

        candidate_previous_page_button.state(
            ["disabled"] if start <= 0 else ["!disabled"]
        )
        candidate_next_page_button.state(
            ["disabled"] if end >= total else ["!disabled"]
        )
        candidate_gallery.update_idletasks()
        if restore_y is not None:
            content_height = max(candidate_gallery.winfo_height(), 1)
            viewport_height = max(candidate_canvas.winfo_height(), 1)
            maximum = max(content_height - viewport_height, 1)
            candidate_canvas.yview_moveto(min(1.0, max(0.0, restore_y / maximum)))
        elif anchor == "down-overlap":
            overlap = max(
                0.0,
                (candidate_visible_count - PAIRING_PAGE_SIZE)
                / max(candidate_visible_count, 1),
            )
            candidate_canvas.yview_moveto(min(1.0, overlap))
        elif anchor == "up-overlap":
            overlap = PAIRING_PAGE_SIZE / max(candidate_visible_count, 1)
            candidate_canvas.yview_moveto(min(1.0, overlap))
        else:
            candidate_canvas.yview_moveto(0)

    def shift_candidate_window(delta: int, *, during_drag: bool = False) -> bool:
        """Grow by twenty, then slide a fixed-size live window through huge runs."""
        nonlocal candidate_window_start, candidate_visible_count
        total = len(candidate_rows_cache)
        if not total:
            return False
        old_y = float(candidate_canvas.canvasy(0))
        old_start = candidate_window_start
        old_visible = candidate_visible_count
        anchor = "top"
        restore_y: float | None = old_y

        if delta > 0:
            current_end = candidate_window_start + candidate_visible_count
            if current_end >= total:
                return False
            if candidate_visible_count < PAIRING_LIVE_WINDOW_LIMIT:
                candidate_visible_count = min(
                    PAIRING_LIVE_WINDOW_LIMIT,
                    candidate_visible_count + PAIRING_PAGE_SIZE,
                    total - candidate_window_start,
                )
            else:
                candidate_window_start = min(
                    max(0, total - candidate_visible_count),
                    candidate_window_start + PAIRING_PAGE_SIZE,
                )
                if during_drag:
                    restore_y = None
                    anchor = "down-overlap"
        else:
            if candidate_window_start <= 0:
                return False
            candidate_window_start = max(
                0, candidate_window_start - PAIRING_PAGE_SIZE
            )
            if during_drag:
                restore_y = None
                anchor = "up-overlap"

        if (
            old_start == candidate_window_start
            and old_visible == candidate_visible_count
        ):
            return False
        render_candidate_page(restore_y=restore_y, anchor=anchor)
        table_status_var.set(
            f"Candidate archives {candidate_page_text_var.get()} · "
            f"widgets capped at {PAIRING_LIVE_WINDOW_LIMIT}"
        )
        return True

    def show_candidate_archive_page(archive_file: str) -> None:
        """Expose an arbitrary search/drop destination inside the bounded window."""
        nonlocal candidate_window_start, candidate_visible_count
        total = len(candidate_rows_cache)
        for index, (candidate_archive, _matches, _message) in enumerate(
            candidate_rows_cache
        ):
            if candidate_archive != archive_file:
                continue
            current_end = candidate_window_start + candidate_visible_count
            if candidate_window_start <= index < current_end:
                return
            candidate_visible_count = min(PAIRING_LIVE_WINDOW_LIMIT, total)
            aligned = (index // PAIRING_PAGE_SIZE) * PAIRING_PAGE_SIZE
            candidate_window_start = min(
                max(0, total - candidate_visible_count),
                max(0, aligned - PAIRING_PAGE_SIZE),
            )
            render_candidate_page()
            return

'''
    source = _replace_function_block(
        source,
        "render_candidate_page",
        "populate_candidate_gallery",
        candidate_window_functions,
    )

    candidate_populate = '''    def populate_candidate_gallery(database_path: Path) -> tuple[int, int]:
        nonlocal candidate_window_start, candidate_visible_count, candidate_rows_cache
        nonlocal pairing_journal_path
        candidate_selection_vars.clear()
        candidate_selection_items.clear()
        candidate_selected_jsons.clear()
        candidate_group_frames.clear()
        candidate_json_card_frames.clear()
        pairing_journal_path = database_path.with_name(
            f"{database_path.stem}-pairing-session.jsonl"
        )

        already_selected = exact_jsons_by_archive(database_path)
        rows: list[tuple[str, list[dict[str, Any]], str]] = []
        archive_count = 0
        match_count = 0
        for archive_file, all_matches in load_candidate_table(database_path):
            archive_count += 1
            selected_jsons = already_selected.get(archive_file, set())
            matches = [
                item
                for item in all_matches
                if str(item.get("json_file", "")) not in selected_jsons
            ]
            match_count += len(matches)

            for item in matches:
                json_file = str(item.get("json_file", ""))
                if json_file:
                    candidate_selection_items[(archive_file, json_file)] = item

            selected = initial_selected_json(
                matches,
                archive_already_paired=bool(selected_jsons),
            )
            if selected:
                candidate_selected_jsons[archive_file] = selected

            if selected_jsons and not matches:
                empty_message = "Already paired in table 2 · no other candidates"
            elif selected_jsons:
                empty_message = "Already paired in table 2"
            else:
                empty_message = "No match at or above the threshold"
            rows.append((archive_file, matches, empty_message))

        candidate_rows_cache = rows
        replay_pairing_journal()
        candidate_window_start = 0
        candidate_visible_count = min(PAIRING_PAGE_SIZE, max(1, len(rows)))
        render_candidate_page()
        return archive_count, match_count

'''
    source = _replace_function_block(
        source,
        "populate_candidate_gallery",
        "render_exact_page",
        candidate_populate,
    )

    exact_window_functions = '''    def render_exact_page(
        *,
        restore_y: float | None = None,
        anchor: str = "top",
    ) -> None:
        """Render a bounded continuous window of selected archive groups."""
        nonlocal exact_window_start, exact_visible_count
        clear_gallery(exact_gallery)
        exact_group_frames.clear()
        exact_json_card_frames.clear()
        add_section_headers(exact_gallery, selectable=False)

        total = len(exact_rows_cache)
        if total:
            exact_visible_count = max(
                1, min(exact_visible_count, PAIRING_LIVE_WINDOW_LIMIT, total)
            )
            exact_window_start = max(
                0, min(exact_window_start, max(0, total - exact_visible_count))
            )
        else:
            exact_window_start = 0
            exact_visible_count = PAIRING_PAGE_SIZE
        start = exact_window_start
        end = min(start + exact_visible_count, total)

        for archive_file, matches in exact_rows_cache[start:end]:
            add_pair_group(exact_gallery, archive_file, matches, exact_mode=True)

        if not total:
            tk.Label(
                exact_gallery,
                text=(
                    "No selected pairings in this run. Check candidates in "
                    "table 1, then press SUBMIT SELECTED."
                ),
                background=PAGE_BG,
                foreground=TEXT_MUTED,
                font=(gui_font_family, 11),
                pady=24,
            ).pack(anchor="w")
            exact_page_text_var.set("0 selected archives")
        else:
            exact_page_text_var.set(
                f"{start + 1}–{end} of {total} selected archives · {end - start} live"
            )

        exact_previous_page_button.state(
            ["disabled"] if start <= 0 else ["!disabled"]
        )
        exact_next_page_button.state(
            ["disabled"] if end >= total else ["!disabled"]
        )
        exact_gallery.update_idletasks()
        if restore_y is not None:
            content_height = max(exact_gallery.winfo_height(), 1)
            viewport_height = max(exact_canvas.winfo_height(), 1)
            maximum = max(content_height - viewport_height, 1)
            exact_canvas.yview_moveto(min(1.0, max(0.0, restore_y / maximum)))
        elif anchor == "down-overlap":
            overlap = max(
                0.0,
                (exact_visible_count - PAIRING_PAGE_SIZE)
                / max(exact_visible_count, 1),
            )
            exact_canvas.yview_moveto(min(1.0, overlap))
        elif anchor == "up-overlap":
            exact_canvas.yview_moveto(
                min(1.0, PAIRING_PAGE_SIZE / max(exact_visible_count, 1))
            )
        else:
            exact_canvas.yview_moveto(0)

    def shift_exact_window(delta: int) -> bool:
        nonlocal exact_window_start, exact_visible_count
        total = len(exact_rows_cache)
        if not total:
            return False
        old_y = float(exact_canvas.canvasy(0))
        old_start = exact_window_start
        old_visible = exact_visible_count
        if delta > 0:
            current_end = exact_window_start + exact_visible_count
            if current_end >= total:
                return False
            if exact_visible_count < PAIRING_LIVE_WINDOW_LIMIT:
                exact_visible_count = min(
                    PAIRING_LIVE_WINDOW_LIMIT,
                    exact_visible_count + PAIRING_PAGE_SIZE,
                    total - exact_window_start,
                )
            else:
                exact_window_start = min(
                    max(0, total - exact_visible_count),
                    exact_window_start + PAIRING_PAGE_SIZE,
                )
        else:
            if exact_window_start <= 0:
                return False
            exact_window_start = max(0, exact_window_start - PAIRING_PAGE_SIZE)
        if old_start == exact_window_start and old_visible == exact_visible_count:
            return False
        render_exact_page(restore_y=old_y)
        table_status_var.set(
            f"Selected archives {exact_page_text_var.get()} · "
            f"widgets capped at {PAIRING_LIVE_WINDOW_LIMIT}"
        )
        return True

    def show_exact_archive_page(archive_file: str) -> None:
        nonlocal exact_window_start, exact_visible_count
        total = len(exact_rows_cache)
        for index, (selected_archive, _matches) in enumerate(exact_rows_cache):
            if selected_archive != archive_file:
                continue
            current_end = exact_window_start + exact_visible_count
            if exact_window_start <= index < current_end:
                return
            exact_visible_count = min(PAIRING_LIVE_WINDOW_LIMIT, total)
            aligned = (index // PAIRING_PAGE_SIZE) * PAIRING_PAGE_SIZE
            exact_window_start = min(
                max(0, total - exact_visible_count),
                max(0, aligned - PAIRING_PAGE_SIZE),
            )
            render_exact_page()
            return

'''
    source = _replace_function_block(
        source,
        "render_exact_page",
        "populate_exact_gallery",
        exact_window_functions,
    )

    exact_populate = '''    def populate_exact_gallery(database_path: Path) -> tuple[int, int]:
        nonlocal exact_window_start, exact_visible_count, exact_rows_cache
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

        exact_rows_cache = list(grouped.items())
        exact_window_start = 0
        exact_visible_count = min(PAIRING_PAGE_SIZE, max(1, len(exact_rows_cache)))
        render_exact_page()
        return sum(len(values) for values in grouped.values()), related_count

'''
    source = _replace_function_block(
        source,
        "populate_exact_gallery",
        "populate_combined_gallery",
        exact_populate,
    )

    choose_old = '''                    chosen_var = candidate_selection_vars[a][chosen_json]
                    if chosen_var.get():
                        for other_json, other_var in candidate_selection_vars[a].items():
                            if other_json != chosen_json:
                                other_var.set(False)
'''
    choose_new = '''                    chosen_var = candidate_selection_vars[a][chosen_json]
                    selected_now = bool(chosen_var.get())
                    if selected_now:
                        candidate_selected_jsons[a] = chosen_json
                        for other_json, other_var in candidate_selection_vars[a].items():
                            if other_json != chosen_json:
                                other_var.set(False)
                    elif candidate_selected_jsons.get(a) == chosen_json:
                        candidate_selected_jsons.pop(a, None)
                    append_pairing_journal(
                        "selection",
                        archive_file=a,
                        json_file=chosen_json,
                        selected=selected_now,
                    )
'''
    source = _replace_once(source, choose_old, choose_new, "durable selection model")

    deselect_block = '''    def deselect_all_candidates() -> None:
        """Clear every candidate choice in the logical run and its live widgets."""
        if pairing_mode_var.get() != "select":
            pairing_mode_var.set("select")
        cleared = len(candidate_selected_jsons)
        candidate_selected_jsons.clear()
        for vars_for_archive in candidate_selection_vars.values():
            for selected_var in vars_for_archive.values():
                selected_var.set(False)
        append_pairing_journal("deselect-all", cleared=cleared)
        table_status_var.set(
            f"Deselected all candidates ({cleared} checkmark{'s' if cleared != 1 else ''} cleared)."
        )

'''
    source = _replace_function_block(
        source,
        "deselect_all_candidates",
        "selected_candidate_items",
        deselect_block,
    )

    selected_block = '''    def selected_candidate_items() -> dict[str, dict[str, Any]]:
        """Return choices from the lightweight model, including off-window rows."""
        sync_visible_candidate_selections()
        selections: dict[str, dict[str, Any]] = {}
        for archive_file, json_file in candidate_selected_jsons.items():
            item = candidate_selection_items.get((archive_file, json_file))
            if item is not None:
                selections[archive_file] = item
        return selections

'''
    source = _replace_function_block(
        source,
        "selected_candidate_items",
        "submit_selected_pairings",
        selected_block,
    )

    drag_old = '''        if direction:
            candidate_canvas.yview_scroll(direction, "units")
            candidate_canvas.update_idletasks()
            update_drop_target_from_pointer()
            drag_state["autoscroll_job"] = root.after(45, drag_autoscroll_tick)
'''
    drag_new = '''        if direction:
            before = candidate_canvas.yview()
            candidate_canvas.yview_scroll(direction, "units")
            candidate_canvas.update_idletasks()
            after = candidate_canvas.yview()
            shifted = False
            at_top = direction < 0 and before[0] <= 0.0001 and after[0] <= 0.0001
            at_bottom = direction > 0 and before[1] >= 0.9999 and after[1] >= 0.9999
            if (
                (at_top or at_bottom)
                and not drag_state.get("window_shift_pending")
            ):
                drag_state["window_shift_pending"] = True
                shifted = shift_candidate_window(
                    -1 if at_top else 1,
                    during_drag=True,
                )
                root.after(
                    180,
                    lambda: drag_state.__setitem__("window_shift_pending", False),
                )
            if not shifted:
                update_drop_target_from_pointer()
            drag_state["autoscroll_job"] = root.after(45, drag_autoscroll_tick)
'''
    source = _replace_once(source, drag_old, drag_new, "drag across virtual windows")

    # Record an intended long-distance drop before the existing reassignment code
    # mutates the database.  This crumb is useful for audit/recovery and is tiny.
    drop_pattern = re.compile(
        r'(?m)^(?P<i>        )if not target_archive or target_archive == source_archive or not isinstance\(item, dict\):\n'
        r'(?P<body>(?:(?P=i).+\n)+?)(?P=i)    return "break"\n'
    )
    drop_match = drop_pattern.search(source)
    if drop_match is None:
        raise RuntimeError("v36 virtual-gallery patch target not found: drop journal")
    drop_insert = (
        drop_match.group(0)
        + '        append_pairing_journal(\n'
        + '            "drop-request",\n'
        + '            source_archive=source_archive,\n'
        + '            target_archive=target_archive,\n'
        + '            json_file=str(item.get("json_file", "")),\n'
        + '        )\n'
    )
    source = source[: drop_match.start()] + drop_insert + source[drop_match.end() :]

    return source
