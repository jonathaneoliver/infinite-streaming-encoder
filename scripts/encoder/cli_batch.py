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


def _ecs():
    return boto3.client("ecs", region_name=_region())


# containerInstanceArn -> EC2 instance-id, resolved once per instance. The ARN
# is stable per machine, so a cache hit avoids re-describing; on missing ecs perm
# we fall back to a short slug of the ARN (still a stable per-machine key, which
# is all the chunk-plot colouring needs).
_HOST_CACHE: dict = {}


def _ec2_for_container_instance(ci_arn: str) -> str:
    """Map an ECS container-instance ARN to its EC2 instance-id (cached).
    Falls back to the last 8 chars of the ARN if ecs:DescribeContainerInstances
    isn't granted — a per-machine key is all the caller needs for colouring."""
    if not ci_arn:
        return ""
    if ci_arn in _HOST_CACHE:
        return _HOST_CACHE[ci_arn]
    # arn:aws:ecs:region:acct:container-instance/<cluster>/<id>
    slug = ci_arn.rsplit("/", 1)[-1][:8]
    resolved = slug
    try:
        parts = ci_arn.split(":container-instance/", 1)[-1].split("/")
        cluster = parts[0] if len(parts) == 2 else None
        if cluster:
            r = _ecs().describe_container_instances(
                cluster=cluster, containerInstances=[ci_arn])
            cis = r.get("containerInstances", [])
            if cis and cis[0].get("ec2InstanceId"):
                resolved = cis[0]["ec2InstanceId"]
    except (ClientError, KeyError, IndexError):
        pass
    _HOST_CACHE[ci_arn] = resolved
    return resolved


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


def _tag_hosts_for_jobs(described_jobs: list, log_state: dict) -> None:
    """Emit ENCODER-HOST for each described Batch job's stage keys, so the UI
    colours those rows/cells by the EC2 instance the job ran on. Deduped per
    (stage key, instance) via log_state, so a job is only announced once. Shared
    by the live RUNNING poll, the SUCCEEDED backfill, and the end-of-run sweep."""
    for j in described_jobs:
        keys = _host_stage_keys(j.get("jobName", ""))
        ci_arn = j.get("container", {}).get("containerInstanceArn")
        if not (keys and ci_arn):
            continue
        inst = _ec2_for_container_instance(ci_arn)
        if not inst:
            continue
        for key in keys:
            seen_key = "_host:" + key
            if log_state.get(seen_key) != inst:
                log_state[seen_key] = inst
                _emit_host(key, inst)


def _backfill_completed_hosts(exec_name: str, log_state: dict) -> None:
    """Colour chunks that completed BETWEEN polls. A short chunk (small H.264
    rung) can start and finish inside one poll interval, so it's never observed
    RUNNING and never tagged — its cell falls back to the default (blue). A
    SUCCEEDED job keeps its containerInstanceArn, so describe the ones whose stage
    keys aren't coloured yet and emit their host. Bounded: only untagged jobs are
    described (via the name, no API call), so each is described at most once."""
    batch = _batch()
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    try:
        done = batch.list_jobs(jobQueue=queue, jobStatus="SUCCEEDED"
                               ).get("jobSummaryList", [])
    except ClientError:
        return
    ids = []
    for j in done:
        name = j.get("jobName", "")
        if exec_name not in name:
            continue
        keys = _host_stage_keys(name)
        if keys and any(log_state.get("_host:" + k) is None for k in keys):
            ids.append(j["jobId"])
    for i in range(0, len(ids), 100):
        try:
            jobs = batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", [])
        except ClientError:
            continue
        _tag_hosts_for_jobs(jobs, log_state)


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
        # Colour every running job's rows (encode chunks + mezzanine / audio /
        # package / fragments / hls) by the instance it landed on. Best-effort.
        _tag_hosts_for_jobs(jobs, log_state)


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
        if msg.startswith("[[ENCODER-BOOT ") or msg.startswith("[[ENCODER-STAGE "):
            # Verbatim so the Go scanner parses it — ENCODER-STAGE carries the
            # live ffmpeg % for this chunk/variant, driving the progress bars.
            # (ENCODER-SPEED is forwarded on exit by _forward_container_timing —
            # it's emitted after the last live poll, so tailing misses it.)
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


def _emit_plan(variants: "list | None" = None,
               do_h264: bool = True, do_hevc: bool = True) -> None:
    """Announce the pipeline stages for THIS run. Only the codecs actually being
    encoded are listed, so a h264-only run doesn't render empty hevc rows.
    Package order is package -> fragments -> hls (the LL-HLS playlists embed the
    fragment byteranges, so hls must run last). The trailing download:outputs
    stage is the driver's own S3 -> local sync-back of the finished package.

    The full per-variant/per-chunk encode grid is declared up front from the SFN
    input's `variants` (each carries codec, label, chunk_indices, chunked), so a
    28-chunk 2160p variant shows all 28 cells from the start instead of the bar
    growing as Map iterations trickle in. Chunk stage keys match the running
    jobs: chunked -> encode:<codec>:<label>:chunk<i>, whole -> encode:<codec>:<label>."""
    keys = ["mezzanine", "audio"]
    for v in variants or []:
        codec, label = v.get("codec"), v.get("label")
        if not codec or not label:
            continue
        if str(v.get("chunked", "")).lower() == "true":
            for i in v.get("chunk_indices") or [0]:
                keys.append(f"encode:{codec}:{label}:chunk{i}")
        else:
            keys.append(f"encode:{codec}:{label}")
    for codec, on in (("h264", do_h264), ("hevc", do_hevc)):
        if on:
            keys += [f"package:{codec}", f"fragments:{codec}", f"hls:{codec}"]
    keys.append("download:outputs")
    seen: set = set()
    stages = []
    for k in keys:  # de-dupe, preserve order
        if k not in seen:
            seen.add(k)
            stages.append({"key": k, "label": k.replace(":", " ")})
    print(f"[[ENCODER-PLAN {json.dumps(stages)}]]", flush=True)


def _emit_stage(key: str, status: str, percent: float = 0.0) -> None:
    print(f"[[ENCODER-STAGE key={key} status={status} percent={percent:.1f}]]",
          flush=True)


def _emit_host(key: str, instance: str) -> None:
    """Report the machine a stage's Batch job landed on, for the chunk plot's
    per-instance colouring. Emitted once per (key, instance) by the log poller."""
    print(f"[[ENCODER-HOST key={key} instance={instance}]]", flush=True)


def _narrate(msg: str) -> None:
    """A plain (non-marker) line for the job log — the Go server appends these
    verbatim, so they narrate the pipeline in the app's 'Show log'."""
    print(msg, flush=True)


def _reclaim_stats(attempts) -> "tuple[int, float]":
    """(count, wasted_seconds) of spot reclaims across a Batch job's attempts.
    A reclaimed attempt failed with 'Host EC2 instance was terminated'; the wall
    time it ran before dying (StartedAt->StoppedAt) is encode work thrown away —
    the number that says how much the reclaim actually cost. The retry re-runs
    each on fresh capacity, so this is the only place a reclaim becomes visible
    under submitJob.sync."""
    n, lost = 0, 0.0
    for a in attempts or []:
        reason = str(a.get("StatusReason", "") or
                     a.get("Container", {}).get("Reason", ""))
        if reason.startswith("Host EC2") or "was terminated" in reason.lower():
            n += 1
            st, sp = a.get("StartedAt"), a.get("StoppedAt")
            if isinstance(st, (int, float)) and isinstance(sp, (int, float)) and sp > st:
                lost += (sp - st) / 1000.0
    return n, lost


def _count_reclaims(attempts) -> int:
    return _reclaim_stats(attempts)[0]


def _encode_total_s(attempts, now_ms=None) -> float:
    """Total wall-seconds across ALL of a job's attempts (successful + wasted) —
    the denominator of the reclaim-waste ratio. A still-running attempt (no
    StoppedAt) is counted up to now_ms so the live ratio isn't stuck at 100%."""
    t = 0.0
    for a in attempts or []:
        st = a.get("StartedAt")
        if not isinstance(st, (int, float)):
            continue
        sp = a.get("StoppedAt")
        end = sp if isinstance(sp, (int, float)) and sp > st else now_ms
        if isinstance(end, (int, float)) and end > st:
            t += (end - st) / 1000.0
    return t


def _var_label(job_name: str) -> str:
    """'var-h264-360p-c3-<exec>' -> 'encode h264 360p chunk3' (whole -> no
    chunk). Falls back to the raw name if it doesn't parse."""
    m = re.match(r"var-(\w+?)-(\w+?)-(whole|c(\d+))-", job_name)
    if not m:
        return job_name
    tail = f" chunk{m.group(4)}" if m.group(4) else ""
    return f"encode {m.group(1)} {m.group(2)}{tail}"


def _stage_from_jobname(job_name: str) -> "str | None":
    """'var-h264-360p-c3-<exec>' -> stage key 'encode:h264:360p:chunk3'
    ('...-whole-...' -> 'encode:h264:360p'). None if it doesn't parse."""
    m = re.match(r"var-(\w+?)-(\w+?)-(whole|c(\d+))-", job_name)
    if not m:
        return None
    return f"encode:{m.group(1)}:{m.group(2)}" + (
        f":chunk{m.group(4)}" if m.group(4) else "")


_PKGALL_JOBNAME_RE = re.compile(r"^pkgall-(\w+?)-")


def _host_stage_keys(job_name: str) -> list:
    """Stage keys a running Batch job maps to, for ENCODER-HOST colouring. A
    pkgall-<codec> job drives three per-codec rows (package/fragments/hls), all
    on the same box; mezz/audio one each; var-* the encode chunk/whole key."""
    m = _PKGALL_JOBNAME_RE.match(job_name)
    if m:
        c = m.group(1)
        return [f"package:{c}", f"fragments:{c}", f"hls:{c}"]
    head = job_name.split("-", 1)[0]
    if head == "mezz":
        return ["mezzanine"]
    if head == "audio":
        return ["audio"]
    k = _stage_from_jobname(job_name)
    return [k] if k else []


def _report_live_reclaims(exec_name: str, seen: dict) -> None:
    """Poll this execution's non-terminal encode jobs' attempts[] for spot
    reclaims. Emit ENCODER-RECLAIM (per-stage cumulative count + wasted seconds)
    on change, and flag the stage 'reclaimed' (red) while it's waiting to
    restart (RUNNABLE/STARTING after a failed attempt). Best-effort/cosmetic."""
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    batch = _batch()
    status_by_id, name_by_id, ids = {}, {}, []
    for status in ("RUNNABLE", "STARTING", "RUNNING"):
        try:
            for j in batch.list_jobs(jobQueue=queue, jobStatus=status).get("jobSummaryList", []):
                jn = j.get("jobName", "")
                if exec_name in jn and jn.startswith("var-"):
                    ids.append(j["jobId"])
                    status_by_id[j["jobId"]] = status
                    name_by_id[j["jobId"]] = jn
        except ClientError:
            return
    for i in range(0, len(ids), 100):
        try:
            jobs = batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", [])
        except ClientError:
            continue
        now_ms = time.time() * 1000.0
        for job in jobs:
            jid = job["jobId"]
            n, lost = _reclaim_stats(job.get("attempts"))
            if n == 0:
                continue
            stage = _stage_from_jobname(name_by_id.get(jid, ""))
            if not stage:
                continue
            total = _encode_total_s(job.get("attempts"), now_ms)
            if seen.get(jid) != (n, round(lost), round(total)):
                seen[jid] = (n, round(lost), round(total))
                print(f"[[ENCODER-RECLAIM key={stage} count={n} "
                      f"lost_s={lost:.1f} total_s={total:.1f}]]", flush=True)
            if status_by_id.get(jid) in ("RUNNABLE", "STARTING"):
                _emit_stage(stage, "reclaimed", 0.0)  # red until the retry runs


def _report_reclaims(exit_ev: dict, label: str) -> None:
    """Reclaims for tasks whose exit output KEEPS the Batch result (mezzanine,
    audio, package). The encode tasks set ResultPath:null — their Attempts[] is
    stripped from the exit output — so they're handled off the TaskSucceeded
    event instead (see _translate_events)."""
    try:
        attempts = json.loads(
            exit_ev.get("stateExitedEventDetails", {}).get("output", "")
        ).get("Attempts", [])
    except (ValueError, TypeError):
        return
    if _count_reclaims(attempts):
        _narrate(f"⚠ {label}: spot-reclaimed {_count_reclaims(attempts)}x, "
                 f"retried on fresh capacity")


def _forward_container_timing(stream: str | None) -> None:
    """Pull two end-of-container lines from the worker's CloudWatch stream:
    the [timing] breakdown (fetch/encode/upload) for the job log, and the
    [[ENCODER-SPEED ...]] marker that feeds the control plane's learned
    dynamic-chunk model. Both are emitted after the last live progress poll,
    so this drain is the reliable place to forward them — _tail_progress misses
    them. Driven off the TaskSucceeded event (which carries the Batch result's
    LogStreamName); the TaskStateExited output can't be used because the encode
    tasks set ResultPath:null, which strips the Container block."""
    if not stream:
        return
    try:
        events = _logs().get_log_events(
            logGroupName=_BATCH_LOG_GROUP, logStreamName=stream,
            limit=100, startFromHead=False,
        ).get("events", [])
    except ClientError:
        return
    speed_line = timing_line = None
    for e in reversed(events):
        s = e.get("message", "").lstrip()
        if speed_line is None and s.startswith("[[ENCODER-SPEED "):
            speed_line = s.rstrip()
        elif timing_line is None and s.startswith("[timing]"):
            timing_line = s.rstrip()
        if speed_line and timing_line:
            break
    # Verbatim (own line, no prefix) so the Go scanner's speedMarkerRe matches
    # and Manager.learnSpeed folds it into encode_speeds.json exactly once.
    if speed_line:
        print(speed_line, flush=True)
    if timing_line:
        _narrate("    " + timing_line)


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
                    # reclaims handled off TaskSucceeded (ResultPath:null here)
            elif name == "EncodeWhole":
                idn = _enter_of(ev, whole_enter)
                if idn:
                    c, t = idn
                    _emit_stage(f"encode:{c}:{t}", "done", 100.0)
                    _narrate(f"✓ encode {c} {t} done")

        elif etype == "TaskSucceeded":
            # The Batch result (carrying Container.LogStreamName) lives on this
            # event, NOT on TaskStateExited — the encode tasks set
            # ResultPath:null, which strips the Container block from the exited
            # output. Only variant/chunk jobs (JobName "var-*") emit an
            # [[ENCODER-SPEED]] sample + [timing] line worth draining.
            d = ev.get("taskSucceededEventDetails", {})
            try:
                out = json.loads(d.get("output", "") or "{}")
            except (ValueError, TypeError):
                out = {}
            jn = str(out.get("JobName", ""))
            if jn.startswith("var-"):
                _forward_container_timing(out.get("Container", {}).get("LogStreamName"))
                # Attempts[] lives on this event (not the ResultPath:null exit).
                # Emit the reclaim accounting for EVERY finished chunk — even
                # zero-reclaim ones — so the job's total_s denominator (all
                # encode time) is complete for the %-wasted ratio.
                atts = out.get("Attempts")
                n, lost = _reclaim_stats(atts)
                total = _encode_total_s(atts)
                stage = _stage_from_jobname(jn)
                if stage:
                    print(f"[[ENCODER-RECLAIM key={stage} count={n} "
                          f"lost_s={lost:.1f} total_s={total:.1f}]]", flush=True)
                if n:
                    _narrate(f"⚠ {_var_label(jn)}: spot-reclaimed {n}x, "
                             f"{lost / 60:.1f} min of encoding lost, retried")

        elif etype == "TaskFailed":
            _report_task_failure(ev.get("taskFailedEventDetails", {}))


def _download_outputs(s3_prefix: str, local_dir: Path, output_stem: str = "",
                      output_tag: str = "") -> int:
    """Mirror s3://.../<prefix>/output_<codec>/ into local_dir.

    The state machine writes each codec's packaged dir as output_<codec>/. When
    output_stem is given, each is downloaded to a per-codec top-level directory
    named <output_stem>_<codec>[_<output_tag>]/ (e.g. myclip_p200_hevc_xs/) —
    matching the local pipeline's naming contract (OutputStem + codec + profile
    tag appended LAST so the _p200_<codec> shape smashing keys off stays intact).
    That lets moveTmpToOutput move each codec independently, so codecs of the same
    clip COEXIST in OUTPUT_DIR instead of one replacing the other's <stem> wrapper.
    Without output_stem (legacy), the raw output_<codec>/ layout is preserved."""
    tag_suffix = f"_{output_tag}" if output_tag else ""
    if not s3_prefix.startswith("s3://"):
        return 0
    rest = s3_prefix[len("s3://"):].rstrip("/")
    bucket, _, base_key = rest.partition("/")
    s3 = _s3()
    local_dir.mkdir(parents=True, exist_ok=True)

    def _dst_for(rel: str):
        # rel = "output_<codec>/<path...>"; remap to "<stem>_<codec>/<path...>".
        if not output_stem:
            return local_dir / rel
        head, _, tail = rel.partition("/")
        if not tail or not head.startswith("output_"):
            return None  # dir marker or unexpected key — skip
        codec = head[len("output_"):]
        return local_dir / f"{output_stem}_{codec}{tag_suffix}" / tail

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
        dst = _dst_for(rel)
        if dst is None:
            done_bytes += size
            continue
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


# Blended Graviton per-vCPU-hour rates, us-west-2 (c7g/c8g .2xlarge/.4xlarge).
# cloud-batch always runs on the SPOT compute env, so every run's savings = the
# on-demand price it avoided. Estimates — good enough for "saved by spot".
_SPOT_VCPU_HR = 0.011
_ONDEMAND_VCPU_HR = 0.037


def _job_vcpu(job: dict) -> float:
    for rr in job.get("container", {}).get("resourceRequirements", []):
        if rr.get("type") == "VCPU":
            try:
                return float(rr["value"])
            except (KeyError, ValueError):
                return 0.0
    return 0.0


def _collect_exec_jobs(exec_name: str) -> list:
    """All terminal Batch jobs of one execution, fully described. Paginates
    list_jobs (a single page caps at 100 — the old cost summary silently
    dropped everything past the first page and, worse, read 0 when the terminal
    states hadn't propagated yet). Retries a few times so a just-finished run's
    jobs have settled before we sum them."""
    queue = os.environ.get("BATCH_JOB_QUEUE", "encoder-queue")
    batch = _batch()
    for attempt in range(6):
        ids = []
        for status in ("SUCCEEDED", "FAILED"):
            tok = None
            while True:
                try:
                    kw = dict(jobQueue=queue, jobStatus=status, maxResults=100)
                    if tok:
                        kw["nextToken"] = tok
                    r = batch.list_jobs(**kw)
                except ClientError:
                    break
                ids += [j["jobId"] for j in r.get("jobSummaryList", [])
                        if exec_name in j.get("jobName", "")]
                tok = r.get("nextToken")
                if not tok:
                    break
        jobs = []
        for i in range(0, len(ids), 100):
            try:
                jobs += batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", [])
            except ClientError:
                continue
        # Any job with a real runtime means the states have propagated.
        if any(isinstance(j.get("startedAt"), (int, float)) and
               isinstance(j.get("stoppedAt"), (int, float)) for j in jobs):
            return jobs
        time.sleep(2)
    return jobs


def _emit_cost_summary(exec_name: str, log_state: dict | None = None) -> None:
    """At job end, sum the execution's Batch compute and emit two markers the
    control plane consumes: ENCODER-COST (spot-vs-on-demand savings, accumulated
    into 'saved by using spot') and ENCODER-STATS (run efficiency: encode wall,
    vCPU-hours, the slowest single chunk = makespan floor, job count, max vCPUs).
    exec= keeps both idempotent across a reattach."""
    jobs = _collect_exec_jobs(exec_name)
    # Definitive host sweep: every job is fully described here (with its
    # containerInstanceArn), so colour any chunk the live poll missed — nothing
    # is left the default blue by the time the run finishes.
    _tag_hosts_for_jobs(jobs, log_state if log_state is not None else {})
    vcpu_s = 0.0
    mn = mx = None
    longest_s = 0.0
    slowest = ""
    n = 0
    for job in jobs:
        v = _job_vcpu(job)
        st, sp = job.get("startedAt"), job.get("stoppedAt")
        if v and isinstance(st, (int, float)) and isinstance(sp, (int, float)) and sp > st:
            dur = (sp - st) / 1000.0
            vcpu_s += v * dur
            n += 1
            mn = st if mn is None else min(mn, st)
            mx = sp if mx is None else max(mx, sp)
            if dur > longest_s:
                longest_s = dur
                slowest = _stage_from_jobname(job.get("jobName", "")) or job.get("jobName", "")
    vh = vcpu_s / 3600.0
    wall_s = (mx - mn) / 1000.0 if mn is not None else 0.0
    spot, ondemand = vh * _SPOT_VCPU_HR, vh * _ONDEMAND_VCPU_HR
    saved = ondemand - spot
    try:
        from encoder.cloud.compute_env import get_vcpus
        max_vcpus = int(get_vcpus().get("max_vcpus") or 0)
    except Exception:  # noqa: BLE001 — best-effort
        max_vcpus = 0
    print(f"[[ENCODER-COST exec={exec_name} spot_usd={spot:.4f} "
          f"ondemand_usd={ondemand:.4f} saved_usd={saved:.4f} vcpu_hours={vh:.2f}]]",
          flush=True)
    print(f"[[ENCODER-STATS exec={exec_name} wall_s={wall_s:.1f} vcpu_h={vh:.3f} "
          f"longest_s={longest_s:.1f} slowest={slowest or '-'} jobs={n} "
          f"max_vcpus={max_vcpus}]]", flush=True)
    conc = (vcpu_s / wall_s) if wall_s else 0.0
    eff = (conc / max_vcpus * 100.0) if max_vcpus else 0.0
    _narrate(f"💰 saved ${saved:.2f} using spot — {vh:.1f} vCPU-hr "
             f"(${spot:.2f} spot vs ${ondemand:.2f} on-demand)")
    _narrate(f"📊 {n} jobs · {vh:.1f} vCPU-hr · avg {conc:.0f} vCPUs busy "
             f"({eff:.0f}% of {max_vcpus}) · slowest chunk {longest_s / 60:.1f} min")


def cmd_poll(args: argparse.Namespace) -> int:
    sfn = _sfn()
    # Read the execution input up front so the plan lists only the codecs we're
    # actually encoding (do_h264 / do_hevc from buildSFNInput).
    do_h264 = do_hevc = True
    variants: list = []
    try:
        inp = json.loads(sfn.describe_execution(
            executionArn=args.execution_arn).get("input") or "{}")
        do_h264 = bool(inp.get("do_h264", True))
        do_hevc = bool(inp.get("do_hevc", True))
        variants = inp.get("variants") or []
    except (ClientError, ValueError, TypeError):
        pass
    _emit_plan(variants, do_h264, do_hevc)

    seen: set[int] = set()
    log_state: dict[str, int] = {}  # stream -> last-forwarded timestamp
    reclaim_seen: dict = {}         # jobId -> last-emitted (count, lost, total)
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
        # Detect spot reclaims of in-flight encode jobs (red bar + waste stats).
        try:
            _report_live_reclaims(exec_name, reclaim_seen)
        except Exception:  # noqa: BLE001 — reclaim reporting is best-effort
            pass
        # Forward live [progress]/[phase] lines from running containers so
        # long phases show activity, not a dark gap. Best-effort.
        try:
            _forward_running_logs(exec_name, log_state)
        except Exception:  # noqa: BLE001 — live tailing is cosmetic
            pass
        # Backfill instance colour for chunks that finished between polls (short
        # rungs never seen RUNNING), so their cells aren't left the default blue.
        try:
            _backfill_completed_hosts(exec_name, log_state)
        except Exception:  # noqa: BLE001 — host colouring is cosmetic
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
                n = _download_outputs(args.s3_prefix, Path(args.local_dir),
                                      getattr(args, "output_stem", ""),
                                      getattr(args, "output_tag", ""))
                print(f"    downloaded {n} files", flush=True)
                try:
                    _emit_cost_summary(exec_name, log_state)  # cost + host sweep
                except Exception:  # noqa: BLE001 — cost summary is best-effort
                    pass
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
    # Base output name (OutputStem, no codec). When set, each codec's outputs
    # land in <output-stem>_<codec>/ so codecs coexist in OUTPUT_DIR.
    pp.add_argument("--output-stem", dest="output_stem", default="")
    pp.add_argument("--output-tag", dest="output_tag", default="",
                    help="profile suffix appended AFTER the codec (e.g. 'xs')")
    pp.set_defaults(fn=cmd_poll)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
