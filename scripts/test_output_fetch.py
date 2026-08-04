"""Tests for metadata-only sync-back and the on-demand media fetch (#214).

Run directly (`python3 scripts/test_output_fetch.py`) or via `make check`. No
test framework: the repo has none, and this needs nothing beyond assert.

Why these are pinned rather than left to review: every property here is
INVISIBLE in a passing encode. A run that downloads 2.6 GB and a run that
downloads 4 MB both finish "successfully" — the difference only shows up on an
AWS bill weeks later, or as a Download button that silently re-pays for objects
already on disk.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import cli_batch  # noqa: E402


class FakeS3:
    """Minimal stand-in: paginated list + a download that writes `size` bytes."""

    def __init__(self, objects: dict[str, int], lifecycle=None):
        self.objects = objects
        self.lifecycle = lifecycle
        self.downloaded: list[str] = []

    def get_paginator(self, _op):
        outer = self

        class P:
            def paginate(self, Bucket=None, Prefix=""):  # noqa: N803
                contents = [{"Key": k, "Size": s}
                            for k, s in sorted(outer.objects.items())
                            if k.startswith(Prefix)]
                # Two pages, so a single-page reader would fail this.
                mid = len(contents) // 2
                yield {"Contents": contents[:mid]}
                yield {"Contents": contents[mid:]}
        return P()

    def download_file(self, bucket, key, dst):
        self.downloaded.append(key)
        Path(dst).write_bytes(b"\0" * self.objects[key])

    def get_bucket_lifecycle_configuration(self, Bucket=None):  # noqa: N803
        if self.lifecycle is None:
            raise RuntimeError("AccessDenied")
        return self.lifecycle


def _ladder_objects(base: str) -> dict[str, int]:
    """One codec's packaged output, in the real proportions measured on disk:
    media is 99.85% of the bytes, metadata 0.151%."""
    objs = {}
    for i in range(40):
        objs[f"{base}/output_h264/1080p/segment_{i:05d}.m4s"] = 3_600_000
        objs[f"{base}/output_h264/1080p/segment_{i:05d}.m4s.byteranges"] = 2_600
    objs[f"{base}/output_h264/1080p/init.mp4"] = 900
    objs[f"{base}/output_h264/1080p/playlist.m3u8"] = 148_000
    objs[f"{base}/output_h264/master.m3u8"] = 1_200
    objs[f"{base}/output_h264/manifest.mpd"] = 940_000
    objs[f"{base}/output_h264/encode.json"] = 1_123
    return objs


def _patch(monkey: dict, fake: FakeS3):
    monkey["_s3"] = cli_batch._s3
    monkey["_emit_stage"] = cli_batch._emit_stage
    monkey["_narrate"] = cli_batch._narrate
    cli_batch._s3 = lambda: fake
    cli_batch._emit_stage = lambda *a, **k: True
    cli_batch._narrate = lambda *a, **k: None


def _unpatch(monkey: dict):
    for name, fn in monkey.items():
        setattr(cli_batch, name, fn)


def test_metadata_only_leaves_media_in_s3() -> None:
    base = "jobs/j1-clip"
    fake = FakeS3(_ladder_objects(base))
    monkey: dict = {}
    _patch(monkey, fake)
    try:
        with tempfile.TemporaryDirectory() as td:
            res = cli_batch._download_outputs(
                f"s3://buck/{base}", Path(td), "clip_p200", "xs",
                include_media=False)

            # Nothing with a media suffix was fetched.
            assert not [k for k in fake.downloaded
                        if k.endswith(cli_batch._MEDIA_SUFFIXES)], fake.downloaded
            # Every manifest / init / metadata object WAS.
            assert res.files == 5, res
            assert res.skipped_files == 80, res

            # The saving is the whole point: assert the ratio, not just the flag.
            frac = res.bytes / (res.bytes + res.skipped_bytes)
            assert frac < 0.01, f"metadata should be <1% of bytes, got {frac:.3%}"

            # Layout preserved, because parseOutputMeta infers resolutions from
            # subdirectory existence (internal/api/handlers.go).
            out = Path(td) / "clip_p200_h264_xs"
            assert (out / "1080p" / "playlist.m3u8").exists()
            assert (out / "master.m3u8").exists()
            assert not list((out / "1080p").glob("*.m4s"))
    finally:
        _unpatch(monkey)


def test_sidecar_records_what_was_left_and_when_it_expires() -> None:
    base = "jobs/j1-clip"
    fake = FakeS3(_ladder_objects(base), lifecycle={
        "Rules": [{"Status": "Enabled", "Filter": {"Prefix": "jobs/"},
                   "Expiration": {"Days": 7}}]})
    monkey: dict = {}
    _patch(monkey, fake)
    try:
        with tempfile.TemporaryDirectory() as td:
            cli_batch._download_outputs(f"s3://buck/{base}", Path(td),
                                        "clip_p200", "xs", include_media=False)
            sc = Path(td) / "clip_p200_h264_xs" / cli_batch.REMOTE_SIDECAR
            assert sc.exists(), "no sidecar written"
            meta = json.loads(sc.read_text())
            assert meta["s3_prefix"] == f"s3://buck/{base}/output_h264"
            assert meta["pending_files"] == 80
            # Read from the bucket's own rule, not a hardcoded constant, so the
            # two cannot drift.
            assert meta["expiry_days"] == 7, meta
            assert meta["expires_at"] > meta["recorded_at"]
    finally:
        _unpatch(monkey)


def test_expiry_falls_back_when_lifecycle_is_unreadable() -> None:
    fake = FakeS3({}, lifecycle=None)   # get_bucket_lifecycle_configuration raises
    assert cli_batch._staging_expiry_days(fake, "buck") == \
        cli_batch._STAGING_EXPIRY_DAYS


def test_full_download_is_unchanged_by_default() -> None:
    base = "jobs/j1-clip"
    fake = FakeS3(_ladder_objects(base))
    monkey: dict = {}
    _patch(monkey, fake)
    try:
        with tempfile.TemporaryDirectory() as td:
            res = cli_batch._download_outputs(f"s3://buck/{base}", Path(td),
                                              "clip_p200", "xs")
            assert res.files == 85, res
            assert res.skipped_files == 0, res
            # No sidecar: nothing is remote, so the UI must not offer Download.
            assert not (Path(td) / "clip_p200_h264_xs"
                        / cli_batch.REMOTE_SIDECAR).exists()
    finally:
        _unpatch(monkey)


def test_fetch_without_a_sidecar_is_a_no_op() -> None:
    # The Download button's idempotency: a second click, or a click on an output
    # that was never remote, must not re-download anything.
    with tempfile.TemporaryDirectory() as td:
        rc = cli_batch.cmd_fetch(types.SimpleNamespace(dir=td, dry_run=False))
        assert rc == 0, rc


def test_fetch_resumes_and_only_clears_the_sidecar_when_complete() -> None:
    base = "jobs/j1-clip"
    objs = {k: v for k, v in _ladder_objects(base).items()
            if k.startswith(f"{base}/output_h264")}
    fake = FakeS3(objs)
    monkey: dict = {}
    _patch(monkey, fake)
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "clip_p200_h264_xs"
            (out / "1080p").mkdir(parents=True)
            (out / cli_batch.REMOTE_SIDECAR).write_text(json.dumps({
                "s3_prefix": f"s3://buck/{base}/output_h264",
                "pending_files": 80, "pending_bytes": 144_104_000,
                "expires_at": "2099-01-01T00:00:00Z"}))

            # Simulate an interrupted fetch: one segment already landed, at the
            # right size. It must not be pulled again.
            already = out / "1080p" / "segment_00000.m4s"
            already.write_bytes(b"\0" * 3_600_000)
            # ...and one that landed TRUNCATED must be re-pulled.
            partial = out / "1080p" / "segment_00001.m4s"
            partial.write_bytes(b"\0" * 17)

            rc = cli_batch.cmd_fetch(types.SimpleNamespace(dir=str(out),
                                                           dry_run=False))
            assert rc == 0, rc
            names = set(fake.downloaded)
            assert f"{base}/output_h264/1080p/segment_00000.m4s" not in names, \
                "re-downloaded an object already present at the right size"
            assert f"{base}/output_h264/1080p/segment_00001.m4s" in names, \
                "did not re-fetch a truncated object"
            # Complete now, so it stops being remote.
            assert not (out / cli_batch.REMOTE_SIDECAR).exists()
    finally:
        _unpatch(monkey)


def test_fetch_dry_run_reports_without_downloading() -> None:
    # The UI shows size before the click, because the click costs ~$0.09/GB.
    base = "jobs/j1-clip"
    objs = {k: v for k, v in _ladder_objects(base).items()
            if k.startswith(f"{base}/output_h264")}
    fake = FakeS3(objs)
    monkey: dict = {}
    _patch(monkey, fake)
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "d"
            out.mkdir()
            (out / cli_batch.REMOTE_SIDECAR).write_text(json.dumps({
                "s3_prefix": f"s3://buck/{base}/output_h264",
                "expires_at": "2099-01-01T00:00:00Z"}))
            rc = cli_batch.cmd_fetch(types.SimpleNamespace(dir=str(out),
                                                           dry_run=True))
            assert rc == 0
            assert fake.downloaded == [], "dry run downloaded something"
            # Sidecar survives a dry run — nothing was fetched.
            assert (out / cli_batch.REMOTE_SIDECAR).exists()
    finally:
        _unpatch(monkey)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"      {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
