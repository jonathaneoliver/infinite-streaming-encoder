"""Tests for the orchestrator's single stage-emission chokepoint.

Three independent sources announce stage state, and they observe the same run
through channels with different latencies:

    _translate_events        Step Functions history (queued on enter, done on exit)
    _sync_stages_from_batch  Batch job status, the authoritative census
    _drain_telemetry         the worker's own markers, carrying live percent

They routinely disagree about the present tense. Reversals were fixed twice by
adding a guard at ONE call site, and both times they came back, because a
different source was still speaking unguarded. So the rule lives in _emit_stage
and this pins it.

Run directly or via `make check`.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from infinite_streaming_encoder import cli_batch  # noqa: E402
except ModuleNotFoundError as e:  # pragma: no cover — a dependency is absent
    # ONLY a missing dependency is a skip. This used to catch Exception, which
    # meant a module that could not import at all reported as a PASS — and it
    # did exactly that, hiding a NameError that would have stopped the
    # orchestrator from starting. A broken module must fail loudly.
    if "boto3" in str(e) or "botocore" in str(e):
        print(f"test_stage_state: skipped (dependency absent: {e})")
        raise SystemExit(0)
    raise


def emit(key, status, percent=0.0):
    """Call the chokepoint, returning (was_emitted, printed_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = cli_batch._emit_stage(key, status, percent)
    return ok, buf.getvalue()


def reset():
    cli_batch._STAGE_STATE.clear()


def test_done_is_never_walked_back():
    """The bug the user saw three times: a finished cell going blank or restarting.

    Step Functions history lags Batch, so a chunk marked done by the census can
    afterwards have its "entered" event surface and re-announce it as queued.
    """
    reset()
    emit("encode:h264:1440p:chunk3", "running", 41.0)
    emit("encode:h264:1440p:chunk3", "done", 100.0)

    for stale in ("queued", "running", "starting"):
        ok, out = emit("encode:h264:1440p:chunk3", stale, 18.4)
        assert not ok, f"a finished chunk was re-announced as {stale}"
        assert out == "", f"suppressed transition still printed: {out!r}"


def test_a_failed_chunk_may_still_retry():
    """`failed` is deliberately NOT final.

    The state machine's Retry block resubmits a NEW Batch job, so failed ->
    running is a real transition. Blocking it would freeze the cell on a dead
    attempt — the reason this guard is not simply "never leave a terminal state".
    """
    reset()
    emit("encode:h264:1080p:chunk7", "running", 10.0)
    emit("encode:h264:1080p:chunk7", "failed", 0.0)
    ok, out = emit("encode:h264:1080p:chunk7", "running", 0.0)
    assert ok, "a retried chunk was frozen on its failed attempt"
    assert "status=running" in out


def test_done_may_be_reannounced():
    """done -> done must pass: several sources legitimately report the same
    completion, and blocking the repeat would depend on which arrives first."""
    reset()
    emit("encode:h264:720p:chunk1", "done", 100.0)
    ok, _ = emit("encode:h264:720p:chunk1", "done", 100.0)
    assert ok


def test_normal_progression_is_untouched():
    reset()
    for st, pct in (("queued", 0.0), ("starting", 0.0), ("running", 12.0),
                    ("running", 88.0), ("done", 100.0)):
        ok, out = emit("encode:h264:2160p:chunk9", st, pct)
        assert ok, f"{st} was wrongly suppressed"
        assert f"status={st}" in out


def test_keys_are_independent():
    reset()
    emit("encode:h264:540p:chunk0", "done", 100.0)
    ok, _ = emit("encode:h264:540p:chunk1", "running", 5.0)
    assert ok, "one chunk finishing suppressed a different chunk"


def test_state_survives_across_emitters():
    """The whole point: a `done` recorded by one source must be visible to the
    others. Before this, each kept its own map and the guard had blind spots."""
    reset()
    # as if from _translate_events reading SFN history
    emit("encode:h264:396p:chunk2", "done", 100.0)
    # as if from _drain_telemetry, a stale worker marker arriving later
    ok, _ = emit("encode:h264:396p:chunk2", "running", 18.4)
    assert not ok, "a done from one emitter was invisible to another"


def test_state_channel_names_fit_the_tighter_limit():
    """Both names are built from ONE core sized to the EventBridge limit (64),
    which is tighter than SQS's 80 — so they can never drift apart."""
    longest = "j" * 60 + "-abc123"
    rule, queue = cli_batch._state_names(longest)
    assert rule == queue, "rule and queue names diverged"
    assert len(rule) <= cli_batch._EB_RULE_NAME_MAX, f"{len(rule)} chars: {rule}"
    # two executions of the SAME job differ only in the trailing suffix
    a, _ = cli_batch._state_names("j" * 60 + "-aaaaaa")
    b, _ = cli_batch._state_names("j" * 60 + "-bbbbbb")
    assert a != b, "trimming collapsed two executions onto one rule"


def test_every_batch_status_maps_to_a_stage_status():
    """A status with no mapping is silently ignored by the drain, which would
    show as cells that never leave queued. Pin the full set Batch emits."""
    for st in ("SUBMITTED", "RUNNABLE", "STARTING", "RUNNING", "SUCCEEDED", "FAILED"):
        assert st in cli_batch._EVENT_STAGE_STATUS, f"{st} has no stage mapping"
    m = cli_batch._EVENT_STAGE_STATUS
    assert m["RUNNABLE"] == "queued" and m["STARTING"] == "starting"
    assert m["RUNNING"] == "running" and m["SUCCEEDED"] == "done"


def test_events_cannot_walk_a_chunk_back_either():
    """EventBridge is at-least-once AND unordered, so the event path needs the
    same guard as every other channel — not a weaker one."""
    reset()
    emit("encode:h264:1440p:chunk3", "done", 100.0)
    # a RUNNING event redelivered after the SUCCEEDED one
    ok, _ = emit("encode:h264:1440p:chunk3", "running", 0.0)
    assert not ok, "an out-of-order Batch event un-finished a chunk"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001 — reporting, not handling
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    reset()
    if failed:
        print(f"{failed} of {len(tests)} stage-state tests failed")
        return 1
    print(f"stage state: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
