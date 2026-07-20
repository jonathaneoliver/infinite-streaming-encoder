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
import re
import sys
import time
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore


def _region() -> str | None:
    """Target region from the environment. Passed explicitly to every client
    because the server container mounts ~/.aws (whose default region may point
    elsewhere) and botocore's default resolution can prefer that config file
    over the AWS_REGION env var — which then rejects a state-machine ARN in a
    different region."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def _sfn():
    if boto3 is None:
        raise RuntimeError("boto3 required for cli_batch")
    return boto3.client("stepfunctions", region_name=_region())


def _s3():
    return boto3.client("s3", region_name=_region())


def _logs():
    return boto3.client("logs", region_name=_region())


def _batch():
    return boto3.client("batch", region_name=_region())


# var-<codec>-<tier>-c<N>-<execName> — the chunk job name from the SFN template.
_CHUNK_JOBNAME_RE = re.compile(r"^var-([^-]+)-([^-]+)-c(\d+)-")
# var-<codec>-<tier>-whole-<execName> — the whole-variant (single-chunk) job.
_WHOLE_JOBNAME_RE = re.compile(r"^var-([^-]+)-([^-]+)-whole-")


def _reflect_batch_status(exec_name: str) -> None:
    """Reflect each chunk job's actual Batch status into its grid cell, so a
    cell shows 'queued' (waiting for a slot) vs 'running' (actually encoding).
    The SFN state-enter event only says 'submitted', which spans RUNNABLE →
    STARTING → RUNNING — so we ask Batch directly. Done chunks are SUCCEEDED
    and won't appear in these lists, so their 'done' state is left intact."""
    batch = _batch()
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")

    def _emit_for(status_filter: str, stage_status: str) -> None:
        try:
            jobs = batch.list_jobs(jobQueue=queue, jobStatus=status_filter
                                   ).get("jobSummaryList", [])
        except ClientError:
            return
        for j in jobs:
            name = j.get("jobName", "")
            if exec_name not in name:
                continue
            m = _CHUNK_JOBNAME_RE.match(name)
            if m:
                c, t, ci = m.group(1), m.group(2), int(m.group(3))
                _emit_stage(f"encode:{c}:{t}:chunk{ci}", stage_status, 0.0)
                continue
            w = _WHOLE_JOBNAME_RE.match(name)
            if w:
                _emit_stage(f"encode:{w.group(1)}:{w.group(2)}", stage_status, 0.0)

    # RUNNABLE -> queued, STARTING -> running (0%). RUNNING is deliberately NOT
    # reflected here: once the container is up it emits its own ENCODER-STAGE
    # percent markers (now forwarded live), and re-stamping running/0% every
    # poll would reset that smooth progress back to zero.
    _emit_for("RUNNABLE", "queued")
    _emit_for("STARTING", "running")


# CloudWatch log group the Batch job definitions write to (see
# infra/terraform/modules/jobs/main.tf).
_BATCH_LOG_GROUP = "/aws/batch/encoder"

# Only these lines from a running container are worth surfacing live — the
# throttled S3 transfer bars (_Xfer in cli_phase) and the per-phase "[phase X]
# doing Y" lines. Everything else (raw ffmpeg/Shaka spew) stays in CloudWatch.
_PROGRESS_RE = re.compile(r"\[(?:progress|phase)\b")


def _short_label(jobname: str) -> str:
    """Friendly label for a Batch job name in forwarded progress lines."""
    m = _CHUNK_JOBNAME_RE.match(jobname)
    if m:
        return f"{m.group(1)} {m.group(2)} chunk{m.group(3)}"
    w = _WHOLE_JOBNAME_RE.match(jobname)
    if w:
        return f"{w.group(1)} {w.group(2)}"
    return {"mezz": "mezzanine", "audio": "audio", "pkg": "package",
            "hls": "hls", "br": "fragments", "concat": "concat",
            }.get(jobname.split("-", 1)[0], jobname.split("-", 1)[0])


def _forward_running_logs(exec_name: str, log_state: dict) -> None:
    """Tail the CloudWatch streams of currently-RUNNING jobs and forward their
    [progress]/[phase] lines into the app log — so long phases (mezzanine,
    packaging, big S3 transfers) show live activity instead of a dark gap
    between 'submitted' and 'done'. Best-effort; never breaks polling."""
    batch = _batch()
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    try:
        running = batch.list_jobs(jobQueue=queue, jobStatus="RUNNING"
                                  ).get("jobSummaryList", [])
    except ClientError:
        return
    ids = [j["jobId"] for j in running if exec_name in j.get("jobName", "")]
    for i in range(0, len(ids), 100):
        try:
            jobs = batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", [])
        except ClientError:
            continue
        for j in jobs:
            stream = j.get("container", {}).get("logStreamName")
            if stream:
                _tail_progress(stream, _short_label(j.get("jobName", "")), log_state)


def _tail_progress(stream: str, label: str, log_state: dict) -> None:
    """Forward new progress-marked lines from one running container's stream.
    Tracks the last-seen timestamp per stream so each line is emitted once."""
    since = log_state.get(stream, 0)
    try:
        events = _logs().get_log_events(
            logGroupName=_BATCH_LOG_GROUP, logStreamName=stream,
            startTime=since + 1 if since else 0, startFromHead=True,
        ).get("events", [])
    except ClientError:
        return
    for e in events:
        log_state[stream] = max(log_state.get(stream, 0), e.get("timestamp", 0))
        msg = e.get("message", "").rstrip()
        if (msg.startswith("[[ENCODER-BOOT ") or msg.startswith("[[ENCODER-STAGE ")
                or msg.startswith("[[ENCODER-SPEED ")):
            # Verbatim so the Go scanner parses it — ENCODER-STAGE carries the
            # live ffmpeg % for this chunk/variant, driving the progress bars;
            # ENCODER-SPEED feeds the control plane's learned dynamic-chunk model.
            print(msg, flush=True)
        elif _PROGRESS_RE.search(msg):
            _narrate(f"{label}: {msg}")


def _report_task_failure(details: dict) -> None:
    """Surface a Batch task failure in the JOB LOG (stdout), not just stderr —
    including the container's exit code and the tail of its CloudWatch log, so
    the app shows *why* a phase failed instead of an opaque 'TaskFailed'.

    The cause of a batch:submitJob.sync failure is the full DescribeJobs JSON,
    which carries the container's ExitCode + LogStreamName."""
    cause = details.get("cause", "")
    error = details.get("error", "")
    exit_code = reason = stream = None
    try:
        job = json.loads(cause)
        container = job.get("Container", {})
        exit_code = container.get("ExitCode")
        reason = job.get("StatusReason") or container.get("Reason")
        stream = container.get("LogStreamName")
        name = job.get("JobName", "?")
    except (ValueError, TypeError):
        name = "?"

    print(f"!!! phase failed [{name}] error={error} "
          f"exit={exit_code} reason={reason or cause[:160]}", flush=True)

    if not stream:
        return
    # Pull the tail of the container's own log so the real error is visible.
    try:
        events = _logs().get_log_events(
            logGroupName=_BATCH_LOG_GROUP, logStreamName=stream,
            limit=25, startFromHead=False,
        ).get("events", [])
        if events:
            print(f"    --- {stream} (last {len(events)} lines) ---", flush=True)
            for e in events:
                print(f"    {e.get('message', '').rstrip()}", flush=True)
    except ClientError as e:
        print(f"    (could not fetch container log: {e})", flush=True)


# Step Function step name → ENCODER-STAGE key, for the phases that are their
# own SFN step. The packaging phases (package / fragments / hls) are now a single
# per-codec package-all job; their sub-stages arrive via live log tailing of the
# running container (_tail_progress), not from history.
_STEP_TO_STAGE: dict[str, str] = {
    "Mezzanine": "mezzanine",
    "Audio": "audio",
}


def _emit_plan(do_h264: bool = True, do_hevc: bool = True) -> None:
    """Announce the pipeline stages for THIS run. Only the codecs actually being
    encoded are listed, so a h264-only run doesn't render empty hevc rows.
    Package order is package -> fragments -> hls (the LL-HLS playlists embed the
    fragment byteranges, so hls must run last). The trailing download:outputs
    stage is the driver's own S3 -> local sync-back of the finished package.
    Variant encode stages are emitted dynamically as Map iterations start."""
    keys = ["mezzanine", "audio"]
    for codec, on in (("h264", do_h264), ("hevc", do_hevc)):
        if on:
            keys += [f"package:{codec}", f"fragments:{codec}", f"hls:{codec}"]
    keys.append("download:outputs")
    stages = [{"key": k, "label": k.replace(":", " ")} for k in keys]
    print(f"[[ENCODER-PLAN {json.dumps(stages)}]]", flush=True)


def _emit_stage(key: str, status: str, percent: float = 0.0) -> None:
    print(f"[[ENCODER-STAGE key={key} status={status} percent={percent:.1f}]]",
          flush=True)


def _narrate(msg: str) -> None:
    """A plain (non-marker) line for the job log — the Go server appends these
    verbatim, so they narrate the pipeline in the app's 'Show log'."""
    print(msg, flush=True)


def _report_reclaims(exit_ev: dict, label: str) -> None:
    """Surface spot interruptions in the job log. A reclaim fails a Batch job
    attempt with a 'Host EC2 instance was terminated' reason and the retry
    strategy re-runs it on fresh capacity — all transparent under
    submitJob.sync, so this is the ONLY place a reclaim becomes visible. The
    completed job's Attempts[] (carried in the exit event output) records each
    interrupted attempt."""
    try:
        out = exit_ev.get("stateExitedEventDetails", {}).get("output", "")
        attempts = json.loads(out).get("Attempts", [])
    except (ValueError, TypeError):
        return
    n = 0
    for a in attempts:
        reason = str(a.get("StatusReason", "") or
                     a.get("Container", {}).get("Reason", ""))
        if reason.startswith("Host EC2") or "was terminated" in reason.lower():
            n += 1
    if n:
        _narrate(f"⚠ {label}: spot-reclaimed {n}x, retried on fresh capacity "
                 f"(resumed the chunk, not the whole variant)")


def _forward_container_timing(exit_ev: dict) -> None:
    """Pull the container's [timing] line (fetch/encode/upload) from CloudWatch
    for a just-completed chunk and add it to the job log, so the per-chunk
    breakdown shows up live as chunks finish."""
    try:
        out = exit_ev.get("stateExitedEventDetails", {}).get("output", "")
        stream = json.loads(out).get("Container", {}).get("LogStreamName")
    except (ValueError, TypeError):
        stream = None
    if not stream:
        return
    try:
        events = _logs().get_log_events(
            logGroupName=_BATCH_LOG_GROUP, logStreamName=stream,
            limit=100, startFromHead=False,
        ).get("events", [])
    except ClientError:
        return
    for e in reversed(events):
        msg = e.get("message", "")
        if msg.lstrip().startswith("[timing]"):
            _narrate("    " + msg.strip())
            return


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def _execution_name_base(input_doc: str) -> str | None:
    """The stable per-job prefix ({jobid}-{stem}) from the S3 job prefix,
    sanitized to [A-Za-z0-9_-]. Every execution for a given job shares this
    prefix (a short random suffix makes each one unique), so it doubles as a
    filter for finding this job's prior executions."""
    import re
    try:
        prefix = json.loads(input_doc).get("s3_prefix", "")
    except (ValueError, TypeError):
        return None
    base = re.sub(r"[^A-Za-z0-9_-]", "_",
                  prefix.rstrip("/").rsplit("/", 1)[-1])[:60].strip("_-")
    return base or None


def _execution_name(input_doc: str) -> str | None:
    """A readable, unique execution name: {jobid}-{stem}-{rand6}. Flows into the
    Batch job names (which the app parses phase-first, so the tail is ignored)."""
    import uuid
    base = _execution_name_base(input_doc)
    return f"{base}-{uuid.uuid4().hex[:6]}" if base else None


def _stop_prior_executions(sfn, sm_arn: str, name_prefix: str) -> None:
    """Abort any still-RUNNING executions for this job (same {jobid}-{stem}
    prefix) before starting a new one. Without this, a driver killed mid-run
    (a server restart kills the cli_batch subprocess without stopping the SFN
    execution) leaves the execution running orphaned, and the re-submitted job
    starts a second one — piling up duplicate executions that all keep spending
    on Batch capacity. Best-effort."""
    try:
        paginator = sfn.get_paginator("list_executions")
        for page in paginator.paginate(stateMachineArn=sm_arn, statusFilter="RUNNING"):
            for e in page.get("executions", []):
                if e["name"].startswith(name_prefix):
                    sfn.stop_execution(
                        executionArn=e["executionArn"], error="Superseded",
                        cause="superseded by a re-submission of the same job")
    except ClientError:
        pass


def cmd_submit(args: argparse.Namespace) -> int:
    sfn = _sfn()
    with open(args.input_json) as f:
        input_doc = f.read()
    base = _execution_name_base(input_doc)
    if base:
        # Kill any orphaned/prior execution for this job first.
        _stop_prior_executions(sfn, args.state_machine_arn, base + "-")
    kwargs = {"stateMachineArn": args.state_machine_arn, "input": input_doc}
    if base:
        import uuid
        kwargs["name"] = f"{base}-{uuid.uuid4().hex[:6]}"
    resp = sfn.start_execution(**kwargs)
    print(resp["executionArn"], flush=True)
    return 0


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------

def _chunk_identity(input_json: str) -> tuple[str, str, int] | None:
    """(codec, label, chunk_index) from an EncodeChunk state's input, or None.
    `label` is the rung identity ("1080p" or "1080p_1" for an apple dup)."""
    try:
        d = json.loads(input_json)
        codec, label, ci = d.get("codec"), d.get("label"), d.get("chunk_index")
        if codec and label and ci is not None:
            return codec, label, int(ci)
    except (ValueError, TypeError):
        pass
    return None


def _whole_identity(input_json: str) -> tuple[str, str] | None:
    """(codec, label) from an EncodeWhole state's input, or None. Whole-variant
    (single-chunk) runs have no chunk_index; their stage key is the unsuffixed
    encode:<codec>:<label> that run_ffmpeg_with_progress emits on the worker."""
    try:
        d = json.loads(input_json)
        codec, label = d.get("codec"), d.get("label")
        if codec and label:
            return codec, label
    except (ValueError, TypeError):
        pass
    return None


def _translate_events(events: list[dict], seen: set[int]) -> None:
    """Translate Step Functions history events into ENCODER-STAGE markers.
    `seen` tracks already-emitted event ids so repeat polls don't double-emit.

    Mezzanine / Audio map by state name via _STEP_TO_STAGE. Each variant is
    either an EncodeWhole task (single-chunk mode -> `encode:<codec>:<tier>`) or a
    nested Map of EncodeChunk tasks (-> `encode:<codec>:<tier>:chunk<N>`, grouped
    into the per-variant chunk grid). Both get their completion from the state's
    *exit* event here — crucial for EncodeWhole, whose worker-side `done` marker
    would otherwise be missed once the job leaves RUNNING between log-tail polls
    (leaving the bar stuck at ~99%). Chunks are joined + packaged inside the
    package-all job, whose sub-stages arrive via live log tailing, not history.

    A chunk's codec/tier/chunk_index live in its *entered* event's input; the
    *exited* event carries no input, so we index every EncodeChunk enter by event
    id and, for an exit, walk previousEventId back to its enter. The full history
    is passed each poll, so the map is always complete.
    """
    by_id = {e["id"]: e for e in events}

    # Index enter events (they carry the input with codec/tier/chunk_index).
    chunk_enter: dict[int, tuple[str, str, int]] = {}
    whole_enter: dict[int, tuple[str, str]] = {}
    for e in events:
        if e["type"] != "TaskStateEntered":
            continue
        det = e.get("stateEnteredEventDetails", {})
        name, inp = det.get("name", ""), det.get("input", "{}")
        if name == "EncodeChunk":
            idn = _chunk_identity(inp)
            if idn:
                chunk_enter[e["id"]] = idn
        elif name == "EncodeWhole":
            idn = _whole_identity(inp)
            if idn:
                whole_enter[e["id"]] = idn

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
                _narrate(f"▶ {key.replace(':', ' ')} submitted")
            elif name == "EncodeChunk":
                idn = chunk_enter.get(ev["id"])
                if idn:
                    c, t, ci = idn
                    _emit_stage(f"encode:{c}:{t}:chunk{ci}", "running", 0.0)
                    _narrate(f"▶ encode {c} {t} chunk{ci} submitted")
            elif name == "EncodeWhole":
                idn = whole_enter.get(ev["id"])
                if idn:
                    c, t = idn
                    _emit_stage(f"encode:{c}:{t}", "running", 0.0)
                    _narrate(f"▶ encode {c} {t} submitted")

        elif etype == "TaskStateExited":
            name = ev.get("stateExitedEventDetails", {}).get("name", "")
            key = _STEP_TO_STAGE.get(name)
            if key:
                _emit_stage(key, "done", 100.0)
                _narrate(f"✓ {key.replace(':', ' ')} done")
                _report_reclaims(ev, key.replace(':', ' '))
            elif name == "EncodeChunk":
                idn = _enter_of(ev, chunk_enter)
                if idn:
                    c, t, ci = idn
                    _emit_stage(f"encode:{c}:{t}:chunk{ci}", "done", 100.0)
                    _narrate(f"✓ encode {c} {t} chunk{ci} done")
                    _report_reclaims(ev, f"encode {c} {t} chunk{ci}")
                    _forward_container_timing(ev)
            elif name == "EncodeWhole":
                idn = _enter_of(ev, whole_enter)
                if idn:
                    c, t = idn
                    _emit_stage(f"encode:{c}:{t}", "done", 100.0)
                    _narrate(f"✓ encode {c} {t} done")
                    _report_reclaims(ev, f"encode {c} {t}")
                    _forward_container_timing(ev)

        elif etype == "TaskFailed":
            _report_task_failure(ev.get("taskFailedEventDetails", {}))


def _download_outputs(s3_prefix: str, local_dir: Path) -> int:
    """Mirror s3://.../<prefix>/output_*/ into local_dir. The state
    machine writes each codec's packaged dir as output_<codec>/."""
    if not s3_prefix.startswith("s3://"):
        return 0
    rest = s3_prefix[len("s3://"):].rstrip("/")
    bucket, _, base_key = rest.partition("/")
    s3 = _s3()
    local_dir.mkdir(parents=True, exist_ok=True)

    # List everything first so the sync-back can drive a real progress bar
    # (weighted by bytes — segment counts vary wildly in size). This runs in the
    # driver, whose stdout is forwarded straight to the app, so the marker needs
    # no CloudWatch round-trip.
    objs: list[tuple[str, int]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{base_key}/output_"):
        for obj in page.get("Contents", []):
            objs.append((obj["Key"], obj.get("Size", 0)))

    total_bytes = sum(s for _, s in objs) or 1
    done_bytes = 0
    last_pct = -1.0
    if objs:
        _emit_stage("download:outputs", "running", 0.0)
    for key, size in objs:
        rel = key[len(base_key) + 1:]
        dst = local_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, key, str(dst))
        done_bytes += size
        pct = done_bytes / total_bytes * 100.0
        if pct - last_pct >= 1.0:  # throttle: at most ~100 markers
            _emit_stage("download:outputs", "running", pct)
            last_pct = pct
    if objs:
        _emit_stage("download:outputs", "done", 100.0)
    return len(objs)


def cmd_poll(args: argparse.Namespace) -> int:
    sfn = _sfn()
    # Read the execution input up front so the plan lists only the codecs we're
    # actually encoding (do_h264 / do_hevc from buildSFNInput).
    do_h264 = do_hevc = True
    try:
        inp = json.loads(sfn.describe_execution(
            executionArn=args.execution_arn).get("input") or "{}")
        do_h264 = bool(inp.get("do_h264", True))
        do_hevc = bool(inp.get("do_hevc", True))
    except (ClientError, ValueError, TypeError):
        pass
    _emit_plan(do_h264, do_hevc)

    seen: set[int] = set()
    log_state: dict[str, int] = {}  # stream -> last-forwarded timestamp
    exec_name = args.execution_arn.rsplit(":", 1)[-1]
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
        # Then refine chunk cells to queued/running from live Batch status
        # (runs after _translate_events so it wins over the enter-event's
        # coarse "running"). Best-effort — never let it break polling.
        try:
            _reflect_batch_status(exec_name)
        except Exception:  # noqa: BLE001 — status reflection is cosmetic
            pass
        # Forward live [progress]/[phase] lines from running containers so
        # long phases show activity, not a dark gap. Best-effort.
        try:
            _forward_running_logs(exec_name, log_state)
        except Exception:  # noqa: BLE001 — live tailing is cosmetic
            pass

        desc = sfn.describe_execution(executionArn=args.execution_arn)
        status = desc["status"]

        if status in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            if status == "SUCCEEDED":
                # Safety net: the packaging sub-stages come from live tailing;
                # if the last tail poll missed a "done" marker, force them
                # complete now so no packaging row is left stuck at "running".
                for codec, on in (("h264", do_h264), ("hevc", do_hevc)):
                    if on:
                        for k in ("package", "fragments", "hls"):
                            _emit_stage(f"{k}:{codec}", "done", 100.0)
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
