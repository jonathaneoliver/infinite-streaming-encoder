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

def _chunk_identity(input_json: str) -> tuple[str, str, int] | None:
    """(codec, tier, chunk_index) from an EncodeChunk state's input, or None."""
    try:
        d = json.loads(input_json)
        codec, tier, ci = d.get("codec"), d.get("tier"), d.get("chunk_index")
        if codec and tier and ci is not None:
            return codec, tier, int(ci)
    except (ValueError, TypeError):
        pass
    return None


def _concat_identity(input_json: str) -> tuple[str, str] | None:
    """(codec, tier) from a ConcatVariant state's input, or None."""
    try:
        d = json.loads(input_json)
        codec, tier = d.get("codec"), d.get("tier")
        if codec and tier:
            return codec, tier
    except (ValueError, TypeError):
        pass
    return None


def _translate_events(events: list[dict], seen: set[int]) -> None:
    """Translate Step Functions history events into ENCODER-STAGE markers.
    `seen` tracks already-emitted event ids so repeat polls don't double-emit.

    Fixed phases (Mezzanine / Audio / Package* / Hls* / Byteranges*) map by
    state name via _STEP_TO_STAGE. The variant fan-out is a nested Map — an
    inner Map of EncodeChunk tasks per (codec, tier), then a ConcatVariant —
    so each chunk becomes `encode:<codec>:<tier>:chunk<N>` (which the UI groups
    into the per-variant chunk grid) and the join becomes `concat:<codec>:<tier>`.

    A state's codec/tier/chunk_index live in its *entered* event's input; the
    *exited* event carries no input, so we index every EncodeChunk/ConcatVariant
    enter by event id and, for an exit, walk previousEventId back to its enter.
    The full history is passed each poll, so those maps are always complete.
    """
    by_id = {e["id"]: e for e in events}

    # Index enter events (they carry the input with codec/tier/chunk_index).
    chunk_enter: dict[int, tuple[str, str, int]] = {}
    concat_enter: dict[int, tuple[str, str]] = {}
    for e in events:
        if e["type"] != "TaskStateEntered":
            continue
        det = e.get("stateEnteredEventDetails", {})
        name, inp = det.get("name", ""), det.get("input", "{}")
        if name == "EncodeChunk":
            idn = _chunk_identity(inp)
            if idn:
                chunk_enter[e["id"]] = idn
        elif name == "ConcatVariant":
            idn = _concat_identity(inp)
            if idn:
                concat_enter[e["id"]] = idn

    def _enter_of(exit_ev: dict, table: dict) -> tuple | None:
        """Follow previousEventId from an exit event back to its matching
        enter (whose identity is in `table`). Bounded walk."""
        cur = exit_ev
        for _ in range(64):
            pid = cur.get("previousEventId")
            if pid is None or pid not in by_id:
                return None
            if pid in table:
                return table[pid]
            cur = by_id[pid]
        return None

    for ev in events:
        if ev["id"] in seen:
            continue
        seen.add(ev["id"])
        etype = ev["type"]

        if etype == "TaskStateEntered":
            name = ev.get("stateEnteredEventDetails", {}).get("name", "")
            key = _STEP_TO_STAGE.get(name)
            if key:
                _emit_stage(key, "running", 0.0)
            elif name == "EncodeChunk":
                idn = chunk_enter.get(ev["id"])
                if idn:
                    c, t, ci = idn
                    _emit_stage(f"encode:{c}:{t}:chunk{ci}", "running", 0.0)
            elif name == "ConcatVariant":
                idn = concat_enter.get(ev["id"])
                if idn:
                    c, t = idn
                    _emit_stage(f"concat:{c}:{t}", "running", 0.0)

        elif etype == "TaskStateExited":
            name = ev.get("stateExitedEventDetails", {}).get("name", "")
            key = _STEP_TO_STAGE.get(name)
            if key:
                _emit_stage(key, "done", 100.0)
            elif name == "EncodeChunk":
                idn = _enter_of(ev, chunk_enter)
                if idn:
                    c, t, ci = idn
                    _emit_stage(f"encode:{c}:{t}:chunk{ci}", "done", 100.0)
            elif name == "ConcatVariant":
                idn = _enter_of(ev, concat_enter)
                if idn:
                    c, t = idn
                    _emit_stage(f"concat:{c}:{t}", "done", 100.0)

        elif etype == "TaskFailed":
            cause = ev.get("taskFailedEventDetails", {}).get("cause", "")[:200]
            print(f"!!! Step failure: {cause}", file=sys.stderr, flush=True)


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
        # Stream new history events first so the UI gets per-step updates even
        # on the terminal tick. Paginate the full history — a chunked job has
        # far more than one page of events (12 variants x N chunks x several
        # events each), and _translate_events needs them all to correlate each
        # chunk's exit back to its enter.
        events: list[dict] = []
        token: str | None = None
        while True:
            kwargs = dict(executionArn=args.execution_arn, maxResults=1000,
                          reverseOrder=False)
            if token:
                kwargs["nextToken"] = token
            hist = sfn.get_execution_history(**kwargs)
            events.extend(hist.get("events", []))
            token = hist.get("nextToken")
            if not token:
                break
        _translate_events(events, seen)

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
