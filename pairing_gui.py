#!/usr/bin/env python3
"""Doku Doujins Pairing Gallery v31.0.

The readable implementation is stored as compressed payload chunks beside this
launcher. Use ``--dump-source PATH`` to write one standalone readable script.
"""
from __future__ import annotations

import base64
import bz2
import hashlib
import sys
from pathlib import Path

APP_VERSION = "31.0"
_SOURCE_SHA256 = "af0b416c33edf52e2cbb0e33efc44bbd83677eefd5fec28ef03cee99bec411db"


def _read_embedded_source() -> str:
    payload_dir = Path(__file__).resolve().parent / ".payload-v30"
    parts = sorted(payload_dir.glob("part*.b64"))
    if len(parts) != 11:
        raise RuntimeError(
            f"Expected 11 v30 payload parts beside this launcher, found {len(parts)}"
        )
    encoded = "".join(part.read_text(encoding="ascii").strip() for part in parts)
    source_bytes = bz2.decompress(base64.b64decode(encoded))
    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != _SOURCE_SHA256:
        raise RuntimeError(f"Embedded source checksum mismatch: {actual}")
    return source_bytes.decode("utf-8")


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"v31 source patch target not found: {label}")
    return source.replace(old, new, 1)


def _read_patched_source() -> str:
    """Apply the lightweight Table 3 fixes and v31 ingest-export workflow."""
    source = _read_embedded_source()
    source = source.replace('APP_VERSION = "30.0"', 'APP_VERSION = "31.0"', 1)
    source = source.replace("pady=(-8, 10)", "pady=(0, 10)")
    source = source.replace("pady=(-8,10)", "pady=(0, 10)")

    helper_path = Path(__file__).resolve().parent / "table3_ingest_export.py"
    helper_source = helper_path.read_text(encoding="utf-8")
    source = _replace_once(
        source,
        "\ndef choose_gui_font_family() -> str:\n",
        "\n" + helper_source + "\n\ndef choose_gui_font_family() -> str:\n",
        "module export helpers",
    )

    source = _replace_once(
        source,
        '        if path.is_dir() and not path.name.startswith(".")\n',
        '        if path.is_dir()\n'
        '        and not path.name.startswith(".")\n'
        '        and path.name != "export-for-ingest"\n',
        "exclude export directory from Table 3 runs",
    )

    toolbar_old = """    older_button = ttk.Button(toolbar, text="◀ Older")
    older_button.grid(row=0, column=0, padx=(0, 6))

    newer_button = ttk.Button(toolbar, text="Newer ▶")
"""
    toolbar_new = """    older_button = ttk.Button(toolbar, text="◀ Older")
    older_button.grid(row=0, column=0, padx=(0, 6))

    export_table3_button = tk.Button(
        toolbar,
        text="EXPORT",
        background="#237a3b",
        activebackground="#1d6531",
        foreground="white",
        activeforeground="white",
        font=(gui_font_family, 10, "bold"),
        relief="raised",
        borderwidth=2,
        padx=16,
        pady=4,
        cursor="hand2",
    )
    export_table3_button.grid(row=0, column=0, padx=(0, 6))
    export_table3_button.grid_remove()

    newer_button = ttk.Button(toolbar, text="Newer ▶")
"""
    source = _replace_once(
        source, toolbar_old, toolbar_new, "green EXPORT button"
    )

    export_handler = r'''
    def export_current_table3() -> None:
        database_path = current_combined_database()
        if database_path is None:
            messagebox.showinfo(
                "No Table 3 run selected",
                "Choose a Combined run in Table 3 first.",
                parent=root,
            )
            return
        export_table3_button.configure(state="disabled")
        root.configure(cursor="watch")
        combined_loaded_var.set("EXPORTING INGEST TABLE…")
        table_status_var.set(
            f"Exporting ingest handoff from {database_path.parent}…"
        )
        root.update_idletasks()
        try:
            sqlite_path, sql_path, work_count = export_table3_for_ingest(
                database_path,
                EXPORT_FOR_INGEST_ROOT,
            )
        except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
            messagebox.showerror(
                "Export failed",
                str(exc),
                parent=root,
            )
            combined_loaded_var.set("EXPORT FAILED")
            table_status_var.set(f"Table 3 export failed: {exc}")
        else:
            combined_loaded_var.set(
                f"EXPORTED {work_count} WORK{'S' if work_count != 1 else ''}"
            )
            table_status_var.set(
                f"Exported {work_count} ingest-ready work rows to {sqlite_path}"
            )
            messagebox.showinfo(
                "Table 3 export complete",
                f"Works exported: {work_count}\n\n"
                f"SQLite:\n{sqlite_path}\n\n"
                f"SQL dump:\n{sql_path}\n\n"
                f"Stable latest copy:\n{EXPORT_FOR_INGEST_ROOT / 'latest.sqlite3'}",
                parent=root,
            )
        finally:
            export_table3_button.configure(state="normal")
            root.configure(cursor="")

'''
    source = _replace_once(
        source,
        "    def notebook_tab_changed(_event: object | None = None) -> None:\n",
        export_handler
        + "    def notebook_tab_changed(_event: object | None = None) -> None:\n",
        "Table 3 export handler",
    )

    tab_old = """    def notebook_tab_changed(_event: object | None = None) -> None:
        if active_tab_index() == 2:
            table2_actions.grid_remove()
            table3_actions.grid()
            reload_combined_databases(display=True)
            refresh_table3_actions()
        else:
            table3_actions.grid_remove()
            refresh_pairing_header_only()
            refresh_table2_actions()
"""
    tab_new = """    def notebook_tab_changed(_event: object | None = None) -> None:
        if active_tab_index() == 2:
            older_button.grid_remove()
            export_table3_button.grid()
            table2_actions.grid_remove()
            table3_actions.grid()
            reload_combined_databases(display=True)
            refresh_table3_actions()
        else:
            export_table3_button.grid_remove()
            older_button.grid()
            table3_actions.grid_remove()
            refresh_pairing_header_only()
            refresh_table2_actions()
"""
    source = _replace_once(
        source, tab_old, tab_new, "show EXPORT only in Table 3"
    )

    source = _replace_once(
        source,
        "    older_button.configure(command=lambda: navigate_runs(-1))\n",
        "    export_table3_button.configure(command=export_current_table3)\n"
        "    older_button.configure(command=lambda: navigate_runs(-1))\n",
        "bind EXPORT command",
    )
    return source


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--dump-source":
    destination = Path(
        sys.argv[2] if len(sys.argv) >= 3 else "pairing_gui_v31_source.py"
    )
    destination.write_text(_read_patched_source(), encoding="utf-8")
    print(destination.resolve())
    raise SystemExit(0)


_SOURCE = _read_patched_source()
exec(compile(_SOURCE, __file__, "exec"), globals(), globals())
