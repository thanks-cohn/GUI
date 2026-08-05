#!/usr/bin/env python3
"""Developer access to the Table 3 ingest-export schema v2 source.

The GUI launcher verifies and injects the complete readable helper stored in
``.payload-table3-v2/source.b64``. Running this file writes that readable source
without starting the GUI:

    python3 table3_ingest_export.py --dump-source /tmp/table3_ingest_export_v2.py

The standalone GUI produced by ``pairing_gui.py --dump-source`` already contains
the full helper and does not need this payload directory at runtime.
"""
from __future__ import annotations

import base64
import bz2
import hashlib
import sys
from pathlib import Path

SOURCE_SHA256 = "f08beca4d9bdcc6ec9e44a3ec8138f8cf64ebd915fc087c2ca609fb7ca962930"


def read_source() -> str:
    payload = Path(__file__).resolve().parent / ".payload-table3-v2" / "source.b64"
    source_bytes = bz2.decompress(
        base64.b64decode(payload.read_text(encoding="ascii").strip())
    )
    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != SOURCE_SHA256:
        raise RuntimeError(f"Table 3 export helper checksum mismatch: {actual}")
    return source_bytes.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--dump-source":
        print(__doc__.strip())
        return 2
    destination = Path(args[1]).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(read_source(), encoding="utf-8")
    print(destination.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
