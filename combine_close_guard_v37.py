"""Add main-window close protection to the asynchronous v37 combine dialog."""
from __future__ import annotations


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"v37 close-guard patch target not found: {label}")
    return source.replace(old, new, 1)


def apply(source: str) -> str:
    source = _replace_once(
        source,
        '''        progress_dialog.protocol("WM_DELETE_WINDOW", request_cancel)\n\n        combine_structure_button.state(["disabled"])\n''',
        '''        progress_dialog.protocol("WM_DELETE_WINDOW", request_cancel)\n        progress_dialog.bind(\n            "<Escape>",\n            lambda _event: (request_cancel(), "break")[1],\n        )\n        # Never destroy Python in the middle of a move. The main window close\n        # button becomes a safe cancellation request until cleanup finishes.\n        root.protocol("WM_DELETE_WINDOW", request_cancel)\n\n        combine_structure_button.state(["disabled"])\n''',
        "protect progress and main-window close actions",
    )
    source = _replace_once(
        source,
        '''        def restore_main_window() -> None:\n            root.configure(cursor="")\n            try:\n''',
        '''        def restore_main_window() -> None:\n            root.configure(cursor="")\n            root.protocol("WM_DELETE_WINDOW", close_gui)\n            try:\n''',
        "restore normal close behavior",
    )
    return source
