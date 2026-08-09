"""Packaging runs on the control plane as well as in a Batch job (#197).

The tail-side twin of test_host_mezzanine.py, and the same shape of claim: the
capability is that ONE implementation serves both deployments, differing only in
where `--s3-out` points. What is worth pinning is the seams — the places where
the two halves agree by convention and nothing would raise if they stopped.

Run directly or via `make check`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from infinite_streaming_encoder import cli_batch, cli_phase  # noqa: E402
except ModuleNotFoundError as e:  # pragma: no cover — a dependency is absent
    if "boto3" in str(e) or "botocore" in str(e):
        print(f"test_host_package: skipped (dependency absent: {e})")
        raise SystemExit(0)
    raise

import inspect  # noqa: E402


def test_deliver_dir_moves_locally_and_uploads_to_s3() -> None:
    """`--s3-out` takes a local directory as well as an s3:// URI.

    The move is the point: uploading the packaged ladder so the orchestrator can
    download it straight back is the second of the two serial transfers #197
    removes.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pkg = root / "work" / "output_h264"
        (pkg / "720p").mkdir(parents=True)
        (pkg / "720p" / "seg1.m4s").write_bytes(b"media")
        (pkg / "manifest.mpd").write_text("<MPD/>")

        dest = root / "out"
        got = cli_phase._deliver_dir(pkg, str(dest), "output_h264")

        assert Path(got) == dest / "output_h264", got
        assert (dest / "output_h264" / "720p" / "seg1.m4s").read_bytes() == b"media"
        assert (dest / "output_h264" / "manifest.mpd").is_file()
        # Moved, not copied — a copy would mean a second full-size pass over the
        # slowest disk in the system for every run.
        assert not pkg.exists(), "the packaged dir was copied rather than moved"


def test_deliver_dir_replaces_rather_than_merges() -> None:
    """A retry must not leave the previous attempt's segments beside the new set.

    Both are named by the packager, so nothing downstream could tell them apart
    and a playlist would reference media from two different encodes.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dest = root / "out"
        stale = dest / "output_h264"
        stale.mkdir(parents=True)
        (stale / "ghost.m4s").write_bytes(b"old")

        fresh = root / "work" / "output_h264"
        fresh.mkdir(parents=True)
        (fresh / "seg1.m4s").write_bytes(b"new")

        cli_phase._deliver_dir(fresh, str(dest), "output_h264")

        assert not (dest / "output_h264" / "ghost.m4s").exists(), (
            "a stale segment from a previous attempt survived")
        assert (dest / "output_h264" / "seg1.m4s").read_bytes() == b"new"


def test_s3_destination_still_uploads() -> None:
    """The Batch path is unchanged — an s3:// dest must not be treated as a dir."""
    src = inspect.getsource(cli_phase._deliver_dir)
    assert 'dest.startswith("s3://")' in src
    assert "_upload_dir(local, target)" in src, (
        "the s3:// branch no longer uploads; every Batch-packaged run would "
        "silently produce no output in S3")


def test_local_codec_dir_puts_the_tag_after_the_codec() -> None:
    """`<stem>_<codec>[_<tag>]`, so the `_p200_<codec>` shape stays intact.

    OutputStem, resolveCodec and the watcher all key off that shape. Two callers
    need this name now — the sync-back and host packaging — and if they spelled
    it differently the same clip would land in two directories.
    """
    assert cli_batch._local_codec_dir("clip_p200", "h264", "") == "clip_p200_h264"
    assert cli_batch._local_codec_dir("clip_p200", "hevc", "xs") == "clip_p200_hevc_xs"
    # The sync-back must go through the same helper, not its own f-string.
    dl = inspect.getsource(cli_batch._download_outputs)
    assert "_local_codec_dir(" in dl, (
        "_download_outputs builds the directory name itself again — the two "
        "spellings will drift")


def test_fetch_regex_matches_what_cli_phase_actually_prints() -> None:
    """The cost accounting hangs on one printed line matching one regex.

    With packaging on the host, the egress a run causes is the CHUNKS it pulls,
    not the packaged output the sync-back no longer fetches. cli_batch recovers
    that number by scanning cli_phase's own measurement as it relays it. Nothing
    raises if the wording drifts: the regex simply stops matching, the run
    reports zero egress, and a host-packaged run looks nearly free next to a
    Batch-packaged one. So the two are pinned to each other here.
    """
    # The exact line phase_package_all emits, reproduced from its f-string.
    line = ("[phase package-all] fetched 336 objects, 2261 MB in 24s "
            "(94.2 MB/s, 32 threads)")
    m = cli_batch._PKG_FETCH_RE.search(line)
    assert m, "the regex no longer matches the line cli_phase prints"
    assert int(m.group(1)) == 336
    assert int(float(m.group(2)) * 1e6) == 2261 * 10**6

    # ...and that the f-string still produces that shape.
    src = inspect.getsource(cli_phase.phase_package_all)
    assert '"[phase package-all] fetched {total_objs} objects, "' in src or \
           "fetched {total_objs} objects" in src, (
        "phase_package_all's fetch line changed shape; cli_batch._PKG_FETCH_RE "
        "must change with it or egress silently reads zero")


def test_cost_summary_counts_the_chunk_staging_when_packaging_is_local() -> None:
    """staged_bytes drives storage AND the fitted Tier1 estimate.

    With packaging on the host, _download_outputs finds no output_* objects and
    returns all zeros. Feeding only that to _emit_cost_summary prices a run's
    storage and its PUTs at zero while its chunks sit in S3 costing both — the
    same "a trade looks like a saving" failure the egress term above exists to
    stop, one argument over.

    Host packaging removes the PACKAGED OUTPUT's staging (~1486 objects PUT and
    then GET back on a full run, roughly doubled by the per-segment .byteranges
    sidecars). It does not remove the chunks.
    """
    poll = inspect.getsource(cli_batch.cmd_poll)
    assert "staged_bytes=res.bytes + res.skipped_bytes" in poll
    assert "+ pkg.bytes)" in poll, (
        "staged_bytes omits the chunk staging — a host-packaged run reports "
        "zero S3 storage and zero Tier1 cost")
    # ...and egress must count it too, for the same reason.
    assert "egress_bytes=res.bytes + pkg.bytes" in poll
    assert "egress_files=res.files + pkg.files" in poll


def test_host_packaging_gives_each_codec_its_own_scratch() -> None:
    """cli_phase rmtree's ENCODER_WORK_DIR on entry.

    A shared scratch would delete a sibling codec's inputs mid-run, and only
    when more than one codec was selected.
    """
    src = inspect.getsource(cli_batch._package_on_host)
    assert 'f"pkg-work-{codec}"' in src, (
        "the per-codec scratch dir is gone — two codecs would wipe each other")
    assert 'env["ENCODER_WORK_DIR"] = str(work)' in src


def test_host_packaging_does_not_publish_to_sqs() -> None:
    """On the host, stdout IS the channel to the server.

    Leaving ENCODER_TELEMETRY_EXEC set would publish a second copy of every
    marker into the execution's telemetry queue, which the orchestrator is the
    consumer of — it would drain its own output back.
    """
    src = inspect.getsource(cli_batch._package_on_host)
    assert 'env.pop("ENCODER_TELEMETRY_EXEC", None)' in src


def test_host_packaging_takes_timing_from_the_execution_input() -> None:
    """Segment/partial duration must come from the run, not this process's env.

    The chunks were encoded to a specific segment duration; packaging to a
    different one produces playlists whose boundaries do not land on keyframes.
    The orchestrator's own environment is not that source.
    """
    src = inspect.getsource(cli_batch._package_on_host)
    assert 'env["SEGMENT_DURATION"] = segment_duration' in src
    assert 'env["PARTIAL_DURATION"] = partial_duration' in src

    poll = inspect.getsource(cli_batch.cmd_poll)
    assert 'inp.get("segment_duration")' in poll, (
        "cmd_poll no longer reads the timing off the execution input")


def test_poll_plans_the_codecs_it_encodes_not_the_ones_batch_packages() -> None:
    """do_h264 means 'the STATE MACHINE packages h264' once #197 is on.

    Feeding it straight to _emit_plan would show a run encoding nothing.
    """
    poll = inspect.getsource(cli_batch.cmd_poll)
    assert 'inp.get("host_package")' in poll
    # #272 replaced the do_h264-OR-host_package union with an explicit
    # encoded_codecs list, because deferring empties BOTH sets and the union
    # would then be empty for a run that encoded perfectly well.
    assert '"h264" in encoded_codecs' in poll, (
        "the run plan is built from packaging flags rather than from the "
        "encoded-codec set, so a host-packaged or deferred run reports no codecs")
    assert 'inp.get("encoded_codecs")' in poll


def _run() -> int:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"test_host_package: {len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
