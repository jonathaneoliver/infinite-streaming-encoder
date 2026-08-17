"""Tests for local-dist stage state: queued vs running (#stage-state).

Run directly (`python3 scripts/test_dist_stage_state.py`) or via `make check`.
No test framework: the repo has none, and this needs nothing beyond assert.

The distinction is invisible in any output — the encode is identical either way,
the grid just lies about what is happening. It said every chunk was "running"
the instant the workflow fanned out, so on a 3-machine farm 30 chunks claimed to
be encoding while nothing was executing them. That also made the machine
timeline look broken: a box legitimately had no lane, because it genuinely had
not started anything, while the grid beside it showed its rungs running.

Temporal gives both facts cleanly and they must not be conflated:

    ACTIVITY_TASK_SCHEDULED       -> queued  (in the task queue, no worker)
    pending activity == STARTED   -> running (a worker is executing it)

Activity ids are `enc-<codec>-<label>-c<N>` — note the `c`. _stage_keys_for
returns [] for any other shape, so a fixture that gets it wrong exercises
nothing at all, silently. These use the real format.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import cli_local_dist as D  # noqa: E402


class FakeAttr:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeEvent:
    def __init__(self, event_type, event_id, sched=None, started=None):
        self.event_type = event_type
        self.event_id = event_id
        self.activity_task_scheduled_event_attributes = sched
        self.activity_task_started_event_attributes = started
        self.activity_task_completed_event_attributes = None


class ET:
    EVENT_TYPE_ACTIVITY_TASK_SCHEDULED = 1
    EVENT_TYPE_ACTIVITY_TASK_STARTED = 2
    EVENT_TYPE_ACTIVITY_TASK_COMPLETED = 3


def _capture(fn):
    """Run fn, returning the [[ENCODER-STAGE ...]] markers it emitted."""
    seen = []
    real = D.emit_stage
    D.emit_stage = lambda key, status, pct=0.0: seen.append((key, status))
    try:
        fn()
    finally:
        D.emit_stage = real
    return seen


def test_scheduled_is_queued_not_running() -> None:
    import asyncio

    class Handle:
        async def fetch_history(self):
            return FakeAttr(events=[
                FakeEvent(ET.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED, 1,
                          sched=FakeAttr(activity_id="enc-h264-1080p-c0")),
                FakeEvent(ET.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED, 2,
                          sched=FakeAttr(activity_id="enc-h264-1080p-c1")),
            ])

    seen = _capture(lambda: asyncio.run(
        D._emit_temporal_progress(Handle(), ET, {}, None)))
    assert seen, "nothing emitted"
    for key, status in seen:
        assert status == "queued", (
            f"{key} reported {status!r} on SCHEDULE — a chunk nobody has picked "
            f"up must not claim to be running")


def test_started_pending_activity_is_promoted_to_running() -> None:
    import asyncio

    D._RUN_SEEN.clear()

    class Handle:
        async def describe(self):
            return FakeAttr(raw_description=FakeAttr(pending_activities=[
                FakeAttr(activity_id="enc-h264-1080p-c0", state=D._PA_STARTED,
                         last_worker_identity="macmini", heartbeat_details=None),
            ]))

    class PCV:
        def from_payloads(self, p):
            return []

    client = FakeAttr(data_converter=FakeAttr(payload_converter=PCV()))
    seen = _capture(lambda: asyncio.run(D._emit_fleet_cpu(Handle(), client)))
    assert ("encode:h264:1080p:chunk0", "running") in seen, seen


def test_a_started_activity_with_no_identity_still_runs() -> None:
    # The machine checks below this `continue` when identity is unknown. An
    # activity whose worker identity has not been reported is still executing,
    # and must not be left showing as queued for its whole life.
    import asyncio

    D._RUN_SEEN.clear()

    class Handle:
        async def describe(self):
            return FakeAttr(raw_description=FakeAttr(pending_activities=[
                FakeAttr(activity_id="enc-h264-720p-c3", state=D._PA_STARTED,
                         last_worker_identity="", heartbeat_details=None),
            ]))

    class PCV:
        def from_payloads(self, p):
            return []

    client = FakeAttr(data_converter=FakeAttr(payload_converter=PCV()))
    seen = _capture(lambda: asyncio.run(D._emit_fleet_cpu(Handle(), client)))
    assert ("encode:h264:720p:chunk3", "running") in seen, seen


def test_non_chunk_activities_are_promoted_too() -> None:
    # The host/chunk bookkeeping below is scoped to `enc-` ids. Promotion must
    # not be, or mezzanine/audio/package would sit at "queued" until they
    # completed and jump straight to done.
    import asyncio

    D._RUN_SEEN.clear()

    class Handle:
        async def describe(self):
            return FakeAttr(raw_description=FakeAttr(pending_activities=[
                FakeAttr(activity_id="mezzanine", state=D._PA_STARTED,
                         last_worker_identity="mac", heartbeat_details=None),
                FakeAttr(activity_id="pkg-h264", state=D._PA_STARTED,
                         last_worker_identity="mac", heartbeat_details=None),
            ]))

    class PCV:
        def from_payloads(self, p):
            return []

    client = FakeAttr(data_converter=FakeAttr(payload_converter=PCV()))
    seen = _capture(lambda: asyncio.run(D._emit_fleet_cpu(Handle(), client)))
    got = {k for k, st in seen if st == "running"}
    assert "mezzanine" in got, seen
    assert "package:h264" in got, seen


def _host_markers(fn) -> list:
    """Run fn, returning the [[ENCODER-HOST ...]] lines it printed, as
    (key, instance) pairs. Printed rather than emitted through a seam, because
    this process's stdout IS the channel to the Go server."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    out = []
    for line in buf.getvalue().splitlines():
        if line.startswith("[[ENCODER-HOST "):
            f = dict(p.split("=", 1) for p in
                     line[len("[[ENCODER-HOST "):-2].split(" "))
            out.append((f["key"], f["instance"]))
    return out


def _one_started(activity_id: str, machine: str = "ubuntu"):
    """A describe() handle + client whose only pending activity is this one,
    STARTED on `machine`."""
    import asyncio

    class Handle:
        async def describe(self):
            return FakeAttr(raw_description=FakeAttr(pending_activities=[
                FakeAttr(activity_id=activity_id, state=D._PA_STARTED,
                         last_worker_identity=machine, heartbeat_details=None),
            ]))

    class PCV:
        def from_payloads(self, p):
            return []

    client = FakeAttr(data_converter=FakeAttr(payload_converter=PCV()))
    return lambda: asyncio.run(D._emit_fleet_cpu(Handle(), client))


def test_a_running_phase_says_which_box_it_is_on() -> None:
    """#293. A stage's machine only ever arrives on an ENCODER-HOST marker, and
    the machine timeline drops any stage without one — so a phase that reports
    none draws its box as IDLE for the whole time it is working, which on the
    tail phases is exactly when someone consults the chart.

    Temporal writes no ActivityTaskStarted event into history until the activity
    COMPLETES, so the history reader cannot answer this while it matters. The
    pending-activity record can, and does: `last_worker_identity` was correct
    for the entire 9 minutes the stage reported null.
    """
    D._RUN_SEEN.clear()
    D._HOST_SEEN.clear()
    got = _host_markers(_one_started("pkg-h264"))
    assert got == [("package:h264", "ubuntu"),
                   ("fragments:h264", "ubuntu"),
                   ("hls:h264", "ubuntu")], got


def test_every_row_of_a_multi_row_activity_is_coloured() -> None:
    """The dedup is keyed by ACTIVITY, so it has to be tested once and then the
    keys looped — testing it per key would record on the first row and suppress
    the other two, leaving package coloured and fragments/hls blank."""
    D._RUN_SEEN.clear()
    D._HOST_SEEN.clear()
    keys = {k for k, _i in _host_markers(_one_started("pkg-hevc"))}
    assert keys == {"package:hevc", "fragments:hevc", "hls:hevc"}, keys


def test_a_chunk_still_reports_its_box() -> None:
    """The colouring the phases now share was the chunk grid's first, and the
    per-chunk plot is the more visible half of it."""
    D._RUN_SEEN.clear()
    D._HOST_SEEN.clear()
    got = _host_markers(_one_started("enc-h264-1080p-c7", machine="macmini"))
    assert got == [("encode:h264:1080p:chunk7", "macmini")], got


def test_a_self_run_phase_is_not_re_tagged_by_the_worker_holding_it() -> None:
    """The mezzanine is built HERE and the workflow still schedules an activity
    for it, which a worker picks up, finds the .done and returns from. That
    worker must not claim the row: the box that did the work is this one, and
    ENCODER-HOST is last-writer-wins on the Go side."""
    D._RUN_SEEN.clear()
    D._HOST_SEEN.clear()
    D._SELF_RUN_STAGES.add("mezzanine")
    try:
        got = _host_markers(_one_started("mezzanine"))
    finally:
        D._SELF_RUN_STAGES.discard("mezzanine")
    assert got == [], f"a worker re-tagged a host-built phase: {got}"


def test_host_run_phases_report_this_box() -> None:
    """The other half of #293, and the bigger one since #298: the mezzanine and
    the packaging never reach a worker at all, so nothing else can say where
    they ran — leaving the master blank for the head and the tail of every run
    while it is the only machine doing anything."""
    import os

    old = os.environ.get("WORKER_LABEL")
    os.environ["WORKER_LABEL"] = "mac"
    try:
        got = _host_markers(lambda: D._emit_self_run_host("pkg-h264"))
        assert got == [("package:h264", "mac"), ("fragments:h264", "mac"),
                       ("hls:h264", "mac")], got
        assert _host_markers(lambda: D._emit_self_run_host("mezzanine")) == [
            ("mezzanine", "mac")]
        # An id that drives no row emits nothing rather than a marker the Go
        # side would drop anyway.
        assert _host_markers(lambda: D._emit_self_run_host("byteranges-h264")) == []
        # Unknown box: a missing lane is a gap, a guessed one is a machine that
        # does not exist.
        del os.environ["WORKER_LABEL"]
        assert _host_markers(lambda: D._emit_self_run_host("mezzanine")) == []
    finally:
        if old is None:
            os.environ.pop("WORKER_LABEL", None)
        else:
            os.environ["WORKER_LABEL"] = old


def test_host_run_phases_are_coloured_before_they_run() -> None:
    """Not after. The point is the lane filling WHILE the phase works — a marker
    emitted on completion colours a box for a run that has already ended."""
    for fn in (D._build_mezzanine_on_host, D._package_on_host):
        src = inspect.getsource(fn)
        emit = src.index("_emit_self_run_host")
        assert emit < src.index("subprocess.Popen"), (
            f"{fn.__name__} colours its lane after starting the work")


def test_promotion_is_emitted_once_not_every_poll() -> None:
    # The loop polls every second for the life of the run. Re-announcing running
    # for every chunk on every tick would flood the marker channel the whole
    # encode rides on.
    import asyncio

    D._RUN_SEEN.clear()

    class Handle:
        async def describe(self):
            return FakeAttr(raw_description=FakeAttr(pending_activities=[
                FakeAttr(activity_id="enc-h264-540p-c2", state=D._PA_STARTED,
                         last_worker_identity="ubuntu", heartbeat_details=None),
            ]))

    class PCV:
        def from_payloads(self, p):
            return []

    h, c = Handle(), FakeAttr(data_converter=FakeAttr(payload_converter=PCV()))
    first = _capture(lambda: asyncio.run(D._emit_fleet_cpu(h, c)))
    again = _capture(lambda: asyncio.run(D._emit_fleet_cpu(h, c)))
    assert any(st == "running" for _k, st in first), first
    assert not any(st == "running" for _k, st in again), (
        f"running re-announced on the second poll: {again}")


def test_package_all_drives_all_three_package_rows() -> None:
    """package-all is the only finalization activity, so it owns all three rows.

    The workflow used to run `byteranges` and `hls` after it, each re-downloading
    the whole packaged ladder from MinIO to redo work package-all had already
    done. Removing them is only safe while the rows they used to drive are
    driven by something — otherwise a run ends with fragments/hls stuck queued.
    """
    assert D._stage_keys_for("pkg-h264") == [
        "package:h264", "fragments:h264", "hls:h264"]


def test_removed_finalization_activities_are_not_mapped() -> None:
    """A box mid-rolling-update can still run the OLD workflow, whose extra
    activities would re-announce rows this run already finished — and nothing
    between emit_stage and the UI stops a cell walking backwards."""
    assert D._stage_keys_for("byteranges-h264") == []
    assert D._stage_keys_for("hls-h264") == []


def test_host_built_mezzanine_is_not_re_announced_by_history() -> None:
    """The mezzanine is built by the orchestrator now, and the workflow still
    schedules an activity that finds the .done and returns. That activity's
    SCHEDULED event must not drag a row that is already done back to queued."""
    import asyncio

    class Handle:
        async def fetch_history(self):
            return FakeAttr(events=[
                FakeEvent(ET.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED, 1,
                          sched=FakeAttr(activity_id="mezzanine")),
            ])

    D._SELF_RUN_STAGES.add("mezzanine")
    try:
        seen = _capture(lambda: asyncio.run(
            D._emit_temporal_progress(Handle(), ET, {}, None)))
    finally:
        D._SELF_RUN_STAGES.discard("mezzanine")
    assert seen == [], f"host-built mezzanine re-announced from history: {seen}"


def _plan_keys(sync_back: bool = True) -> list:
    rungs = {"h264": [FakeAttr(label="1080p")]}  # _emit_plan reads only .label
    seen = []
    real = D.emit_plan
    D.emit_plan = lambda stages: seen.extend(s.key for s in stages)
    try:
        D._emit_plan(rungs, {("h264", "1080p"): 2}, True, sync_back=sync_back)
    finally:
        D.emit_plan = real
    return seen


def test_plan_declares_no_source_upload_row() -> None:
    """Nothing stages the source any more, so a row for it would sit pending for
    the whole run and then finish the encode still pending."""
    seen = _plan_keys()
    assert "upload:source" not in seen, seen
    assert "mezzanine" in seen, seen
    for k in ("package:h264", "fragments:h264", "hls:h264", "download:outputs"):
        assert k in seen, (k, seen)


def test_host_packaged_run_declares_no_download_row() -> None:
    """Host packaging writes straight to the output dir, so download:outputs
    never fires — and a declared stage that never fires is a row that sits
    pending forever at the end of every run."""
    seen = _plan_keys(sync_back=False)
    assert "download:outputs" not in seen, seen
    # The packaging rows must SURVIVE: they are what host packaging drives live.
    for k in ("package:h264", "fragments:h264", "hls:h264"):
        assert k in seen, (k, seen)


def test_codec_dir_puts_the_tag_after_the_codec() -> None:
    """`<stem>_<codec>[_<tag>]`, so the `_p200_<codec>` shape stays intact.

    OutputStem, resolveCodec and the watcher all key off that shape. Two callers
    need this name — the sync-back and host packaging — and if they spelled it
    differently the same clip would land in two directories.
    """
    assert D._codec_dir("clip_p200", "h264", "") == "clip_p200_h264"
    assert D._codec_dir("clip_p200", "hevc", "xs") == "clip_p200_hevc_xs"


def test_host_package_plan_key_matches_what_the_workflow_reads() -> None:
    """`host_package` is a contract between two files and nothing raises if they
    stop agreeing: the orchestrator would package a codec the workflow also
    packaged (wasted work, MinIO copy discarded), or — far worse the other way —
    neither would, and the run would finish with no output for that codec.

    Read as text because temporalio is not installed on the host, so the
    workflow cannot be imported here.
    """
    wf = (Path(__file__).resolve().parent / "infinite_streaming_encoder"
          / "temporal_worker.py").read_text()
    assert 'plan.get("host_package")' in wf, (
        "the workflow no longer reads host_package; every host-packaged codec "
        "would be packaged in a worker as well")
    assert "if codec in host_pkg:" in wf and "continue" in wf, (
        "the workflow reads host_package but no longer skips on it")
    src = inspect.getsource(D.run_temporal)
    assert '"host_package": host_package' in src, (
        "the orchestrator no longer sends host_package; the workflow would "
        "package nothing it was told to skip")


def test_chunks_by_variant_plan_key_matches_what_the_workflow_reads() -> None:
    """`chunks_by_variant` is the same shape of contract as `host_package` above,
    and it fails the same way: silently, in both directions (#362).

    Spelled differently in the two files and the workflow falls back to the
    uniform `chunks` for every rung — every variant encoded on one grid again,
    which is precisely the behaviour this replaced, with no error anywhere. The
    UI would be the only witness: `_emit_plan` draws its chunk grid from the
    per-variant counts, so the rows would be sized for a plan nothing ran.

    Read as text for the same reason as above — temporalio is not installed on
    the host.
    """
    wf = (Path(__file__).resolve().parent / "infinite_streaming_encoder"
          / "temporal_worker.py").read_text()
    assert 'plan.get("chunks_by_variant")' in wf, (
        "the workflow no longer reads chunks_by_variant; every variant would "
        "fall back to the uniform grid and per-variant sizing would be inert")
    assert "_chunks_for(codec," in wf, (
        "the workflow reads chunks_by_variant but no longer dispatches on it")
    src = inspect.getsource(D.run_temporal)
    assert '"chunks_by_variant"' in src, (
        "the orchestrator no longer sends chunks_by_variant; the workflow would "
        "silently use one grid for every rung")
    # The uniform list must SURVIVE alongside it: it is what an unsized rung uses
    # and what a worker on pre-#362 code reads. Dropping it turns a rolling
    # update into a run with no chunk plan at all.
    assert '"chunks": [' in src, (
        "the orchestrator dropped the uniform chunks list; a rung Go did not "
        "size, and any worker still on older code, would have no plan")


def test_host_packaging_is_the_default_with_an_escape() -> None:
    """On by default — a worker-packaged codec is uploaded to MinIO purely so
    this process can download it straight back. The escape exists because host
    packaging is the one step with no Temporal retry behind it.
    """
    p = D.build_parser()
    base = ["--input", "x.mp4", "--output", "x", "--output-dir", "/tmp",
            "--s3-bucket", "b", "--job-prefix", "jobs/x"]
    assert p.parse_args(base).no_host_package is False
    assert p.parse_args(base + ["--no-host-package"]).no_host_package is True


def test_cached_mezzanine_is_reused_not_attributed_to_a_box() -> None:
    """A cache hit means NO box built the mezzanine. Naming the worker that drew
    the short-circuit guard reads as 'ubuntu built it' when ubuntu did a HEAD and
    returned. And with mezz_ready the row has no other announcer at all, so the
    `done` here is the only thing that will ever move it off pending."""
    src = inspect.getsource(D.run_temporal)
    hit = src.split("mezzanine cache HIT")[1].split("else:")[0]
    # Code only. The comment there names the helper it is deliberately NOT
    # calling, which is the whole point of the comment and would fail below.
    hit = "\n".join(ln for ln in hit.splitlines()
                    if not ln.lstrip().startswith("#"))
    assert '_emit_reused("mezzanine")' in hit, (
        "a cached mezzanine no longer emits ENCODER-REUSED; the row loses the "
        "neutral styling that distinguishes it from work someone did")
    assert 'emit_stage("mezzanine", "done"' in hit, (
        "a cached mezzanine never completes its row — no activity is scheduled, "
        "so nothing else will")
    assert "_emit_self_run_host" not in hit and "host_marker" not in hit, (
        "a cached mezzanine attributes the row to a machine — nothing built it")


def test_a_self_run_mezzanine_is_never_re_announced_by_history() -> None:
    """Both branches — built here, or found warm in the cache — leave the row
    done. Only a farm mid-rolling-update can still schedule the activity, and its
    SCHEDULED event would walk that finished row back to `queued`."""
    import re

    src = inspect.getsource(D.run_temporal)
    # At the function's own indent — i.e. OUTSIDE the mezzanine if/else, so both
    # the cache hit and the host build are covered. Indented, it guards one
    # branch and leaves the other to be walked backwards.
    assert re.search(r'^    _SELF_RUN_STAGES\.add\("mezzanine"\)$', src, re.M), (
        "the mezzanine history row is unsuppressed, or suppressed inside only "
        "one branch of the if/else; an older worker's activity walks it back "
        "to queued")


def test_mezz_ready_stops_the_workflow_scheduling_the_activity() -> None:
    """The orchestrator has already put the mezzanine in MinIO by the time the
    workflow starts, so the activity could only re-run the check it had just
    done — on whatever box drew it. Contract between two files, read as text
    because temporalio is not installed on the host."""
    wf = (Path(__file__).resolve().parent / "infinite_streaming_encoder"
          / "temporal_worker.py").read_text()
    assert 'if not plan.get("mezz_ready"):' in wf, (
        "the workflow no longer honours mezz_ready; every run pays the "
        "redundant guard activity again")
    src = inspect.getsource(D.run_temporal)
    assert '"mezz_ready": True' in src, (
        "the orchestrator no longer sends mezz_ready, so the workflow will "
        "keep scheduling the activity")


def test_packaged_dir_rename_tolerates_dest_equal_to_src() -> None:
    """cli_phase delivers output_<codec>/ and this renames it to
    <stem>_<codec>/. A source named output.mp4 with no duration suffix gives the
    stem "output", so the two names are the SAME — and an unguarded
    rmtree(dest) would delete the ladder that was just packaged."""
    src = inspect.getsource(D._package_on_host)
    assert "if dest != src:" in src, (
        "the rename no longer guards dest == src; packaging a clip whose stem "
        "is 'output' would delete its own output")


def test_host_packaging_failure_says_the_chunks_survive() -> None:
    """The one thing giving up Temporal's retry costs, and the only cheap
    recovery: a Retry from the UI mints a NEW job id and therefore a new staging
    prefix, so it re-encodes everything. Re-running against the same prefix does
    not."""
    src = inspect.getsource(D._package_on_host)
    assert "--job-prefix" in src, (
        "the failure message no longer tells you how to recover without "
        "re-encoding the whole ladder")


def test_the_task_queue_comes_from_the_environment() -> None:
    """The server passes TEMPORAL_TASK_QUEUE into this container and every
    worker reads the same var. A hardcoded default meant changing the queue
    moved the server and the workers while the ORCHESTRATOR kept starting
    workflows on "encode" — with no error, because both queues exist and each
    side succeeds. Found while trying to isolate a run onto one box: the local
    worker sat on an empty queue while the remote boxes ran the job."""
    import os
    old = os.environ.get("TEMPORAL_TASK_QUEUE")
    os.environ["TEMPORAL_TASK_QUEUE"] = "encode-isolated"
    try:
        base = ["--input", "x.mp4", "--output", "x", "--output-dir", "/tmp",
                "--s3-bucket", "b", "--job-prefix", "jobs/x"]
        got = D.build_parser().parse_args(base).temporal_task_queue
        assert got == "encode-isolated", got
        del os.environ["TEMPORAL_TASK_QUEUE"]
        assert D.build_parser().parse_args(base).temporal_task_queue == "encode"
    finally:
        if old is None:
            os.environ.pop("TEMPORAL_TASK_QUEUE", None)
        else:
            os.environ["TEMPORAL_TASK_QUEUE"] = old


def test_temporal_is_the_only_backend() -> None:
    """Temporal is the only backend, and asking for the removed one must FAIL.

    The `pool` backend was a second implementation of this orchestrator that
    Temporal replaced and nobody deleted. It survived as the CLI's DEFAULT while
    the Go server always passed `--backend temporal`, so the path that shipped
    and the path you got by hand were different — and they drifted (#173 fixed
    the packaging env on one of them).

    Deleting the code is not enough on its own: `--backend pool` lives in shell
    histories and in muscle memory. If argparse silently accepted it, or the
    default reverted, the failure would be a wrong-looking encode rather than an
    error. So this pins both halves.
    """
    p = D.build_parser()
    base = ["--input", "x.mp4", "--output", "x", "--output-dir", "/tmp",
            "--s3-bucket", "b", "--job-prefix", "jobs/x"]
    args = p.parse_args(base)
    assert args.backend == "temporal", args.backend

    try:
        p.parse_args(base + ["--backend", "pool"])
    except SystemExit as e:
        assert e.code != 0, "--backend pool exited 0"
    else:
        raise AssertionError("--backend pool was accepted; it must fail loudly")

    assert not hasattr(D, "run_phase"), "pool helper run_phase survived"
    assert not hasattr(D, "build_pool"), "pool helper build_pool survived"
    assert not hasattr(D, "encode_chunks_distributed"), "pool dispatch survived"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
