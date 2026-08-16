# Demo video tooling

Drive the real app with Playwright, run a real encode, and turn the recording
into a narrated, captioned, fast-forwarded MP4 — without hand-editing a
timeline.

Built and used end to end on 2026-08-14: a 14.5-minute video, two halves (local
farm, then AWS Batch) joined by a fade, with two selectable voice tracks.

**Nothing here is on the encode path.** It drives the app from outside, over
HTTP and the DOM, exactly as a person would.

## Why it is in the repo

Everything it drives is repo behaviour — the encode form, the ladder picker, the
Jobs view, the Outputs panel. When those change, the demo breaks, and there was
nothing in-tree to notice. Three from building it, none of them demo bugs:

- The scaling segment narrated "adding HEVC doubles the jobs" while the click
  **removed** HEVC, because the codec checkboxes default to H.264 **and** HEVC
  and the demo set the codec *after* the segment that depended on it.
- `resolveCodec` skips a file whose `<stem>_<codec>` output exists, so a second
  take produced a 0-stage job that "succeeded" in 35 seconds.
- `_detailsCollapsed()` returns `!_jobRunning(j)`, so a running job's details
  are open by default — a blind click on the toggle CLOSED the chunk rows the
  demo existed to show.

## Layout

| file | does |
| --- | --- |
| `record_local.js` | drives the app, records webm, writes `cues.json` (caption text + exact timings + segment marks) |
| `record_cloud.js` | the cloud twin: target=cloud, cloud fleet panels, capacity-wait and idle-billing narration |
| `narrator_app.py` + `narrator.html` | browser editor: scrub, edit caption text, audition/select voices, drag the fast-forward range, export with a progress bar |
| `narrate_sentences.py` | text → speech, sentence-split with controlled pauses, hash-cached |
| `pronounce.py` | written → spoken (`4K` → "four K"). The one piece with a test |
| `audition.py` | speaks that vocabulary aloud, so you can judge it by ear |
| `make_ass.py` | cues → ASS subtitles positioned in the caption strip |
| `join.py` | concatenates halves with a fade to black, preserving every audio track |
| `edit_text.py` | narration in and out of a plain text file, and in and out of git |
| `narrative/` | **tracked**: the scripts themselves, one file per project |

## Where things live

Everything generated — recordings, `cues.json`, ~190 wav clips, the exports —
goes to **`$DEMO_DIR`**, which defaults to `~/Desktop/encoder-demo` and is
outside the repo. Only the tools and `narrative/` are tracked.

```bash
export DEMO_DIR=~/Desktop/encoder-demo     # the default
```

That split is the point: a take writes hundreds of megabytes, and none of it
belongs in a checkout. The *script* does.

## The six steps

```bash
cd tools/demo

# 1. RECORD — drives a real encode and stamps every caption's time.
#    Those timings are the whole trick: the narration lands on the action
#    because the recorder wrote down when the action happened.
SOURCE=smoke.mp4 CODEC=h264 LADDER=apple-uniq-live-xs node record_local.js

# 2. EDIT — the browser tool. Text edits invalidate one clip; a 30s debounce
#    then regenerates it in the background.
python3 narrator_app.py            # then open the URL it prints

# 3. VOICE — any subset of the voice catalogue; each selected voice becomes
#    its own labelled audio track. Hover to audition.

# 4. FAST-FORWARD — a range with a factor. Cue times are remapped through it,
#    so audio and captions follow the compressed timeline.

# 5. EXPORT — FFWD render -> mux (loudness-normalised) -> burn captions.

# 6. JOIN — fade to black between the halves.
python3 join.py local.mp4 cloud.mp4 encoder-demo-full.mp4
```

Steps 2-5 are all inside `narrator_app.py`; the command line versions
(`narrate_sentences.py`, `make_ass.py`) exist for re-running one stage without
the UI.

## The narrative is in git

`narrative/<project>.txt` is the script. The recording supplies the timings, and
the two are joined **by index**.

```bash
python3 edit_text.py save encoder-local   # cues.json -> narrative/, commit this
python3 edit_text.py load encoder-local   # narrative/ -> cues.json
```

Edit the tracked file, commit, and `git diff` shows what changed about what is
*said*. Two rules make that work, and both are pinned by the test:

- **No timestamps in the tracked file.** They move a little on every take, so
  tracking them means a re-record rewrites every line and the diff shows 60
  changes when nothing was said differently.
- **`load` refuses a cue-count mismatch.** A narrative written against a 42-cue
  drive applied to a 21-cue one would shift every line onto the wrong moment,
  invisibly until playback.

Caption text may contain newlines (7 of the first demo's 42 cues do); they are
escaped as `\n` in the tracked file so one cue stays one line.

## Checking the pronunciations

```bash
python3 pronounce.py --list    # the vocabulary, written -> spoken
python3 audition.py            # the same list, out loud
python3 audition.py --both     # written form first, then spoken — the A/B
```

`make check`'s `py demospeech` asserts fixed pairs; it can tell you the table
changed and never that it sounds wrong. The listing and the audition are how
you answer the second question, and every rule is required to have a sample in
`pronounce.SAMPLES` so nothing is unauditionable.

Two things worth knowing when it misbehaves:

- **Generation happens up front, playback second.** Voicebox plays a clip
  itself as it finishes generating, so generating and playing in one loop plays
  every new clip twice a beat apart — an echo that makes the pronunciation
  impossible to judge. Clips are cached by the SPOKEN text, so changing a rule
  invalidates exactly the clips it affects.
- **A wedged voice server looks like a slow one.** It keeps accepting requests
  and reporting `generating` forever. `audition.py` gives up after 45s and says
  so; restart Voicebox. (`narrate_sentences` still allows 600s, which is right
  for a long narration line and was badly wrong for a one-second clip.)

## Rules learned the hard way

These are in `/demo` too, which is the version to follow when actually recording.

- **Record a smoke-length version first.** A 20s clip caught three wrong
  narration lines before the 5.6-minute cloud run, for about a cent.
- **Set the codec and ladder BEFORE any segment that describes them.**
- **Use an output suffix** so a demo never archives a comparison encode.
- **Re-encode, not Encode**, or a repeat run silently skips (`resolveCodec`).
- **Clear terminal jobs first** — two `CANCELLED` cards sat on screen through
  the whole fast-forward of the finished video.

## Using this for another project

The split is clean, and it is worth knowing which half you are touching.

**Generic** — `narrate_sentences.py`, `pronounce.py`, `make_ass.py`, `join.py`,
`edit_text.py`, `narrator_app.py`, `narrator.html`. These know about cues,
timings, voices and ffmpeg. Nothing in them is about video encoding.

**App-specific** — `record_local.js` and `record_cloud.js`. Selectors, tab
names, the ladder picker, what counts as "done". A second app writes its own
recorder and reuses everything else.

Three seams are already parameterised for that:

```bash
BASE=http://localhost:3000 \
DEMO_PROJECT=infinite-streaming \
DEMO_PRONOUNCE=narrative/infinite-streaming.pronounce \
  node record_myapp.js
```

- `BASE` — the app under test.
- `DEMO_PROJECT` — which `narrative/<project>.txt` is the script.
- `DEMO_PRONOUNCE` — extra `pattern<TAB>replacement` lines. **Add project
  vocabulary here rather than editing `pronounce.py`'s table**, or the two
  projects fork the file. Extras splice in before the de-pluralising tail; see
  that module's docstring for what that does and does not buy you.

What a second project would still need, and does not have yet: the recorders
share a lot of shape (cursor, caption strip, spotlight, mark-taking) that is
currently copy-pasted between the two here. That is the thing to factor out
when there is a third — not before, since two copies have disagreed usefully.

## Requirements

`playwright` (npm) for the recorders; `ffmpeg`/`ffprobe` on PATH; a local
Voicebox-compatible TTS server on `127.0.0.1:17493` for narration. The
pronunciation and narrative tests need none of these — they are stdlib only,
which is why they can run in CI.
