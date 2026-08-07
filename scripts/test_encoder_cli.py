"""Tests for the command-line encode client (encoder_cli.py).

Run directly (`python3 scripts/test_encoder_cli.py`) or via `make check`.

The load-bearing test here is the FIRST one: it reflects over JobConfig in
internal/encode/job.go and fails when a field exists there but has no flag. 23
fields hand-copied into an argparse parser will drift, and the drift is SILENT —
the flag simply does not exist, while the browser keeps working, so nothing
notices until someone tries to script the option that was added last month.

That failure mode has a name in the sibling infinite-stream project: "we keep
forgetting to plumb new fields everywhere", recorded there after three
half-day debugging loops in a single session. Their answer was a checklist,
which is what kept failing. A test is the same idea with teeth.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import encoder_cli  # noqa: E402

JOB_GO = (Path(__file__).resolve().parent.parent
          / "internal" / "encode" / "job.go")


def _jobconfig_json_tags() -> list[str]:
    """Every `json:"name"` tag inside `type JobConfig struct { ... }`.

    Read from the Go source rather than duplicated here, for the same reason
    TestRatesMatchThePythonDefinitions reads pricing.py from Go: two languages
    cannot share a definition, so the only defence against drift is one side
    reading the other."""
    src = JOB_GO.read_text()
    start = src.index("type JobConfig struct {")
    # First line that closes the struct at column 0.
    end = src.index("\n}\n", start)
    body = src[start:end]
    return [m.group(1) for m in re.finditer(r'json:"([a-z_]+)', body)]


def test_every_jobconfig_field_is_reachable_from_the_command_line() -> None:
    tags = _jobconfig_json_tags()
    assert len(tags) > 15, f"only found {len(tags)} fields — the parse broke"

    mapped = (set(encoder_cli._STR_FIELDS.values())
              | set(encoder_cli._BOOL_FIELDS.values())
              | set(encoder_cli._TRISTATE_FIELDS.values())
              | set(encoder_cli._NOT_USER_FIELDS))
    missing = [t for t in tags if t not in mapped]
    assert not missing, (
        f"JobConfig fields with no CLI flag: {missing}. Add each to _STR_FIELDS "
        "/ _BOOL_FIELDS / _TRISTATE_FIELDS with a matching argparse argument — "
        "or to _NOT_USER_FIELDS if it is filled by the server rather than typed.")

    # And the reverse: a flag mapped to a field Go no longer has would be
    # accepted here and silently ignored by the server.
    stale = [f for f in mapped if f not in tags]
    assert not stale, f"CLI maps fields JobConfig does not have: {stale}"


def test_every_mapped_field_has_a_real_argparse_flag() -> None:
    # The maps could name a field and still have no flag behind it, which would
    # satisfy the test above while leaving the option unreachable.
    p = encoder_cli.build_parser()
    dests = {a.dest for a in p._actions}
    for table in (encoder_cli._STR_FIELDS, encoder_cli._BOOL_FIELDS,
                  encoder_cli._TRISTATE_FIELDS):
        for dest in table:
            assert dest in dests, f"{dest} is mapped but has no argparse argument"


def test_unset_options_are_omitted_so_server_defaults_apply() -> None:
    """The whole reason the tri-states are pointers in Go: nil means "use the
    server default". Sending false instead would silently disable burn-in on
    every scripted encode, and turn SKIP_OUTPUT_MEDIA=1 off."""
    args = encoder_cli.build_parser().parse_args(["clip.mp4"])
    body = encoder_cli.build_body(args)
    assert body == {"files": ["clip.mp4"]}, body
    for k in ("burnin", "skip_media_download", "use_spot", "codec", "target"):
        assert k not in body, f"{k} sent when the user did not ask for it"


def test_tristates_send_both_true_and_false_when_asked() -> None:
    p = encoder_cli.build_parser()
    on = encoder_cli.build_body(p.parse_args(
        ["c.mp4", "--burnin", "--skip-media-download", "--spot"]))
    assert on["burnin"] is True and on["skip_media_download"] is True \
        and on["use_spot"] is True, on
    # An explicit NO must reach the server — that is the difference between
    # "leave it alone" and "turn it off", and store_true could not express it.
    off = encoder_cli.build_body(p.parse_args(
        ["c.mp4", "--no-burnin", "--no-skip-media-download", "--no-spot"]))
    assert off["burnin"] is False and off["skip_media_download"] is False \
        and off["use_spot"] is False, off


def test_plain_booleans_are_omitted_when_false() -> None:
    # false IS the default for these, so sending it is noise that would also
    # make every dry-run body harder to read.
    body = encoder_cli.build_body(
        encoder_cli.build_parser().parse_args(["c.mp4", "--force-reencode"]))
    assert body["force_reencode"] is True
    for k in ("keep_mezzanine", "promote", "measure_vmaf", "vmaf_prescale",
              "hevc_single_pass"):
        assert k not in body, f"{k}=false sent needlessly"


def test_the_body_matches_what_the_browser_posts() -> None:
    # Pinned against the request static/index.html builds for the same options,
    # because "the CLI works" and "the CLI submits the same job" are different
    # claims and only the second one matters.
    args = encoder_cli.build_parser().parse_args([
        "smoke.mp4", "--target", "local", "--codec", "h264",
        "--max-res", "720p", "--chunk-duration", "12"])
    assert encoder_cli.build_body(args) == {
        "files": ["smoke.mp4"], "target": "local", "codec": "h264",
        "max_res": "720p", "chunk_duration": "12",
    }


def test_enums_are_rejected_locally() -> None:
    # Shape only. A typo should fail in the shell, not 400 after a round trip.
    import contextlib
    import io
    for bad in (["c.mp4", "--codec", "h265"],          # it is "hevc"
                ["c.mp4", "--target", "cloud-batch"],  # server alias, not a flag value
                ["c.mp4", "--cpu-arch", "arm"],
                ["c.mp4", "--hls-format", "dash"],
                ["c.mp4", "--max-res", "1080"],        # missing the p
                ["c.mp4", "--chunk-duration", "12s"]):
        try:
            # argparse prints the usage block to stderr on rejection; swallow it
            # so a PASSING run stays quiet and a real failure stays visible.
            with contextlib.redirect_stderr(io.StringIO()):
                encoder_cli.build_parser().parse_args(bad)
        except SystemExit:
            continue
        raise AssertionError(f"accepted {bad}")


def test_unusual_but_valid_resolutions_are_accepted() -> None:
    # The Apple-uniq ladders carry 954p / 1800p / 594p, so a fixed choices list
    # would reject real tiers. Which tiers exist depends on the ladder, and the
    # server owns that — this only checks the shape.
    for tier in ("594p", "954p", "1800p", "2160p"):
        args = encoder_cli.build_parser().parse_args(["c.mp4", "--max-res", tier])
        assert encoder_cli.build_body(args)["max_res"] == tier


def test_dry_run_prints_the_body_and_touches_no_server(capsys=None) -> None:
    import io
    import contextlib
    buf = io.StringIO()
    # No server is running in a test; a dry run that tried to reach one would
    # hang or fail here rather than printing.
    with contextlib.redirect_stdout(buf):
        rc = encoder_cli.main(["clip.mp4", "--target", "cloud", "--dry-run",
                               "--server", "http://127.0.0.1:1"])
    assert rc == 0, rc
    assert json.loads(buf.getvalue()) == {"files": ["clip.mp4"], "target": "cloud"}


def test_unreachable_server_says_so_rather_than_tracebacking() -> None:
    import io
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = encoder_cli.main(["clip.mp4", "--server", "http://127.0.0.1:1"])
    assert rc == 1, rc
    assert "could not reach" in err.getvalue(), err.getvalue()


def test_a_vanished_job_counts_as_failure() -> None:
    """Silence is not success. A job that disappears from /api/jobs must fail
    the wait, or `make smoke` reports PASS for a run that never happened."""
    import types
    calls = {"n": 0}

    def fake_call(url, body=None):
        calls["n"] += 1
        return [], None          # job never appears

    import contextlib
    import io
    orig = encoder_cli._call
    encoder_cli._call = fake_call
    try:
        args = types.SimpleNamespace(timeout=1, poll_interval=0, json=False,
                                     quiet=True)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc, _ = encoder_cli._wait("http://x", ["job-1"], args)
        assert rc == 1, "a missing job was treated as done"
    finally:
        encoder_cli._call = orig


def test_a_local_path_uploads_and_a_bare_name_does_not() -> None:
    """The positional argument means two things and the filesystem decides
    which. `encoder_cli ./clip.mp4` from a laptop and `encoder_cli clip.mp4` on
    the master box are both natural, and getting it backwards either uploads a
    file that is already there or submits a name the server has never seen."""
    import tempfile
    import types
    uploaded = []

    def fake_upload(server, path, quiet):
        uploaded.append(path)
        return "uploaded-" + Path(path).name, None

    orig = encoder_cli._upload
    encoder_cli._upload = fake_upload
    try:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "clip.mp4"
            local.write_bytes(b"\0" * 10)
            args = types.SimpleNamespace(no_upload=False, quiet=True)

            names, err = encoder_cli._resolve_inputs(
                "http://x", [str(local), "already-there.mp4"], args)
            assert err is None, err
            assert names == ["uploaded-clip.mp4", "already-there.mp4"], names
            assert uploaded == [str(local)], uploaded

            # --no-upload forces the server-side reading even for a real file.
            uploaded.clear()
            args.no_upload = True
            names, err = encoder_cli._resolve_inputs("http://x", [str(local)], args)
            assert err is None and uploaded == [], (names, uploaded)
    finally:
        encoder_cli._upload = orig


def test_a_path_that_does_not_exist_locally_is_an_error_not_a_source_name() -> None:
    # Otherwise a typo'd path is sent as a source name and comes back as a
    # confusing 400 about SOURCE_DIR.
    import types
    args = types.SimpleNamespace(no_upload=False, quiet=True)
    _, err = encoder_cli._resolve_inputs("http://x", ["./nope/clip.mp4"], args)
    assert err and "no such local file" in err, err


def test_dry_run_does_not_upload() -> None:
    # A dry run must have no side effects, and pushing a multi-GB source is the
    # largest side effect this tool has.
    import contextlib
    import io
    import tempfile
    called = []
    orig = encoder_cli._upload
    encoder_cli._upload = lambda *a, **k: (called.append(a), ("x", None))[1]
    try:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "clip.mp4"
            local.write_bytes(b"\0")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = encoder_cli.main([str(local), "--dry-run",
                                       "--server", "http://127.0.0.1:1"])
            assert rc == 0 and called == [], called
    finally:
        encoder_cli._upload = orig


def test_progress_line_prefers_stages_over_the_stale_worker_line() -> None:
    """Observed in a real smoke run: on local-dist `progress` freezes at the
    orchestrator's "[dist] starting workflow …" while the stage list keeps
    moving, so showing both repeated the same 60 stale characters on every
    line — text that reads as current and is not."""
    d = encoder_cli._describe({
        "status": "running", "overall_progress": 42.4,
        "progress": "[dist] starting workflow encode-jobs-123 on host:7233",
        "stages": [{"key": "encode:h264:1080p", "label": "h264 1080p",
                    "status": "running"},
                   {"key": "mezz", "label": "mezzanine", "status": "done"}]})
    assert "42%" in d and "h264 1080p" in d, d
    assert "starting workflow" not in d, f"stale worker line still shown: {d}"
    # Done stages are not "running" and must not be listed as though they were.
    assert "mezzanine" not in d, d

    # With no stage running, the worker line IS the only thing there is — the
    # cloud upload phase and a plain local encode both look like this.
    assert encoder_cli._describe(
        {"status": "running", "progress": "frame= 1200"}) == "frame= 1200"
    assert encoder_cli._describe(
        {"status": "running", "overall_progress": 5, "progress": "uploading",
         "stages": [{"key": "mezz", "status": "done"}]}) == "5% · uploading"

    # Nothing to say falls back to the status, and an empty line must never
    # print as " · · ".
    assert encoder_cli._describe({"status": "pending"}) == "pending"


def test_download_skips_files_already_present_at_the_right_size() -> None:
    """rsync's useful property. An interrupted download must resume rather than
    re-pay, and the size comes from the listing so the skip costs no request."""
    import tempfile
    calls = []

    def fake_call(url, body=None):
        return {"files": [{"path": "master.m3u8", "size": 3},
                          {"path": "1080p/seg_0.m4s", "size": 5}]}, None

    fetched = []

    class FakeResp:
        def __init__(self, data):
            self._d = data

        def read(self, n=-1):
            d, self._d = self._d, b""
            return d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(url, timeout=0):
        fetched.append(url)
        return FakeResp(b"\0" * (3 if url.endswith(".m3u8") else 5))

    orig_call, orig_open = encoder_cli._call, encoder_cli.urllib.request.urlopen
    encoder_cli._call = fake_call
    encoder_cli.urllib.request.urlopen = fake_open
    try:
        with tempfile.TemporaryDirectory() as td:
            # Pre-place one at the RIGHT size and one SHORT.
            good = Path(td) / "clip_h264" / "master.m3u8"
            good.parent.mkdir(parents=True)
            good.write_bytes(b"\0" * 3)
            short = Path(td) / "clip_h264" / "1080p" / "seg_0.m4s"
            short.parent.mkdir(parents=True)
            short.write_bytes(b"\0")            # truncated -> must re-fetch

            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = encoder_cli._download("http://x", ["clip_h264"], td, True)
            assert rc == 0, rc
            assert len(fetched) == 1 and fetched[0].endswith("seg_0.m4s"), fetched
            assert short.stat().st_size == 5, "truncated file not repaired"
            # Nested layout preserved — parseOutputMeta infers resolutions from
            # subdirectory existence, so a flattened download is unusable.
            assert (Path(td) / "clip_h264" / "1080p" / "seg_0.m4s").exists()
    finally:
        encoder_cli._call, encoder_cli.urllib.request.urlopen = orig_call, orig_open
        _ = calls


def test_download_implies_wait_and_fetches_what_the_job_reported() -> None:
    """There is nothing to fetch until the job finishes, so --download without
    --wait can only mean the user expected one. And the dirs come from the
    JOB's `outputs` — a client cannot derive them, because resolveCodec may
    narrow the codec list and the ladder may add an output_tag."""
    import contextlib
    import io
    seen = {}

    def fake_call(url, body=None):
        if url.endswith("/api/encode"):
            return [{"id": "job-1"}], None
        if url.endswith("/api/jobs"):
            return [{"id": "job-1", "status": "done",
                     "outputs": ["clip_p200_h264", "clip_p200_hevc"]}], None
        raise AssertionError(url)

    def fake_download(server, names, dest, quiet):
        seen["names"], seen["dest"] = names, dest
        return 0

    orig_call, orig_dl = encoder_cli._call, encoder_cli._download
    encoder_cli._call, encoder_cli._download = fake_call, fake_download
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = encoder_cli.main(["clip.mp4", "--no-upload",
                                   "--download", "/tmp/out", "--quiet"])
        assert rc == 0, rc
        assert seen.get("names") == ["clip_p200_h264", "clip_p200_hevc"], seen
        assert seen.get("dest") == "/tmp/out", seen
    finally:
        encoder_cli._call, encoder_cli._download = orig_call, orig_dl


def test_nothing_produced_is_not_a_download_failure() -> None:
    # resolveCodec skipped every codec because the output already existed. The
    # job is legitimately done; there is simply nothing new to fetch, and
    # erroring here would break any script that re-runs idempotently.
    import contextlib
    import io

    def fake_call(url, body=None):
        if url.endswith("/api/encode"):
            return [{"id": "job-1"}], None
        return [{"id": "job-1", "status": "done", "outputs": []}], None

    called = []
    orig_call, orig_dl = encoder_cli._call, encoder_cli._download
    encoder_cli._call = fake_call
    encoder_cli._download = lambda *a: called.append(a) or 0
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = encoder_cli.main(["clip.mp4", "--no-upload",
                                   "--download", "/tmp/out", "--quiet"])
        assert rc == 0, rc
        assert called == [], "downloaded when nothing was produced"
        assert "already encoded" in buf.getvalue(), buf.getvalue()
    finally:
        encoder_cli._call, encoder_cli._download = orig_call, orig_dl


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"      {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
