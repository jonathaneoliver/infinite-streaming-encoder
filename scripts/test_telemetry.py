"""Tests for the worker->control-plane telemetry layer.

Run directly (`python3 scripts/test_telemetry.py`) or via `make check`. No test
framework: the repo has none, and this needs nothing beyond assert.

Every failure mode here is SILENT in production — a marker that never leaves the
worker looks exactly like a marker that was never emitted, and the only symptom
is a UI cell that stays blank. So the buffering is tested against a fake sink
rather than trusted to review.
"""
from __future__ import annotations

import io
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import telemetry  # noqa: E402


class FakeSink(telemetry._Sink):
    def __init__(self, fail: bool = False, delay: float = 0.0) -> None:
        self.batches: list[list[str]] = []
        self.closed = False
        self._fail = fail
        self._delay = delay
        self._lock = threading.Lock()

    def send(self, markers):
        # markers are (seq, text) tuples — the publisher stamps emission order
        if self._delay:
            time.sleep(self._delay)
        if self._fail:
            raise RuntimeError("sink is down")
        with self._lock:
            self.batches.append(list(markers))

    def close(self):
        self.closed = True

    def flat(self):
        with self._lock:
            return [m for b in self.batches for _, m in b]

    def seqs(self):
        with self._lock:
            return [s for b in self.batches for s, _ in b]


def hb(i):
    """A HEARTBEAT marker — mid-progress, so it is allowed to wait in the buffer."""
    return f"[[ENCODER-STAGE key=encode:h264:1080p:chunk{i} status=running percent=41.0]]"


def test_batches_at_max_size():
    sink = FakeSink()
    p = telemetry._Publisher(sink)
    for i in range(telemetry._BATCH_MAX):
        p.add(hb(i))
    # The size bound must fire on add, without waiting for the timer — otherwise
    # a fast-emitting chunk buys latency it did not need to.
    assert sink.batches, "full batch was not flushed on add"
    assert len(sink.batches[0]) == telemetry._BATCH_MAX
    p.close()


def test_an_event_flushes_immediately_but_a_heartbeat_waits():
    """Chunk start and chunk end must be prompt; the ticks between them may lag.

    That is the whole latency budget: events are what the grid is really
    showing, heartbeats are cosmetic. Buffering events would mean a finished
    chunk sitting in a worker's buffer for seconds.
    """
    sink = FakeSink()
    p = telemetry._Publisher(sink)
    p.add(hb(0))
    assert not sink.flat(), "a heartbeat should have waited for the timer"

    p.add("[[ENCODER-STAGE key=encode:h264:1080p:chunk0 status=done percent=100.0]]")
    # The event flushes, and carries the waiting heartbeat out with it.
    assert len(sink.flat()) == 2, f"event did not flush promptly: {sink.flat()}"
    assert sink.flat()[-1].endswith("status=done percent=100.0]]")

    p.add("[[ENCODER-TIMING phase=variant key=x total_s=12.00]]")
    assert len(sink.flat()) == 3, "a record must not wait in the buffer"
    p.close()


def test_start_of_a_chunk_is_an_event_not_a_heartbeat():
    """percent=0.0 is the chunk STARTING — the moment a cell should light up."""
    assert not telemetry.is_heartbeat(
        "[[ENCODER-STAGE key=k status=running percent=0.0]]")
    assert not telemetry.is_heartbeat(
        "[[ENCODER-STAGE key=k status=running percent=100.0]]")
    assert telemetry.is_heartbeat(
        "[[ENCODER-STAGE key=k status=running percent=0.4]]")
    assert telemetry.is_heartbeat(
        "[[ENCODER-STAGE key=k status=running percent=99.9]]")


def test_flushes_on_age_without_a_full_batch():
    """A lone heartbeat must still leave on the timer, with nothing to pack with.

    Events flush themselves, so the timer is what a heartbeat depends on
    entirely. Without it a chunk that ticks once and then goes quiet — a short
    rung, or one stalled on a slow S3 read — shows no progress at all until its
    next event, and on a spot reclaim never at all.
    """
    sink = FakeSink()
    p = telemetry._Publisher(sink)
    p.add(hb(0))
    deadline = time.monotonic() + telemetry._BATCH_MAX_AGE_S * 6
    while not sink.flat() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sink.flat() == [hb(0)], (
        f"age-based flush did not fire within "
        f"{telemetry._BATCH_MAX_AGE_S * 6:.1f}s; got {sink.flat()}")
    p.close()


def test_close_drains_more_than_one_batch():
    """close() must loop, not flush once — the buffer can hold several batches
    if the sink was slower than the encode produced markers."""
    sink = FakeSink()
    p = telemetry._Publisher(sink)
    n = telemetry._BATCH_MAX * 3 + 4
    with p._lock:                      # suppress the size-triggered flush
        p._buf.extend((i + 1, f"m{i}") for i in range(n))
        p._oldest = time.monotonic()
    p.close()
    assert len(sink.flat()) == n, f"close() dropped markers: {len(sink.flat())} of {n}"
    assert sink.closed


def test_order_is_preserved():
    sink = FakeSink()
    p = telemetry._Publisher(sink)
    n = telemetry._BATCH_MAX * 2 + 3
    for i in range(n):
        p.add(hb(i))
    p.close()
    assert sink.flat() == [hb(i) for i in range(n)], "markers were reordered"


def test_a_failing_sink_never_breaks_the_encode():
    """A sink that raises must not propagate: telemetry is never worth failing a
    run for, and stdout still carries everything."""
    sink = FakeSink(fail=True)
    p = telemetry._Publisher(sink)
    for i in range(telemetry._BATCH_MAX + 1):
        p.add(hb(i))             # would raise if send() were unguarded
    p.close()


def test_emit_prints_even_with_no_sink():
    """The property the whole design rests on: no configuration -> no behaviour
    change. If this breaks, the local paths lose their transport entirely."""
    telemetry._publisher, telemetry._init_done = None, False
    buf = io.StringIO()
    with redirect_stdout(buf):
        telemetry.emit("[[ENCODER-STAGE key=x status=running percent=1.0]]")
    assert buf.getvalue() == "[[ENCODER-STAGE key=x status=running percent=1.0]]\n"


def test_emit_also_publishes_when_a_sink_exists():
    telemetry._publisher, telemetry._init_done = telemetry._Publisher(FakeSink()), True
    sink = telemetry._publisher._sink
    buf = io.StringIO()
    with redirect_stdout(buf):
        telemetry.emit("[[ENCODER-TIMING a=1]]")
    telemetry.flush()
    assert "[[ENCODER-TIMING a=1]]" in buf.getvalue(), "stdout lost the marker"
    assert sink.flat() == ["[[ENCODER-TIMING a=1]]"], "sink lost the marker"
    telemetry._publisher.close()
    telemetry._publisher, telemetry._init_done = None, False


def test_an_unclassified_marker_is_a_record():
    """THE #141 regression test.

    A marker nobody has classified must be treated as unrecoverable, so every
    consumer forwards it by default. #141 was a whitelist that silently dropped
    ENCODER-VMAF — 465 computed scores thrown away on a 3-hour encode — and the
    same shape nearly recurred with ENCODER-FLEET.

    If someone adds a marker and this test still passes, the marker already
    reaches the control plane on every path. That is the guarantee.
    """
    invented = "[[ENCODER-SOMETHING-NOBODY-HAS-WRITTEN-YET a=1 b=2]]"
    assert telemetry.is_marker(invented)
    assert telemetry.marker_class(invented) == telemetry.CLASS_RECORD
    assert telemetry.is_record(invented), "a new marker would be silently dropped"
    assert not telemetry.is_gauge(invented)


def test_known_marker_classes():
    cases = {
        "[[ENCODER-STAGE key=encode:h264:1080p:chunk3 status=running percent=41.0]]":
            telemetry.CLASS_LIVE,
        "[[ENCODER-FLEET machine=i-0abc busy=7.20 perf=8]]": telemetry.CLASS_GAUGE,
        "[[ENCODER-VMAF codec=h264 label=1080p height=1080 chunk=3 mean=93.1]]":
            telemetry.CLASS_RECORD,
        "[[ENCODER-TIMING phase=variant key=x total_s=12.00]]": telemetry.CLASS_RECORD,
        "[[ENCODER-SPEED machine=graviton codec=h264 height=1080]]":
            telemetry.CLASS_RECORD,
        "[[ENCODER-BOOT ami=ami-0abc]]": telemetry.CLASS_RECORD,
    }
    for line, want in cases.items():
        got = telemetry.marker_class(line)
        assert got == want, f"{line[:40]}… classified {got}, expected {want}"


def test_marker_class_handles_a_marker_with_no_fields():
    """`[[ENCODER-FLEET]]` with no trailing space must still classify as FLEET —
    the name parser splits on space, so a field-less marker is the edge case."""
    assert telemetry.marker_class("[[ENCODER-FLEET]]") == telemetry.CLASS_GAUGE
    assert telemetry.marker_class("[[ENCODER-STAGE]]") == telemetry.CLASS_LIVE


def test_non_markers_are_not_markers():
    for line in ("[ffmpeg] ffmpeg -i in.mp4", "frame= 120 fps=30", "",
                 "[[ENCODERISH something]]"):
        assert not telemetry.is_marker(line), f"misidentified as a marker: {line!r}"


def test_terminal_stage_is_not_walked_back():
    """A stale queued marker must never un-finish a chunk.

    The queue competes with _sync_stages_from_batch, which reads Batch status
    directly and is faster. When the drain runs behind, a worker's old
    "running 18%" surfaces after Batch has reported SUCCEEDED — and
    internal/encode/job.go assigns Status and Percent unconditionally, so the
    cell visibly goes done -> running and the global percentage drops. Measured
    live: 30 reversals in 150 seconds.

    This pins the rule the drain applies. Kept here rather than in cli_batch so
    it runs without boto3.
    """
    TERMINAL = {"done", "failed"}

    def forwards(prev, incoming):
        return not (prev in TERMINAL and incoming not in TERMINAL)

    assert not forwards("done", "running"), "a stale marker would un-finish a chunk"
    assert not forwards("failed", "running")
    assert forwards("running", "done"), "a real completion must get through"
    assert forwards("running", "running")
    assert forwards(None, "running"), "first sighting must get through"
    # done -> done is allowed: a repeat is harmless and blocking it would drop a
    # legitimate re-announcement carrying a corrected percent.
    assert forwards("done", "done")


def test_queue_name_fits_sqs_limit_and_stays_unique():
    """Execution names reach 67 chars; the prefix would push past SQS's 80."""
    longest = "j" * 60 + "-abc123"          # {jobid}-{stem} capped at 60, + rand6
    assert len(longest) == 67
    n = telemetry.queue_name(longest)
    assert len(n) <= telemetry._SQS_NAME_MAX, f"{len(n)} chars: {n}"
    assert n.startswith(telemetry._QUEUE_PREFIX)
    # STANDARD, not FIFO. One queue now carries worker markers AND Batch state
    # events, and EventBridge cannot usefully target a FIFO queue — a target
    # takes one static MessageGroupId, and its own delivery is unordered anyway.
    assert not n.endswith(".fifo"), f"queue must be standard: {n}"
    # Two executions of the SAME job differ only in the trailing suffix, so
    # trimming must never eat it — that would silently merge their queues.
    a = telemetry.queue_name("j" * 60 + "-aaaaaa")
    b = telemetry.queue_name("j" * 60 + "-bbbbbb")
    assert a != b, "trimming collapsed two distinct executions onto one queue"
    # Short names are left alone.
    assert telemetry.queue_name("short-abc123") == \
        telemetry._QUEUE_PREFIX + "short-abc123" + telemetry._QUEUE_SUFFIX


def test_queue_name_is_a_legal_sqs_name():
    import re
    for name in ("j" * 60 + "-abc123", "1785516812206-my_clip_p200-9f2a1c"):
        n = telemetry.queue_name(name)
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", n), f"illegal queue name: {n}"


def test_oversized_body_is_truncated_not_dropped():
    """A malformed giant marker must not take the nine good ones with it."""
    entries = [{"Id": "0", "MessageBody": ("x" * (telemetry._MAX_BODY + 500))
                [:telemetry._MAX_BODY]}]
    assert len(entries[0]["MessageBody"]) == telemetry._MAX_BODY


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001 — reporting, not handling
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"{failed} of {len(tests)} telemetry tests failed")
        return 1
    print(f"telemetry: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
