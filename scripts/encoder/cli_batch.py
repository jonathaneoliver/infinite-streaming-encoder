"""Submit + poll an encode execution on the AWS Step Functions state
machine. Invoked by the Go server's `cloud-batch` target the same way
it invokes cli_cloud.py for the legacy spot target:

  python -m encoder.cli_batch submit \\
    --state-machine-arn ARN --input-json FILE
  # prints the execution ARN on stdout

  python -m encoder.cli_batch poll \\
    --execution-arn ARN --s3-prefix URI --local-dir PATH
  # blocks until terminal state; emits ENCODER-STAGE markers as
  # per-step events land in GetExecutionHistory

We split submit + poll so the Go manager can stash the execution ARN
in its job state (for reconcile after server restart) and re-invoke
poll against that ARN.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore


def _sfn():
    if boto3 is None:
        raise RuntimeError("boto3 required for cli_batch")
    return boto3.client("stepfunctions")


def _s3():
    return boto3.client("s3")


# Step Function step name → ENCODER-STAGE key. These match the labels
# the legacy cloud target already emits, so the existing UI works
# unchanged.
_STEP_TO_STAGE: dict[str, str] = {
    "Mezzanine": "mezzanine",
    "Audio": "audio",
    "PackageH264": "package:h264",
    "HlsH264": "hls:h264",
    "ByterangesH264": "fragments:h264",
    "PackageHevc": "package:hevc",
    "HlsHevc": "hls:hevc",
    "ByterangesHevc": "fragments:hevc",
}


def _emit_plan() -> None:
    """Emit the same plan the Go UI already renders for cloud jobs.
    Variant stages are emitted dynamically as Map iterations start
    (we don't know the exact variant list here — the Step Function's
    input has it)."""
    stages = [
        {"key": k, "label": k.replace(":", " ")}
        for k in [
            "mezzanine", "audio",
            "package:h264", "hls:h264", "fragments:h264",
            "package:hevc", "hls:hevc", "fragments:hevc",
        ]
    ]
    print(f"[[ENCODER-PLAN {json.dumps(stages)}]]", flush=True)


def _emit_stage(key: str, status: str, percent: float = 0.0) -> None:
    print(f"[[ENCODER-STAGE key={key} status={status} percent={percent:.1f}]]",
          flush=True)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def cmd_submit(args: argparse.Namespace) -> int:
    sfn = _sfn()
    with open(args.input_json) as f:
        input_doc = f.read()
    resp = sfn.start_execution(
        stateMachineArn=args.state_machine_arn,
        input=input_doc,
    )
    print(resp["executionArn"], flush=True)
    return 0


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------

def _translate_events(events: list[dict], seen: set[int]) -> None:
    """Translate new Step Functions history events into ENCODER-STAGE
    markers. Keeps track of event ids we've already emitted against
    so repeat polls don't double-emit.

    Event kinds we care about:
      TaskStateEntered  → step started → emit running 0%
      TaskSucceeded     → emit done 100%
      TaskFailed        → emit failed 0%
      MapStateEntered / MapIterationStarted → each variant becomes
        encode:<codec>:<tier>. The iteration's parameters contain
        codec + tier.
    """
    for ev in events:
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        etype = ev["type"]

        if etype == "TaskStateEntered":
            step = ev.get("stateEnteredEventDetails", {}).get("name", "")
            key = _STEP_TO_STAGE.get(step)
            if key:
                _emit_stage(key, "running", 0.0)

        elif etype in ("TaskSucceeded", "TaskStateExited"):
            step = ev.get("stateExitedEventDetails", {}).get("name", "")
            key = _STEP_TO_STAGE.get(step)
            if key:
                _emit_stage(key, "done", 100.0)

        elif etype == "TaskFailed":
            # Task-type events don't always carry stateEnteredEventDetails;
            # fall back to the most recent task-state we saw (best effort).
            fail = ev.get("taskFailedEventDetails", {})
            cause = fail.get("cause", "")[:200]
            print(f"!!! Step failure: {cause}", file=sys.stderr, flush=True)

        elif etype == "MapIterationStarted":
            params = ev.get("mapIterationStartedEventDetails", {})
            index = params.get("index")
            # The actual variant codec/tier lives in the iteration's
            # input (MapRunStarted or TaskStateEntered for EncodeVariant);
            # not directly on this event. We'll catch the inner task.

        elif etype == "TaskStateEntered" and \
                ev.get("stateEnteredEventDetails", {}).get("name") == "EncodeVariant":
            inp = ev["stateEnteredEventDetails"].get("input", "{}")
            try:
                data = json.loads(inp)
                codec = data.get("codec"); tier = data.get("tier")
                if codec and tier:
                    _emit_stage(f"encode:{codec}:{tier}", "running", 0.0)
            except Exception:
                pass


def _download_outputs(s3_prefix: str, local_dir: Path) -> int:
    """Mirror s3://.../<prefix>/output_*/ into local_dir. The state
    machine writes each codec's packaged dir as output_<codec>/."""
    if not s3_prefix.startswith("s3://"):
        return 0
    rest = s3_prefix[len("s3://"):].rstrip("/")
    bucket, _, base_key = rest.partition("/")
    s3 = _s3()
    local_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{base_key}/output_"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(base_key) + 1:]
            dst = local_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dst))
            count += 1
    return count


def cmd_poll(args: argparse.Namespace) -> int:
    sfn = _sfn()
    _emit_plan()

    seen: set[int] = set()
    interval_s = int(os.environ.get("BATCH_POLL_INTERVAL_S", "5"))
    timeout_s = int(os.environ.get("BATCH_POLL_TIMEOUT_S", "14400"))  # 4h ceiling

    print(f">>> Polling execution {args.execution_arn}", flush=True)
    elapsed = 0
    while elapsed < timeout_s:
        # Stream new history events first so the UI gets per-step
        # updates even on the terminal tick.
        hist = sfn.get_execution_history(
            executionArn=args.execution_arn, maxResults=200, reverseOrder=False,
        )
        _translate_events(hist.get("events", []), seen)

        desc = sfn.describe_execution(executionArn=args.execution_arn)
        status = desc["status"]

        if status in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            if status == "SUCCEEDED":
                print(f"    downloading outputs from {args.s3_prefix}", flush=True)
                n = _download_outputs(args.s3_prefix, Path(args.local_dir))
                print(f"    downloaded {n} files", flush=True)
                return 0
            print(f"!!! execution ended with status {status}: "
                  f"{desc.get('cause', '')}", file=sys.stderr)
            return 2

        time.sleep(interval_s)
        elapsed += interval_s

    print("!!! poll timed out", file=sys.stderr)
    return 3


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="encoder.cli_batch")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("submit")
    ps.add_argument("--state-machine-arn", required=True, dest="state_machine_arn")
    ps.add_argument("--input-json", required=True, dest="input_json")
    ps.set_defaults(fn=cmd_submit)

    pp = sub.add_parser("poll")
    pp.add_argument("--execution-arn", required=True, dest="execution_arn")
    pp.add_argument("--s3-prefix", required=True, dest="s3_prefix")
    pp.add_argument("--local-dir", required=True, dest="local_dir")
    pp.set_defaults(fn=cmd_poll)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
