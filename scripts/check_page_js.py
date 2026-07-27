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

import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"


class ScriptCollector(HTMLParser):
    """Collect the body of every inline <script>.

    Uses the stdlib parser rather than a regex deliberately. Regex tag matching
    kept missing real forms — `<SCRIPT>`, `<script type="module">`, `</script >`
    — and each miss was SILENT: the block was skipped and the checker still
    reported success, which is worse than having no checker. HTMLParser handles
    tag case, attributes and whitespace by construction, so the class of bug is
    gone rather than patched case by case.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self.skipped = 0
        self._collecting = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        d = {k.lower(): (v or "").lower() for k, v in attrs}
        # An external script has no inline body to check, and a non-JS type
        # (application/json, text/template) isn't JavaScript.
        t = d.get("type", "")
        if "src" in d or (t and "javascript" not in t and t != "module"):
            self.skipped += 1
            return
        self._collecting = True

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self.blocks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._collecting = False


def main() -> int:
    if not shutil.which("node"):
        print("node not installed — skipped")
        return 0
    if not PAGE.exists():
        print(f"{PAGE} not found")
        return 1

    collector = ScriptCollector()
    collector.feed(PAGE.read_text())
    collector.close()
    blocks, skipped = collector.blocks, collector.skipped

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
