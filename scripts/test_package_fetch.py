"""Tests for the package-all parallel chunk fetch (#209).

Run directly (`python3 scripts/test_package_fetch.py`) or via `make check`. No
test framework: the repo has none, and this needs nothing beyond assert.

The chunk fetch in `phase_package_all` was a nested serial loop: 12 rungs x 28
chunks, and `_download_if_complete` pulls a .done sidecar too, so 672 sequential
round-trips on small objects. It measured ~23 MB/s in-region against an instance
that sustains an order of magnitude more with concurrency.

Both properties pinned here are INVISIBLE in a passing encode — a serial fetch
and a parallel one produce byte-identical output, just minutes apart — which is
why they are tested rather than left to review.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import cli_phase  # noqa: E402

try:  # botocore ships with boto3, which is in requirements.txt but not always
    import botocore.config  # noqa: F401
    HAVE_BOTOCORE = True
except ImportError:  # a bare checkout without `pip install -r requirements.txt`
    HAVE_BOTOCORE = False


def test_pooled_client_sizes_its_connection_pool_to_the_workers() -> None:
    # boto3 defaults to 10 connections. Threads above that queue on the pool
    # rather than on the network, so raising the worker count alone does nothing
    # — the two have to move together.
    if not HAVE_BOTOCORE:
        print("      (skipped: botocore not installed)")
        return
    captured: dict = {}

    def fake_client(_svc, **kw):
        captured.update(kw)
        return object()

    real_boto = cli_phase.boto3
    endpoint = os.environ.pop("S3_ENDPOINT_URL", None)
    cli_phase.boto3 = types.SimpleNamespace(client=fake_client)
    try:
        cli_phase._pooled_s3(32)
        got = captured["config"].max_pool_connections
        assert got == 32, f"pool = {got}, want 32"

        cli_phase._pooled_s3(4)
        got = captured["config"].max_pool_connections
        assert got == 10, f"pool = {got}; must not shrink below boto3's default"
    finally:
        cli_phase.boto3 = real_boto
        if endpoint is not None:
            os.environ["S3_ENDPOINT_URL"] = endpoint


def test_download_helpers_use_the_client_they_are_given() -> None:
    # The point of _pooled_s3 is defeated if a helper quietly falls back to
    # _s3(), which builds a NEW client per call — 672 of them in the old loop.
    calls = {"n": 0, "clients": set()}

    class FakeS3:
        def head_object(self, **_kw):
            return {"ContentLength": 3}

        def download_file(self, _bucket, key, dest, Callback=None):
            calls["n"] += 1
            calls["clients"].add(id(self))
            Path(dest).write_text("3" if key.endswith(".done") else "abc")

    def boom():
        raise AssertionError("_s3() called — the pooled client was bypassed")

    real = cli_phase._s3
    cli_phase._s3 = boom
    try:
        fake = FakeS3()
        with tempfile.TemporaryDirectory() as d:
            ok = cli_phase._download_if_complete(
                "s3://b/k/obj.mp4", Path(d) / "obj.mp4",
                client=fake, progress=False)
        assert ok is True, "size matched the .done sidecar, so this must succeed"
        assert calls["n"] == 2, f"want object + .done, got {calls['n']}"
        assert len(calls["clients"]) == 1, "must reuse the one client passed in"
    finally:
        cli_phase._s3 = real


def test_download_if_complete_still_rejects_a_size_mismatch() -> None:
    # The .done-matches-size invariant is what keeps a half-uploaded chunk out
    # of the packager. Threading a client through must not weaken it.
    class FakeS3:
        def head_object(self, **_kw):
            return {"ContentLength": 3}

        def download_file(self, _bucket, key, dest, Callback=None):
            # .done claims 99 bytes; the object is 3.
            Path(dest).write_text("99" if key.endswith(".done") else "abc")

    with tempfile.TemporaryDirectory() as d:
        ok = cli_phase._download_if_complete(
            "s3://b/k/obj.mp4", Path(d) / "obj.mp4",
            client=FakeS3(), progress=False)
    assert ok is False, "a .done that disagrees with the file must fail the fetch"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
