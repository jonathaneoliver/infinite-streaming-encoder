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

    # Match the opening tag WITH its attributes, case-insensitively. A
    # case-sensitive `<script>` literal would silently skip a `<SCRIPT>` or
    # `<script type="module">` block — and a checker that quietly covers
    # nothing is worse than no checker, since it still reports success.
    blocks: list[str] = []
    skipped = 0
    for attrs, body in re.findall(
        r"<script([^>]*)>(.*?)</script>", PAGE.read_text(), re.S | re.I
    ):
        a = attrs.lower()
        # An external script has no inline body to check, and a non-JS type
        # (application/json, text/template) isn't JavaScript.
        if "src=" in a or ("type=" in a and "javascript" not in a and "module" not in a):
            skipped += 1
            continue
        blocks.append(body)

    if not blocks:
        # index.html is a single self-contained page whose whole UI is inline
        # JS. Finding none means the extraction broke, not that there's
        # nothing to check — fail rather than report a vacuous pass.
        print(f"no inline <script> blocks found in {PAGE.name} — extraction is broken")
        return 1
    print(f"{len(blocks)} inline block(s), {skipped} external/non-JS skipped", end=" ")

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
