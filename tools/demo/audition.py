#!/usr/bin/env python3
"""Hear how the narration will say things.

    python3 audition.py                     # every sample in pronounce.SAMPLES
    python3 audition.py "a 4K HEVC run"     # one phrase
    python3 audition.py --raw "4K"          # skip the table, speak it verbatim
    python3 audition.py --voice <id>        # a specific profile
    python3 audition.py --no-play           # generate and cache, print paths

The listing (`pronounce.py --list`) tells you what the table DOES. This tells
you whether it SOUNDS right, which is the only question that actually matters
and the one no test can answer.

Each phrase is spoken twice when they differ — written form first, then spoken
form — so you can hear what the rule bought. That is the comparison you need:
"4K" is only obviously wrong once you have heard it read as a word.

Clips are cached by hash under $DEMO_DIR/audition/, so a second run is instant
and you can re-listen without re-generating.
"""
import argparse, hashlib, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pronounce                      # noqa: E402
import narrate_sentences as ns        # noqa: E402

CACHE = os.path.join(os.environ.get("DEMO_DIR",
                     os.path.expanduser("~/Desktop/encoder-demo")), "audition")


def play(path):
    # afplay is macOS; ffplay is the fallback everywhere else. Neither being
    # present is not fatal — the clip is still on disk and its path is printed.
    for cmd in (["afplay", path], ["ffplay", "-v", "quiet", "-nodisp", "-autoexit", path]):
        try:
            subprocess.run(cmd, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def speak(text, voice, do_play):
    os.makedirs(CACHE, exist_ok=True)
    key = hashlib.sha1(("%s|%s" % (voice or "", text)).encode()).hexdigest()[:12]
    dest = os.path.join(CACHE, "a-%s.wav" % key)
    if not os.path.exists(dest):
        d = ns.generate(text, dest, profile=voice)
        if not d:
            print("      (generation failed — is the voice server up on %s?)" % ns.BASE)
            return None
    if do_play:
        play(dest)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*", help="phrase(s) to audition; default is the sample list")
    ap.add_argument("--voice", "-p", default=None, help="profile id (default: narrate_sentences.PROFILE)")
    ap.add_argument("--raw", action="store_true", help="speak verbatim, skipping the table")
    ap.add_argument("--no-play", action="store_true", help="generate and cache only")
    ap.add_argument("--both", action="store_true",
                    help="also speak the WRITTEN form, to hear what the rule bought")
    args = ap.parse_args()

    phrases = args.text or pronounce.SAMPLES
    do_play = not args.no_play
    width = max(len(p) for p in phrases)

    for p in phrases:
        spoken = p if args.raw else pronounce.for_speech(p)
        changed = spoken != p
        print("%-*s  ->  %s%s" % (width, p, spoken, "" if changed else "   (unchanged)"))
        if args.both and changed:
            print("      written form first…")
            speak(p, args.voice, do_play)
        path = speak(spoken, args.voice, do_play)
        if path and not do_play:
            print("      %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
