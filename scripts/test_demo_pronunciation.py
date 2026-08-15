#!/usr/bin/env python3
"""The demo narration's written -> spoken table, and the narrative round-trip.

`tools/demo/` is judged by watching a video, which is a slow and subjective
gate and not one CI can run. Two pieces of it are not like that — they have
right answers — and this pins those:

  1. `pronounce.for_speech`. Every acronym and duration form here mispronounced
     in a real take before the rule existed. A regression is inaudible to
     everything except a person listening to a 14-minute video.
  2. `edit_text` save/load. The narrative is tracked in git and joined back to
     the recording BY INDEX, so a lossy round-trip or an off-by-one silently
     attaches the script to the wrong moments.

Lives in `scripts/` rather than beside the tooling because CI globs
`scripts/test_*.py` — a test under `tools/` would need a CI edit to run, and an
enumerated list goes stale (the reason that glob exists at all).

Stdlib only. No ffmpeg, no network, no voice engine.
"""
import json, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "demo"))

import pronounce  # noqa: E402
import edit_text  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        failures.append(name)


def says(written, spoken):
    got = pronounce.for_speech(written)
    check(f"{written!r} -> {spoken!r}", got == spoken, f"got {got!r}")


print("acronyms — each of these was mispronounced in a real take")
says("AWS", "A W S")
says("VBV", "V B V")
says("CPU", "C P U")
says("HLS", "H L S")
says("HEVC", "H E V C")
says("AV1", "A V one")
says("S3", "S three")
says("FPV", "F P V")
says("4K", "four K")
says("H.264", "H 264")
says("OUTPUT_DIR", "the output directory")

print("\nffmpeg — case-insensitive, because the caption spells it three ways")
# Without this rule the only way to get the pronunciation right was to
# misspell it ON SCREEN.
says("ffmpeg", "FF MPEG")
says("FFmpeg", "FF MPEG")
says("FFMPEG", "FF MPEG")

print("\nLL-HLS before HLS — a REAL ordering dependency, unlike the two below")
# A hyphen is a word boundary, so \bHLS\b matches inside "LL-HLS" and leaves
# "LL-H L S". Ordering is the only thing that prevents it: once HLS has fired
# there is no "LL-HLS" left for a later rule to catch. Found by listening to
# `pronounce.py --list`, which is what that listing is for.
says("LL-HLS playlists", "L L H L S playlists")
says("plain HLS", "plain H L S")
hls_last = [r for r in pronounce.SPEECH if r[0] != r"\bLL-HLS\b"] + [(r"\bLL-HLS\b", "L L H L S")]
check("moving LL-HLS after HLS breaks it",
      pronounce.for_speech("LL-HLS", hls_last) == "LL-H L S",
      "the counterexample no longer reproduces")

print("\nfMP4 is not shadowed by the later bare MP4 rule")
# Held by \b (there is no word boundary between `f` and `M`), NOT by ordering —
# the module docstring used to claim the opposite.
says("fMP4", "fragmented M P four")
says("MP4", "M P four")

print("\ndurations, as the estimate panel prints them")
says("1h 39m", "1 hour 39 minutes")
says("2h 15m", "2 hours 15 minutes")
says("1h", "1 hour")
says("1m", "1 minute")
says("1s", "1 second")
says("6s", "6 seconds")
says("5m 30s", "5 minutes 30 seconds")

print("\nresolutions")
says("1080p", "1080 p")

print("\na whole caption, the way one actually reads")
says("The 6s ladder at 1080p took 1h 39m on ffmpeg.",
     "The 6 seconds ladder at 1080 p took 1 hour 39 minutes on FF MPEG.")

print("\nthe de-pluralising tail must stay LAST — the one load-bearing ordering")
# It repairs "1 hours" / "1 minutes" / "1 seconds" left by the expansions above
# it, so anything added after it goes unrepaired. Proven by construction rather
# than asserted: build the same rules in the wrong order and watch it break.
wrong = pronounce.DEPLURALISE + pronounce.BASE_SPEECH
check("reordered table mispronounces '1h 39m'",
      pronounce.for_speech("1h 39m", wrong) == "1 hours 39 minutes",
      "the counterexample no longer reproduces — has the tail moved?")
check("correct table still repairs it",
      pronounce.for_speech("1h 39m") == "1 hour 39 minutes")

print("\napplying twice changes nothing more than applying once")
# A new rule that re-fires on its own output would compound silently over a
# corpus; this is the cheap guard against it.
for line in ["The 6s ladder at 1080p took 1h 39m.", "a 4K HEVC run on S3",
             "fMP4 via ffmpeg", "1h", "1m", "1s"]:
    once = pronounce.for_speech(line)
    check(f"idempotent: {line!r}", pronounce.for_speech(once) == once,
          f"{once!r} -> {pronounce.for_speech(once)!r}")

print("\nproject extras splice in BEFORE the de-pluralising tail")
# The extension point for a second project. Extras run AFTER the base rules and
# BEFORE the tail, so what they get for free is the plural repair — not the
# duration expansion, which has already happened by then. An extra wanting
# "1h" spoken must therefore emit "1 hours" and let the tail fix it, or emit
# the finished string itself.
extra = [(r"\bONEHOUR\b", "1 hours")]
check("an extra emitting '1 hours' is de-pluralised by the tail",
      pronounce.for_speech("ONEHOUR", pronounce.build(extra)) == "1 hour",
      f"got {pronounce.for_speech('ONEHOUR', pronounce.build(extra))!r}")
# And the same extra appended after the tail is NOT repaired — the ordering
# this splice point exists to get right.
check("the same extra appended after the tail is left unrepaired",
      pronounce.for_speech("ONEHOUR", pronounce.SPEECH + extra) == "1 hours")

print("\nDEMO_PRONOUNCE parsing")
with tempfile.TemporaryDirectory() as td:
    good = os.path.join(td, "extra.tsv")
    with open(good, "w") as f:
        f.write("# a comment\n\n" + r"\bGOPR\b" + "\tgo pro\n")
    check("well-formed extras load", pronounce.load_extras(good) == [(r"\bGOPR\b", "go pro")])

    bad = os.path.join(td, "bad.tsv")
    with open(bad, "w") as f:
        f.write("no tab here\n")
    try:
        pronounce.load_extras(bad)
        check("a line with no tab raises", False, "it was accepted")
    except ValueError as e:
        check("a line with no tab raises, naming the line", "bad.tsv:1" in str(e), str(e))

    badre = os.path.join(td, "badre.tsv")
    with open(badre, "w") as f:
        f.write("[unclosed\tx\n")
    try:
        pronounce.load_extras(badre)
        check("an invalid regex raises at LOAD time", False, "it was accepted")
    except Exception:
        check("an invalid regex raises at LOAD time", True)

print("\nevery rule has a sample you can hear")
# `pronounce.py --list` prints SAMPLES, and `audition.py` speaks them. A rule
# absent from that list is one nobody will ever listen to — and listening is
# the only way to judge a pronunciation, since no assertion here can tell you
# that "L L H L S" sounds better than "LL-H L S". This keeps the audible
# surface equal to the actual surface.
missing = pronounce.unfired()
check("no rule is unauditionable", not missing,
      "add a sample to pronounce.SAMPLES for: "
      + ", ".join(p for p, _ in missing))

print("\nnarrative round-trip — text in git, timings in cues.json, joined by index")
# The 7-of-42 case that broke the first version: a caption containing a newline
# is written across two file lines, and the continuation parses as neither a cue
# nor a comment.
sample = {"cues": [
    {"at": 0.0, "text": "Plain line."},
    {"at": 4.2, "text": "Target is where the encode runs. \nLocal spreads it across this network."},
    {"at": 9.9, "text": "A backslash \\ and a 4K mention."},
]}
with tempfile.TemporaryDirectory() as td:
    cues_path = os.path.join(td, "cues.json")
    json.dump(sample, open(cues_path, "w"))
    edit_text.CUES = cues_path
    edit_text.NARRATIVE_DIR = td

    edit_text.save("t")
    rc = edit_text.load("t")
    check("round-trip applies cleanly", rc == 0, f"rc={rc}")
    back = json.load(open(cues_path))["cues"]
    for i, c in enumerate(sample["cues"]):
        check(f"cue {i} survives the round-trip byte-for-byte",
              back[i]["text"] == c["text"], f"{back[i]['text']!r} != {c['text']!r}")
    check("timings are NOT in the tracked file",
          "00:04" not in open(os.path.join(td, "t.txt")).read(),
          "a timestamp leaked into the narrative — every re-record would diff every line")

    # A narrative written against a different drive must never be applied by
    # index: it shifts the script onto the wrong moments, invisibly until playback.
    json.dump({"cues": sample["cues"][:2]}, open(cues_path, "w"))
    check("a cue-count mismatch is REFUSED", edit_text.load("t") == 2)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all demo pronunciation + narrative checks passed")
