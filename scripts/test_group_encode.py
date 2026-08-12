"""Tests for the shared-decode group (#317): several rungs off ONE decode.

Run directly (`python3 scripts/test_group_encode.py`) or via `make check`.

The saving is real but invisible in the output — a grouped encode and N separate
ones produce the same files — so every property worth having here is one that
fails silently if it breaks:

  * a branch drifting from the single-rung command ships a ladder whose bottom
    rungs were encoded to different settings than its top ones,
  * a member missing from the upload produces a ladder short a rung, found at
    playback, because package-all discovers variants by LISTING,
  * a member's stage key not being driven leaves the grid blank and the machine
    timeline empty for work that ran.
"""
from __future__ import annotations

import inspect
import re
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import cli_local_dist as D  # noqa: E402
from infinite_streaming_encoder import cli_phase as P  # noqa: E402
from infinite_streaming_encoder.chunking import Chunk  # noqa: E402
from infinite_streaming_encoder.encode_variants import (  # noqa: E402
    EncodeContext, build_ffmpeg_cmd, build_group_ffmpeg_cmd,
)
from infinite_streaming_encoder.ladder import Rung, burnin_for_height  # noqa: E402


def _rung(label: str, w: int, h: int, b: int) -> Rung:
    ftc, flbl, x, ytc, ylbl = burnin_for_height(h)
    return Rung(label=label, res_name=label, width=w, height=h, bitrate=b,
                preset="medium", fontsize_tc=ftc, fontsize_label=flbl,
                burnin_x=x, burnin_y_tc=ytc, burnin_y_label=ylbl)


LOW = [_rung("594p", 1056, 594, 1200), _rung("540p", 960, 540, 900),
       _rung("360p", 640, 360, 500)]
CHUNK = Chunk(index=7, start_s=84.0, duration_s=12.0)


def _ctx(passes: int = 1) -> EncodeContext:
    return EncodeContext(
        mezzanine_path=Path("/tmp/mezz.mp4"), output_dir=Path("/tmp/w"),
        fps=Fraction(30000, 1001), gop_duration_s=1.0,
        content_duration_s=334.4, padding_duration_s=0.0,
        burnin=False, passes={"h264": passes},
    )


def _branches(cmd: list) -> "list[list]":
    """Split a grouped argv into its per-branch option lists (after -map)."""
    out, cur = [], None
    for tok in cmd[cmd.index("-filter_complex") + 2:]:
        if tok == "-map":
            if cur is not None:
                out.append(cur)
            cur = []
        elif cur is not None:
            cur.append(tok)
    if cur is not None:
        out.append(cur[:-3] if cur[-3:] == ["-loglevel", "warning", "-stats"] else cur)
    return [b[1:] for b in out]  # drop the [oN] label itself


def test_the_chunk_is_decoded_once() -> None:
    """The whole point. One input, one filter graph, N branches off one split."""
    cmd = build_group_ffmpeg_cmd(_ctx(), "h264", LOW, chunk=CHUNK)
    assert cmd.count("-i") == 1, cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]split=3"), graph
    assert cmd.count("-map") == 3, cmd


def test_each_branch_is_the_single_rung_command() -> None:
    """DERIVED, not reimplemented. A drift in rate control, keyint, pixel
    format, tagging or fragmentation would not fail a run — it would ship a
    ladder whose bottom rungs used different settings than its top ones, found
    whenever someone compared bitrates."""
    ctx = _ctx()
    grouped = build_group_ffmpeg_cmd(ctx, "h264", LOW, chunk=CHUNK)
    for rung, branch in zip(LOW, _branches(grouped)):
        single = build_ffmpeg_cmd(ctx, "h264", rung, chunk=CHUNK)
        want = single[single.index("-vf") + 2:-3]   # options + output path
        assert branch == want, (rung.label, branch, want)


def test_the_scale_moves_into_the_graph() -> None:
    """Each branch's -vf becomes its scale in the filter graph, so the rungs
    still differ in resolution while sharing the decode."""
    ctx = _ctx()
    grouped = build_group_ffmpeg_cmd(ctx, "h264", LOW, chunk=CHUNK)
    graph = grouped[grouped.index("-filter_complex") + 1]
    for i, rung in enumerate(LOW):
        single = build_ffmpeg_cmd(ctx, "h264", rung, chunk=CHUNK)
        assert f"[s{i}]{single[single.index('-vf') + 1]}[o{i}]" in graph, rung.label
    assert "-vf" not in grouped, "a branch still carries its own -vf"


def test_two_pass_gives_every_branch_its_own_stats() -> None:
    """x264/x265 carry the stats path INSIDE the codec param string, one per
    rung, so branches cannot collide — which is what makes grouped two-pass work
    at all. If they shared a path, pass 2 would read another rung's analysis and
    the bitrates would be wrong with nothing failing."""
    ctx = _ctx(passes=2)
    p1 = build_group_ffmpeg_cmd(ctx, "h264", LOW, pass_num=1, chunk=CHUNK)
    stats = re.findall(r"stats=([^\s:]+)", " ".join(p1))
    assert len(stats) == 3, p1
    assert len(set(stats)) == 3, stats
    for s in stats:
        assert "_2pass.log" in s, s
    # Pass 1 discards every branch's muxed output, exactly as the single-rung
    # path does; N null sinks in one command is fine.
    assert " ".join(p1).count("-f null") == 3, p1


def test_two_pass_lists_the_branches_in_the_same_order() -> None:
    """AV1's stats file is keyed by ffmpeg's OUTPUT STREAM INDEX (pass 1 writes
    `p-0.log`, `p-1.log`), so pass 2 must list the branches exactly as pass 1
    did. Verified in the encoder image; if a reordering ever crept in, pass 2
    would read another rung's stats — or fail with `can't open stats file`."""
    ctx = _ctx(passes=2)
    order = []
    for n in (1, 2):
        cmd = build_group_ffmpeg_cmd(ctx, "h264", LOW, pass_num=n, chunk=CHUNK)
        graph = cmd[cmd.index("-filter_complex") + 1]
        order.append(re.findall(r"\[s(\d)\]scale=(\d+):(\d+)", graph))
    assert order[0] == order[1], order


def test_a_group_of_one_is_not_a_group() -> None:
    """It would take the shared-decode path to decode once for one rung, which
    is what the single-rung path already does — with a simpler command."""
    assert D._plan_groups({"h264": LOW}, 1) == {}
    assert D._plan_groups({"h264": LOW}, 0) == {}


def test_grouping_takes_the_BOTTOM_rungs_lead_first() -> None:
    """The bottom, because that is where the decode dominates: 2.15 cores at
    2160p against 4.02 at 234p. The lead is the largest member — it names the
    activity, carries the group's CPU, and keeps the VMAF audit."""
    rungs = [_rung("2160p", 3840, 2160, 12000), _rung("1080p", 1920, 1080, 4000),
             *LOW]
    got = D._plan_groups({"h264": rungs}, 3)
    assert got == {"h264": ["594p", "540p", "360p"]}, got


def test_a_grouped_activity_drives_every_members_row() -> None:
    """The workflow names a grouped activity for its LEAD only. Without this
    mapping the other rungs' rows never move — and since #293 a row with no
    instance is also dropped from the machine timeline, so the work would be
    invisible twice over."""
    D._GROUP_KEYS.clear()
    D._GROUP_KEYS["enc-h264-594p-c7"] = [
        "encode:h264:594p:chunk7", "encode:h264:540p:chunk7"]
    try:
        assert D._stage_keys_for("enc-h264-594p-c7") == [
            "encode:h264:594p:chunk7", "encode:h264:540p:chunk7"]
        # An ungrouped chunk is unaffected.
        assert D._stage_keys_for("enc-h264-2160p-c7") == ["encode:h264:2160p:chunk7"]
    finally:
        D._GROUP_KEYS.clear()


def test_group_members_parse_and_refuse_to_be_dropped() -> None:
    """A group that quietly dropped a member would produce a ladder missing a
    rung — package-all discovers variants by listing, so nothing downstream
    would notice until playback."""
    lead = LOW[0]
    got = P._group_rungs("594p:1056:594:1200,540p:960:540:900", lead)
    assert [r.label for r in got] == ["594p", "540p"]
    assert [r.bitrate for r in got] == [1200, 900]
    assert got[1].preset == lead.preset, "member did not inherit the lead's preset"
    for bad in ("594p:1056:594", "", "  ,  "):
        try:
            P._group_rungs(bad, lead)
        except ValueError:
            continue
        raise AssertionError(f"malformed group {bad!r} was accepted")


def test_the_workflow_scores_a_group_by_its_members() -> None:
    """A group scored by its LEAD ranks as one cheap rung and sorts LAST, and
    priority drives dispatch order. On the cloud run that tried it, the four big
    rungs were 28/28 done while the grouped ones sat at 0/28.

    Read as text: temporalio is not installed on the host.
    """
    wf = (Path(__file__).resolve().parent / "infinite_streaming_encoder"
          / "temporal_worker.py").read_text()
    assert 'ci.get("group")' in wf, "the workflow no longer reads the plan's group"
    assert "w = sum(" in wf, "a group is not scored as the sum of its members"
    assert '"ENCODE_GROUP": group_arg' in wf, "the group never reaches the worker"


def test_a_grouped_encode_files_no_speed_sample() -> None:
    """One wall time covers several rungs, so a sample filed against the lead
    teaches the model the lead is N times slower than it is — and the planner
    sizes chunks from that curve. Suppressed rather than approximated; #314 was
    the same class of wrongness reached from the other direction."""
    src = inspect.getsource(P.phase_variant)
    assert "if grouped_outs:" in src and "no ENCODER-SPEED sample" in src, (
        "a grouped encode now emits a speed sample; the learned curve it feeds "
        "plans every later run of the source")


def test_the_group_marker_matches_what_go_parses() -> None:
    """A cross-language contract with no error on either side: the orchestrator
    prints it, the Go server pattern-matches it, and a spelling drift would just
    mean the machine timeline keeps drawing six blocks for one job."""
    src = inspect.getsource(D.run_temporal)
    assert "[[ENCODER-GROUP codec=" in src, src[:0]
    go = (Path(__file__).resolve().parent.parent / "internal" / "encode"
          / "job.go").read_text()
    pat = re.search(r"groupMarkerRe = regexp\.MustCompile\(`(.+?)`\)", go)
    assert pat, "groupMarkerRe is gone from the Go side"
    rendered = "[[ENCODER-GROUP codec=h264 lead=594p members=540p|360p]]"
    assert re.match(pat.group(1).replace("\\\\", "\\"), rendered), pat.group(1)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
