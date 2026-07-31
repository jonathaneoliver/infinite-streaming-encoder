"""One way for a worker to say something to the control plane.

Every worker in this system produces the same thing — a stream of
`[[ENCODER-…]]` markers — but each deployment moves them differently:

    single-container local   worker stdout -> `docker logs -f` -> Go server
    local-dist (Temporal)    worker stdout -> temporal_worker  -> heartbeat /
                             activity result -> orchestrator -> Go server
    cloud (Batch)            worker stdout -> CloudWatch -> orchestrator
                             polls, re-prints -> Go server

Before this module the choice of transport was made independently at every
emission site, by whoever wrote that marker. That is how #141 happened: VMAF was
computed per chunk and thrown away, because the relay forwarded a whitelist and
nobody added the new marker to it. The same shape nearly recurred with
ENCODER-FLEET.

So: emit through `emit()` and the marker travels on whatever transports this
process has. Adding a marker never again requires knowing how many pipes exist.

STDOUT IS ALWAYS ONE OF THEM. It is not a legacy path — it is what makes the
abstraction safe to adopt:

  * it is the only transport that needs no configuration, no IAM and no network,
    so a sink that fails to initialise degrades to "exactly what happened
    before" rather than to silence;
  * on the single-container path it is load-bearing for restart resilience —
    `docker logs -f` is what lets the Go server reattach to an encode that
    outlived it (see CLAUDE.md, "Worker containers");
  * on local-dist it IS the sink: temporal_worker reads its child's stdout and
    routes each marker onto the heartbeat or the activity result.

A sink is therefore always an ADDITIONAL, lower-latency or lower-cost channel —
never a replacement. Losing one degrades performance, never correctness.

Sinks are selected from the environment so a file like cli_local.py, which runs
both as the local orchestrator and as the Batch worker entrypoint, needs no
knowledge of which role it is in: the orchestrator simply has no queue
configured and `emit()` is precisely `print()`.

SCOPE — this abstracts ONE hop: worker -> control plane. The orchestrators
(cli_batch, cli_local_dist) also print `[[ENCODER-…]]` markers, but those travel
control plane -> Go server over a pipe the Go server is already attached to.
That hop has exactly one transport and no reachability problem, so it stays a
plain `print`. Routing it through here would also be a live hazard on the cloud
path: cli_batch is the CONSUMER of the queue, and giving it a sink would let it
republish what it just drained.
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time

# The Step Functions execution this worker belongs to, injected by the workflow
# definition as $$.Execution.Name. Its presence is what enables the SQS sink;
# absent everywhere else, which is what makes emit() a plain print on the local
# paths.
_EXEC_ENV = "ENCODER_TELEMETRY_EXEC"

_QUEUE_PREFIX = "encoder-telemetry-"
# SQS hard limit on queue names. Not advisory — CreateQueue rejects longer.
_SQS_NAME_MAX = 80


def queue_name(execution_name: str) -> str:
    """The telemetry queue for one Step Functions execution.

    CONTRACT. The worker derives its queue from this, and so does the
    orchestrator that creates, drains and deletes it. Deriving it separately in
    either place is how you get a worker publishing into the void while the
    orchestrator polls an empty queue it created itself — with no error on
    either side, because both operations succeed.

    Execution names run to 67 characters ({jobid}-{stem} capped at 60, plus a
    6-hex uniqueness suffix), which with the prefix overflows the 80-char SQS
    limit. The trailing suffix is what makes the name unique, so when trimming
    is needed the READABLE HEAD gives way and the suffix is preserved — trimming
    the tail instead would let two executions of the same job share a queue.
    """
    room = _SQS_NAME_MAX - len(_QUEUE_PREFIX)
    if len(execution_name) > room:
        execution_name = execution_name[:room - 7] + execution_name[-7:]
    return _QUEUE_PREFIX + execution_name

# Publish in batches, since SQS bills per request and accepts 10 messages in one.
# Bounded by AGE as well as size: at the 2s progress cadence a single chunk fills
# 10 slots in ~20s, and a progress bar 20s stale is worse than a few extra API
# calls. Whichever bound trips first wins.
_BATCH_MAX = 10
_BATCH_MAX_AGE_S = 1.0

# SQS caps a SendMessageBatch payload at 256 KiB total. Markers are ~100 bytes,
# so this only ever trips on something malformed; truncating beats failing the
# whole batch and losing the nine well-formed markers travelling with it.
_MAX_BODY = 200_000

# Enough to notice the channel is down without turning a per-chunk failure into
# thousands of duplicate lines in the log we are trying to read.
_MAX_WARNINGS = 3


# ---------------------------------------------------------------------------
# Marker taxonomy
#
# Every consumer has to answer the same question — "may I drop this?" — and
# before this each answered it with its own hardcoded tuple of marker names:
# temporal_worker excluded STAGE and FLEET from its activity-result relay,
# cli_batch skipped FLEET on a finished stream, and cli_batch's queue drain
# skipped a stale FLEET. One concept, three literals, and every new marker had
# to be added to all three by hand. That is #141's mechanism, not a hypothetical.
#
# So classify centrally, and make the DEFAULT the safe one: a marker nobody has
# classified is a RECORD, and records are never dropped. Getting it wrong that
# way costs a little bandwidth. Getting it wrong the other way threw away 465
# computed VMAF scores on a 3-hour encode.
# ---------------------------------------------------------------------------

MARKER_PREFIX = "[[ENCODER-"

#: Superseded by the next value of the same key, and duplicated by a state
#: channel on both distributed paths (Batch job status / Temporal history). Safe
#: to drop; a late copy actively fights the live value with stale data.
CLASS_LIVE = "live"
#: A point-in-time sample whose meaning depends on WHEN it arrives — the control
#: plane stamps arrival time. Safe to drop, and must be dropped when stale,
#: because replaying one registers an old reading as the machine's current state.
CLASS_GAUGE = "gauge"
#: Measured once and unrecoverable without re-encoding. Never dropped.
CLASS_RECORD = "record"

_CLASS_BY_NAME = {
    "STAGE": CLASS_LIVE,
    "FLEET": CLASS_GAUGE,
}


def is_marker(line: str) -> bool:
    """True if `line` is an `[[ENCODER-…]]` marker."""
    return line.startswith(MARKER_PREFIX)


def marker_class(line: str) -> str:
    """CLASS_LIVE / CLASS_GAUGE / CLASS_RECORD for one marker line.

    Unknown markers are records — see the note above. This is the property that
    makes adding a marker safe by default: write it, and every consumer already
    forwards it.
    """
    name = line[len(MARKER_PREFIX):].split(" ", 1)[0].rstrip("]")
    return _CLASS_BY_NAME.get(name, CLASS_RECORD)


def is_record(line: str) -> bool:
    """True if losing this marker loses data that cannot be recomputed."""
    return marker_class(line) == CLASS_RECORD


def is_gauge(line: str) -> bool:
    """True if this marker's value is only meaningful at its moment of arrival."""
    return marker_class(line) == CLASS_GAUGE


class _Sink:
    """A side-channel for markers. Never raises; never blocks the encode."""

    def send(self, markers: list[str]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        pass


class _SqsSink(_Sink):
    """Publish markers to a per-execution SQS queue.

    One queue per Step Functions execution, so the orchestrator polling it is the
    only consumer and can delete everything it receives. A single shared queue
    would put concurrent runs in competition: SQS returns a SAMPLE of available
    messages, so an orchestrator whose run is small would repeatedly draw another
    run's messages, be unable to delete them, and starve.
    """

    def __init__(self, queue_name: str) -> None:
        import boto3  # local import: the local paths must not need boto3

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        self._sqs = boto3.client("sqs", region_name=region)
        # Resolve once. If the queue is missing this raises and the caller
        # disables the sink — stdout still carries everything.
        self._url = self._sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
        self._warned = 0

    def send(self, markers: list[str]) -> None:
        entries = [{"Id": str(i), "MessageBody": m[:_MAX_BODY]}
                   for i, m in enumerate(markers)]
        try:
            self._sqs.send_message_batch(QueueUrl=self._url, Entries=entries)
        except Exception as e:  # noqa: BLE001 — telemetry must never fail a run
            self._warn(f"send failed: {type(e).__name__}: {e}")

    def _warn(self, msg: str) -> None:
        if self._warned >= _MAX_WARNINGS:
            return
        self._warned += 1
        tail = " (further warnings suppressed)" if self._warned == _MAX_WARNINGS else ""
        print(f"[telemetry] {msg}{tail}", file=sys.stderr, flush=True)


class _Publisher:
    """Buffers markers and hands them to a sink in batches.

    Flushing is done by a daemon thread rather than opportunistically on the next
    emit(). A chunk's LAST markers — ENCODER-TIMING, ENCODER-SPEED — are followed
    by no further emissions, so an emit-driven flush would hold exactly the
    records that cannot be reconstructed until process exit, and lose them
    outright if the container is killed (spot reclaim) before atexit runs.
    """

    def __init__(self, sink: _Sink) -> None:
        self._sink = sink
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._oldest = 0.0
        self._warned = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="telemetry-flush")
        self._thread.start()
        atexit.register(self.close)

    def add(self, marker: str) -> None:
        with self._lock:
            if not self._buf:
                self._oldest = time.monotonic()
            self._buf.append(marker)
            full = len(self._buf) >= _BATCH_MAX
        if full:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            batch, self._buf = self._buf[:_BATCH_MAX], self._buf[_BATCH_MAX:]
            self._oldest = time.monotonic() if self._buf else 0.0
        if not batch:
            return
        try:
            self._sink.send(batch)
        except Exception as e:  # noqa: BLE001 — see below
            # Guarded HERE, not only inside each sink. flush() is called from
            # add(), which sits directly in the encode's call path, so an
            # unguarded sink turns a telemetry outage into a failed encode. The
            # batch is dropped rather than requeued: markers are only worth
            # sending while they are current, and a sink that is down would
            # otherwise grow the buffer without bound for the rest of the run.
            self._warn(f"{type(e).__name__}: {e}")

    def _warn(self, msg: str) -> None:
        if self._warned >= _MAX_WARNINGS:
            return
        self._warned += 1
        tail = " (further warnings suppressed)" if self._warned == _MAX_WARNINGS else ""
        print(f"[telemetry] dropped a batch: {msg}{tail}",
              file=sys.stderr, flush=True)

    def _loop(self) -> None:
        while not self._stop.wait(_BATCH_MAX_AGE_S / 2):
            with self._lock:
                due = bool(self._buf) and (
                    time.monotonic() - self._oldest >= _BATCH_MAX_AGE_S)
            if due:
                self.flush()

    def close(self) -> None:
        self._stop.set()
        # Drain fully: the buffer may hold more than one batch if the sink was
        # slower than the encode produced markers.
        while True:
            with self._lock:
                if not self._buf:
                    break
            self.flush()
        self._sink.close()


# Resolved once, lazily, on first emit. None = stdout only.
_publisher: _Publisher | None = None
_init_done = False


def _publisher_for_env() -> _Publisher | None:
    exec_name = os.environ.get(_EXEC_ENV, "").strip()
    if not exec_name:
        return None
    try:
        return _Publisher(_SqsSink(queue_name(exec_name)))
    except Exception as e:  # noqa: BLE001 — no side channel is not a failure
        # Deliberately not fatal. The worker keeps printing to stdout, the
        # orchestrator keeps its CloudWatch fallback, and the run is slower to
        # report rather than broken.
        print(f"[telemetry] disabled ({type(e).__name__}: {e}); "
              f"markers go to stdout only", file=sys.stderr, flush=True)
        return None


def emit(marker: str) -> None:
    """Emit one `[[ENCODER-…]]` marker on every transport this process has."""
    global _publisher, _init_done
    print(marker, flush=True)
    if not _init_done:
        _init_done = True
        _publisher = _publisher_for_env()
    if _publisher is not None:
        _publisher.add(marker)


def flush() -> None:
    """Block until buffered markers have been handed to the sink.

    Call at the end of a phase. atexit covers the normal path, but a worker that
    is SIGKILLed — which on spot capacity is routine, not exceptional — never
    runs atexit handlers.
    """
    if _publisher is not None:
        _publisher.flush()
