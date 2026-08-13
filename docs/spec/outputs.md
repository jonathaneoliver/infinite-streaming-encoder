# Outputs: naming, and the four states a directory can be in

An output directory looks the same whether it holds a complete encode or four
megabytes of manifests describing media that no longer exists. This is the file
that says how to tell them apart, and why the answer is a sidecar rather than a
field.

## The core model

**An output's state is carried by which sidecar FILE is present, not by a value
inside one.** There are four states and they are mutually exclusive:

| on disk | means | offer |
| --- | --- | --- |
| neither sidecar | complete | Play |
| `.remote.json` | packaged; SEGMENTS still in S3 | Download |
| `.pending.json` | encoded, never packaged; CHUNKS in S3 | Package |
| either, with `gone: true` or expired | unrecoverable | nothing, and say why |

### Why a file and not a field

The sidecar moves with the directory through `moveTmpToOutput` and survives a
restart of the server, so the two languages agree on a filename and nothing
else. A field would need a record that both sides read, write and version.

More importantly, **a metadata-only output is indistinguishable from a complete
one by every other signal** — right name, right rung subdirectories, manifests
present, `parseOutputMeta` happy. Miss the sidecar and the UI offers Play,
hls.js loads the playlist, and every segment 404s (#225).

## The rules that must hold

1. **The directory name is `<stem>_p<partial>[_padblack|_padpink]_<codec>[_<tag>]`.**
   The tag goes **last**, after the codec, so the `_p200_<codec>` shape that
   downstream tooling keys off stays intact.
2. **`OutputStem` produces everything up to and including the padding suffix;
   the encode script appends `_<codec>`; the tag is appended after that.** Three
   producers, one layout.
3. **A pending directory is the FURTHEST from complete** — no manifests, no rung
   subdirectories, one JSON file — so it is indistinguishable from an *empty
   finished encode*. The UI must therefore check `pending` **before** `remote`
   and before the no-badge fallback. Get the order wrong and a deferred run
   renders as a finished one with nothing in it.
4. **`gone` is SET, never signalled by deleting the sidecar.** Deleting it
   reclassifies the output as complete — the one wrong answer available.
5. **`expires_at` says when the lifecycle rule will remove the media; it does
   NOT say the media is still there.** An absent or unparseable stamp means
   *not* expired, because the stamp is advisory and the operation itself is the
   authority: it looks, and says `gone` if the prefix is empty.
6. **Callers ask `Fetchable()`** rather than re-deriving availability from
   `Expired()`. The three states — available, expired, deleted — used to render
   as two, and the missing one degraded worst.
7. **The sidecar is removed LAST**, after packaged media is moved in. Its
   absence is what reclassifies the output as finished, so removing it first
   makes the directory read as complete for the minutes packaging takes.
8. **Packaging stages into a sibling `.packaging-<name>/`**, never in place,
   for the same reason.
9. **No S3 call belongs on the `/api/outputs` path.** It already costs ~0.8 s
   over 30 directories; a HEAD per remote output every poll would be far worse
   than the problem it solves.
10. **Media exclusion is stated as an EXCLUSION, not an allow-list.**
    `_MEDIA_SUFFIXES` is `.m4s` and `.byteranges`, so a new metadata file the
    packager starts writing ships by default rather than being silently dropped.
    Measured on a real ladder: metadata is 3.99 MB of 2.64 GB — 0.151%.
11. **Deferring supersedes skip-media-download; they are not combinable.** Both
    keep bytes in S3 and deferring keeps strictly more — the packaged output is
    never made, so there is nothing to leave behind. Honouring both would write
    a `.remote.json` describing media that does not exist.

**Enforced by:** rules 1–2 by the shared naming helpers and `parseOutputMeta`'s
tests; rules 4–6 by `remote.go`/`pending.go` and `remote_test.go`; rule 11 by
the config resolution. Rule 3 is an **ordering rule in the page** with no test
that fails when it is inverted. Rule 9 is **not enforced** — nothing stops a
future handler adding one.

## Blast radius — what does NOT change

The naming layout is shared by `OutputStem`, the encode scripts, `resolveCodec`,
`parseOutputMeta` and the watcher's `alreadyEncoded`. Changing the format means
touching all five. Adding a *tag* does not, because the tag is appended after
the part they key on — that is the whole reason it goes last.

`.archive/` is not an output. Superseded copies live under `OUTPUT_DIR/.archive/`
(#332) and the dot prefix means every existing "skip hidden entries" guard
covers them for free.

## Operations on an output

| operation | route | valid when |
| --- | --- | --- |
| play | `/content/<dir>/…` | complete |
| download / fetch | `POST /api/outputs/{name}/fetch` | `.remote.json` present and `Fetchable()` |
| package | `POST /api/outputs/{name}/package` | `.pending.json` present and `Packageable()` |
| promote | `POST /api/outputs/{name}/promote` | complete; rsyncs to `PROMOTE_DESTS` |
| inspect | `GET /api/outputs/{name}`, `/files`, `/ladder`, `/run`, `/logs`, `/playlists` | any |

`mediaFileServer` sets the Content-Type for `.m3u8`, `.mpd`, `.m4s` and `.ts` —
Go's MIME database does not know them and players will not play without it.

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| download everything (default) | egress, which was 64% of a measured cloud run's cost | the output is complete and playable immediately | shipped |
| `skip_media_download` | media stays in S3 and is subject to its expiry | ~4 MB instead of ~2.6 GB | shipped (#214) |
| `defer_packaging` | the CHUNKS are the only copy; an expiry costs the whole run | the post-encode tail disappears rather than shrinking | shipped (#272) |

Under `.remote.json` an expiry costs the media but the manifests survive. Under
`.pending.json` it costs the run, which must be re-encoded from source. That
asymmetry is why `staging_retention_days` is a decision rather than an inherited
default, and why `cmd_package` spends one `list_objects_v2` up front so "this can
never work" is reported as `EXIT_STAGING_GONE` rather than as an accurate but
useless "no h264 variants found".

## As it stands

One DASH manifest per output, written **in place** at fragment granularity
(#282): one `<SegmentURL @media @mediaRange>` per fragment. There is no
`manifest_fragmented.mpd` and no `.byteranges` sidecar. `@mediaRange` is
inclusive, so length is `last-first+1`, and the first fragment starts at byte
**432** — bytes 0–431 are the segment's own `styp`/`sidx` header and belong to
no fragment.

Old outputs still carry `.byteranges` sidecars and every path that reads or
classifies them must keep tolerating both shapes.

## What is unmeasured

- **How often an expiry actually catches someone.** Both `gone` paths exist and
  are exercised, but no counter distinguishes "expired before I wanted it" from
  "deliberately cleared".
- **Whether the pending state's ordering rule survives UI edits.** Rule 3 has no
  failing test, and it is exactly the kind of thing a refactor reorders.
- **The cost of `/api/outputs` on a much larger library.** Measured at 133 dirs /
  108,606 files (#228); untested an order of magnitude above that.
