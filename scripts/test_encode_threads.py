#!/usr/bin/env python3
"""ENCODE_THREADS must reach EVERY codec (#183).

The bug was silent: the av1 branch emitted neither `-threads` nor `lp=`, so
building the argv with threads=2 and threads=8 produced a byte-identical command
while h264 and hevc both changed. SVT-AV1 then sized its own pool from the cores
it could see — measured at ~4.2 threads per encode against the 2 that
`_default_slots` (physical/2) assumes, putting 22 cores of load on a fleet whose
perf-core budget is 16.

Run: python3 scripts/test_encode_threads.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.encode_variants import _codec_specific_args

CODECS = ("h264", "hevc", "av1")


def args_for(codec, threads):
    return _codec_specific_args(codec, 5000, 48, "medium", threads_override=threads)


def test_every_codec_responds_to_the_thread_budget():
    """The regression itself: changing the budget must change the argv."""
    for codec in CODECS:
        two, eight = args_for(codec, 2), args_for(codec, 8)
        assert two != eight, (
            f"{codec}: threads=2 and threads=8 produce identical argv — the "
            f"declared budget does not reach this encoder")
    print("ok  every codec's argv responds to the thread budget")


def test_av1_carries_the_budget_as_lp():
    """av1 specifically, and in SVT's own knob rather than -threads."""
    argv = args_for("av1", 2)
    params = argv[argv.index("-svtav1-params") + 1]
    assert "lp=2" in params, f"expected lp=2 in {params!r}"

    argv8 = args_for("av1", 8)
    params8 = argv8[argv8.index("-svtav1-params") + 1]
    assert "lp=8" in params8, f"expected lp=8 in {params8!r}"

    # -threads is deliberately NOT set for av1: SVT runs its own pool and
    # ignores it, so emitting it would imply a control that does not exist.
    assert "-threads" not in argv, "av1 must not claim to honour -threads"
    print("ok  av1 carries the budget as lp=, and does not fake -threads")


def test_unset_budget_keeps_the_previous_behaviour():
    """lp=0 is SVT's auto — an unset budget must not start constraining av1.

    Guards the deployment where ENCODE_THREADS is unset and there is no cgroup
    quota (a single sequential local encode), which should still use the box.
    """
    argv = _codec_specific_args("av1", 5000, 48, "medium", threads_override=None)
    params = argv[argv.index("-svtav1-params") + 1]
    assert "lp=0" in params, f"expected lp=0 (auto) in {params!r}"
    print("ok  an unset budget leaves av1 on SVT's auto pool sizing")


def test_the_keyint_contract_survives():
    """lp rides alongside the GOP params; it must not displace them.

    keyint/scd are what make chunk boundaries line up between codecs, so a
    formatting slip here breaks far more than threading.
    """
    for threads in (None, 2, 8):
        argv = _codec_specific_args("av1", 5000, 48, "medium",
                                    threads_override=threads)
        params = argv[argv.index("-svtav1-params") + 1]
        assert "keyint=48" in params, f"keyint lost at threads={threads}: {params!r}"
        assert "scd=0" in params, f"scd lost at threads={threads}: {params!r}"
    print("ok  keyint/scd survive alongside lp at every budget")


if __name__ == "__main__":
    test_every_codec_responds_to_the_thread_budget()
    test_av1_carries_the_budget_as_lp()
    test_unset_budget_keeps_the_previous_behaviour()
    test_the_keyint_contract_survives()
    print("PASS")
