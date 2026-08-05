#!/usr/bin/env python3
"""Doku Doujins Pairing Gallery v30.0.

The readable implementation is stored as compressed payload chunks beside this
launcher. Use ``--dump-source PATH`` to write one standalone readable script.
"""
from __future__ import annotations

import base64
import bz2
import hashlib
import sys
from pathlib import Path

APP_VERSION = "30.0"
_SOURCE_SHA256 = "af0b416c33edf52e2cbb0e33efc44bbd83677eefd5fec28ef03cee99bec411db"


def _read_embedded_source() -> str:
    payload_dir = Path(__file__).resolve().parent / ".payload-v30"
    parts = sorted(payload_dir.glob("part*.b64"))
    if len(parts) != 10:
        raise RuntimeError(
            f"Expected 10 v30 payload parts beside this launcher, found {len(parts)}"
        )
    encoded = "".join(part.read_text(encoding="ascii").strip() for part in parts)
    source_bytes = bz2.decompress(base64.b64decode(encoded))
    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != _SOURCE_SHA256:
        raise RuntimeError(f"Embedded source checksum mismatch: {actual}")
    return source_bytes.decode("utf-8")


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--dump-source":
    destination = Path(
        sys.argv[2] if len(sys.argv) >= 3 else "pairing_gui_v30_source.py"
    )
    destination.write_text(_read_embedded_source(), encoding="utf-8")
    print(destination.resolve())
    raise SystemExit(0)


_SOURCE = _read_embedded_source()
exec(compile(_SOURCE, __file__, "exec"), globals(), globals())
