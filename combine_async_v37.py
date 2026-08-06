"""Source patch for safe, responsive Table 2 Combine and Structure v37.

The v37 patch narrows sibling adoption to explicit/name-matched files, leaves
unrelated direct files untouched, adds a byte/file preflight, and runs the
potentially long combine operation outside Tk's main event loop.
"""
from __future__ import annotations

import re


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"v37 source patch target not found: {label}")
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
            f"v37 source patch target not found: {function_name} block"
        )
    return source[: match.start()] + replacement + source[match.end() :]


def apply(source: str) -> str:
    """Apply the v37 safe asynchronous combine workflow to generated source."""
    source = _replace_once(
        source,
        'APP_VERSION = "36.0"',
        'APP_VERSION = "37.0"',
        "application version",
    )

    signature_old = '''def combine_and_structure_pairings(
    database_path: Path,
    *,
    destination_dir: Path | None = None,
    combined_root: Path = DEFAULT_COMBINED_ROOT,
) -> CombineResult:
'''
    signature_new = '''def combine_and_structure_pairings(
    database_path: Path,
    *,
    destination_dir: Path | None = None,
    combined_root: Path = DEFAULT_COMBINED_ROOT,
    progress_callback: Any | None = None,
    preflight_callback: Any | None = None,
    cancel_event: threading.Event | None = None,
) -> CombineResult:
'''
    source = _replace_once(
        source,
        signature_old,
        signature_new,
        "combine callback signature",
    )

    helper_anchor = '''    database_path = database_path.expanduser().resolve()
    raw_rows = _load_combine_rows(database_path)
'''
    helper_replacement = '''    def emit_progress(stage: str, **payload: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback({"stage": stage, **payload})
        except Exception:
            # UI reporting must never corrupt or abort a filesystem transaction.
            pass

    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise OSError("Combine canceled safely by the user.")

    database_path = database_path.expanduser().resolve()
    emit_progress("loading", message="Reading approved Table 2 pairings…")
    check_cancelled()
    raw_rows = _load_combine_rows(database_path)
'''
    source = _replace_once(
        source,
        helper_anchor,
        helper_replacement,
        "combine progress helpers",
    )

    parent_setup_old = '''    shared_plans: list[dict[str, Any]] = []
    globally_assigned: set[Path] = {
'''
    parent_setup_new = '''    shared_plans: list[dict[str, Any]] = []
    unrelated_skipped = 0
    globally_assigned: set[Path] = {
'''
    source = _replace_once(
        source,
        parent_setup_old,
        parent_setup_new,
        "unrelated-file counter",
    )

    parent_loop_old = '''    for source_parent, parent_plans in plans_by_parent.items():
        try:
'''
    parent_loop_new = '''    parent_groups = list(plans_by_parent.items())
    for parent_index, (source_parent, parent_plans) in enumerate(parent_groups, 1):
        check_cancelled()
        emit_progress(
            "scanning",
            current=parent_index,
            total=len(parent_groups),
            path=str(source_parent),
            message=f"Inspecting sibling folder {parent_index} of {len(parent_groups)}…",
        )
        try:
'''
    source = _replace_once(
        source,
        parent_loop_old,
        parent_loop_new,
        "responsive sibling scan",
    )

    single_parent_old = '''        if len(parent_plans) == 1:
            plan = parent_plans[0]
            for source in all_files:
                if source in selected_archive_paths and source != plan["archive_path"]:
                    continue
                if source not in globally_assigned:
                    plan["source_paths"].append(source)
                    globally_assigned.add(source)
            continue
'''
    single_parent_new = '''        if len(parent_plans) == 1:
            plan = parent_plans[0]
            explicit_related = {
                Path(path_text).expanduser().resolve()
                for path_text in plan.get("related_files", [])
                if str(path_text).strip()
            }
            match_keys = tuple(
                key
                for key in {
                    compact_name(plan["json_path"].stem),
                    compact_name(str(plan.get("title", ""))),
                }
                if len(key) >= 3
            )
            for source in all_files:
                if source in selected_archive_paths and source != plan["archive_path"]:
                    continue
                if source in globally_assigned:
                    continue
                source_key = compact_name(source.stem)
                clear_name_match = bool(
                    source_key
                    and any(key in source_key for key in match_keys)
                )
                if source in explicit_related or clear_name_match:
                    plan["source_paths"].append(source)
                    globally_assigned.add(source)
                else:
                    unrelated_skipped += 1
            continue
'''
    source = _replace_once(
        source,
        single_parent_old,
        single_parent_new,
        "single-folder sibling safety",
    )

    multi_match_old = '''            matching_plans: list[dict[str, Any]] = []
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
'''
    multi_match_new = '''            matching_plans: list[dict[str, Any]] = []
            for plan, keys in plan_keys:
                explicit_related = {
                    Path(path_text).expanduser().resolve()
                    for path_text in plan.get("related_files", [])
                    if str(path_text).strip()
                }
                clear_name_match = bool(
                    source_key
                    and any(
                        len(key) >= 3 and key in source_key
                        for key in keys
                        if key
                    )
                )
                if source in explicit_related or clear_name_match:
                    matching_plans.append(plan)
            if len(matching_plans) == 1:
                matching_plans[0]["source_paths"].append(source)
                globally_assigned.add(source)
            elif len(matching_plans) > 1:
                ambiguous_sources.append(source)
                globally_assigned.add(source)
            else:
                # An unrelated file beside the JSON is not combine cargo.
                unrelated_skipped += 1
'''
    source = _replace_once(
        source,
        multi_match_old,
        multi_match_new,
        "shared-folder sibling safety",
    )

    preflight_anchor = '''    manifest_database = target_root / "combined-pairings.sqlite3"
    manifest_sql = target_root / "combined-pairings.sql"
'''
    preflight_replacement = '''    total_bytes = 0
    for entry in move_entries:
        try:
            entry["size_bytes"] = int(entry["source"].stat().st_size)
        except OSError:
            entry["size_bytes"] = 0
        total_bytes += int(entry["size_bytes"])

    preflight_summary = {
        "pairing_count": len(plans),
        "primary_file_count": len(plans) * 2,
        "matched_sibling_file_count": sum(
            1
            for entry in move_entries
            if entry["kind"] == "pair" and entry.get("role") == "sibling"
        ),
        "shared_file_count": sum(
            1 for entry in move_entries if entry["kind"] == "shared"
        ),
        "unrelated_skipped_file_count": unrelated_skipped,
        "total_file_count": len(move_entries),
        "total_bytes": total_bytes,
        "destination": str(target_root),
    }
    emit_progress("preflight-ready", **preflight_summary)
    if preflight_callback is not None and not bool(
        preflight_callback(dict(preflight_summary))
    ):
        raise OSError("Combine canceled before any files were moved.")
    check_cancelled()

    manifest_database = target_root / "combined-pairings.sqlite3"
    manifest_sql = target_root / "combined-pairings.sql"
'''
    source = _replace_once(
        source,
        preflight_anchor,
        preflight_replacement,
        "exact combine preflight",
    )

    move_loop_old = '''        for entry in move_entries:
            source: Path = entry["source"]
            destination: Path = entry["destination"]
            plan: dict[str, Any] = entry["plan"]
            if not source.is_file():
                raise FileNotFoundError(
                    "A source changed after preflight and before its move:\\n" + str(source)
                )
            shutil.move(str(source), str(destination))
            entry["destination_actual"] = destination.resolve()
            moved_pairs.append((destination, source))
            plan["moved_files"].append(str(destination.resolve()))
'''
    move_loop_new = '''        bytes_moved = 0
        for move_index, entry in enumerate(move_entries, 1):
            check_cancelled()
            source: Path = entry["source"]
            destination: Path = entry["destination"]
            plan: dict[str, Any] = entry["plan"]
            if not source.is_file():
                raise FileNotFoundError(
                    "A source changed after preflight and before its move:\\n" + str(source)
                )
            shutil.move(str(source), str(destination))
            entry["destination_actual"] = destination.resolve()
            moved_pairs.append((destination, source))
            plan["moved_files"].append(str(destination.resolve()))
            bytes_moved += int(entry.get("size_bytes", 0))
            emit_progress(
                "moving",
                current=move_index,
                total=len(move_entries),
                bytes_done=bytes_moved,
                total_bytes=total_bytes,
                path=str(source),
                message=f"Moved {move_index} of {len(move_entries)} files…",
            )
'''
    source = _replace_once(
        source,
        move_loop_old,
        move_loop_new,
        "move progress and cancellation",
    )

    database_anchor = '''        # One transaction spans the source pairing DB and attached combined manifest.
        source_connection = sqlite3.connect(database_path)
'''
    database_replacement = '''        check_cancelled()
        emit_progress(
            "database",
            message="All files moved. Committing SQLite manifests…",
            current=len(move_entries),
            total=len(move_entries),
            bytes_done=total_bytes,
            total_bytes=total_bytes,
        )
        # One transaction spans the source pairing DB and attached combined manifest.
        source_connection = sqlite3.connect(database_path)
'''
    source = _replace_once(
        source,
        database_anchor,
        database_replacement,
        "database commit progress",
    )

    rollback_anchor = '''    except Exception as exc:
        if source_connection is not None:
'''
    rollback_replacement = '''    except Exception as exc:
        emit_progress(
            "rollback",
            message="A problem or cancellation occurred. Rolling moved files back safely…",
        )
        if source_connection is not None:
'''
    source = _replace_once(
        source,
        rollback_anchor,
        rollback_replacement,
        "rollback progress",
    )

    finalize_anchor = '''    # SQL dump generation is post-commit: a dump failure must not undo a valid,
    # already-committed file/DB transaction. The SQLite manifests remain authoritative.
    try:
'''
    finalize_replacement = '''    emit_progress(
        "finalizing",
        message="SQLite commit complete. Writing portable SQL dumps…",
    )
    # SQL dump generation is post-commit: a dump failure must not undo a valid,
    # already-committed file/DB transaction. The SQLite manifests remain authoritative.
    try:
'''
    source = _replace_once(
        source,
        finalize_anchor,
        finalize_replacement,
        "post-commit progress",
    )

    complete_anchor = '''    remember_last_combined_directory(target_root)
    pair_file_count = sum(len(plan["moved_files"]) for plan in plans)
'''
    complete_replacement = '''    remember_last_combined_directory(target_root)
    emit_progress(
        "complete",
        message="Combine and Structure complete.",
        current=len(move_entries),
        total=len(move_entries),
        bytes_done=total_bytes,
        total_bytes=total_bytes,
    )
    pair_file_count = sum(len(plan["moved_files"]) for plan in plans)
'''
    source = _replace_once(
        source,
        complete_anchor,
        complete_replacement,
        "completion progress",
    )

    async_gui = r'''    def run_combine_workflow(*, use_last_location: bool) -> None:
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
            "Prepare Combine and Structure",
            f"This will prepare a background preflight to {verb}:\n\n"
            f"{destination_text}\n\n"
            f"Approved pairings: {len(selected_rows)}\n\n"
            "No files move during the scan. Only the chosen CBZ/ZIP, chosen JSON, "
            "explicit related files, and clearly name-matched siblings are eligible. "
            "Unrelated files beside a JSON remain where they are. After scanning, "
            "you will see the exact file count and total size before approving the move.\n\n"
            "Begin preflight?",
            icon="warning",
            parent=root,
        )
        if not confirmed:
            return

        import queue as _combine_queue

        def human_bytes(value: object) -> str:
            try:
                size = float(value or 0)
            except (TypeError, ValueError):
                size = 0.0
            units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
            unit = units[0]
            for unit in units:
                if abs(size) < 1024.0 or unit == units[-1]:
                    break
                size /= 1024.0
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"

        progress_dialog = tk.Toplevel(root)
        progress_dialog.title("Combine and Structure")
        progress_dialog.geometry("720x320")
        progress_dialog.minsize(620, 280)
        progress_dialog.transient(root)
        progress_dialog.grab_set()

        body = ttk.Frame(progress_dialog, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        phase_var = tk.StringVar(value="Preparing background preflight…")
        detail_var = tk.StringVar(value="No files have moved.")
        file_var = tk.StringVar(value="")
        ttk.Label(
            body,
            textvariable=phase_var,
            font=(gui_font_family, 13, "bold"),
            wraplength=670,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            body,
            textvariable=detail_var,
            wraplength=670,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(
            body,
            textvariable=file_var,
            foreground="#666666",
            wraplength=670,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        progress_bar = ttk.Progressbar(body, mode="indeterminate", maximum=100)
        progress_bar.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        progress_bar.start(12)

        controls = ttk.Frame(body)
        controls.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        controls.columnconfigure(0, weight=1)
        safety_var = tk.StringVar(
            value="Safe cancellation is available before the SQLite commit begins."
        )
        ttk.Label(
            controls,
            textvariable=safety_var,
            foreground="#555555",
        ).grid(row=0, column=0, sticky="w")

        cancel_event = threading.Event()
        control_queue = _combine_queue.Queue()
        latest_progress: dict[str, Any] = {}
        progress_lock = threading.Lock()
        progress_serial = 0
        displayed_serial = -1
        worker_finished = False
        cancel_locked = False

        def request_cancel() -> None:
            nonlocal cancel_locked
            if cancel_locked:
                messagebox.showinfo(
                    "Finishing safely",
                    "The SQLite commit has begun, so this final stage cannot be interrupted.",
                    parent=progress_dialog,
                )
                return
            cancel_event.set()
            cancel_button.state(["disabled"])
            phase_var.set("Cancellation requested…")
            detail_var.set(
                "The current file will finish, then every completed move will be rolled back."
            )
            safety_var.set("Do not force-close the Python process while rollback is running.")

        cancel_button = ttk.Button(
            controls,
            text="CANCEL SAFELY",
            command=request_cancel,
        )
        cancel_button.grid(row=0, column=1, padx=(12, 0))
        progress_dialog.protocol("WM_DELETE_WINDOW", request_cancel)

        combine_structure_button.state(["disabled"])
        use_last_location_button.state(["disabled"])
        root.configure(cursor="watch")
        try:
            notebook.state(["disabled"])
        except tk.TclError:
            pass
        table_status_var.set("Combine preflight is running in the background…")

        def report_progress(payload: dict[str, Any]) -> None:
            nonlocal progress_serial
            with progress_lock:
                latest_progress.clear()
                latest_progress.update(payload)
                progress_serial += 1

        def approve_preflight(summary: dict[str, Any]) -> bool:
            decision_event = threading.Event()
            decision: dict[str, bool] = {"approved": False}
            control_queue.put(("preflight", summary, decision_event, decision))
            while not decision_event.wait(0.1):
                if cancel_event.is_set():
                    return False
            return bool(decision.get("approved"))

        def worker() -> None:
            try:
                result = combine_and_structure_pairings(
                    database_path,
                    destination_dir=destination,
                    progress_callback=report_progress,
                    preflight_callback=approve_preflight,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                control_queue.put(("error", exc))
            else:
                control_queue.put(("done", result))

        def close_progress_dialog() -> None:
            progress_bar.stop()
            try:
                progress_dialog.grab_release()
            except tk.TclError:
                pass
            try:
                progress_dialog.destroy()
            except tk.TclError:
                pass

        def restore_main_window() -> None:
            root.configure(cursor="")
            try:
                notebook.state(["!disabled"])
            except tk.TclError:
                pass
            refresh_table2_actions()

        def display_progress(payload: dict[str, Any]) -> None:
            nonlocal cancel_locked
            stage = str(payload.get("stage", ""))
            message = str(payload.get("message", "")).strip()
            path = str(payload.get("path", "")).strip()
            current = int(payload.get("current", 0) or 0)
            total = int(payload.get("total", 0) or 0)
            bytes_done = int(payload.get("bytes_done", 0) or 0)
            total_bytes = int(payload.get("total_bytes", 0) or 0)

            if message:
                phase_var.set(message)
            file_var.set(path)

            if stage in {"scanning", "moving"} and total > 0:
                progress_bar.stop()
                progress_bar.configure(mode="determinate", maximum=total, value=current)
            else:
                progress_bar.configure(mode="indeterminate")
                progress_bar.start(12)

            if stage == "scanning":
                detail_var.set(f"Sibling folder {current} of {total}. No files have moved.")
            elif stage == "moving":
                detail_var.set(
                    f"File {current} of {total} · "
                    f"{human_bytes(bytes_done)} of {human_bytes(total_bytes)}"
                )
            elif stage == "rollback":
                progress_bar.configure(mode="indeterminate")
                progress_bar.start(10)
                cancel_button.state(["disabled"])
                detail_var.set("Restoring completed moves to their original paths…")
            elif stage in {"database", "finalizing", "complete"}:
                cancel_locked = True
                cancel_button.state(["disabled"])
                safety_var.set("Filesystem moves are complete; final metadata is being committed.")
                if total_bytes:
                    detail_var.set(
                        f"{total} files · {human_bytes(total_bytes)} processed safely."
                    )

        def handle_preflight(
            summary: dict[str, Any],
            decision_event: threading.Event,
            decision: dict[str, bool],
        ) -> None:
            progress_bar.stop()
            progress_bar.configure(mode="determinate", maximum=1, value=0)
            phase_var.set("Preflight complete. Nothing has moved yet.")
            detail_var.set(
                f"{summary.get('total_file_count', 0)} files · "
                f"{human_bytes(summary.get('total_bytes', 0))}"
            )
            file_var.set(str(summary.get("destination", "")))
            approved = messagebox.askyesno(
                "Approve exact move plan",
                f"Pairings: {summary.get('pairing_count', 0)}\n"
                f"Primary CBZ/JSON files: {summary.get('primary_file_count', 0)}\n"
                f"Matched sibling files: {summary.get('matched_sibling_file_count', 0)}\n"
                f"Ambiguous shared files: {summary.get('shared_file_count', 0)}\n"
                f"Unrelated files left untouched: {summary.get('unrelated_skipped_file_count', 0)}\n\n"
                f"TOTAL FILES TO MOVE: {summary.get('total_file_count', 0)}\n"
                f"TOTAL SIZE: {human_bytes(summary.get('total_bytes', 0))}\n\n"
                f"Destination:\n{summary.get('destination', '')}\n\n"
                "Move exactly this plan?",
                icon="warning",
                parent=progress_dialog,
            )
            decision["approved"] = bool(approved)
            if approved:
                phase_var.set("Approved. Moving files in the background…")
                progress_bar.configure(mode="indeterminate")
                progress_bar.start(12)
            else:
                cancel_event.set()
                phase_var.set("Canceled before any files moved.")
                cancel_button.state(["disabled"])
            decision_event.set()

        def finish_error(exc: Exception) -> None:
            nonlocal worker_finished
            worker_finished = True
            close_progress_dialog()
            restore_main_window()
            text = str(exc)
            canceled = cancel_event.is_set() or "canceled" in text.casefold()
            if canceled:
                table_status_var.set(
                    "Combine canceled safely; completed moves, if any, were rolled back."
                )
                messagebox.showinfo(
                    "Combine canceled safely",
                    "No completed batch was cleared. Any files already moved were returned "
                    "to their original locations before this message appeared.",
                    parent=root,
                )
            else:
                table_status_var.set(
                    "Combine and Structure failed; no completed batch was cleared."
                )
                messagebox.showerror(
                    "Combine and Structure failed",
                    text,
                    parent=root,
                )

        def finish_success(result: CombineResult) -> None:
            nonlocal worker_finished
            worker_finished = True
            close_progress_dialog()
            restore_main_window()
            # The filesystem/database transaction is already complete. Repaint only
            # bounded v36 gallery windows now, never during file movement.
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

        def poll_worker() -> None:
            nonlocal displayed_serial
            with progress_lock:
                serial = progress_serial
                payload = dict(latest_progress)
            if serial != displayed_serial and payload:
                displayed_serial = serial
                display_progress(payload)

            while True:
                try:
                    item = control_queue.get_nowait()
                except _combine_queue.Empty:
                    break
                kind = item[0]
                if kind == "preflight":
                    handle_preflight(item[1], item[2], item[3])
                elif kind == "error":
                    finish_error(item[1])
                    return
                elif kind == "done":
                    finish_success(item[1])
                    return

            if not worker_finished:
                root.after(75, poll_worker)

        threading.Thread(target=worker, daemon=True).start()
        root.after(50, poll_worker)

'''
    source = _replace_function_block(
        source,
        "run_combine_workflow",
        "notebook_tab_changed",
        async_gui,
    )

    return source
