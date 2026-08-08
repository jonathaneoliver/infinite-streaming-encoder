"""The mezzanine phase reads a LOCAL source as well as an s3:// URI (#266).

That one capability is what lets the control plane build the mezzanine on the
host and skip both the source upload and the mezzanine Batch job. The important
property is that it is the SAME code either way: the chunk plan is built from
the mezzanine's exact duration and cli_phase refuses when the two drift, so a
host-side reimplementation would be a second thing to keep in step with that.

Run directly or via `make check`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from infinite_streaming_encoder import cli_phase  # noqa: E402
except ModuleNotFoundError as e:  # pragma: no cover — a dependency is absent
    if "boto3" in str(e) or "botocore" in str(e):
        print(f"test_host_mezzanine: skipped (dependency absent: {e})")
        raise SystemExit(0)
    raise

import inspect  # noqa: E402

SRC = inspect.getsource(cli_phase.phase_mezzanine)


def test_local_path_skips_the_download() -> None:
    """A local source must not be downloaded, and must not be copied either.

    Copying would double the disk cost for a file that can be many GB and that
    nothing in this phase writes to.
    """
    assert "local_src.is_file()" in SRC, (
        "cmd_mezzanine no longer detects a local source — the host path (#266) "
        "depends on it")
    # The download call must be on the else branch, not unconditional.
    before, _, after = SRC.partition("if not src_uri.startswith(\"s3://\")")
    assert "_download(" not in before, (
        "_download is called before the local-source check, so a local path "
        "would still be downloaded")
    assert "_download(" in after, "the s3:// branch lost its download"


def test_s3_uri_still_downloads() -> None:
    """The Batch path is unchanged: an s3:// URI still downloads."""
    assert 's3://' in SRC and "_download(src_uri" in SRC, (
        "the s3:// branch no longer downloads — this would break every cloud "
        "mezzanine job")


def test_progress_bar_covers_the_whole_phase_either_way() -> None:
    """The stage bar must run 0->100 on both paths.

    On the s3 path the split is download 0-45, copy 45-55, upload 55-100. With
    no download there is nothing to occupy the first 45, so the copy widens to
    0-55 rather than leaving the bar parked at 0 until the upload starts.
    """
    assert "copy_lo, copy_hi = 0.0, 55.0" in SRC, "local path lost its widened bar"
    assert "copy_lo, copy_hi = 45.0, 55.0" in SRC, "s3 path lost its original bar"
    assert "pct_lo=copy_lo, pct_hi=copy_hi" in SRC, (
        "create_mezzanine no longer uses the computed bar range")
    assert '("mezzanine", 55.0, 100.0)' in SRC, "upload no longer finishes the bar"


def test_time_limit_still_applied_on_both_paths() -> None:
    """#184's duration limit is applied at this one point, so it must survive.

    A limited mezzanine truncates everything downstream. If the host path
    dropped it, a `--time 10` cloud run would silently encode the whole clip.
    """
    assert "_TIME_LIMIT_S" in SRC and "time_limit_s=time_limit_s" in SRC, (
        "the duration limit is no longer applied in cmd_mezzanine")
    # It must be read AFTER the source is resolved, so it applies either way.
    assert SRC.index("local_src.is_file()") < SRC.index("time_limit_s = _TIME_LIMIT_S"), (
        "the time limit is resolved before the source branch — check it still "
        "applies on the local path")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
