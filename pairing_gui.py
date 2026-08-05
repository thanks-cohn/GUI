#!/usr/bin/env python3
"""Doku Doujins Pairing Gallery v30.1.

The readable implementation is stored as compressed payload chunks beside this
launcher. Use ``--dump-source PATH`` to write one standalone readable script.
"""
from __future__ import annotations

import base64
import bz2
import hashlib
import sys
from pathlib import Path

APP_VERSION = "30.1"
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


def _read_patched_source() -> str:
    """Apply the v30.1 hotfix before compiling or exporting the source."""
    source = _read_embedded_source()
    source = source.replace('APP_VERSION = "30.0"', 'APP_VERSION = "30.1"', 1)
    # Tk 8.6/9 rejects negative external padding. The lightweight Table 3 card
    # inherited one cosmetic negative pady value, which aborted rendering after
    # the first directory even though all directories had been discovered.
    source = source.replace("pady=(-8, 10)", "pady=(0, 10)")
    source = source.replace("pady=(-8,10)", "pady=(0, 10)")
    return source


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--dump-source":
    destination = Path(
        sys.argv[2] if len(sys.argv) >= 3 else "pairing_gui_v30_source.py"
    )
    destination.write_text(_read_patched_source(), encoding="utf-8")
    print(destination.resolve())
    raise SystemExit(0)


_SOURCE = _read_patched_source()
exec(compile(_SOURCE, __file__, "exec"), globals(), globals())