#!/usr/bin/env python3
"""Syntax-check the inline JavaScript in static/index.html.

The UI is a single self-contained HTML file with no build step, so nothing else
would catch a syntax error before it reaches the browser — the page just loads
blank. This extracts every <script> block and runs `node --check` over the
concatenation.

Syntax only: it can't catch a typo'd element id or a missing endpoint. Exits 0
when clean, 1 with node's error when not, and 0 with a notice when node isn't
installed (so `make check` stays usable without it).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


def main() -> int:
    if not shutil.which("node"):
        print("node not installed — skipped")
        return 0
    if not PAGE.exists():
        print(f"{PAGE} not found")
        return 1

    # Only bare <script> blocks: a src= tag has no inline body, and a
    # type="application/json" block isn't JavaScript.
    blocks = re.findall(r"<script>(.*?)</script>", PAGE.read_text(), re.S)
    if not blocks:
        print("no inline <script> blocks found — nothing to check")
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write("\n".join(blocks))
        tmp = f.name
    try:
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    finally:
        Path(tmp).unlink(missing_ok=True)

    if r.returncode != 0:
        # Line numbers refer to the concatenation, not index.html — close enough
        # to locate the block, since there's normally only one.
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
