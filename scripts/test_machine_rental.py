"""Tests for the machine-rental report (#195).

The report runs once, at the very end of a cloud run, and degrades silently on
any exception — which is right for a finished encode and terrible for
confidence. So the arithmetic is exercised here against stubbed AWS rather than
discovered after a twenty-minute run.

Run directly or via `make check`.
"""
from __future__ import annotations

import datetime
import io
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from infinite_streaming_encoder import cli_batch
except ModuleNotFoundError as e:  # pragma: no cover — dependency absent
    if "boto3" in str(e) or "botocore" in str(e):
        print(f"test_machine_rental: skipped (dependency absent: {e})")
        raise SystemExit(0)
    raise


def _stub_inventory(vcpu_by_type):
    """Stand in for cloud.inventory, which needs botocore."""
    mod = types.ModuleType("infinite_streaming_encoder.cloud.inventory")
    mod._vcpus_for_type = lambda t: vcpu_by_type.get(t, 0)
    sys.modules["infinite_streaming_encoder.cloud.inventory"] = mod


class FakeEC2:
    def __init__(self, instances):
        self._instances = instances

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [
            i for i in self._instances if i["InstanceId"] in InstanceIds]}]}


def run_report(jobs, instances, vcpu_by_type, job_vcpu=2.0):
    _stub_inventory(vcpu_by_type)
    orig = (cli_batch._ec2, cli_batch._ec2_for_container_instance,
            cli_batch._job_vcpu)
    cli_batch._ec2 = lambda: FakeEC2(instances)
    # arn "...:ci/i-xxxx" -> "i-xxxx"
    cli_batch._ec2_for_container_instance = lambda arn: arn.rsplit("/", 1)[-1]
    cli_batch._job_vcpu = lambda j: job_vcpu
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            cli_batch._emit_machine_rental("exec-test", jobs)
    finally:
        (cli_batch._ec2, cli_batch._ec2_for_container_instance,
         cli_batch._job_vcpu) = orig
    return buf.getvalue()


def job(iid, start_s, stop_s):
    return {"container": {"containerInstanceArn": f"arn:aws:ecs:r:a:ci/{iid}"},
            "startedAt": start_s * 1000, "stoppedAt": stop_s * 1000}


def inst(iid, itype, launch_s, term_s=None, state="terminated"):
    d = {"InstanceId": iid, "InstanceType": itype, "State": {"Name": state},
         "LaunchTime": datetime.datetime.fromtimestamp(
             launch_s, datetime.timezone.utc)}
    if term_s is not None:
        t = datetime.datetime.fromtimestamp(term_s, datetime.timezone.utc)
        d["StateTransitionReason"] = (
            "User initiated (" + t.strftime("%Y-%m-%d %H:%M:%S") + " GMT)")
    return d


def test_idle_before_and_after_are_measured():
    """The whole point: boot/pull before the first chunk, and the tail before
    scale-down, are what a job-derived figure cannot see."""
    out = run_report(
        jobs=[job("i-aaa", 1000, 1100)],
        instances=[inst("i-aaa", "c7g.2xlarge", launch_s=900, term_s=1200)],
        vcpu_by_type={"c7g.2xlarge": 8})
    assert "i-aaa" in out, out
    assert "300s" in out, f"lifetime 300s not reported: {out}"
    assert "100s" in out, f"idle-before (1000-900) not reported: {out}"
    # busy 100s of a 300s life
    assert "33%" in out, f"utilisation not 33%: {out}"


def test_machine_hours_exceed_allocated():
    """machine vCPU-hours must count the INSTANCE, allocated counts the job."""
    out = run_report(
        jobs=[job("i-bbb", 1000, 1100)],
        instances=[inst("i-bbb", "c7g.2xlarge", launch_s=900, term_s=1200)],
        vcpu_by_type={"c7g.2xlarge": 8}, job_vcpu=2.0)
    # machine: 300s x 8 vCPU = 2400 vCPU-s = 0.667 h
    # allocated: 100s x 2 vCPU = 200 vCPU-s = 0.056 h
    assert "0.67" in out, f"machine vCPU-hours wrong: {out}"
    assert "0.06" in out, f"allocated vCPU-hours wrong: {out}"
    assert "92%" in out, f"unallocated share wrong (expect ~92%): {out}"
    assert "ENCODER-MACHINES" in out


def test_a_short_lived_instance_is_visible():
    """The 129s instance that encoded almost nothing — the case that motivated
    this. It must stand out rather than average away."""
    out = run_report(
        jobs=[job("i-short", 1005, 1015), job("i-long", 1000, 1900)],
        instances=[inst("i-short", "c7g.2xlarge", launch_s=1000, term_s=1129),
                   inst("i-long", "c7g.2xlarge", launch_s=990, term_s=1950)],
        vcpu_by_type={"c7g.2xlarge": 8})
    short = [l for l in out.splitlines() if "i-short" in l][0]
    assert "129s" in short, f"short lifetime not shown: {short}"
    assert "  8%" in short or " 8%" in short, f"low utilisation not shown: {short}"


def test_a_still_running_instance_is_marked_not_dropped():
    """No termination time means the lifetime is a lower bound. Say so rather
    than silently omitting the instance or pretending the number is final."""
    out = run_report(
        jobs=[job("i-alive", 1000, 1100)],
        instances=[inst("i-alive", "c8g.large", launch_s=900,
                        term_s=None, state="running")],
        vcpu_by_type={"c8g.large": 2})
    assert "i-alive" in out, f"a live instance was dropped: {out}"
    assert "~" in out, f"live instance not flagged: {out}"


def test_no_instances_reports_nothing_rather_than_zeroes():
    """A run with no resolvable hosts must stay silent — printing 0.00 vCPU-hours
    would read as 'nothing was rented', which is a different claim."""
    out = run_report(jobs=[], instances=[], vcpu_by_type={})
    assert out.strip() == "", f"expected silence, got: {out}"


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
        print(f"{failed} of {len(tests)} machine-rental tests failed")
        return 1
    print(f"machine rental: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
