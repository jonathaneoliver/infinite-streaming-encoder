"""Deferred packaging: leave the chunks in S3, package on demand (#272).

The encode side is close to subtraction — nothing packages, so nothing runs.
What needs pinning is everything that has to be TRUE for the absence to be
recoverable later, because none of it fails loudly:

  * a marker directory exists at all (without one a deferred run leaves no
    trace on disk and looks like a run that never happened)
  * the packaging parameters were written down (they must outlive the Step
    Functions execution they currently come from)
  * the plan does not declare rows nothing will ever fill
  * the sidecar is removed LAST, because its absence is what reclassifies the
    output as finished

Run directly or via `make check`.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from infinite_streaming_encoder import cli_batch  # noqa: E402
except ModuleNotFoundError as e:  # pragma: no cover — a dependency is absent
    if "boto3" in str(e) or "botocore" in str(e):
        print(f"test_deferred_packaging: skipped (dependency absent: {e})")
        raise SystemExit(0)
    raise

import inspect  # noqa: E402


def test_pending_sidecar_is_a_separate_file_from_the_remote_one() -> None:
    """The two describe different situations and offer different actions.

    .remote.json  packaged; the SEGMENTS are in S3   -> Download
    .pending.json not packaged; the CHUNKS are in S3 -> Package

    Folding the second into a flag on the first is the collapse #225 spent two
    attempts undoing.
    """
    assert cli_batch.PENDING_SIDECAR == ".pending.json"
    assert cli_batch.PENDING_SIDECAR != cli_batch.REMOTE_SIDECAR


def test_sidecar_records_the_packaging_parameters() -> None:
    """They must outlive the execution they currently come from.

    cmd_poll reads segment/partial duration from describe_execution. Step
    Functions history ages out, and this output may be packaged months later.
    Packaging to a different segment duration than the chunks were encoded to
    yields playlists whose boundaries do not land on keyframes — and nothing
    downstream would report it.
    """
    src = inspect.getsource(cli_batch._write_pending_sidecar)
    for key in ('"s3_prefix"', '"codec"', '"segment_duration"',
                '"partial_duration"', '"expires_at"', '"expiry_days"'):
        assert key in src, f"the pending sidecar no longer records {key}"


def test_a_deferred_run_writes_one_marker_dir_per_encoded_codec() -> None:
    """Without it, a deferred run leaves nothing on disk at all.

    The directory is what makes the run appear in the Outputs tab; it travels
    through moveTmpToOutput exactly like a packaged one.
    """
    poll = inspect.getsource(cli_batch.cmd_poll)
    assert "if defer_packaging and encoded_codecs:" in poll
    assert "_write_pending_sidecar(" in poll
    assert "_local_codec_dir(stem, codec, tag)" in poll, (
        "the marker directory is named by hand rather than through the shared "
        "helper — it will drift from what the sync-back produces")
    # ...and it must not then try to sync back an output that does not exist.
    assert poll.index("_write_pending_sidecar(") < poll.index("_download_outputs("), (
        "the deferred branch runs after the sync-back; it must return before it")


def test_a_deferred_run_declares_no_packaging_rows() -> None:
    """A declared stage that never fires sits pending forever.

    Exactly the download:outputs defect #197 fixed, and the same fix: do not
    declare what will not run.
    """
    poll = inspect.getsource(cli_batch.cmd_poll)
    assert '"h264" in encoded_codecs and not defer_packaging' in poll
    assert "sync_back=not defer_packaging" in poll


def test_package_removes_the_sidecar_last() -> None:
    """Its ABSENCE is what reclassifies the output as finished.

    Remove it before the media is in place and, for the minutes packaging takes,
    the directory reads as a complete output — the UI offers Play and every
    segment 404s.
    """
    src = inspect.getsource(cli_batch.cmd_package)
    assert "sidecar.unlink(missing_ok=True)" in src
    assert src.index("shutil.move(str(entry), str(dst))") < \
        src.index("sidecar.unlink(missing_ok=True)"), (
        "the sidecar is removed before the media is moved in")


def test_package_stages_into_a_sibling_not_in_place() -> None:
    """A half-populated output dir is indistinguishable from a finished one."""
    src = inspect.getsource(cli_batch.cmd_package)
    assert 'f".packaging-{out_dir.name}"' in src, (
        "packaging writes straight into the output dir — it would read as "
        "complete while still filling")


def test_package_reports_gone_distinctly_from_failed() -> None:
    """"This failed" and "this can never work" are different answers.

    An expired run would otherwise reach phase_package_all and die with
    "no h264 variants found" — accurate, and useless to someone deciding
    whether clicking again will help.
    """
    src = inspect.getsource(cli_batch.cmd_package)
    assert "EXIT_STAGING_GONE" in src
    assert "list_objects_v2(" in src, (
        "nothing checks whether the chunks are still there, so an expired run "
        "cannot be distinguished from a broken one")
    # The already-known case must not pay for the listing again.
    assert 'meta.get("gone")' in src


def test_package_is_idempotent() -> None:
    """The button must be safe to press twice; a packaged output stays packaged."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "clip_p200_h264"
        d.mkdir()
        rc = cli_batch.cmd_package(type("A", (), {"dir": str(d)})())
        assert rc == 0, "a directory with no sidecar should be a no-op, not an error"


def test_sidecar_json_matches_the_go_contract() -> None:
    """Field names are read by encode.PendingInfo; a rename is silent on both sides."""
    go = (Path(__file__).resolve().parent.parent
          / "internal" / "encode" / "pending.go").read_text()
    src = inspect.getsource(cli_batch._write_pending_sidecar)
    # Every key Python writes must appear as a json tag in the Go struct.
    for key in ("s3_prefix", "codec", "segment_duration", "partial_duration",
                "recorded_at", "expires_at", "expiry_days"):
        assert f'"{key}"' in src, f"python stopped writing {key}"
        assert f'json:"{key}"' in go, f"Go has no field for {key}"
    # ...and the payload must be valid JSON of the shape Go unmarshals into.
    assert "json.dumps(payload" in src


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
    print(f"test_deferred_packaging: {len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
