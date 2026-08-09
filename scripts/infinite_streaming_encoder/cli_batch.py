"""Submit + poll an encode execution on the AWS Step Functions state
machine. Invoked by the Go server's `cloud-batch` target the same way
it invokes cli_cloud.py for the legacy spot target:

  python -m infinite_streaming_encoder.cli_batch submit \\
    --state-machine-arn ARN --input-json FILE
  # prints the execution ARN on stdout

  python -m infinite_streaming_encoder.cli_batch poll \\
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
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

# Taxonomy only — cli_batch is the queue's CONSUMER and must never emit through
# telemetry, or it would republish what it just drained.
from infinite_streaming_encoder import pricing
from infinite_streaming_encoder.telemetry import (
    is_gauge, is_marker, is_record, queue_name, trim_execution_name)

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
    # max_pool_connections defaults to 10; the parallel output download runs up
    # to DOWNLOAD_CONCURRENCY threads against this client, and a pool narrower
    # than the thread count just moves the queue from the network into botocore.
    from botocore.config import Config
    n = max(10, int(os.environ.get("DOWNLOAD_CONCURRENCY", "64")))
    return boto3.client("s3", region_name=_region(),
                        config=Config(max_pool_connections=n))


def _logs():
    return boto3.client("logs", region_name=_region())


def _batch():
    return boto3.client("batch", region_name=_region())


def _ecs():
    return boto3.client("ecs", region_name=_region())


def _ec2():
    return boto3.client("ec2", region_name=_region())


def _sqs():
    return boto3.client("sqs", region_name=_region())


def _events():
    return boto3.client("events", region_name=_region())


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
# <codec>_<tier>_chunk<NNN>.mp4 — a staged chunk OUTPUT object (tier may carry an
# ordinal suffix like 540p_2, hence the greedy middle group). Excludes .mp4.done.
_CHUNK_OBJ_RE = re.compile(r"^([^_]+)_(.+)_chunk(\d+)\.mp4$")


# _reflect_batch_status was removed. It reflected RUNNABLE -> queued and
# STARTING -> running on EVERY poll with no dedupe, which _sync_stages_from_batch
# now supersedes and actively conflicted with:
#
#   - it re-stamped "queued" on every RUNNABLE chunk each poll — 100-300 markers
#     per cycle on a full ladder, rewriting cells that had not changed, which
#     showed in the UI as chunk pills constantly re-rendering.
#   - it called STARTING "running" while _sync_stages_from_batch calls it
#     "starting". Both ran each poll, so a placed-but-not-yet-encoding chunk
#     flipped between the two states continuously.
#
# Its purpose is covered: submission now emits "queued" (a chunk is queued from
# the moment it is handed to Batch until placed), and _sync_stages_from_batch
# emits starting/running/done/failed exactly once per transition.


# CloudWatch log group the Batch job definitions write to (see
# infra/terraform/modules/jobs/main.tf).
_BATCH_LOG_GROUP = "/aws/batch/infinite-streaming-encoder"

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


def _set_host(key: str, inst: str, log_state: dict, src: str) -> None:
    """Colour one stage by machine, deduped, and SAY SO when the colour changes.

    Both the event path and the describe_jobs path land here, so this is the one
    place that can tell a first colouring from a RECOLOURING. The distinction is
    what makes "is something messing up my chunk colours?" answerable:

      * first colouring (was unset) is normal and silent
      * a change from one machine to ANOTHER is not — a chunk runs on one box,
        so it means two sources disagree, or a retry moved it. Either way it is
        a visible cell changing colour, and it is now logged with both values
        and which source did it.
    """
    seen_key = "_host:" + key
    prev = log_state.get(seen_key)
    if prev == inst:
        return
    log_state[seen_key] = inst
    if prev:
        print(f"[host] {time.strftime('%H:%M:%S')} RECOLOUR {key}: "
              f"{prev} -> {inst} (via {src})", flush=True)
    _emit_host(key, inst)


def _tag_host_from_arn(jobname: str, ci_arn: str, log_state: dict) -> None:
    """Colour a job's stage keys from an arn we were HANDED rather than looked up.

    The event carries containerInstanceArn from STARTING onward, so this replaces
    the describe_jobs behind _tag_hosts_for_jobs for the event path. Same dedupe
    key, so the two paths cannot double-announce. Only the ARN -> EC2 id lookup
    remains, and that is cached per ARN.
    """
    inst = _ec2_for_container_instance(ci_arn)
    if not inst:
        return
    for key in _host_stage_keys(jobname):
        _set_host(key, inst, log_state, "event")


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
            _set_host(key, inst, log_state, "poll")


# One scan of this execution's jobs, shared by every caller within a poll.
#
# _list_exec_jobs used to make a SEPARATE list_jobs call per status, each
# paginating the queue's ENTIRE history and filtering to this execution in
# Python. Measured on a live run: 4 status scans cost 7.9s per poll against a
# nominal 5s sleep, dominated by SUCCEEDED at 5.97s — because the queue holds
# thousands of succeeded jobs from previous runs and it walked all of them to
# find this execution's 68. Worse, the cost GREW as the run progressed, so the
# grid fell further behind exactly as more chunks finished. The grid was measured
# 43 chunks behind Batch mid-run, and showing 30 chunks still queued after Batch
# had succeeded all 336 (#187).
#
# AFTER_CREATED_AT scoped to the execution's own start collapses that to this
# run's jobs. It cannot be combined with jobStatus — Batch rejects the pair with
# "job status [is] not applicable when ListJobs filters are specified" — but that
# is fine, because one unfiltered-by-status scan returns everything we need and
# the statuses are bucketed here. Four calls become one, and the SUCCEEDED scan
# that _sync_stages_from_batch and _backfill_completed_hosts each performed
# separately is now shared.
_EXEC_JOBS_TTL_S = 2.0
_exec_jobs_cache: dict[str, tuple[float, dict[str, list]]] = {}


def _exec_start_ms(exec_name: str) -> int | None:
    """Execution start as epoch ms, from the job id the SFN execution name
    carries as its prefix (`<jobID>-<base>-<hash>`; jobID is a ms timestamp).

    Read from the name rather than describe_execution so this costs no API call
    and cannot itself become a source of latency. Returns None if the prefix is
    not a plausible timestamp, which sends the caller back to the unfiltered
    per-status path rather than silently narrowing the scan to nothing.
    """
    head = exec_name.split("-", 1)[0]
    if not head.isdigit():
        return None
    ms = int(head)
    # Sanity: 2001-09-09 .. 2286-11-20 in ms. A plain second-precision value or a
    # stray number would otherwise shift the window by decades.
    return ms if 1_000_000_000_000 <= ms <= 9_999_999_999_999 else None


def _exec_jobs_snapshot(exec_name: str) -> dict[str, list] | None:
    """This execution's jobs bucketed by status, cached briefly so the several
    callers in one poll cycle share a single scan.

    None means the snapshot is UNAVAILABLE (start time underivable, or the scan
    errored) and the caller should fall back. An empty dict is a valid answer —
    the execution has no jobs yet — and must NOT trigger the fallback, or every
    poll before the first job appears would pay for the expensive path.
    """
    now = time.monotonic()
    hit = _exec_jobs_cache.get(exec_name)
    if hit and now - hit[0] < _EXEC_JOBS_TTL_S:
        return hit[1]

    start = _exec_start_ms(exec_name)
    if start is None:
        return None  # unavailable — caller falls back to the per-status path

    batch = _batch()
    queue = os.environ.get("BATCH_JOB_QUEUE", "infinite-streaming-encoder-queue")
    out: dict[str, list] = {}
    tok = None
    while True:
        try:
            kw = dict(jobQueue=queue, maxResults=100,
                      filters=[{"name": "AFTER_CREATED_AT", "values": [str(start)]}])
            if tok:
                kw["nextToken"] = tok
            r = batch.list_jobs(**kw)
        except ClientError:
            # Partial results are worse than none here: a truncated snapshot
            # would look like jobs disappearing. Fall back to the old path.
            return None
        for j in r.get("jobSummaryList", []):
            if exec_name in j.get("jobName", ""):
                out.setdefault(j.get("status", ""), []).append(j)
        tok = r.get("nextToken")
        if not tok:
            break
    _exec_jobs_cache[exec_name] = (now, out)
    return out


def _list_exec_jobs(exec_name: str, status: str) -> list:
    """Every Batch job summary in `status` whose name carries this execution,
    following nextToken. list_jobs returns at most 100 summaries per page; the
    host-tagging callers below used to read only the FIRST page, so on a ladder
    with >100 jobs the chunks past it were never described and their cells stayed
    the default blue. Paginating covers every chunk regardless of ladder size."""
    # Preferred: one execution-scoped scan, shared across callers. Returns {} if
    # the execution start could not be derived or the scan failed, in which case
    # fall through to the original per-status walk so behaviour is never worse
    # than before.
    snap = _exec_jobs_snapshot(exec_name)
    if snap is not None:
        return snap.get(status, [])

    batch = _batch()
    queue = os.environ.get("BATCH_JOB_QUEUE", "infinite-streaming-encoder-queue")
    out, tok = [], None
    while True:
        try:
            kw = dict(jobQueue=queue, jobStatus=status, maxResults=100)
            if tok:
                kw["nextToken"] = tok
            r = batch.list_jobs(**kw)
        except ClientError:
            break
        out += [j for j in r.get("jobSummaryList", [])
                if exec_name in j.get("jobName", "")]
        tok = r.get("nextToken")
        if not tok:
            break
    return out


def _backfill_completed_hosts(exec_name: str, log_state: dict) -> None:
    """Colour chunks that completed BETWEEN polls. A short chunk (small H.264
    rung) can start and finish inside one poll interval, so it's never observed
    RUNNING and never tagged — its cell falls back to the default (blue). A
    SUCCEEDED job keeps its containerInstanceArn, so describe the ones whose stage
    keys aren't coloured yet and emit their host. Bounded: only untagged jobs are
    described (via the name, no API call), so each is described at most once.
    Paginates SUCCEEDED so no completed chunk is missed on a >100-job ladder."""
    batch = _batch()
    ids = []
    for j in _list_exec_jobs(exec_name, "SUCCEEDED"):
        keys = _host_stage_keys(j.get("jobName", ""))
        if keys and any(log_state.get("_host:" + k) is None for k in keys):
            ids.append(j["jobId"])
    for i in range(0, len(ids), 100):
        try:
            jobs = batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", [])
        except ClientError:
            continue
        _tag_hosts_for_jobs(jobs, log_state)


# How many FINISHED jobs' streams to drain in one poll. At ~0.37s per stream this
# bounds that work to a few seconds, so a wave of completions cannot stall the
# poll loop — and therefore cannot delay the stage sync that runs before it.
_MAX_DRAINS_PER_POLL = 12


def _forward_running_logs(exec_name: str, log_state: dict,
                          drain_finished: bool = True) -> None:
    """Tail the CloudWatch streams of currently-RUNNING jobs and forward their
    [progress]/[phase] lines into the app log — so long phases (mezzanine,
    packaging, big S3 transfers) show live activity instead of a dark gap
    between 'submitted' and 'done'. Best-effort; never breaks polling."""
    batch = _batch()
    # RUNNING *and* recently-SUCCEEDED. A job that starts and finishes inside one
    # poll interval is never observed RUNNING, so tailing only that state misses
    # its output entirely — and cloud chunks are short: a 396p chunk measured at
    # 0.66s of encode against a 5s poll. Draining succeeded streams too means
    # every job's markers are forwarded exactly once (log_state dedupes by
    # timestamp per stream).
    # RUNNING *and* not-yet-drained SUCCEEDED. A job that starts and finishes
    # inside one poll interval is never observed RUNNING, so tailing only that
    # state misses its output entirely — and cloud chunks are short: a 396p chunk
    # measured at 0.66s of encode against a 5s poll.
    #
    # Completed streams are drained ONCE (tracked in _drained) rather than every
    # poll: log_state dedupes lines by timestamp, but re-listing hundreds of
    # finished jobs each poll would cost a GetLogEvents call apiece for nothing.
    drained = log_state.setdefault("_drained", set())
    live_ids = [j["jobId"] for j in _list_exec_jobs(exec_name, "RUNNING")]
    # `drain_finished` False means the telemetry queue is delivering markers, so
    # a finished container's stream holds nothing we still need — and reading it
    # is the single most expensive thing in the poll loop (125.53s for 337
    # streams, measured). Skipping it is the whole point of #188; the RUNNING
    # tail below stays, because the [progress]/[phase] NARRATION it forwards for
    # long phases is not marker traffic and never went on the queue.
    done_ids = ([j["jobId"] for j in _list_exec_jobs(exec_name, "SUCCEEDED")
                 if j["jobId"] not in drained] if drain_finished else [])
    # BOUNDED. Draining a finished job's stream costs ~0.37s (get_log_events per
    # stream, measured at 125s for 337 of them). A burst of completions would
    # otherwise make one poll take minutes, and everything after it in the loop —
    # including the next status sync — waits that long. Capping the per-poll
    # drain keeps the cycle short; the remainder is picked up next poll, and
    # nothing is lost because `drained` is only marked once a stream is actually
    # read. Live (RUNNING) jobs are never capped: those carry the progress
    # percentages the UI animates.
    if len(done_ids) > _MAX_DRAINS_PER_POLL:
        done_ids = done_ids[:_MAX_DRAINS_PER_POLL]
    ids = live_ids + done_ids
    live = set(live_ids)
    for i in range(0, len(ids), 100):
        try:
            jobs = batch.describe_jobs(jobs=ids[i:i + 100]).get("jobs", [])
        except ClientError:
            continue
        for j in jobs:
            stream = j.get("container", {}).get("logStreamName")
            if stream:
                is_live = j.get("jobId") in live
                _tail_progress(stream, _short_label(j.get("jobName", "")),
                               log_state, live=is_live)
                if not is_live:
                    drained.add(j.get("jobId"))
        # Colour every running job's rows (encode chunks + mezzanine / audio /
        # package / fragments / hls) by the instance it landed on. Best-effort.
        _tag_hosts_for_jobs(jobs, log_state)


# ---------------------------------------------------------------------------
# Telemetry queue — the transport that replaces scraping finished containers'
# CloudWatch streams for their markers (#188).
#
# Shape: ONE queue per execution, created here before the execution starts and
# deleted when it ends. Per-execution rather than shared because SQS returns a
# SAMPLE of available messages: with one queue, two concurrent runs would each
# keep drawing the other's messages, be unable to delete them, and the smaller
# run could starve indefinitely.
#
# The worker derives the same name from telemetry.queue_name(). That function is
# the single definition of the name — see its docstring.
# ---------------------------------------------------------------------------

# Messages outlive a server restart, which CloudWatch tailing state did not:
# bouncing the server mid-encode used to strand a job's grid permanently (a run
# stuck displaying 159/336) because the replay could not recover what had
# already been read. 1h is far longer than any run and keeps orphans cheap.
_TELEMETRY_RETENTION_S = "3600"
# A message we take but fail to delete comes back after this. Short, because the
# only reason we would not delete is a crash between receive and delete.
_TELEMETRY_VISIBILITY_S = "30"
# How long one poll may spend draining. Replaced a 40-receive (400-message) cap
# that measured badly: a 336-chunk run ended with 3,292 messages still queued
# and every chunk already finished, which stalls the grid and lets stale markers
# arrive after Batch has reported done. Budgeting TIME bounds the poll cycle
# directly — the thing that actually matters — instead of guessing a count
# against round-trip latency we do not control.
_TELEMETRY_DRAIN_BUDGET_S = 8.0
# Pause between drain passes when the queue is empty. Short: the whole point is
# that a marker surfaces about a second after it is published.
_TELEMETRY_DRAIN_IDLE_S = 0.5
# Backlog worth reporting. A handful in flight is normal; hundreds means the
# drain is losing to the publishers and the grid is about to look stuck.
_TELEMETRY_BACKLOG_WARN = 200
_TELEMETRY_LOG_EVERY_S = 20.0
# An ENCODER-FLEET sample is a GAUGE, so a backlog can deliver one that is no
# longer true. Records (TIMING/SPEED/VMAF) are kept regardless of age — they
# cannot be recovered without re-encoding.
_TELEMETRY_GAUGE_MAX_AGE_S = 30.0
# How long the queue may stay silent WHILE CONTAINERS ARE RUNNING before we
# conclude the workers cannot publish and go back to reading logs. Generous: a
# worker's first marker lands within seconds of its container starting, so this
# only trips on a real misconfiguration.
_TELEMETRY_SILENT_GIVEUP_S = 120
# How often the Batch census still runs when events ARE flowing. Long, because
# it exists only to catch a transition EventBridge failed to deliver; short
# enough that such a gap closes well inside a run.
_CENSUS_BACKSTOP_S = 60.0
# Grace before a target-less state rule may be swept. Covers the window between
# another submit's put_rule and its put_targets, during which its execution does
# not yet exist and so cannot appear in the keep-list.
_STATE_RULE_MIN_AGE_S = 300.0


def _create_telemetry_queue(exec_name: str) -> str | None:
    """Create this execution's queue. Returns its URL, or None if unavailable.

    Called BEFORE the execution starts, so no worker can race a missing queue.
    Failure is not fatal: workers fall back to stdout->CloudWatch and the
    orchestrator keeps its log-draining path.
    """
    try:
        return _sqs().create_queue(
            QueueName=queue_name(exec_name),
            Attributes={
                "MessageRetentionPeriod": _TELEMETRY_RETENTION_S,
                "VisibilityTimeout": _TELEMETRY_VISIBILITY_S,
                # Long-poll by default so an empty receive costs one request
                # rather than spinning.
                "ReceiveMessageWaitTimeSeconds": "1",
                # FIFO. Ordering is a property we need and standard queues do
                # not provide — on the first 336-chunk run every chunk's
                # progress meter visibly danced up and down as a stage's 40%
                # printed after its 60%. Reconstructing order on read from
                # SentTimestamp is guesswork: that stamp is applied on ARRIVAL,
                # so publisher-side buffering can invert two markers or land
                # them in the same millisecond.
                #
                # Throughput is not a concern: ~7,000 messages over a ~10-minute
                # run is ~12/s against FIFO's 3,000/s batched ceiling.
                "FifoQueue": "true",
                # We supply MessageDeduplicationId ourselves — see the sink.
                "ContentBasedDeduplication": "false",
            },
        )["QueueUrl"]
    except Exception as e:  # noqa: BLE001 — degrade to the log path, never fail
        print(f"!!! telemetry queue unavailable ({type(e).__name__}: {e}); "
              f"falling back to CloudWatch log draining", file=sys.stderr,
              flush=True)
        return None


def _delete_telemetry_queue(url: str | None) -> None:
    if not url:
        return
    try:
        _sqs().delete_queue(QueueUrl=url)
    except Exception:  # noqa: BLE001 — retention expires it anyway
        pass


def _active_execution_cores(sm_arn: str) -> set:
    """Trimmed name cores of executions that are RUNNING right now.

    The keep-list for both sweeps. Derived from the same trim the resources were
    named with, so a match is exact rather than a prefix guess. On failure this
    returns an empty set — which makes the sweep MORE aggressive, so callers
    must keep their other bounds rather than rely on this alone.
    """
    if not sm_arn:
        return set()
    try:
        paginator = _sfn().get_paginator("list_executions")
        cores = set()
        for page in paginator.paginate(stateMachineArn=sm_arn,
                                       statusFilter="RUNNING"):
            for e in page.get("executions", []):
                cores.add(trim_execution_name(
                    e["name"], _EB_RULE_NAME_MAX - len(_STATE_PREFIX)))
                cores.add(queue_name(e["name"]).rsplit("/", 1)[-1])
        return cores
    except Exception:  # noqa: BLE001 — best-effort; see the docstring
        return set()


def _gc_telemetry_queues(sm_arn: str = "") -> None:
    """Delete telemetry queues left behind by runs that never finished cleanly.

    A driver killed mid-run (the server restarting kills the cli_batch
    subprocess) never reaches its own delete. Retention expires the MESSAGES
    after an hour but the empty queue itself persists forever, so without this
    the account accumulates one queue per crashed run. Best-effort, and bounded:
    only queues with no messages and no in-flight messages, older than the
    retention window, are removed — so a live run's queue can never be deleted
    out from under it, even if this races one.
    """
    keep = _active_execution_cores(sm_arn)
    try:
        sqs = _sqs()
        names = []
        # BOTH channels: telemetry (worker -> control plane) and Batch state
        # (EventBridge -> control plane). Same lifetime, same failure mode — a
        # killed driver never runs its own delete — so one sweep covers both.
        for prefix in ("encoder-telemetry-", _STATE_PREFIX):
            names += sqs.list_queues(QueueNamePrefix=prefix,
                                     MaxResults=1000).get("QueueUrls") or []
    except Exception:  # noqa: BLE001 — housekeeping is best-effort
        return
    now = time.time()
    for url in names:
        # KEEP-LIST FIRST. "Empty and old" is no longer sufficient evidence that
        # a queue is abandoned: the drain thread now holds a healthy queue at
        # zero messages, so a run lasting longer than the retention window would
        # look exactly like an orphan and be deleted out from under itself.
        # Same lesson as the MinIO staging GC, which passes ActiveDistPrefixes
        # for precisely this reason.
        if any(c and c in url for c in keep):
            continue
        try:
            a = sqs.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=["CreatedTimestamp",
                                "ApproximateNumberOfMessages",
                                "ApproximateNumberOfMessagesNotVisible"],
            )["Attributes"]
            age = now - float(a.get("CreatedTimestamp", now))
            empty = (int(a.get("ApproximateNumberOfMessages", 1)) == 0 and
                     int(a.get("ApproximateNumberOfMessagesNotVisible", 1)) == 0)
            if empty and age > float(_TELEMETRY_RETENTION_S):
                sqs.delete_queue(QueueUrl=url)
        except Exception:  # noqa: BLE001 — one bad queue must not stop the sweep
            continue
    _gc_state_rules(sm_arn)


def _gc_state_rules(sm_arn: str = "") -> None:
    """Delete EventBridge rules left behind by killed drivers.

    Separate from the queue sweep because a rule is invisible to SQS listing —
    deleting only the queue would leave the rule matching events forever, with
    nowhere to deliver them. A rule with no targets is by definition orphaned:
    _create_state_channel always attaches one, and _delete_state_channel removes
    targets before the rule, so a target-less rule is either mid-teardown or
    abandoned. Either way it is safe to remove.
    """
    try:
        events = _events()
        rules = events.list_rules(NamePrefix=_STATE_PREFIX,
                                  Limit=100).get("Rules") or []
    except Exception:  # noqa: BLE001 — best-effort
        return
    keep = _active_execution_cores(sm_arn)
    sqs = _sqs()
    for r in rules:
        name = r.get("Name")
        if not name or any(c and c in name for c in keep):
            continue
        try:
            # AGE, not targets. Having a target used to mean "live, leave it
            # alone", which is wrong and left five rules behind: a run that is
            # CANCELLED never reaches _delete_state_channel, so its rule keeps
            # both its target and its existence forever. Target presence
            # distinguishes "configured" from "half-built", not "live" from
            # "abandoned" — the keep-list above is what says live.
            #
            # The race the target check was really guarding is a rule created by
            # another submit moments ago, whose execution does not exist yet and
            # so cannot be in the keep-list. The queue is created BEFORE the rule
            # and carries a timestamp, so it is the evidence the rule lacks: a
            # young queue means a young rule, and no queue at all means genuinely
            # orphaned.
            try:
                a = sqs.get_queue_attributes(
                    QueueUrl=sqs.get_queue_url(QueueName=name)["QueueUrl"],
                    AttributeNames=["CreatedTimestamp"])["Attributes"]
                if time.time() - float(a["CreatedTimestamp"]) < _STATE_RULE_MIN_AGE_S:
                    continue
            except Exception:  # noqa: BLE001 — no queue: nothing left to protect
                pass
            # Targets must go before the rule; a rule with targets cannot be
            # deleted, which is the other reason the old branch never cleaned up.
            try:
                events.remove_targets(Rule=name, Ids=["1"])
            except Exception:  # noqa: BLE001 — may already have none
                pass
            events.delete_rule(Name=name)
        except Exception:  # noqa: BLE001 — one bad rule must not stop the sweep
            continue


def _msg_attr(m: dict, name: str) -> str:
    return ((m.get("MessageAttributes") or {}).get(name) or {}).get("StringValue") or ""


def _msg_pub(m: dict) -> str:
    """Publisher id, or "" for a message from a worker predating sequencing."""
    return _msg_attr(m, "pub")


def _msg_seq(m: dict) -> int:
    """Publisher-monotonic emission order, or 0 if absent/malformed.

    0 disables the ordering guard for that message rather than failing it, so a
    worker running an older image degrades to the previous behaviour instead of
    having its telemetry dropped.
    """
    try:
        return int(_msg_attr(m, "seq") or 0)
    except ValueError:
        return 0


def _drain_telemetry(url: str | None, log_state: dict) -> int:
    """Print every marker waiting on the queue. Returns how many were handled.

    This is what `_forward_running_logs` used to do for finished jobs, at
    ~0.37s per container stream (measured: 125.53s for 337 of them). Here the
    whole run's markers arrive in one place, ten to a request, with no
    dependency on CloudWatch ingestion latency.
    """
    if not url:
        return 0
    sqs = _sqs()
    handled = 0
    suppressed = 0
    collected: list = []
    t0 = time.monotonic()
    receives = 0
    # TIME-budgeted, not count-budgeted. The first cap was 40 receives (400
    # messages) per poll, which sounded generous and was not: a 336-chunk run
    # left 3,292 messages queued with every chunk already finished. The grid is
    # gated on this drain, so a backlog does not just delay markers — it makes
    # the grid stall and then complete in a burst, and it lets a stale "running
    # 18%" arrive after Batch has already reported the chunk done.
    #
    # A count cap cannot be tuned, because what matters is throughput against
    # the poll interval, and each receive is a round trip whose latency we do
    # not control. A time budget bounds the poll cycle directly, which is the
    # thing we actually care about, and drains as much as that buys.
    while time.monotonic() - t0 < _TELEMETRY_DRAIN_BUDGET_S:
        try:
            resp = sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10,
                # Long-poll only the FIRST receive of a cycle. It confirms an
                # empty queue is really empty (a short poll samples servers and
                # can return nothing while messages exist), and costs at most 1s
                # per poll. Subsequent receives in the same drain short-poll,
                # because we already know there is a backlog to clear.
                WaitTimeSeconds=1 if receives == 0 else 0,
                AttributeNames=["SentTimestamp"],
                MessageAttributeNames=["pub", "seq"],
            )
        except ClientError:
            break
        receives += 1
        msgs = resp.get("Messages") or []
        if not msgs:
            break
        collected.extend(msgs)
        try:
            sqs.delete_message_batch(
                QueueUrl=url,
                Entries=[{"Id": str(n), "ReceiptHandle": m["ReceiptHandle"]}
                         for n, m in enumerate(msgs)])
        except ClientError:
            # Not deleted -> redelivered after the visibility timeout. The seq
            # guard below drops the repeat for droppable markers; records are
            # idempotent on the control plane. So a repeat is harmless.
            pass

    # ORDER. Receive order IS emission order — the queue is FIFO and every
    # message carries MessageGroupId = the publishing process, so a chunk's
    # markers can never overtake each other. Deliberately NOT sorted here:
    # re-sorting a FIFO drain by SentTimestamp would REINTRODUCE the bug it was
    # meant to fix, because that stamp is applied on arrival and publisher-side
    # buffering can invert two markers or land them in the same millisecond.
    #
    # This replaced a standard queue, where the symptom was unmistakable on a
    # 336-chunk run: every chunk's progress meter dancing up and down as a
    # stage's 40% printed after its 60%.
    seen_seq = log_state.setdefault("_tel_seq", {})
    now_ms = time.time() * 1000.0
    for m in collected:
        # One message carries a PACKED batch of markers, newline-joined. The
        # consumer's ceiling is ROUND TRIPS — SQS returns at most 10 messages per
        # receive — so the publisher fills each body instead of sending one
        # marker at a time. Order within a body is emission order.
        sent_ms = float(m.get("Attributes", {}).get("SentTimestamp", "0"))
        pub, seq = _msg_pub(m), _msg_seq(m)

        # Redelivery backstop, NOT the ordering mechanism — FIFO already
        # guarantees order. A batch we read but failed to delete reappears after
        # the visibility timeout, and replaying an old percent would still walk a
        # bar backwards.
        #
        # Keyed per PUBLISHER, which is what keeps retries working: a chunk
        # re-run after a spot reclaim is a NEW process with its own seq from 1,
        # so it is never suppressed by the attempt it replaces.
        replay = bool(pub and seq and seq <= seen_seq.get(pub, 0))
        if pub and seq and not replay:
            seen_seq[pub] = seq

        for body in (m.get("Body") or "").split("\n"):
            body = body.rstrip()
            if not is_marker(body):
                continue
            # A replayed message is dropped only for DROPPABLE classes. Records
            # go through even when repeated: losing one loses data no re-read can
            # recover, and the control plane tolerates a duplicate record.
            if replay and not is_record(body):
                continue
            if (is_gauge(body) and sent_ms and
                    now_ms - sent_ms > _TELEMETRY_GAUGE_MAX_AGE_S * 1000.0):
                continue  # stale gauge — see _TELEMETRY_GAUGE_MAX_AGE_S
            if sm := _STAGE_LINE_RE.match(body):
                # Through the chokepoint, so this marker is judged against what
                # the OTHER emitters have already said — including a `done` that
                # came from Step Functions history, which a check local to this
                # function could not see. That blind spot is why reversals
                # survived the first attempt at fixing them.
                if not _emit_stage(sm.group(1), sm.group(2), float(sm.group(3)),
                                   src="worker"):
                    suppressed += 1
                    continue
                handled += 1
                continue
            print(body, flush=True)
            handled += 1

    # Report only when something is WRONG or notable, and at most every
    # _TELEMETRY_LOG_EVERY_S — the drain thread runs continuously, so logging
    # each pass would bury the job log. A drain that cannot keep up otherwise
    # looks exactly like an encode running slowly, which is how the 3,292-message
    # backlog went unnoticed until the grid visibly stalled.
    now = time.monotonic()
    # Running total the poll loop reads to decide whether the queue is silent.
    # Must be the DRAIN's own count: _STAGE_STATE is populated by the Batch
    # backstop too, so it cannot distinguish "telemetry is working" from
    # "telemetry is dead and Batch is covering for it".
    log_state["_tel_handled"] = log_state.get("_tel_handled", 0) + handled
    if handled or suppressed:
        remaining = _queue_depth(sqs, url)
        interesting = remaining > _TELEMETRY_BACKLOG_WARN or suppressed
        if interesting and now - log_state.get("_tel_logged", 0) > _TELEMETRY_LOG_EVERY_S:
            log_state["_tel_logged"] = now
            _narrate(f"[telemetry] drained {handled} in {now - t0:.1f}s "
                     f"({receives} receives)"
                     f"{f', {suppressed} stale suppressed' if suppressed else ''}"
                     f"{f', ~{remaining} still queued' if remaining else ''}")
    return handled


def _queue_depth(sqs, url: str) -> int:
    """Approximate messages still waiting, or 0 if unavailable."""
    try:
        a = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
        return int(a["Attributes"]["ApproximateNumberOfMessages"])
    except Exception:  # noqa: BLE001 — a stat we log, never act on
        return 0


def _start_drain_thread(tel_url: str | None, state_url: str | None,
                        log_state: dict):
    """Drain the queue continuously on a background thread. Returns a stop event.

    The drain used to run once per poll iteration, which coupled how fast a
    marker reached the UI to how long everything ELSE in the loop took — and the
    loop paginates the entire Step Functions execution history each cycle, which
    for 336 chunks is thousands of events. Markers then arrived in bursts
    whenever the cycle came round, which is precisely what "the grid hasn't
    updated recently, then bang it completed" looks like from the outside.

    A thread decouples the two: markers surface within a second of being
    published regardless of what the poll loop is doing. That also removes most
    of the cross-source race, because the queue stops running behind Batch
    status — the terminal-status guard in _drain_telemetry remains as the
    correctness backstop rather than the primary defence.
    """
    if not (tel_url or state_url):
        return None
    stop = threading.Event()

    # ONE THREAD PER QUEUE, not one thread draining both in sequence.
    #
    # Draining them in turn made a cycle as long as the sum of their budgets:
    # 8s for state, 8s for telemetry, plus the idle wait. An event landing just
    # after its queue was read then waited a whole cycle, or two if unlucky.
    #
    # Measured, and unmistakable in the log: the eight worst-lagged chunks of a
    # run were all delivered at 19:55:34, within 0.2s of each other, carrying
    # lags of 32-35s. Arriving in one clump means the consumer was stalled and
    # caught up in a single pass — not that EventBridge was slow, which it was
    # not (0.6s median, verified in #188 step 0).
    #
    # Each queue now has its own thread, so neither waits on the other and a
    # cycle is about a second. The two drains touch shared state only through
    # _emit_stage and log_state; _emit_stage is the ordering chokepoint and is
    # already the arbiter between racing sources, so concurrency here is the
    # case it was built for rather than a new hazard.
    def _loop(fn, url, name) -> None:
        while not stop.is_set():
            try:
                fn(url, log_state)
            except Exception:  # noqa: BLE001 — never let a drain kill the run
                pass
            stop.wait(_TELEMETRY_DRAIN_IDLE_S)

    for fn, url, name in ((_drain_state, state_url, "state-drain"),
                          (_drain_telemetry, tel_url, "telemetry-drain")):
        if url:
            threading.Thread(target=_loop, args=(fn, url, name),
                             daemon=True, name=name).start()
    return stop


def _queue_depth(sqs, url: str) -> int:
    """Approximate messages still waiting, or 0 if unavailable."""
    try:
        a = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
        return int(a["Attributes"]["ApproximateNumberOfMessages"])
    except Exception:  # noqa: BLE001 — a stat we log, never act on
        return 0


# ---------------------------------------------------------------------------
# Batch state, event-driven (#188 step 1)
#
# Stage state used to be POLLED from three places at once — Batch job status,
# Step Functions history, and the worker's own markers — which observe the same
# run through channels with different latencies and so disagree about the
# present tense. That skew is what produced the reversals the _emit_stage
# chokepoint now has to defend against.
#
# EventBridge emits a Batch Job State Change event at each transition. Verified
# on this account against a live 336-chunk run before this was built:
#
#   jobName                        present on 100% of events, all statuses
#   container.containerInstanceArn present on 100% of STARTING/RUNNING/SUCCEEDED
#   container.logStreamName        present on 100% of those
#   attempts, exitCode, stoppedAt  present on 100% of SUCCEEDED (n=122)
#   latency vs the job's stoppedAt min 0.3s, median 0.6s, max 5.5s
#
# The STARTING result specifically contradicts what polling showed: a
# describe_jobs poll catches placement as a race and often has no instance arn,
# while the EVENT is emitted with placement already recorded. Host colouring
# therefore needs no lookup at all.
#
# A STANDARD queue, deliberately not FIFO: EventBridge's own delivery is
# unordered, so FIFO would only preserve the order EventBridge happened to
# enqueue in — an ordering guarantee over already-shuffled input. Ordering is
# handled where it belongs, in _emit_stage.
# ---------------------------------------------------------------------------

_STATE_PREFIX = "encoder-state-"
# Dead-letter queue for EventBridge deliveries that fail after its retries.
#
# SHARED and long-lived, not per-execution: a per-run DLQ would be torn down with
# its channel, deleting the evidence it exists to preserve — the failures worth
# investigating are exactly the ones you notice after the run looked wrong.
#
# Named OUTSIDE both sweep prefixes on purpose. "encoder-state-dlq" would match
# the orphan sweep's `encoder-state-` prefix and be deleted whenever it was
# empty, which is most of the time.
_DLQ_NAME = "encoder-eventbridge-dlq"
# 14 days, the SQS maximum. A dropped transition shows up as one stuck cell,
# which may not be noticed for days; short retention would expire the evidence
# before anyone asked the question.
_DLQ_RETENTION_S = "1209600"
# An EventBridge rule name is capped at 64 characters, tighter than SQS's 80.
# Both names are built from ONE trimmed core so they cannot drift apart.
_EB_RULE_NAME_MAX = 64


def _state_names(exec_name: str) -> tuple[str, str]:
    """(rule_name, queue_name) for one execution. Single definition of both."""
    core = trim_execution_name(exec_name, _EB_RULE_NAME_MAX - len(_STATE_PREFIX))
    return _STATE_PREFIX + core, _STATE_PREFIX + core


def _ensure_state_dlq() -> str:
    """Create-or-get the shared DLQ. Returns its ARN, or "" if unavailable.

    Idempotent: CreateQueue returns the existing queue when the attributes match.
    Failure is not fatal — the target simply gets no DLQ, which is the behaviour
    before this existed.
    """
    try:
        sqs = _sqs()
        url = sqs.create_queue(
            QueueName=_DLQ_NAME,
            Attributes={"MessageRetentionPeriod": _DLQ_RETENTION_S},
        )["QueueUrl"]
        arn = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        # Allow any of OUR rules to dead-letter here, so the policy does not need
        # rewriting per execution.
        acct, region = arn.split(":")[4], arn.split(":")[3]
        sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": arn,
                "Condition": {"ArnLike": {"aws:SourceArn":
                              f"arn:aws:events:{region}:{acct}:rule/{_STATE_PREFIX}*"}},
            }],
        })})
        return arn
    except Exception:  # noqa: BLE001 — no DLQ is not a failure
        return ""


def _report_dlq(where: str) -> None:
    """Say how many deliveries EventBridge failed to make. Best-effort.

    A DLQ nobody reads is worse than no DLQ — it manufactures confidence that
    losses would have been noticed. This is the read. Silence means zero, and it
    is checked at the END of a run, when a stuck cell would already be visible
    and the question "was that a lost event?" is the one being asked.
    """
    try:
        sqs = _sqs()
        url = sqs.get_queue_url(QueueName=_DLQ_NAME)["QueueUrl"]
        n = int(sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )["Attributes"]["ApproximateNumberOfMessages"])
    except Exception:  # noqa: BLE001 — best-effort
        return
    if n:
        _narrate(f"!!! {n} Batch state event(s) undelivered ({where}) — "
                 f"EventBridge dead-lettered them to {_DLQ_NAME}. Any chunk stuck "
                 f"mid-state is explained by this; the 60s census should have "
                 f"repaired it.")


def _create_state_channel(exec_name: str) -> str | None:
    """Create this execution's EventBridge rule + queue. Returns the queue URL.

    Scoped by a jobName SUFFIX pattern, so the rule matches only this
    execution's jobs — verified with test-event-pattern, including a negative
    control against another execution's name. That scoping is what keeps
    concurrent runs from having to filter each other's events out.

    Best-effort: on any failure the caller keeps polling, exactly as before.
    """
    rule, queue = _state_names(exec_name)
    try:
        sqs, events = _sqs(), _events()
        url = sqs.create_queue(
            QueueName=queue,
            Attributes={"MessageRetentionPeriod": _TELEMETRY_RETENTION_S,
                        "VisibilityTimeout": _TELEMETRY_VISIBILITY_S,
                        "ReceiveMessageWaitTimeSeconds": "1"},
        )["QueueUrl"]
        qarn = sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        rarn = events.put_rule(
            Name=rule,
            EventPattern=json.dumps({
                "source": ["aws.batch"],
                "detail-type": ["Batch Job State Change"],
                "detail": {"jobName": [{"suffix": exec_name}]},
            }),
        )["RuleArn"]
        # EventBridge can only deliver if the queue lets it, scoped to THIS rule.
        sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": qarn,
                "Condition": {"ArnEquals": {"aws:SourceArn": rarn}},
            }],
        })})
        target = {"Id": "1", "Arn": qarn}
        dlq = _ensure_state_dlq()
        if dlq:
            # Without this a delivery that fails after EventBridge's retries is
            # dropped silently, and the only symptom is one cell stuck forever.
            target["DeadLetterConfig"] = {"Arn": dlq}
        events.put_targets(Rule=rule, Targets=[target])
        return url
    except Exception as e:  # noqa: BLE001 — degrade to polling, never fail a run
        print(f"!!! Batch state events unavailable ({type(e).__name__}: {e}); "
              f"falling back to polling job status", file=sys.stderr, flush=True)
        return None


def _delete_state_channel(exec_name: str, url: str | None) -> None:
    """Stop the event flow. Deliberately leaves the QUEUE for the GC to sweep.

    Targets must be removed before the rule — but deleting the queue here as
    well is what produced the only two entries the DLQ has ever held, both
    NO_RESOURCE ("the specified queue does not exist"). Removing the target
    stops NEW matches; it does not stop EventBridge retrying deliveries it has
    already accepted, and at teardown there are always a few in flight for jobs
    that were terminating.

    Those entries are noise, and noise in a DLQ is worse than an empty one: a
    queue that always holds a couple of end-of-run failures trains you to ignore
    the entry that means something. So the queue outlives the rule by design and
    absorbs the stragglers; _gc_telemetry_queues removes it once it is empty and
    past retention, which it already did for every other channel.
    """
    rule, _ = _state_names(exec_name)
    for fn in (lambda: _events().remove_targets(Rule=rule, Ids=["1"]),
               lambda: _events().delete_rule(Name=rule)):
        try:
            fn()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


def _event_epoch(body: dict, detail: dict) -> float:
    """When the TRANSITION happened, in epoch seconds, or 0 if unknown.

    Prefers the job's own stoppedAt/startedAt over the envelope `time`, because
    those are Batch's record of the transition itself rather than when
    EventBridge got round to describing it.
    """
    for k in ("stoppedAt", "startedAt"):
        v = detail.get(k)
        if isinstance(v, (int, float)) and v:
            return v / 1000.0
    t = body.get("time")
    if isinstance(t, str) and t:
        try:
            from datetime import datetime
            return datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").timestamp() - \
                time.timezone
        except ValueError:
            return 0.0
    return 0.0


def _log_state_event(body: dict, detail: dict, st: str, key: str,
                     applied: bool) -> None:
    """One line per Batch state event, with WALL CLOCK time and the delivery lag.

    Every event, including the ones suppressed as stale — a suppressed event is
    precisely what you want to see when a cell looks wrong, and omitting them
    would hide the evidence. ~1,300 lines on a 336-chunk run, written to the
    per-job log on disk, so a timing question can be answered after the fact
    instead of by reproducing it.

    `lag` is delivery latency: now, minus when Batch says the transition
    happened. That is the number that tells you whether a late-looking cell is
    the channel or the encoder.
    """
    when = _event_epoch(body, detail)
    now = time.time()
    lag = f"{now - when:+.1f}s" if when else "  n/a"
    wall = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
    host = (detail.get("container") or {}).get("containerInstanceArn") or ""
    print(f"[state] {wall} lag={lag:>7} {st:<8} "
          f"{'' if applied else 'SUPPRESSED '}{key}"
          f"{' host=' + host.rsplit('/', 1)[-1][:12] if host else ''}",
          flush=True)


def _drain_state(url: str | None, log_state: dict) -> int:
    """Apply queued Batch state transitions. Returns how many were applied.

    Replaces the per-poll `list_jobs` census for the common case. Every
    transition goes through _emit_stage, which is what makes an out-of-order
    delivery harmless — EventBridge is at-least-once and unordered, so this
    channel needs the same guard as every other one, not a weaker one.
    """
    if not url:
        return 0
    sqs = _sqs()
    applied = 0
    hosts: list = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < _TELEMETRY_DRAIN_BUDGET_S:
        try:
            resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10,
                                       WaitTimeSeconds=1 if not applied else 0)
        except ClientError:
            break
        msgs = resp.get("Messages") or []
        if not msgs:
            break
        try:
            sqs.delete_message_batch(
                QueueUrl=url,
                Entries=[{"Id": str(n), "ReceiptHandle": m["ReceiptHandle"]}
                         for n, m in enumerate(msgs)])
        except ClientError:
            pass  # redelivery is harmless: _emit_stage suppresses the repeat
        for m in msgs:
            try:
                body = json.loads(m.get("Body") or "{}")
                d = body.get("detail") or {}
            except ValueError:
                continue
            key = _stage_key_for_job(d.get("jobName", ""))
            st = _EVENT_STAGE_STATUS.get(d.get("status", ""))
            if not key or not st:
                continue
            ok = _emit_stage(key, st, 100.0 if st == "done" else None,
                             src="event")
            applied += 1 if ok else 0
            _log_state_event(body, d, st, key, ok)
            # The instance is IN the event from STARTING onward, so a chunk is
            # coloured by the same message that says it started — no
            # describe_jobs, and no window where a running cell is uncoloured.
            arn = (d.get("container") or {}).get("containerInstanceArn")
            if arn and st in ("starting", "running", "done"):
                hosts.append((d.get("jobName", ""), arn))
    for jobname, arn in hosts:
        _tag_host_from_arn(jobname, arn, log_state)
    if applied:
        log_state["_state_applied"] = log_state.get("_state_applied", 0) + applied
    return applied


def _stage_key_for_job(jobname: str) -> str | None:
    """`encode:` stage key for a variant Batch job, or None if it isn't one.

    Mirrors encode_variants.variant_stage_key, which is what the workers emit and
    what the Go control plane keys stages by.

    package-all is still absent, deliberately: ONE such job backs THREE stages
    (package / fragments / hls), so it has no single key and its sub-stages come
    from tailing the running container.

    mezzanine and audio ARE mapped, because they used to reach the grid only via
    _translate_events reading Step Functions history — and that was the last
    reason for a third source of stage state to exist.
    """
    if jobname.startswith("mezz-"):
        return "mezzanine"
    if jobname.startswith("audio-"):
        return "audio"
    m = _CHUNK_JOBNAME_RE.match(jobname)
    if m:
        return f"encode:{m.group(1)}:{m.group(2)}:chunk{int(m.group(3))}"
    w = _WHOLE_JOBNAME_RE.match(jobname)
    if w:
        return f"encode:{w.group(1)}:{w.group(2)}"
    return None


# A forwarded worker STAGE line, so its status can be recorded alongside the
# Batch-derived ones — see _sync_stages_from_batch on why they must not fight.
_STAGE_LINE_RE = re.compile(r"^\[\[ENCODER-STAGE key=(\S+) status=(\S+) percent=([0-9.]+)\]\]")

# Batch job status -> the stage status the UI grid renders.
_BATCH_STAGE_STATUS = {
    # RUNNABLE/SUBMITTED are here so the FALLBACK census still shows a chunk as
    # queued. They cost nothing: _exec_jobs_snapshot buckets every status from
    # one scan. Without them, a run with no event channel would leave chunks at
    # `pending` until they started, because _translate_events no longer speaks.
    "SUBMITTED": "queued",
    "RUNNABLE": "queued",
    "RUNNING": "running",
    # STARTING = placed on an instance, creating the container / pulling the
    # image, not yet encoding. Distinct from queued (waiting for a slot, no
    # machine yet) and from running (actually encoding). The UI hatches it, the
    # same way the fleet view hatches a box that is booting or pulling.
    "STARTING": "starting",
    "SUCCEEDED": "done",
    "FAILED": "failed",
}
# Stage statuses that must never be walked back: once a chunk is finished, a
# later poll seeing a stale RUNNING (or a retry attempt) must not un-finish it.
_TERMINAL_STAGE = {"done", "failed"}

# Batch job status -> stage status for the EVENT path, derived from the mapping
# the poll already used so the grid renders identically whichever channel
# delivered the transition. Defined HERE, after its base: it was originally
# placed next to the event code above, which made cli_batch raise NameError on
# import — the orchestrator would not have started at all.
_EVENT_STAGE_STATUS = dict(_BATCH_STAGE_STATUS, RUNNABLE="queued",
                           SUBMITTED="queued")


def _sync_stages_from_batch(exec_name: str, log_state: dict) -> None:
    """Drive `encode:*` stage state from Batch job status, not from log markers.

    Stage state used to come only from [[ENCODER-STAGE]] lines the workers wrote
    to CloudWatch, which the poll tailed. That systematically under-reported
    running work: a chunk shorter than the poll interval is never observed
    RUNNING, and its running+done markers arrive in the same poll, so the cell
    went queued -> done and never rendered as running. Measured on a 343-chunk
    run, the grid showed 1 running while Batch actually had 25 — so a fully busy
    fleet displayed as idle, which is exactly when someone is watching it.

    Batch's own job status is authoritative and complete: every job is in exactly
    one state, and listing by state is a full census rather than a sample. So the
    count of currently-running chunks becomes exact, bounded only by the poll
    interval, with no dependency on CloudWatch ingestion.

    Emits only on CHANGE (tracked in log_state) so the control plane sees one
    transition per stage, and never regresses out of a terminal status.

    Deliberately a BACKSTOP, not a replacement. Worker STAGE lines carry a live
    percent; these carry none, and the control plane assigns Percent
    unconditionally, so re-announcing a stage the worker already reported would
    reset its progress bar to 0 on every poll. _tail_progress records the
    statuses it forwards into the same map, so a stage the worker has spoken for
    is skipped here and only the gaps — chunks whose logs have not been ingested
    yet, or that were never observed RUNNING at all — are filled in.
    """
    newly_announced: list[str] = []
    repairs = 0
    for status, stage_status in _BATCH_STAGE_STATUS.items():
        for j in _list_exec_jobs(exec_name, status):
            key = _stage_key_for_job(j.get("jobName", ""))
            if key is None:
                continue
            # _STAGE_STATE, not a private map. Sharing it with the other two
            # emitters is what stops this backstop clobbering a live percent:
            # the worker announces "running 0.6%", this sees the SAME "running"
            # already recorded, and stays quiet instead of re-stamping 0.0.
            prev = _STAGE_STATE.get(key)
            if prev == stage_status or prev in _TERMINAL_STAGE:
                continue
            if stage_status in ("running", "starting", "done") and j.get("jobId"):
                # Try STARTING as well as RUNNING. Measured, a STARTING job
                # carries containerInstanceArn only SOMETIMES — the status flips
                # around the same time placement is recorded, so it is a race.
                # Attempting it costs one describe we were making anyway, and
                # when the arn is absent _tag_hosts_for_jobs simply skips it and
                # the host lands on the RUNNING transition instead. So a hatched
                # cell is usually machine-coloured, occasionally neutral for a
                # poll — never wrong, just sometimes late.
                newly_announced.append(j["jobId"])
            was_known = key in _STAGE_STATE
            if _emit_stage(key, stage_status,
                           100.0 if stage_status == "done" else None,
                           src="census") and was_known:
                repairs += 1

    # Colour the chunks we just announced, in the same pass — including the ones
    # we are announcing as DONE. A chunk that starts and finishes between polls
    # is never seen running, so it is marked done with no host and renders as an
    # uncoloured cell until _backfill_completed_hosts gets to it a poll or more
    # later. That late attribution repaints a cell that had already settled,
    # which reads as finished blocks filling themselves in again. Tagging on the
    # same pass that announces done means the cell is right the first time.
    #
    # _forward_running_logs already tags hosts, but off its OWN describe_jobs
    # earlier in the poll — so a job that entered the RUNNING census after that
    # call is announced here with no host yet and renders as an uncoloured (blue)
    # cell until a later poll. That window widened when stage state moved to
    # Batch status, because chunks now surface as running sooner than they did
    # when we waited for their log marker. Describing just the newly-announced
    # ids keeps the host no later than the status it belongs to. Bounded by the
    # number of NEW jobs per poll, not the ladder size.
    if repairs:
        # Only the genuine losses are escalated. Seeding is silent in the
        # summary: 44 seed lines on a healthy run would train you to ignore this.
        _narrate(f"[census] REPAIRED {repairs} stage(s) the event path never "
                 f"delivered — see the [census] REPAIRED lines above")
    if newly_announced:
        batch = _batch()
        for i in range(0, len(newly_announced), 100):  # describe_jobs caps at 100
            try:
                described = batch.describe_jobs(
                    jobs=newly_announced[i:i + 100]).get("jobs", [])
            except ClientError:
                continue
            _tag_hosts_for_jobs(described, log_state)


def _tail_progress(stream: str, label: str, log_state: dict,
                   live: bool = True) -> None:
    """Forward new progress-marked lines from one container's stream.
    Tracks the last-seen timestamp per stream so each line is emitted once.

    `live` False means the job has already finished and this is the one-shot
    drain. That distinction matters for ENCODER-FLEET and only for it: CPU is a
    GAUGE, and the control plane stamps arrival time, so replaying a finished
    job's sample would register a reading from seconds ago as the machine's
    current state. Every other marker is a RECORD — losing one loses data that
    cannot be recovered without re-encoding — so those are forwarded either way.
    """
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
        if is_gauge(msg) and not live:
            continue  # a finished job's sample is stale — see the docstring
        if sm := _STAGE_LINE_RE.match(msg):
            # Same chokepoint as the queue path. The fallback must obey the same
            # rule or turning it on would reintroduce the reversals it exists to
            # cover for. _emit_stage prints it, so skip the verbatim forward.
            _emit_stage(sm.group(1), sm.group(2), float(sm.group(3)),
                        src="cwlog")
            continue
        if msg.startswith("[[ENCODER-"):
            # Forward EVERY marker verbatim, not a whitelist. This used to pass
            # only ENCODER-BOOT and ENCODER-STAGE, which meant each new marker
            # was silently dropped until someone noticed — ENCODER-VMAF is
            # computed per chunk and thrown away that way (#141), and adding
            # ENCODER-FLEET would have been the next instance. The Go scanner
            # ignores markers it has no regex for, so forwarding everything
            # costs nothing and makes the next marker work by default.
            # (ENCODER-SPEED still needs _forward_container_timing: it is
            # emitted after the last live poll, so tailing misses it entirely.)
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
               do_h264: bool = True, do_hevc: bool = True,
               sync_back: bool = True) -> None:
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
    # Only when something is packaged in Batch and therefore has to come back
    # from S3. Host packaging (#197) writes straight to the local output dir, so
    # on an all-host run this stage never fires — and a declared stage that never
    # fires is a row that sits pending forever at the end of every run.
    if sync_back:
        keys.append("download:outputs")
    seen: set = set()
    stages = []
    for k in keys:  # de-dupe, preserve order
        if k not in seen:
            seen.add(k)
            stages.append({"key": k, "label": k.replace(":", " ")})
    print(f"[[ENCODER-PLAN {json.dumps(stages)}]]", flush=True)


# Last status announced per stage key. The ONE place that knows what the grid
# currently shows, because three independent sources emit stage state and each
# used to decide alone whether it was allowed to speak:
#
#   _translate_events      Step Functions history (queued on enter, done on exit)
#   _sync_stages_from_batch  Batch job status  (the authoritative census)
#   _drain_telemetry       the worker's own markers, carrying live percent
#
# They observe the same run through channels with DIFFERENT latencies, so they
# routinely disagree about the present tense. SFN history in particular lags
# Batch, so a chunk can be marked done by the census and only afterwards have its
# "entered" event surface — re-announcing a finished chunk as queued, which is a
# filled cell going blank.
_STAGE_STATE: dict[str, str] = {}
# _emit_stage does a read-modify-write — read the previous status, decide, then
# store — and that is NOT atomic just because dict operations are.
#
# It became reachable from two threads the moment each queue got its own drain.
# Without this lock, two drains can both read the same `prev`, both pass the
# guard, and both write: the later writer wins regardless of order, which walks
# a cell backwards. That is the exact defect the guard exists to prevent, so
# leaving it unsynchronised would defeat it in precisely the case it matters.
_STAGE_LOCK = threading.Lock()
# Last percent announced per key. Only the WORKER knows progress: a Batch event
# and the census both carry status and nothing else, so they pass percent=None
# and this supplies the last real value rather than asserting a zero.
_STAGE_PCT: dict[str, float] = {}

# `done` is the only status that may never be walked back. A SUCCEEDED Batch job
# never re-runs, so anything arriving afterwards is stale by construction.
#
# `failed` is deliberately NOT included: the state machine's Retry block
# resubmits a NEW Batch job, so failed -> running is a real transition and
# blocking it would freeze the cell on a dead attempt.
_FINAL_STAGE = {"done"}

# Lifecycle order. A cell may not move BACKWARDS along it.
#
# Guarding `done` alone was not enough. Every channel here is unordered —
# EventBridge is at-least-once with no ordering guarantee, and the census reads a
# snapshot that may already be stale by the time it emits — so a RUNNING event
# can land after the worker has reported 40%, and a STARTING event can land
# after RUNNING. Carrying the percent forward fixed the first case for the bar,
# but not the second: `starting` renders EMPTY whatever the percent is, so a late
# STARTING blanked a cell that was visibly progressing.
#
# So the rule is about the lifecycle, not about one field.
_STAGE_RANK = {
    "pending": 0, "queued": 1, "starting": 2,
    "running": 3, "reclaimed": 3, "failed": 4, "done": 5,
}
# Statuses that may always be announced: they are interruptions, not progress,
# and a chunk genuinely can go from running to reclaimed or failed.
_STAGE_INTERRUPTS = {"failed", "reclaimed"}


def _rendered_width(status: str | None, percent: float | None) -> float:
    """What the UI actually paints, mirroring static/index.html.

    Kept in step with the grid deliberately: "the cell showed less colour" is a
    statement about WIDTH, and width is not the percent field — done and reused
    paint full regardless, queued and starting paint empty regardless. Comparing
    raw percent would miss exactly the transitions being asked about.
    """
    if status in (None, "pending", "queued", "starting"):
        return 0.0
    if status in ("done", "skipped", "reclaimed"):
        return 100.0
    return float(percent or 0.0)


def _emit_stage(key: str, status: str, percent: float | None = 0.0,
                src: str = "") -> bool:
    """Announce a stage transition. Returns False if it was suppressed as stale.

    Every stage emission in this process goes through here — that is the point.
    Guarding at the call sites is what failed: the drain grew a terminal check,
    and reversals continued, because _translate_events was marking chunks done
    from SFN history through a channel the check could not see.
    """
    with _STAGE_LOCK:
        return _emit_stage_locked(key, status, percent, src)


def _emit_stage_locked(key: str, status: str, percent: float | None,
                       src: str) -> bool:
    """The body of _emit_stage. Callers must hold _STAGE_LOCK."""
    prev = _STAGE_STATE.get(key)
    if prev in _FINAL_STAGE and status not in _FINAL_STAGE:
        return False
    # No going backwards. `failed` is exempt as the PREVIOUS state, because the
    # state machine's Retry resubmits a new Batch job and failed -> running is
    # real; and interrupts are exempt as the NEW state, because a running chunk
    # genuinely can be reclaimed.
    if (prev is not None and prev != "failed" and status not in _STAGE_INTERRUPTS
            and _STAGE_RANK.get(status, 0) < _STAGE_RANK.get(prev, 0)):
        return False
    # percent=None means "I do not know" — carry the last known value forward.
    #
    # Batch events and the census know STATUS and nothing else. They used to
    # pass 0.0, which is not ignorance but a claim, and it clobbered the live
    # percent the worker had already reported: a chunk at 40% dropped to 0 and
    # climbed again the moment its RUNNING event landed. The worker emits its
    # first progress marker as ffmpeg starts, while the event lags 0.2-2.4s, so
    # the worker routinely gets there first and was routinely overwritten.
    if percent is None:
        percent = _STAGE_PCT.get(key, 0.0)
    # Never let a non-terminal announcement lower the bar either. Two workers
    # cannot report one chunk, but a redelivered marker can arrive late.
    elif (status not in _FINAL_STAGE and status == prev
            and percent < _STAGE_PCT.get(key, 0.0)):
        percent = _STAGE_PCT[key]
    # ANY emission that makes a cell show LESS colour is logged, whatever the
    # source. The [state] log only ever covered Batch events, while the worker's
    # percent markers — the main driver of fullness — went through here silently.
    # This mirrors the UI's own width rule so the log answers the question the
    # grid raises, rather than a proxy for it.
    before, after = _rendered_width(prev, _STAGE_PCT.get(key)), \
        _rendered_width(status, percent)
    if abs(after - before) > 0.05:
        # EVERY width change, both directions — not just the drops.
        #
        # Logging only decreases was the obvious economy and it is useless for
        # the question being asked: "filled then zeroed" cannot be explained
        # without the record of what FILLED it. The drop names a victim; the
        # rise names the culprit.
        arrow = "DOWN" if after < before else "up"
        print(f"[fill] {time.strftime('%H:%M:%S')} {arrow:>4} {key} "
              f"{before:.0f}% -> {after:.0f}%  ({prev or 'unset'} -> {status}) "
              f"via {src or 'worker'}", flush=True)
    _STAGE_STATE[key] = status
    _STAGE_PCT[key] = percent
    print(f"[[ENCODER-STAGE key={key} status={status} percent={percent:.1f}]]",
          flush=True)
    # The BACKSTOP is logged whenever it actually changes something, because
    # "is the poll fighting the events?" is otherwise unanswerable — a census
    # emission looks identical to an event one in the grid.
    #
    # When events are healthy this should be SILENT: every key already carries
    # the state the census finds, so the caller skips it before reaching here.
    # Any line at all means the event path missed that transition, and the
    # `prev` value says whether this was a repair (prev is behind) or a race
    # (prev is ahead, which _FINAL_STAGE would have blocked).
    if src == "census":
        # Distinguish the two cases, because they mean opposite things and
        # conflating them makes the signal useless.
        #
        # prev unset = the census simply got there FIRST. Normal at fan-out: it
        # runs on the first poll while the first events are still in flight
        # (measured: census at 14:16:21, first events 14:16:51). Both sources
        # agree; nothing was missed.
        #
        # prev set = the event path delivered an EARLIER state and then never
        # delivered this one. That is a genuinely lost transition, and the one
        # worth investigating.
        kind = "seeded" if prev is None else "REPAIRED"
        print(f"[census] {time.strftime('%H:%M:%S')} {kind} {key}: "
              f"{prev or 'unset'} -> {status}", flush=True)
    return True


def _emit_reused(key: str) -> None:
    print(f"[[ENCODER-REUSED key={key}]]", flush=True)


def _emit_reused_chunks(s3_prefix: str) -> None:
    """At poll start, flag every chunk output a PRIOR run already staged as
    reused. This execution hasn't produced any chunks yet, so anything under the
    prefix now is left over from a cancelled/failed run — the SFN re-runs its
    Batch job but cli_phase skips the encode when the output exists, so the UI
    should mark the cell 'reused' rather than colour it as a fresh encode.
    Best-effort; never breaks the poll."""
    if not s3_prefix.startswith("s3://"):
        return
    rest = s3_prefix[len("s3://"):].rstrip("/")
    bucket, _, key = rest.partition("/")
    if not bucket:
        return
    prefix = f"{key}/" if key else ""
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                m = _CHUNK_OBJ_RE.match(obj["Key"].rsplit("/", 1)[-1])
                if m:
                    _emit_reused(f"encode:{m.group(1)}:{m.group(2)}:chunk{int(m.group(3))}")
    except Exception:  # noqa: BLE001 — cosmetic; must never fail the poll
        return


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
    batch = _batch()
    status_by_id, name_by_id, ids = {}, {}, []
    for status in ("RUNNABLE", "STARTING", "RUNNING"):
        for j in _list_exec_jobs(exec_name, status):
            jn = j.get("jobName", "")
            if jn.startswith("var-"):
                ids.append(j["jobId"])
                status_by_id[j["jobId"]] = status
                name_by_id[j["jobId"]] = jn
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
                _emit_stage(stage, "reclaimed", 0.0, src="reclaim")


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
    # Create the telemetry queue BEFORE starting the execution. The mezzanine
    # job is submitted the instant the execution starts, so a queue created
    # afterwards would be raced by the first worker's GetQueueUrl — which fails
    # closed (that worker silently drops to stdout for its whole life).
    if name := kwargs.get("name"):
        _create_telemetry_queue(name)
    # Sweep queues stranded by earlier runs that were killed mid-flight. Submit
    # is a good moment for it — already talking to SQS, off the latency-sensitive
    # poll path — but it is NOT the only trigger, and must not be: an orphan
    # cleaned up only by the next cloud encode persists forever once you stop
    # encoding (#191). The server sweeps on its own hourly cadence via the `gc`
    # subcommand below; this one keeps a busy account tidy between those ticks.
    _gc_telemetry_queues(args.state_machine_arn)
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
    """Narrate Step Functions history. NO stage state — see below.

    This used to announce stage state too (queued on enter, done on exit), which
    made it a THIRD source alongside the Batch census and the worker's markers.
    All three watch the same run through channels with different latencies, so
    they disagree about the present tense — and SFN history is the slowest of
    them, so it was routinely the one re-announcing a finished chunk as queued.
    That is the reversal chased through three separate fixes.

    Batch events now carry every transition this could report, including
    mezzanine and audio (the last stages that reached the grid only from here).
    So this keeps the two things history is genuinely the best source for: the
    human narration line per step, and spot-reclaim reporting off the exit
    events. One source of state, not three.
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
            # SUBMITTED, not running. Entering the state means the job was handed
            # to Batch; it then sits RUNNABLE until a slot frees and only becomes
            # RUNNING once placed on an instance. Emitting "running" here claimed
            # the whole ladder was encoding the instant it fanned out — 336 chunks
            # "running" against a handful actually executing — and, worse, marked
            # them running BEFORE any machine existed to attribute them to, since
            # containerInstanceArn is only assigned at placement. That is what
            # rendered chunks (and the package phase) as uncoloured blue cells:
            # not a failed host lookup, but a status applied before a host could
            # possibly be known.
            #
            # "queued" is what the state actually is, and it makes "running" mean
            # PLACED — at which point _sync_stages_from_batch announces it and
            # tags its host in the same pass, so a running cell is coloured from
            # the moment it appears. _sync_stages_from_batch is now the single
            # source for the RUNNABLE/STARTING/RUNNING split, emitting once per
            # transition rather than re-stamping every cell each poll.
            name = ev.get("stateEnteredEventDetails", {}).get("name", "")
            key = _STEP_TO_STAGE.get(name)
            if key:
                _narrate(f"▶ {key.replace(':', ' ')} submitted")
            elif name == "EncodeChunk":
                idn = chunk_enter.get(ev["id"])
                if idn:
                    c, t, ci = idn
                    _narrate(f"▶ encode {c} {t} chunk{ci} submitted")
            elif name == "EncodeWhole":
                idn = whole_enter.get(ev["id"])
                if idn:
                    c, t = idn
                    _narrate(f"▶ encode {c} {t} submitted")

        elif etype == "TaskStateExited":
            name = ev.get("stateExitedEventDetails", {}).get("name", "")
            key = _STEP_TO_STAGE.get(name)
            if key:
                _narrate(f"✓ {key.replace(':', ' ')} done")
                _report_reclaims(ev, key.replace(':', ' '))
            elif name == "EncodeChunk":
                idn = _enter_of(ev, chunk_enter)
                if idn:
                    c, t, ci = idn
                    _narrate(f"✓ encode {c} {t} chunk{ci} done")
                    # reclaims handled off TaskSucceeded (ResultPath:null here)
            elif name == "EncodeWhole":
                idn = _enter_of(ev, whole_enter)
                if idn:
                    c, t = idn
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


def _parallel_get(s3, bucket: str, items: list, stage_key: str) -> tuple[int, int]:
    """Download (key, size, dst) triples concurrently, emitting stage progress.

    Shared by the end-of-run sync-back and the on-demand `fetch`, so both report
    progress the same way and there is one place where the thread count lives.

    PARALLEL, because this transfer is round-trip bound rather than bandwidth
    bound. Packaged HLS output is thousands of tiny files — 1,486 objects at a
    28 KB median on a measured 12-rung run — and fetching them one at a time
    spends almost all of its time waiting.

    Measured in the orchestrator container, 114 objects / 246.7 MB:

      sequential download_file        29.9s    8.3 MB/s   <- what this was
      ThreadPoolExecutor(16)           4.8s   51.2 MB/s
      ThreadPoolExecutor(32)           4.1s   59.9 MB/s
      TransferManager(16)              4.4s   56.3 MB/s
      TransferManager(32)              4.8s   50.9 MB/s

    A plain thread pool matches or beats s3transfer's TransferManager here, so
    the simpler tool wins: TransferManager exists to orchestrate MULTIPART
    transfers, and at a 28 KB median nothing crosses the 8 MB multipart
    threshold — all that machinery does nothing but coordinate.

    64. This default has moved twice, and BOTH earlier values were measuring
    the destination disk rather than this code:

      32 — benchmarked against container-local /tmp, which is not where this
           writes. Wrong destination.
      16 — benchmarked against the real destination (TMP_DIR), which was then
           an NVMe enclosure negotiating USB 2.0 at 480 Mbps. It capped every
           thread count at ~36 MB/s and collapsed to 8.4 MB/s at 32 threads,
           so 16 looked like a plateau with a cliff just past it.

    With that enclosure on Thunderbolt (3.2 GB/s, ~90x) the disk contributes
    nothing and the transfer is round-trip bound as originally described.
    Re-measured through this path, real prefix, real destination, 1486
    objects / 2512 MB, repeated runs:

        1 thread    9.1 MB/s        32 threads  71.8 / 77.6 / 77.7 MB/s
        4 threads  27.1 MB/s        64 threads  90.3 / 90.3 / 91.9 MB/s
        8 threads  41.4 MB/s       128 threads  95.6 / 96.8 / 98.4 MB/s
       16 threads  58.3 / 55.5 / 32.0 MB/s     192 threads  40.9 MB/s  <- cliff

    64 rather than 128: 128 is ~7% faster but sits close to a collapse that is
    worse than doing nothing, and the knee is a property of the local network
    path (~730 Mbps here), so it will not sit in the same place everywhere.
    64 keeps 3x headroom below the cliff for most of the gain.

    Note 16 is not merely slower but ERRATIC (32.0-58.3 across repeats), which
    is why live runs on it landed anywhere from 44 to 64 MB/s."""
    if not items:
        return 0, 0
    total_bytes = sum(s for _, s, _ in items) or 1
    workers = max(1, int(os.environ.get("DOWNLOAD_CONCURRENCY", "64")))
    done = {"bytes": 0, "pct": -1.0}
    lock = threading.Lock()

    def _one(item) -> None:
        key, size, dst = item
        dst.parent.mkdir(parents=True, exist_ok=True)
        # boto3 clients are thread-safe for this; the per-thread cost is a
        # connection from the shared pool, which is why the pool size and
        # the worker count are kept in step.
        s3.download_file(bucket, key, str(dst))
        with lock:
            done["bytes"] += size
            pct = done["bytes"] / total_bytes * 100.0
            if pct - done["pct"] >= 1.0:   # throttle: at most ~100 markers
                done["pct"] = pct
                _emit_stage(stage_key, "running", pct)

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(_one, it) for it in items]):
            fut.result()   # surface the first failure rather than silently
    dt = time.monotonic() - t0
    _emit_stage(stage_key, "done", 100.0)
    _narrate(f"    downloaded {len(items)} objects, {total_bytes / 1e6:.0f} MB in "
             f"{dt:.0f}s ({total_bytes / 1e6 / max(dt, 0.001):.1f} MB/s, "
             f"{workers} threads)")
    return len(items), total_bytes


# Suffixes that make up ~99.85% of a packaged output's bytes. Excluding these
# leaves manifests, init segments and encode metadata — enough for the Outputs
# tab to describe the run without the media (#214).
#
# Stated as an EXCLUSION rather than an allow-list on purpose: a new metadata
# file added to the packager should ship by default, not be silently dropped.
_MEDIA_SUFFIXES = (".m4s", ".byteranges")


def _local_codec_dir(output_stem: str, codec: str, output_tag: str) -> str:
    """`<stem>_<codec>[_<tag>]` — one spelling of a cloud run's local output dir.

    The profile tag goes AFTER the codec so the `_p200_<codec>` shape that
    OutputStem / resolveCodec / the watcher all key off stays intact. Two callers
    now need this name — the sync-back that downloads a Batch-packaged run, and
    host packaging (#197), which produces the directory directly — and they must
    not spell it differently or the same clip lands in two places.
    """
    return f"{output_stem}_{codec}" + (f"_{output_tag}" if output_tag else "")


# cli_phase's own fetch measurement, relayed so a host-packaged run still
# accounts for the egress it caused. Kept as a contract with the print in
# phase_package_all: change the wording there and the cost quietly reads zero,
# so scripts/test_host_package.py pins the two together.
_PKG_FETCH_RE = re.compile(
    r"\[phase package-all\] fetched (\d+) objects, ([\d.]+) MB")


def _package_on_host(codecs: list[str], s3_prefix: str, local_dir: Path,
                     output_stem: str, output_tag: str,
                     segment_duration: str, partial_duration: str) -> DownloadResult:
    """Run the packaging chain on the CONTROL PLANE instead of in a Batch job.

    The tail-side twin of #266. Same reasoning, opposite end of the pipeline: the
    phase already exists, so this shells out to the SAME `cli_phase package-all`
    the pkgall Batch job runs rather than reimplementing join/Shaka/HLS a second
    time. It differs only in `--s3-out` being a local directory (see
    cli_phase._deliver_dir).

    ## What it removes, measured on two runs post-#209 (1785781612611, 1785863996086)

    The post-encode tail was 4m18s on both, and only ~2m of it was work:

        +0s .. +43s     nothing — Batch queue wait + container start for pkgall
        +43s .. +1m48s  package:h264 (fetch + join + Shaka)
        +1m56s          fragments + hls, ~10s
        +1m57s .. +3m52s  nothing — state machine exit + orchestrator poll
        +3m52s .. +4m18s  download:outputs, 26s

    141s of 258s was dead time bracketing the Batch job, and `download:outputs`
    re-fetches what packaging had just uploaded. Neither survives packaging here.

    ## The bandwidth bet, and why it is already settled

    #197 flags home bandwidth as the risk: the chunks now come down the home link
    instead of being read in-region. But `download:outputs` already moves a
    comparable volume — the packaged ladder — over that same link, and does it at
    96.6 MB/s in 26s. The link is not the constraint it was when the issue was
    filed with a 6m28s sync-back; that number was a USB 2.0 disk and
    DOWNLOAD_CONCURRENCY=16, both since fixed.

    ## No state machine change

    do_h264 / do_hevc / do_av1 gate ONLY the per-codec packaging Choice states, so
    the caller turning them off is enough to skip the whole PerCodec branch —
    exactly how #266 skipped Mezzanine through the existing MezzCheck. Nothing in
    infra/ changes and no deploy is required; rebuild the server and it takes
    effect.

    ## No Batch retry

    Named as a risk in #197 and it is real: this runs once, and a failure fails
    the job rather than being resubmitted onto fresh spot capacity. It is a
    deliberate trade — the phase is minutes of local CPU on a machine that is
    already up, not an hour of spot-reclaimable work — but it is the reason the
    error below names the codec and tells you the chunks are still in S3, since
    re-running with the flag off packages them without re-encoding anything.
    """
    fetched = {"files": 0, "bytes": 0}
    for codec in codecs:
        work = local_dir / f"pkg-work-{codec}"
        # Each codec gets its own scratch: cli_phase._prepare_work_dir rmtree's
        # ENCODER_WORK_DIR on entry, so a shared one would delete a sibling's
        # inputs the moment two codecs ran together.
        env = dict(os.environ)
        env["ENCODER_WORK_DIR"] = str(work)
        # Read off the execution input rather than this process's environment, so
        # the packaging cannot disagree with the timing the chunks were encoded
        # to. A segment duration mismatch here would produce playlists whose
        # boundaries do not land on the media's keyframes.
        env["SEGMENT_DURATION"] = segment_duration
        env["PARTIAL_DURATION"] = partial_duration
        # Deliberately NOT set: ENCODER_TELEMETRY_EXEC. On the host, stdout IS
        # the channel to the server, so the SQS sink would be a second copy of
        # markers that already arrive. telemetry.emit degrades to stdout-only
        # when it is absent, which is precisely the wanted behaviour.
        env.pop("ENCODER_TELEMETRY_EXEC", None)

        print(f"    packaging {codec} on the host (no Batch job, no sync-back)",
              flush=True)
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-m", "infinite_streaming_encoder.cli_phase",
             "package-all", "--codec", codec,
             "--s3-variants", s3_prefix, "--s3-audio", s3_prefix,
             "--s3-out", str(local_dir)],
            env=env, stdout=subprocess.PIPE, text=True, bufsize=1)
        # Relay rather than inherit, so the phase's own fetch measurement can be
        # picked up on the way past. It has to be: with packaging here, the bytes
        # billed as egress are the CHUNKS this pulls, not the packaged output
        # _download_outputs no longer fetches. Leaving it unaccounted would make
        # a host-packaged run look nearly free next to a Batch-packaged one, and
        # CLAUDE.md's rule is that the estimate and the finished cost stay on one
        # basis. Every line is still printed, so stage markers reach the server.
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(line, flush=True)
            m = _PKG_FETCH_RE.search(line)
            if m:
                fetched["files"] += int(m.group(1))
                fetched["bytes"] += int(float(m.group(2)) * 1e6)
        rc = proc.wait()
        shutil.rmtree(work, ignore_errors=True)
        if rc != 0:
            raise RuntimeError(
                f"host packaging failed for {codec} (exit {rc}). The encoded "
                f"chunks are still in {s3_prefix} — re-running with host "
                f"packaging disabled will package them without re-encoding.")

        # cli_phase writes output_<codec>/; the local contract is
        # <stem>_<codec>[_<tag>]/ so codecs of one clip coexist in OUTPUT_DIR.
        src = local_dir / f"output_{codec}"
        if output_stem:
            dst = local_dir / _local_codec_dir(output_stem, codec, output_tag)
            if dst.exists():
                shutil.rmtree(dst)
            src.rename(dst)   # same filesystem — a rename, not a copy
            src = dst
        print(f"    packaged {codec} in {time.monotonic() - t0:.0f}s -> {src.name}",
              flush=True)
    return DownloadResult(fetched["files"], fetched["bytes"], 0, 0)


class DownloadResult(NamedTuple):
    """What a sync-back moved, and what it deliberately left in S3."""
    files: int
    bytes: int
    skipped_files: int
    skipped_bytes: int


def _download_outputs(s3_prefix: str, local_dir: Path, output_stem: str = "",
                      output_tag: str = "", include_media: bool = True,
                      ) -> DownloadResult:
    """Mirror s3://.../<prefix>/output_<codec>/ into local_dir.

    The state machine writes each codec's packaged dir as output_<codec>/. When
    output_stem is given, each is downloaded to a per-codec top-level directory
    named <output_stem>_<codec>[_<output_tag>]/ (e.g. myclip_p200_hevc_xs/) —
    matching the local pipeline's naming contract (OutputStem + codec + profile
    tag appended LAST so the _p200_<codec> shape smashing keys off stays intact).
    That lets moveTmpToOutput move each codec independently, so codecs of the same
    clip COEXIST in OUTPUT_DIR instead of one replacing the other's <stem> wrapper.
    Without output_stem (legacy), the raw output_<codec>/ layout is preserved.

    include_media=False fetches METADATA ONLY — everything except _MEDIA_SUFFIXES.
    Measured at 3.99 MB of a 2.63 GB output dir (0.151%), which is the point: S3
    egress is billed per GB and was 3x the compute on the 1-3 Aug bill. (Costing
    treats every GB as billable — the monthly free allowance is not modelled, so
    a run's cost does not depend on the date.) The directory LAYOUT is preserved
    either way, so
    parseOutputMeta still infers resolutions from subdirectory existence and the
    run lists normally in the Outputs tab. Fetch the media later with
    `cli_batch.py fetch`."""
    if not s3_prefix.startswith("s3://"):
        return DownloadResult(0, 0, 0, 0)
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
        return local_dir / _local_codec_dir(output_stem, codec, output_tag) / tail

    # List everything first so the sync-back can drive a real progress bar
    # (weighted by bytes — segment counts vary wildly in size). This runs in the
    # driver, whose stdout is forwarded straight to the app, so the marker needs
    # no CloudWatch round-trip.
    objs: list[tuple[str, int]] = []
    skipped_files = skipped_bytes = 0
    # Per output_<codec> tally of what was left behind, so each destination dir
    # gets its own sidecar naming the exact prefix a later fetch must pull.
    pending: dict[str, list[int]] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{base_key}/output_"):
        for obj in page.get("Contents", []):
            key, size = obj["Key"], obj.get("Size", 0)
            if not include_media and key.endswith(_MEDIA_SUFFIXES):
                skipped_files += 1
                skipped_bytes += size
                head = key[len(base_key) + 1:].partition("/")[0]
                tally = pending.setdefault(head, [0, 0])
                tally[0] += 1
                tally[1] += size
                continue
            objs.append((key, size))

    total_bytes = sum(s for _, s in objs) or 1
    if not objs:
        return DownloadResult(0, 0, skipped_files, skipped_bytes)
    _emit_stage("download:outputs", "running", 0.0)

    items = []
    for key, size in objs:
        dst = _dst_for(key[len(base_key) + 1:])
        if dst is not None:
            items.append((key, size, dst))

    _parallel_get(s3, bucket, items, "download:outputs")
    if skipped_files:
        for head, (n_pend, b_pend) in sorted(pending.items()):
            dst = _dst_for(f"{head}/x")
            if dst is None:
                continue
            _write_remote_sidecar(dst.parent, f"s3://{bucket}/{base_key}/{head}",
                                  n_pend, b_pend, bucket, base_key)
        _narrate(f"    left {skipped_files} media objects "
                 f"({skipped_bytes / 1e6:.0f} MB) in S3 — fetch on demand")
    return DownloadResult(len(objs), total_bytes, skipped_files, skipped_bytes)


# Matches the `expire-job-staging` lifecycle rule in infra/terraform. Read from
# the bucket when permitted (below) so the two cannot silently drift; this is
# only the fallback when the IAM principal lacks GetLifecycleConfiguration.
_STAGING_EXPIRY_DAYS = 7

REMOTE_SIDECAR = ".remote.json"


def _staging_expiry_days(s3, bucket: str) -> int:
    """Days until `jobs/` objects expire, read from the bucket's own lifecycle."""
    try:
        cfg = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception:  # noqa: BLE001 — permission or no config; use the default
        return _STAGING_EXPIRY_DAYS
    for rule in cfg.get("Rules", []):
        if rule.get("Status") != "Enabled":
            continue
        prefix = (rule.get("Filter") or {}).get("Prefix", "")
        days = (rule.get("Expiration") or {}).get("Days")
        if days and prefix and prefix.rstrip("/") == "jobs":
            return int(days)
    return _STAGING_EXPIRY_DAYS


def _write_remote_sidecar(dst_dir: Path, s3_prefix: str, files: int, size: int,
                          bucket: str, base_key: str) -> None:
    """Record that this output's media is still in S3, and when it evaporates.

    A file rather than a stdout marker: it moves with the directory through
    moveTmpToOutput, survives a server restart, and needs no ordering contract
    between the orchestrator's output and the control plane's log scanner."""
    days = _staging_expiry_days(_s3(), bucket)
    now = time.time()
    payload = {
        "s3_prefix": s3_prefix,
        "pending_files": files,
        "pending_bytes": size,
        "recorded_at": _iso(now),
        # Advisory: the lifecycle clock runs from each object's own creation, so
        # this is the floor, not a guarantee. Shown in the UI so a run is fetched
        # before it lapses rather than found missing afterwards.
        "expires_at": _iso(now + days * 86400),
        "expiry_days": days,
    }
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / REMOTE_SIDECAR).write_text(json.dumps(payload, indent=2))
    except OSError as e:
        print(f"    warn: could not write {REMOTE_SIDECAR} in {dst_dir}: {e}",
              flush=True)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# cmd_fetch's "the prefix is empty, there is nothing to download and there never
# will be" exit. A distinct code because the caller has to tell it apart from a
# transfer that died halfway — one is worth retrying and the other is not, and
# both print about S3. Mirrored by exitStagingGone in internal/encode/remote.go.
EXIT_STAGING_GONE = 4


def _mark_sidecar_gone(sidecar: Path, meta: dict, reason: str) -> None:
    """Record on the sidecar that the staging prefix no longer holds the media.

    Set rather than delete. Deleting it would reclassify the output as COMPLETE
    — right name, right rung subdirs, manifests present — so the UI would offer
    Play and every segment would 404 (#225). Keeping it preserves the record of
    what was lost: which prefix, how much, and when it was expected to expire.
    """
    meta = dict(meta)
    meta["gone"] = True
    meta["gone_detected_at"] = _iso(time.time())
    meta["gone_reason"] = reason
    try:
        tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(sidecar)   # atomic: the sidecar IS the state
    except OSError as e:
        print(f"    warn: could not mark {sidecar} gone: {e}",
              file=sys.stderr, flush=True)


def _local_media_count(out_dir: Path) -> int:
    """How many media objects are already on disk under an output dir.

    Used only to tell "the prefix was cleared before we fetched" from "the fetch
    finished and then the prefix was cleared". Both list zero objects, but only
    the first has actually lost anything."""
    return sum(1 for p in out_dir.rglob("*")
               if p.is_file() and p.name.endswith(_MEDIA_SUFFIXES))


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def cmd_fetch(args) -> int:
    """Pull the media a --no-media poll left in S3, into an existing output dir.

    Idempotent and re-entrant, which the Outputs tab's Download button relies on:

      * no sidecar -> nothing pending, exit 0. A second click is a no-op, and a
        fully-fetched dir stays fetched.
      * objects already on disk at the right size are skipped, so an interrupted
        fetch resumes instead of re-paying for what already landed. Size comes
        from the listing, so this costs no extra request.

    A size check is the right tool HERE and the wrong one for de-duplicating
    re-encodes (#214): these are the same S3 objects, not a fresh encode of the
    same source."""
    out_dir = Path(args.dir)
    sidecar = out_dir / REMOTE_SIDECAR
    if not sidecar.exists():
        print(f"    nothing pending: no {REMOTE_SIDECAR} in {out_dir}", flush=True)
        return 0
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, ValueError) as e:
        print(f"!!! unreadable {sidecar}: {e}", file=sys.stderr)
        return 1

    s3_prefix = meta.get("s3_prefix", "")
    if not s3_prefix.startswith("s3://"):
        print(f"!!! {sidecar} has no usable s3_prefix", file=sys.stderr)
        return 1
    bucket, _, base_key = s3_prefix[len("s3://"):].rstrip("/").partition("/")

    # Already known gone — a clear-time invalidation (#225) got here first. Say
    # so without paying for a listing that will come back empty again.
    if meta.get("gone"):
        print(f"!!! media no longer in S3: {s3_prefix} was emptied "
              f"({meta.get('gone_reason', 'reason not recorded')}, detected "
              f"{meta.get('gone_detected_at', '?')}). Re-encode to recreate it.",
              file=sys.stderr, flush=True)
        return EXIT_STAGING_GONE

    s3 = _s3()
    items, have, listed = [], 0, 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{base_key}/"):
        for obj in page.get("Contents", []):
            listed += 1
            key, size = obj["Key"], obj.get("Size", 0)
            dst = out_dir / key[len(base_key) + 1:]
            if dst.exists() and dst.stat().st_size == size:
                have += 1
                continue
            items.append((key, size, dst))

    # An EMPTY listing is not "nothing left to do" — it is the prefix having
    # ceased to exist, which "not yet expired" was being used as a proxy for
    # (#225). The two used to share the branch below, so a deleted prefix
    # silently cleared its own sidecar and the output was reclassified as
    # complete. Everything else on disk agrees with that lie.
    #
    # The one benign reading is that the media already landed and the prefix was
    # cleared afterwards, so check the disk before crying loss.
    if listed == 0:
        want = meta.get("pending_files") or 0
        if want and _local_media_count(out_dir) >= want:
            if not args.dry_run:
                sidecar.unlink(missing_ok=True)
            print("    already complete: staging is empty but all "
                  f"{want} media objects are on disk", flush=True)
            return 0
        if args.dry_run:
            # Report, don't record: a dry run is asked what WOULD happen, and
            # the sidecar is state, not output.
            print(json.dumps({"files": 0, "bytes": 0, "already_present": 0,
                              "s3_prefix": s3_prefix, "gone": True,
                              "expires_at": meta.get("expires_at", "")}))
            return EXIT_STAGING_GONE
        _mark_sidecar_gone(sidecar, meta, "staging prefix listed empty at fetch")
        print(f"!!! media no longer in S3: {s3_prefix} is empty. It was deleted "
              f"before its {meta.get('expires_at', 'recorded')} expiry — by a "
              "staging clear, a console delete, or the lifecycle firing early. "
              "Re-encode to recreate it.", file=sys.stderr, flush=True)
        return EXIT_STAGING_GONE

    pend_bytes = sum(s for _, s, _ in items)
    if args.dry_run:
        print(json.dumps({"files": len(items), "bytes": pend_bytes,
                          "already_present": have, "s3_prefix": s3_prefix,
                          "expires_at": meta.get("expires_at", "")}))
        return 0
    if not items:
        # Everything is already here — the sidecar is stale (a previous fetch
        # died after the last object but before this line). Clear it so the UI
        # stops offering a download that would do nothing.
        sidecar.unlink(missing_ok=True)
        print(f"    already complete: {have} objects present", flush=True)
        return 0

    print(f"    fetching {len(items)} objects ({pend_bytes / 1e6:.0f} MB) "
          f"from {s3_prefix}" + (f", {have} already present" if have else ""),
          flush=True)
    _parallel_get(s3, bucket, items, "fetch:media")
    # Only now is the output complete, so only now does it stop being remote.
    sidecar.unlink(missing_ok=True)
    return 0


# Blended Graviton per-vCPU-hour rates, us-west-2 (c7g/c8g .2xlarge/.4xlarge).
# cloud-batch always runs on the SPOT compute env, so every run's savings = the
# on-demand price it avoided. Estimates — good enough for "saved by spot".
# Above this, the idle share is called out in words rather than left as a
# figure in a line of figures. Short runs sit high by nature — a fixed ~110s of
# boot and scale-down is most of a 4-minute instance — so this is set where it
# means something for a run of real length.
_IDLE_WARN_PCT = 35.0

# Re-exported from pricing.py so this module's call sites keep their short names
# while there is only ONE definition. Was 0.011 here, 0.013 in commercial_cloud
# and 0.0155 in cli_local — three answers for one quantity (#217).
_SPOT_VCPU_HR = pricing.AWS_SPOT_VCPU_HR
_ONDEMAND_VCPU_HR = pricing.AWS_ONDEMAND_VCPU_HR


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
    queue = os.environ.get("BATCH_JOB_QUEUE", "infinite-streaming-encoder-queue")
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


def _emit_machine_rental(exec_name: str, jobs: list) -> tuple | None:
    """Report what was RENTED, against what was allocated (#195).

    ENCODER-COST sums each JOB's reserved vCPU x its duration. AWS bills for
    instance lifetime, and everything between the two is invisible to a
    job-derived figure because no job is running during any of it: EC2 boot, ECS
    registration, image pull, idle before the first chunk lands, and the idle
    tail before Batch scales the instance down.

    Both numbers are printed side by side so the gap is the headline rather than
    something to be inferred.

    Honest about what it cannot know, rather than quietly wrong:
      * an instance shared with a CONCURRENT run has its idle attributed here in
        full, because this execution cannot see the other one's jobs. Marked (*).
      * an instance still alive when the run ends has no termination time, so its
        lifetime is measured to now and marked (~) — it will grow after we look.
    """
    by_inst: dict = {}
    for job in jobs:
        arn = (job.get("container") or {}).get("containerInstanceArn")
        st, sp = job.get("startedAt"), job.get("stoppedAt")
        if not arn or not isinstance(st, (int, float)) or not isinstance(sp, (int, float)):
            continue
        iid = _ec2_for_container_instance(arn)
        if not iid or not iid.startswith("i-"):
            continue
        a = by_inst.setdefault(iid, {"first": st, "last": sp, "vcpu_s": 0.0, "n": 0})
        a["first"] = min(a["first"], st)
        a["last"] = max(a["last"], sp)
        a["vcpu_s"] += _job_vcpu(job) * (sp - st) / 1000.0
        a["n"] += 1
    if not by_inst:
        return None
    try:
        from infinite_streaming_encoder.cloud.inventory import _vcpus_for_type
        ec2 = _ec2()
        desc = ec2.describe_instances(InstanceIds=list(by_inst))
    except Exception:  # noqa: BLE001 — reporting is best-effort
        return None
    import datetime
    now = time.time()
    rows = []
    for r in desc.get("Reservations", []):
        for i in r.get("Instances", []):
            iid = i["InstanceId"]
            a = by_inst.get(iid)
            if not a:
                continue
            launch = i.get("LaunchTime")
            launch = launch.timestamp() if hasattr(launch, "timestamp") else None
            if launch is None:
                continue
            # A terminated instance carries its end time inside the human-readable
            # reason string; there is no structured field for it.
            end, alive = None, i.get("State", {}).get("Name") not in ("terminated", "shutting-down")
            tr = i.get("StateTransitionReason", "")
            if "(" in tr and ")" in tr:
                try:
                    end = datetime.datetime.strptime(
                        tr[tr.index("(") + 1:tr.index(")")],
                        "%Y-%m-%d %H:%M:%S %Z").replace(
                        tzinfo=datetime.timezone.utc).timestamp()
                except ValueError:
                    end = None
            if end is None:
                end, alive = now, True
            rows.append({
                "id": iid, "type": i.get("InstanceType", "?"),
                "vcpu": _vcpus_for_type(i.get("InstanceType")),
                # Absolute launch/end, not just the derived durations: the UI's
                # machine timeline needs to place each box on a shared axis, and
                # a lifetime alone cannot say WHEN a box appeared relative to the
                # others. Without these the chart can only infer "first appeared"
                # from when a chunk landed, which hides the boot entirely.
                "launch": launch, "end": end,
                # When Batch work actually began and ended on this box. NOT the
                # same as the first/last STAGE: a job keeps running after its
                # stage closes — pkgall's stages total 73s against a 177s job,
                # the rest being the parallel fetch and the packaged-output
                # upload. A timeline drawn from stages alone paints that ~104s
                # as idle, which is how a 3m40s tail came to read as over five
                # minutes. These are Batch's own startedAt/stoppedAt.
                "first_job": a["first"] / 1000.0, "last_job": a["last"] / 1000.0,
                "life": end - launch,
                "before": max(0.0, a["first"] / 1000.0 - launch),
                "after": max(0.0, end - a["last"] / 1000.0),
                "busy": max(0.0, (a["last"] - a["first"]) / 1000.0),
                "vcpu_s": a["vcpu_s"], "n": a["n"], "alive": alive,
            })
    if not rows:
        return None
    rows.sort(key=lambda r: -r["life"])
    machine_vcpu_s = sum(r["life"] * r["vcpu"] for r in rows)
    alloc_vcpu_s = sum(r["vcpu_s"] for r in rows)
    _narrate("")
    _narrate("machine rental — what was rented, vs what jobs allocated")
    _narrate(f"  {'instance':<21}{'type':<13}{'vCPU':>5}{'life':>8}"
             f"{'idle pre':>10}{'idle post':>11}{'busy%':>7}  chunks")
    for r in rows:
        pct = (r["busy"] / r["life"] * 100.0) if r["life"] else 0.0
        flag = "~" if r["alive"] else " "
        _narrate(f"  {r['id']:<21}{r['type']:<13}{r['vcpu']:>5}"
                 f"{r['life']:>7.0f}s{r['before']:>9.0f}s{r['after']:>10.0f}s"
                 f"{pct:>6.0f}%{flag} {r['n']}")
    waste = machine_vcpu_s - alloc_vcpu_s
    pct = (waste / machine_vcpu_s * 100.0) if machine_vcpu_s else 0.0
    _narrate(f"  machine vCPU-hours {machine_vcpu_s / 3600:.2f}  vs allocated "
             f"{alloc_vcpu_s / 3600:.2f}  -> {pct:.0f}% paid for and not allocated")
    _narrate("  (~ still running, so its lifetime is measured to now and will grow;"
             " an instance shared with a concurrent run has that run's time counted"
             " as idle here)")
    print(f"[[ENCODER-MACHINES exec={exec_name} instances={len(rows)} "
          f"machine_vcpu_h={machine_vcpu_s / 3600:.3f} "
          f"allocated_vcpu_h={alloc_vcpu_s / 3600:.3f} "
          f"unallocated_pct={pct:.1f}]]", flush=True)
    # Per-instance rows, so the UI's machine timeline can place each box on a
    # shared axis with exact boot and termination. Everything here was already
    # computed for the table above and then discarded — the aggregate marker
    # says 41% of the fleet was unallocated but not WHICH box, or when.
    #
    # Termination matters most. It is the one fact the browser cannot get for
    # itself: /api/aws/inventory drops terminated instances, so a client polling
    # it can only infer "freed" from a box no longer appearing, and the response
    # is cached — long enough to bill six phantom minutes against machines EC2
    # had already reclaimed. Relaying `end` removes the guess.
    #
    # A plain print, not telemetry.emit(): this is the orchestrator, one hop from
    # the Go server over a pipe it is already attached to (see CLAUDE.md).
    for r in rows:
        print(f"[[ENCODER-RENTAL exec={exec_name} id={r['id']} "
              f"type={r['type']} vcpu={r['vcpu']} "
              f"launch={r['launch']:.0f} end={r['end']:.0f} "
              f"first_job={r['first_job']:.0f} last_job={r['last_job']:.0f} "
              f"alive={1 if r['alive'] else 0} chunks={r['n']}]]", flush=True)
    return pct, machine_vcpu_s / 3600.0


def _emit_cost_summary(exec_name: str, log_state: dict | None = None,
                       egress_bytes: int = 0, egress_avoided_bytes: int = 0,
                       egress_files: int = 0, staged_bytes: int = 0) -> None:
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

    # Rental FIRST, because cost is billed on it.
    #
    # This used to price the run from `vh` — the sum of each JOB's reserved vCPU
    # times its duration. AWS does not bill for allocation; it bills for the
    # instance, from launch to termination, including boot, image pull, and the
    # scale-down tail after the last chunk. On a measured run that gap was 52%,
    # so the reported spot cost was roughly half of what was actually spent.
    #
    # Falls back to allocated hours when the rental cannot be measured (no host
    # resolves), and says which basis was used rather than leaving two very
    # different numbers looking identical.
    try:
        rental = _emit_machine_rental(exec_name, jobs)
    except Exception:  # noqa: BLE001 — never fail a finished run over reporting
        rental = None
    idle_pct, machine_vh = rental if rental else (None, None)
    billed_vh = machine_vh if machine_vh else vh
    basis = "rented" if machine_vh else "allocated"

    spot, ondemand = billed_vh * _SPOT_VCPU_HR, billed_vh * _ONDEMAND_VCPU_HR
    saved = ondemand - spot
    try:
        from infinite_streaming_encoder.cloud.compute_env import get_vcpus
        max_vcpus = int(get_vcpus().get("max_vcpus") or 0)
    except Exception:  # noqa: BLE001 — best-effort
        max_vcpus = 0
    # Egress rides the SAME marker as compute, deliberately. A run's cost is one
    # number to a person, and the reason #214 was found on a billing page rather
    # than in the app is that this marker reported 21% of the bill as if it were
    # all of it — $5.47 of compute against $26.48 actually spent, with $16.75 of
    # egress modelled nowhere.
    #
    # avoided_gb is what --no-media left in S3. Worth reporting because it is the
    # only place the saving is visible: a cheap run and an expensive one look
    # identical once the bytes are (or are not) on disk.
    #
    # GB, not bytes, and dollars alongside: bytes are the ground truth, dollars
    # make it actionable. Flat rate, no free tier — see pricing.EGRESS_USD_PER_GB.
    eg_gb = (egress_bytes or 0) / 1e9
    eg_usd = pricing.egress_usd(egress_bytes)
    avoided_gb = (egress_avoided_bytes or 0) / 1e9

    # Everything below is priced at FULL RATE with no free-tier discount, so the
    # answer is "what would this run cost if every allowance were already
    # spent?" — the number that matters when deciding whether to run it a
    # hundred more times. A line that reads $0.00 today only because an
    # allowance has not run out yet is not information.
    #
    # Step Functions is the case in point: this account is already at 4,000/4,000
    # free transitions, so it bills TODAY. Modelling only what is currently
    # charged would have given an answer with a shelf life.
    sfn_txns = int((log_state or {}).get("_sfn_transitions", 0))
    sfn_cost = pricing.sfn_usd(sfn_txns)

    # Tier2 GETs we can attribute exactly: one per object the sync-back fetched.
    # Worker-side GETs and every Tier1 PUT are NOT attributable from here — see
    # the note on `unmodelled` below.
    get_n = int(egress_files or 0)
    req_usd = pricing.s3_request_usd(tier2=get_n)

    # Staging held for the run's duration. GB-HOURS, not GB-months: treating a
    # day's staging as a month over-states by ~30x.
    store_usd = pricing.s3_storage_usd(
        (staged_bytes or 0) / 1e9, max(wall_s, 0.0) / 3600.0)

    # Tier1 is ESTIMATED from staged bytes, not counted — see
    # pricing.s3_put_estimate_usd. It is the largest line we cannot attribute
    # directly (8.5% of the full-rate total), so a fitted figure beats omitting
    # it, but it is labelled est so nobody mistakes it for a measurement.
    put_est = pricing.s3_put_estimate_usd(staged_bytes)

    total = spot + eg_usd + sfn_cost + req_usd + store_usd + put_est

    # Name what is NOT in the total. #217 existed because a partial number looked
    # complete; repeating that with a longer list of terms would be worse, not
    # better. S3 PUTs are the big omission and are called out by name: 591,572
    # Tier1 requests cost $2.96 over 1-3 Aug, but they happen across the workers
    # and the packager, not here, so attributing them per-run needs counters
    # those paths do not yet keep.
    unmodelled = ",".join(pricing.UNMODELLED)

    print(f"[[ENCODER-COST exec={exec_name} spot_usd={spot:.4f} "
          f"ondemand_usd={ondemand:.4f} saved_usd={saved:.4f} "
          f"vcpu_hours={billed_vh:.2f} "
          f"egress_gb={eg_gb:.3f} egress_usd={eg_usd:.4f} "
          f"egress_avoided_gb={avoided_gb:.3f} "
          f"sfn_transitions={sfn_txns} sfn_usd={sfn_cost:.4f} "
          f"s3_get={get_n} s3_request_usd={req_usd:.4f} "
          f"s3_put_est_usd={put_est:.4f} "
          f"storage_usd={store_usd:.4f} "
          f"total_usd={total:.4f} unmodelled={unmodelled}]]", flush=True)
    print(f"[[ENCODER-STATS exec={exec_name} wall_s={wall_s:.1f} vcpu_h={vh:.3f} "
          f"longest_s={longest_s:.1f} slowest={slowest or '-'} jobs={n} "
          f"max_vcpus={max_vcpus}]]", flush=True)
    conc = (vcpu_s / wall_s) if wall_s else 0.0
    eff = (conc / max_vcpus * 100.0) if max_vcpus else 0.0
    _narrate(f"💰 saved ${saved:.2f} using spot — {billed_vh:.1f} vCPU-hr "
             f"{basis} (${spot:.2f} spot vs ${ondemand:.2f} on-demand)")
    if machine_vh and vh:
        _narrate(f"    (billed on machine lifetime, not allocation: {machine_vh:.1f} "
                 f"vCPU-hr rented vs {vh:.1f} allocated — the difference is boot, "
                 f"image pull and the scale-down tail)")
    # `idle` is a DIFFERENT question from `eff` and both are worth showing.
    #   eff  — of the compute environment's ceiling, how much was in use
    #   idle — of the machine time actually RENTED, how much ran no job at all
    # A run can be efficient by the first measure and wasteful by the second:
    # every box busy, each one paid for through its boot and its scale-down tail.
    idle_txt = f" · {idle_pct:.0f}% machine idle" if idle_pct is not None else ""
    _narrate(f"📊 {n} jobs · {vh:.1f} vCPU-hr · avg {conc:.0f} vCPUs busy "
             f"({eff:.0f}% of {max_vcpus}){idle_txt} · slowest chunk "
             f"{longest_s / 60:.1f} min")
    if idle_pct is not None and idle_pct >= _IDLE_WARN_PCT:
        _narrate(f"    ({idle_pct:.0f}% of rented machine time ran no job — boot, "
                 f"image pull and the scale-down tail. See the machine rental "
                 f"table above for where it went.)")


def cmd_poll(args: argparse.Namespace) -> int:
    sfn = _sfn()
    # Read the execution input up front so the plan lists only the codecs we're
    # actually encoding (do_h264 / do_hevc from buildSFNInput).
    # Defaults are the pre-#197 behaviour, which is also the right fallback when
    # describe_execution fails: Batch packages everything and the sync-back runs.
    do_h264 = do_hevc = do_av1 = True
    variants: list = []
    # Codecs THIS process will package once the execution succeeds (#197).
    # do_h264/do_hevc/do_av1 mean "the STATE MACHINE packages this codec" — that
    # is the only thing the ASL reads them for — so the two sets are disjoint and
    # the plan is their union. Empty list = the old behaviour, packaging in Batch.
    host_package: list[str] = []
    seg_dur = part_dur = ""
    try:
        inp = json.loads(sfn.describe_execution(
            executionArn=args.execution_arn).get("input") or "{}")
        do_h264 = bool(inp.get("do_h264", True))
        do_hevc = bool(inp.get("do_hevc", True))
        do_av1 = bool(inp.get("do_av1", True))
        variants = inp.get("variants") or []
        host_package = [c for c in (inp.get("host_package") or [])
                        if c in ("h264", "hevc", "av1")]
        seg_dur = str(inp.get("segment_duration") or "")
        part_dur = str(inp.get("partial_duration") or "")
    except (ClientError, ValueError, TypeError):
        pass
    # The plan must list what is being ENCODED, which is not what the state
    # machine packages once packaging moves here.
    _emit_plan(variants, do_h264 or "h264" in host_package,
               do_hevc or "hevc" in host_package,
               # The sync-back only has work if the STATE MACHINE packaged
               # something; do_av1 counts here even though it has no plan row.
               sync_back=do_h264 or do_hevc or do_av1)
    # Flag chunks left over from a prior (cancelled/failed) run as reused, so a
    # resume shows them distinctly instead of as fresh encodes.
    _emit_reused_chunks(args.s3_prefix)

    seen: set[int] = set()
    log_state: dict[str, int] = {}  # stream -> last-forwarded timestamp
    reclaim_seen: dict = {}         # jobId -> last-emitted (count, lost, total)
    exec_name = args.execution_arn.rsplit(":", 1)[-1]
    interval_s = int(os.environ.get("BATCH_POLL_INTERVAL_S", "5"))
    timeout_s = int(os.environ.get("BATCH_POLL_TIMEOUT_S", "14400"))  # 4h ceiling
    # Idempotent: cmd_submit already created this, but poll is a separate
    # process and may be re-attached to an execution submitted by an older
    # driver. CreateQueue returns the existing URL when the attributes match.
    tel_url = _create_telemetry_queue(exec_name)
    # Continuous, on its own thread — NOT once per poll iteration. See
    # _start_telemetry_drain for why the coupling mattered.
    state_url = _create_state_channel(exec_name)
    tel_stop = _start_drain_thread(tel_url, state_url, log_state)
    tel_silent_s = 0
    last_census = 0.0

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
        # Free: the poll already has every event. Counting here rather than with
        # a second API call at summary time, and the last poll's count is the
        # complete one because this refetches from the start each iteration.
        if log_state is not None:
            log_state["_sfn_transitions"] = sum(
                1 for e in events if str(e.get("type", "")).endswith("StateEntered"))
        _translate_events(events, seen)
        # Then refine chunk cells to queued/running from live Batch status
        # (runs after _translate_events so it wins over the enter-event's
        # coarse "running"). Best-effort — never let it break polling.
        # Detect spot reclaims of in-flight encode jobs (red bar + waste stats).
        try:
            _report_live_reclaims(exec_name, reclaim_seen)
        except Exception:  # noqa: BLE001 — reclaim reporting is best-effort
            pass
        # STAGE STATE FIRST, deliberately.
        #
        # This derives every chunk's state from Batch's own job status and needs
        # no logs at all — one cached scan, ~0.8s. It used to run AFTER the log
        # forwarding below, which meant the cheap authoritative update was
        # blocked behind a slow drain it does not depend on: draining the
        # CloudWatch stream of each newly-completed job measured 125s for 337
        # streams (~0.37s each), against 0.8s for the scan. During a run, chunks
        # complete continuously, so most polls had streams to drain and the grid
        # sat 35-73 completions behind Batch (#187) — showing holes for work that
        # had already finished.
        # The census is now a BACKSTOP, not the primary path. EventBridge is
        # at-least-once but not guaranteed-delivery, so a periodic full scan
        # still catches a transition that never arrived. Rate-limited because it
        # is the expensive call this issue set out to remove — every poll was
        # ~0.8s of list_jobs to re-derive state the events already delivered.
        #
        # When events are NOT available it falls back to every poll, which is
        # exactly the old behaviour.
        census_gap = _CENSUS_BACKSTOP_S if state_url else 0
        if time.monotonic() - last_census >= census_gap:
            last_census = time.monotonic()
            try:
                _sync_stages_from_batch(exec_name, log_state)
            except Exception:  # noqa: BLE001 — never break polling over the grid
                pass
        # If the queue exists but has produced NOTHING while containers are
        # demonstrably running, the workers are not publishing (missing IAM, an
        # image predating the sink, a queue-name mismatch). Say so and put the
        # log drain back, rather than rendering an increasingly empty grid and
        # leaving the cause to be guessed at.
        #
        # Measured against WORK, not wall time. Batch cold-start routinely takes
        # minutes to place the first container — an elapsed-time trigger would
        # declare the queue broken on every run that had to boot an instance,
        # which is most of them.
        if tel_url and not log_state.get("_tel_handled"):
            started = any(v in ("running", "done")
                          for v in _STAGE_STATE.values())
            tel_silent_s += interval_s if started else 0
            if tel_silent_s >= _TELEMETRY_SILENT_GIVEUP_S:
                print(f"!!! telemetry queue silent for {tel_silent_s}s of "
                      f"running containers — workers are not publishing; "
                      f"falling back to CloudWatch log draining",
                      file=sys.stderr, flush=True)
                if tel_stop is not None:
                    tel_stop.set()  # stop polling a queue we have given up on
                    tel_stop = None
                tel_url = None
        # Then the live [progress]/[phase] lines from running containers, so long
        # phases show activity rather than a dark gap. Cosmetic, and now bounded
        # (see _MAX_DRAINS_PER_POLL) so it cannot starve the loop above.
        try:
            _forward_running_logs(exec_name, log_state,
                                  drain_finished=tel_url is None)
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
            # One last drain before tearing the queue down. A chunk's final
            # records — ENCODER-TIMING, ENCODER-SPEED — are emitted as it exits,
            # so the last jobs to finish publish AFTER the last ordinary poll.
            # This is the same gap _forward_container_timing exists to cover on
            # the log path.
            if tel_stop is not None:
                tel_stop.set()          # stop the thread before the last read
            try:
                _drain_telemetry(tel_url, log_state)
            except Exception:  # noqa: BLE001 — never fail a finished run
                pass
            _delete_telemetry_queue(tel_url)
            tel_url = None
            try:
                _drain_state(state_url, log_state)   # last transitions
            except Exception:  # noqa: BLE001
                pass
            _delete_state_channel(exec_name, state_url)
            state_url = None
            if status == "SUCCEEDED":
                # Safety net: the packaging sub-stages come from live tailing;
                # if the last tail poll missed a "done" marker, force them
                # complete now so no packaging row is left stuck at "running".
                for codec, on in (("h264", do_h264), ("hevc", do_hevc)):
                    if on:
                        for k in ("package", "fragments", "hls"):
                            _emit_stage(f"{k}:{codec}", "done", 100.0)
                # Package here, before the sync-back — these codecs have no
                # packaged output in S3 for _download_outputs to find, because
                # the state machine skipped their PerCodec branch entirely.
                pkg = DownloadResult(0, 0, 0, 0)
                if host_package:
                    pkg = _package_on_host(host_package, args.s3_prefix,
                                           Path(args.local_dir),
                                           getattr(args, "output_stem", ""),
                                           getattr(args, "output_tag", ""),
                                           seg_dur, part_dur)
                media = not getattr(args, "no_media", False)
                what = "outputs" if media else "manifests only"
                print(f"    downloading {what} from {args.s3_prefix}", flush=True)
                res = _download_outputs(args.s3_prefix, Path(args.local_dir),
                                        getattr(args, "output_stem", ""),
                                        getattr(args, "output_tag", ""),
                                        include_media=media)
                print(f"    downloaded {res.files} files", flush=True)
                try:
                    # res is what the sync-back actually moved, so the cost is
                    # measured rather than assumed.
                    # pkg is the chunk fetch host packaging paid for; res is what
                    # the sync-back moved. Both are egress on the same link, and
                    # host packaging TRADES one for the other rather than
                    # avoiding it — counting only res would price the trade as a
                    # saving it is not.
                    _emit_cost_summary(exec_name, log_state,
                                       egress_bytes=res.bytes + pkg.bytes,
                                       egress_avoided_bytes=res.skipped_bytes,
                                       egress_files=res.files + pkg.files,
                                       staged_bytes=res.bytes + res.skipped_bytes)
                except Exception:  # noqa: BLE001 — cost summary is best-effort
                    pass
                return 0
            print(f"!!! execution ended with status {status}: "
                  f"{desc.get('cause', '')}", file=sys.stderr)
            return 2

        time.sleep(interval_s)
        elapsed += interval_s

    if tel_stop is not None:
        tel_stop.set()
    _delete_telemetry_queue(tel_url)
    _delete_state_channel(exec_name, state_url)
    print("!!! poll timed out", file=sys.stderr)
    return 3


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------

def cmd_gc(args: argparse.Namespace) -> int:
    """Sweep telemetry queues and state rules left by runs killed mid-flight.

    The same bounded sweep submit runs, exposed so the CONTROL PLANE can trigger
    it without a cloud encode having to happen (#191). Before this, an orphan was
    reclaimed only by the next submit — fine at one orphan, wrong at a few
    hundred, and never at all for someone who stops encoding.

    --state-machine-arn is REQUIRED, and that is a safety property rather than
    an ergonomic one. It is the source of the keep-list, and _active_execution_cores
    returns an EMPTY set without it — which makes the sweep more aggressive, not
    less. A run lasting longer than the 1h message retention sits at zero
    messages and is indistinguishable from an orphan on the other bounds alone,
    so an unscoped sweep could delete a live run's queue out from under it.
    Refusing beats sweeping blind.
    """
    _gc_telemetry_queues(args.state_machine_arn)
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="infinite_streaming_encoder.cli_batch")
    sub = p.add_subparsers(dest="cmd", required=True)

    # fetch — pull the media a --no-media poll deliberately left in S3, for one
    # already-downloaded output dir. Driven by the Outputs tab's Download button
    # (#214); the control plane shells out to this rather than carrying an AWS
    # SDK for Go, matching how it already calls cloud.batch_admin.
    pf = sub.add_parser("fetch")
    pf.add_argument("--dir", required=True, dest="dir",
                    help="output directory holding a " + REMOTE_SIDECAR)
    pf.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="report what would be fetched, then exit")
    pf.set_defaults(fn=cmd_fetch)

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
    pp.add_argument("--no-media", action="store_true", dest="no_media",
                    default=_env_flag("SKIP_OUTPUT_MEDIA"),
                    help="fetch manifests and metadata only, leaving segments "
                         "in S3 for an on-demand `fetch` (#214)")
    pp.set_defaults(fn=cmd_poll)

    # gc — the submit-time sweep, on demand. The control plane calls this on an
    # hourly timer so orphans do not wait for the next cloud encode (#191).
    pg = sub.add_parser("gc")
    pg.add_argument("--state-machine-arn", required=True, dest="state_machine_arn",
                    help="scopes the keep-list; required because an unscoped "
                         "sweep is MORE aggressive, not less (see cmd_gc)")
    pg.set_defaults(fn=cmd_gc)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
