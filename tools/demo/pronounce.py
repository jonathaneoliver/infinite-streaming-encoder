#!/usr/bin/env python3
"""Written text -> spoken text.

Written and spoken are not the same string. "4K" reads as "four K", "1h 39m" as
"one hour thirty nine minutes", and `ffmpeg` as the letters — and the engine
gets every one of them wrong left alone.

Its own module because it is the one part of the demo tooling with a
*correctness* property rather than a taste one, so it is the one part that can
have a test. `scripts/test_demo_pronunciation.py` pins it. Everything else here
is judged by watching the video.

The spoken form is DERIVED from the caption text, never read from a stored
`spoken` field. With both stored, editing `text` changed the caption and left
the voice saying the old line — the edit appears to do nothing audible. One
editable field, one source of truth.

## Ordering

The rules are applied in sequence, so order can matter — but far less than it
looks, and the difference is worth knowing before you rearrange anything.

**One ordering is load-bearing: the de-pluralising tail must stay LAST.** It
exists to repair `1 hours` / `1 minutes` / `1 seconds` left by the expansions
above it, so a rule added after it is unrepaired. Moving those three to the
front yields "1 hours 39 minutes", and the test pins that.

**Most of the rest is held by `\\b`, not by order.** `fMP4` is not shadowed by
the later `\\bMP4\\b` because there is no word boundary between `f` and `M` —
swapping the two changes nothing. The same goes for the acronyms generally.
This was originally documented here as an ordering hazard for both; writing the
test disproved it.

`\\b(\\d+)h (\\d+)m\\b` is **redundant** — the bare `\\b(\\d+)h\\b` and
`\\b(\\d+)m\\b` rules produce the identical result on every compound tested,
because the de-pluralising tail repairs what the split leaves. It is kept
because it states the intent legibly and costs one `re.sub`, not because
removing it would change output.

## Extending it for another project

`SPEECH` is this project's vocabulary — `apple-uniq-live-xs` and `VBV` mean
nothing elsewhere. A second project adds its own terms through
`DEMO_PRONOUNCE` (a file of `pattern<TAB>replacement` lines) rather than by
editing this table, so the two do not fork.

Extras are spliced in **before** the de-pluralising tail, so an extra that
emits "1 hours" is repaired to "1 hour" for free. Note what that does NOT buy:
the duration rules have already run by then, so an extra emitting a raw "1m"
comes out as "1m". Emit the expanded form and let the tail fix the plural, or
emit the finished string.
"""
import os, re

# The de-pluralising tail. Split out by name because "these must stay last" is
# an invariant, and an invariant referenced by a variable survives an edit that
# a comment does not.
DEPLURALISE = [
    (r"\b1 hours\b", "1 hour"), (r"\b1 minutes\b", "1 minute"), (r"\b1 seconds\b", "1 second"),
]

BASE_SPEECH = [
    (r"\bAWS\b", "A W S"), (r"\bVBV\b", "V B V"), (r"\bCPU\b", "C P U"),
    (r"\bHLS\b", "H L S"), (r"\bAV1\b", "A V one"), (r"\bfMP4\b", "fragmented M P four"),
    (r"\bMP4\b", "M P four"), (r"\bS3\b", "S three"), (r"\bFPV\b", "F P V"),
    (r"\b4K\b", "four K"), (r"\bH\.264\b", "H 264"), (r"\bHEVC\b", "H E V C"),
    # Written as ffmpeg, spoken as the letters. Without this the only way to
    # get the pronunciation right was to misspell it ON SCREEN.
    (r"(?i)\bffmpeg\b", "FF MPEG"),   # what actually sounded right when typed by hand
    (r"\bOUTPUT_DIR\b", "the output directory"),
    (r"apple-uniq-live-xs", "apple uniq live x s"),
    (r"\b(\d+)p\b", r"\1 p"),
    # Durations as the app prints them: "1h 39m" is read "one aitch thirty nine
    # em" unless expanded. These reach the narration whenever a figure is quoted
    # back off the estimate panel.
    (r"\b(\d+)h (\d+)m\b", r"\1 hours \2 minutes"),
    (r"\b(\d+)h\b", r"\1 hours"),
    (r"\b(\d+)m (\d+)s\b", r"\1 minutes \2 seconds"),
    (r"\b(\d+)m\b", r"\1 minutes"),
    (r"\b(\d+)s\b", r"\1 seconds"),
]


def load_extras(path):
    """Read `pattern<TAB>replacement` lines. Blank lines and # comments ignored.

    A malformed line RAISES rather than being skipped: a pronunciation rule that
    silently did not load is indistinguishable from one that did not match, and
    the symptom arrives 200 generated clips later as one mispronounced word.
    """
    out = []
    with open(path) as f:
        for n, ln in enumerate(f, 1):
            ln = ln.rstrip("\n")
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if "\t" not in ln:
                raise ValueError("%s:%d: expected pattern<TAB>replacement, got %r" % (path, n, ln[:60]))
            pat, rep = ln.split("\t", 1)
            re.compile(pat)          # fail here, naming the line, not at first use
            out.append((pat, rep.strip()))
    return out


def build(extras=None):
    """BASE + project extras + the de-pluralising tail, in that order."""
    return BASE_SPEECH + list(extras or []) + DEPLURALISE


_extras_path = os.environ.get("DEMO_PRONOUNCE")
SPEECH = build(load_extras(_extras_path) if _extras_path else None)


def for_speech(t, rules=None):
    for pat, rep in (rules if rules is not None else SPEECH):
        t = re.sub(pat, rep, t)
    return t


if __name__ == "__main__":
    import sys
    for line in (sys.argv[1:] or [l.rstrip("\n") for l in sys.stdin]):
        print(for_speech(line))
