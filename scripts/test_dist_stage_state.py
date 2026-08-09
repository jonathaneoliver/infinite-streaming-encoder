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

Activity ids are `enc-<codec>-<label>-c<N>` — note the `c`. _stage_key_for
returns None for any other shape, so a fixture that gets it wrong exercises
nothing at all, silently. These use the real format.
"""
from __future__ import annotations

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
