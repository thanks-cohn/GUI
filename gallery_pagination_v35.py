"""Source-to-source patch for paginated Table 1 and Table 2 galleries."""
from __future__ import annotations

import re

PAGE_SIZE = 20


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"v35 pagination patch target not found: {label}")
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
            f"v35 pagination patch target not found: {function_name} block"
        )
    return source[: match.start()] + replacement + source[match.end() :]


def apply(source: str) -> str:
    source = _replace_once(
        source,
        '    exact_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {}\n'
        '    drag_state: dict[str, Any] = {\n',
        '    exact_json_card_frames: dict[tuple[str, str], "tk.Frame"] = {}\n'
        f'    PAIRING_PAGE_SIZE = {PAGE_SIZE}\n'
        '    candidate_page_index = 0\n'
        '    exact_page_index = 0\n'
        '    candidate_rows_cache: list[tuple[str, list[dict[str, Any]], str]] = []\n'
        '    exact_rows_cache: list[tuple[str, list[dict[str, Any]]]] = []\n'
        '    candidate_page_text_var = tk.StringVar(value="")\n'
        '    exact_page_text_var = tk.StringVar(value="")\n'
        '    drag_state: dict[str, Any] = {\n',
        "page state",
    )

    source = _replace_once(
        source,
        "    candidate_canvas, candidate_gallery = make_scroll_gallery(candidate_tab)\n"
        "    exact_canvas, exact_gallery = make_scroll_gallery(exact_tab)\n",
        '''    candidate_canvas, candidate_gallery = make_scroll_gallery(candidate_tab)
    exact_canvas, exact_gallery = make_scroll_gallery(exact_tab)

    candidate_pager = ttk.Frame(candidate_tab, padding=(14, 6))
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
''',
        "pager controls",
    )

    candidate_functions = '''    def render_candidate_page() -> None:
        """Render at most twenty archive groups and release the prior page."""
        nonlocal candidate_page_index
        clear_gallery(candidate_gallery)
        candidate_group_frames.clear()
        candidate_json_card_frames.clear()
        add_section_headers(candidate_gallery, selectable=True)

        total = len(candidate_rows_cache)
        page_count = max(1, (total + PAIRING_PAGE_SIZE - 1) // PAIRING_PAGE_SIZE)
        candidate_page_index = max(0, min(candidate_page_index, page_count - 1))
        start = candidate_page_index * PAIRING_PAGE_SIZE
        end = min(start + PAIRING_PAGE_SIZE, total)

        for archive_file, matches, empty_message in candidate_rows_cache[start:end]:
            add_pair_group(
                candidate_gallery,
                archive_file,
                matches,
                selection_vars=candidate_selection_vars.get(archive_file, {}),
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
            candidate_page_text_var.set(f"{start + 1}–{end} of {total} archives")

        candidate_previous_page_button.state(
            ["disabled"] if candidate_page_index <= 0 else ["!disabled"]
        )
        candidate_next_page_button.state(
            ["disabled"] if candidate_page_index >= page_count - 1 else ["!disabled"]
        )
        candidate_canvas.yview_moveto(0)

    def change_candidate_page(delta: int) -> None:
        nonlocal candidate_page_index
        page_count = max(
            1,
            (len(candidate_rows_cache) + PAIRING_PAGE_SIZE - 1)
            // PAIRING_PAGE_SIZE,
        )
        next_page = max(0, min(candidate_page_index + delta, page_count - 1))
        if next_page == candidate_page_index:
            return
        candidate_page_index = next_page
        render_candidate_page()
        table_status_var.set(
            f"Candidate archives {candidate_page_text_var.get()} · "
            "only this page is rendered"
        )

    def show_candidate_archive_page(archive_file: str) -> None:
        nonlocal candidate_page_index
        for index, (candidate_archive, _matches, _message) in enumerate(
            candidate_rows_cache
        ):
            if candidate_archive != archive_file:
                continue
            destination_page = index // PAIRING_PAGE_SIZE
            if destination_page != candidate_page_index:
                candidate_page_index = destination_page
                render_candidate_page()
            return

    def populate_candidate_gallery(database_path: Path) -> tuple[int, int]:
        nonlocal candidate_page_index, candidate_rows_cache
        candidate_selection_vars.clear()
        candidate_selection_items.clear()
        candidate_group_frames.clear()
        candidate_json_card_frames.clear()

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
            rows.append((archive_file, matches, empty_message))

        candidate_rows_cache = rows
        candidate_page_index = 0
        render_candidate_page()
        return archive_count, match_count

'''
    source = _replace_function_block(
        source,
        "populate_candidate_gallery",
        "populate_exact_gallery",
        candidate_functions,
    )

    exact_functions = '''    def render_exact_page() -> None:
        """Render at most twenty selected archive groups."""
        nonlocal exact_page_index
        clear_gallery(exact_gallery)
        exact_group_frames.clear()
        exact_json_card_frames.clear()
        add_section_headers(exact_gallery, selectable=False)

        total = len(exact_rows_cache)
        page_count = max(1, (total + PAIRING_PAGE_SIZE - 1) // PAIRING_PAGE_SIZE)
        exact_page_index = max(0, min(exact_page_index, page_count - 1))
        start = exact_page_index * PAIRING_PAGE_SIZE
        end = min(start + PAIRING_PAGE_SIZE, total)

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
                f"{start + 1}–{end} of {total} selected archives"
            )

        exact_previous_page_button.state(
            ["disabled"] if exact_page_index <= 0 else ["!disabled"]
        )
        exact_next_page_button.state(
            ["disabled"] if exact_page_index >= page_count - 1 else ["!disabled"]
        )
        exact_canvas.yview_moveto(0)

    def change_exact_page(delta: int) -> None:
        nonlocal exact_page_index
        page_count = max(
            1,
            (len(exact_rows_cache) + PAIRING_PAGE_SIZE - 1)
            // PAIRING_PAGE_SIZE,
        )
        next_page = max(0, min(exact_page_index + delta, page_count - 1))
        if next_page == exact_page_index:
            return
        exact_page_index = next_page
        render_exact_page()
        table_status_var.set(
            f"Selected archives {exact_page_text_var.get()} · only this page is rendered"
        )

    def show_exact_archive_page(archive_file: str) -> None:
        nonlocal exact_page_index
        for index, (selected_archive, _matches) in enumerate(exact_rows_cache):
            if selected_archive != archive_file:
                continue
            destination_page = index // PAIRING_PAGE_SIZE
            if destination_page != exact_page_index:
                exact_page_index = destination_page
                render_exact_page()
            return

    def populate_exact_gallery(database_path: Path) -> tuple[int, int]:
        nonlocal exact_page_index, exact_rows_cache
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
        exact_page_index = 0
        render_exact_page()
        return sum(len(values) for values in grouped.values()), related_count

'''
    source = _replace_function_block(
        source,
        "populate_exact_gallery",
        "populate_combined_gallery",
        exact_functions,
    )

    source = _replace_once(
        source,
        '    def scroll_archive_into_view(archive_file: str) -> None:\n'
        '        """Bring a reassignment destination into view and briefly outline it."""\n',
        '    def scroll_archive_into_view(archive_file: str) -> None:\n'
        '        """Bring a reassignment destination into view and briefly outline it."""\n'
        '        show_candidate_archive_page(archive_file)\n',
        "drag destination page",
    )

    source = _replace_once(
        source,
        '        """Switch tabs, scroll to the owning CBZ block, and highlight the JSON card."""\n'
        '        if table_number == 2:\n',
        '        """Switch tabs, scroll to the owning CBZ block, and highlight the JSON card."""\n'
        '        if table_number == 2:\n'
        '            show_exact_archive_page(archive_file)\n'
        '        else:\n'
        '            show_candidate_archive_page(archive_file)\n'
        '\n'
        '        if table_number == 2:\n',
        "search result page",
    )

    return source
