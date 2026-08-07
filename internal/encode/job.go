package encode

import (
	"bufio"
	"bytes"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// defaultChunkDurationSeconds mirrors scripts/infinite_streaming_encoder/chunking
// .DEFAULT_CHUNK_DURATION_S. The control plane computes chunk_count the same
// way the Python phase does (from the same clip duration + chunk size) so the
// two never disagree.
const defaultChunkDurationSeconds = 30.0

// wholeVariantChunkSeconds is a large multiple of the 6s segment duration used
// for "whole variant" (no chunking): any real clip is shorter, so it collapses
// to a single chunk, and it still passes chunking.py's "multiple of segment"
// validation. 86400 = 24h = 14400 x 6.
const wholeVariantChunkSeconds = 86400.0

// chunkCountForDuration mirrors chunking.chunk_count: ceil(dur/chunk), min 1.
func chunkCountForDuration(durationS, chunkS float64) int {
	if durationS <= 0 || chunkS <= 0 {
		return 1
	}
	n := int(math.Ceil(durationS/chunkS - 1e-6))
	if n < 1 {
		return 1
	}
	return n
}

// probeDurationSeconds shells out to ffprobe (baked into the worker image) to
// read a clip's duration. On any failure it returns an error and callers fall
// back to a single whole-clip chunk.
func probeDurationSeconds(path string) (float64, error) {
	cmd := exec.Command("ffprobe", "-v", "error",
		"-show_entries", "format=duration",
		"-of", "default=noprint_wrappers=1:nokey=1", path)
	out, err := cmd.Output()
	if err != nil {
		return 0, err
	}
	return strconv.ParseFloat(strings.TrimSpace(string(out)), 64)
}

// probeSourceWidth reads a clip's video width via ffprobe, so the ladder can
// be capped at the source resolution (never upscale). Returns 0 on failure —
// callers treat that as "unknown, don't cap".
func probeSourceWidth(path string) int {
	cmd := exec.Command("ffprobe", "-v", "error",
		"-select_streams", "v:0", "-show_entries", "stream=width",
		"-of", "default=noprint_wrappers=1:nokey=1", path)
	out, err := cmd.Output()
	if err != nil {
		return 0
	}
	w, err := strconv.Atoi(strings.TrimSpace(string(out)))
	if err != nil {
		return 0
	}
	return w
}

// probeSourceFps reads a clip's video frame rate (rounded to the nearest whole
// fps) via ffprobe, so the speed model — which keys on fps because encode time
// scales with frame count — can size cloud-batch chunks and rank priorities at
// the right rate. Returns 0 on failure (callers treat that as "unknown" → 30).
func probeSourceFps(path string) int {
	cmd := exec.Command("ffprobe", "-v", "error",
		"-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
		"-of", "default=noprint_wrappers=1:nokey=1", path)
	out, err := cmd.Output()
	if err != nil {
		return 0
	}
	s := strings.TrimSpace(string(out))
	if n, d, ok := strings.Cut(s, "/"); ok {
		num, e1 := strconv.ParseFloat(n, 64)
		den, e2 := strconv.ParseFloat(d, 64)
		if e1 == nil && e2 == nil && den > 0 {
			return int(math.Round(num / den))
		}
		return 0
	}
	if f, err := strconv.ParseFloat(s, 64); err == nil {
		return int(math.Round(f))
	}
	return 0
}

// probeHasAudio reports whether the source has at least one audio stream, for
// the commercial cost model's per-track audio add-on.
func probeHasAudio(path string) bool {
	cmd := exec.Command("ffprobe", "-v", "error",
		"-select_streams", "a", "-show_entries", "stream=index",
		"-of", "csv=p=0", path)
	out, err := cmd.Output()
	return err == nil && strings.TrimSpace(string(out)) != ""
}

// probeSourceMbps reads the source's overall bitrate in Mbit/s (0 on failure),
// for the commercial model's high-bitrate multiplier (+25% per 50 Mbps > 50).
func probeSourceMbps(path string) float64 {
	cmd := exec.Command("ffprobe", "-v", "error",
		"-show_entries", "format=bit_rate",
		"-of", "default=noprint_wrappers=1:nokey=1", path)
	out, err := cmd.Output()
	if err != nil {
		return 0
	}
	bps, err := strconv.ParseFloat(strings.TrimSpace(string(out)), 64)
	if err != nil || bps <= 0 {
		return 0
	}
	return bps / 1e6
}

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]|\x1b[()][0-9A-B]|\x1b\][^\x07]*\x07|\r`)

func stripANSI(s string) string {
	return ansiRe.ReplaceAllString(s, "")
}

// Markers the Python orchestrator emits for structured progress. See
// scripts/infinite_streaming_encoder/progress.py for the producer side.
var (
	planMarkerRe  = regexp.MustCompile(`^\[\[ENCODER-PLAN (.+)\]\]$`)
	stageMarkerRe = regexp.MustCompile(`^\[\[ENCODER-STAGE key=(\S+) status=(\S+) percent=([0-9.]+)\]\]$`)
	// [[ENCODER-TIMING phase=variant key=encode:h264:1080p:chunk3 fetch_s=1.8
	//   encode_s=9.2 cpu_s=18.4 mem_mib=425 total_s=11.4]]
	// Captured as a whole so new measurements are picked up without a new
	// regex — the worker adds fields to this marker as it learns to measure
	// more, and a positional pattern would silently ignore them.
	timingMarkerRe = regexp.MustCompile(`^\[\[ENCODER-TIMING (.+)\]\]$`)
	// ENCODER-HOST reports the machine (EC2 instance-id, or a stable ARN slug)
	// a stage's Batch job landed on — used to colour the chunk plot by instance
	// so co-located heavy chunks are visible. Emitted once per (stage, instance).
	hostMarkerRe = regexp.MustCompile(`^\[\[ENCODER-HOST key=(\S+) instance=(\S+)\]\]$`)
	// ENCODER-REUSED flags a chunk whose output a prior run already staged, so
	// this run skips (not re-encodes) it — the orchestrator emits it once per
	// reused chunk at startup. The UI styles those cells distinctly.
	reusedMarkerRe = regexp.MustCompile(`^\[\[ENCODER-REUSED key=(\S+)\]\]$`)
	// Cloud cli_cloud.py prints its computed S3 job id in the plan
	// header (`  job_id:         20260420T203910Z-1`). That's the prefix
	// under `s3://.../jobs/` — we capture it so the retry endpoint can
	// point at the right prior staging.
	cloudJobIDRe = regexp.MustCompile(`^\s+job_id:\s+(\S+)\s*$`)
	// ENCODER-FILE signals the start of a per-file encode within a
	// multi-file job. Emitted by the Go server for local encodes (one
	// worker per file) and by the remote userdata bash loop for cloud
	// batches (one worker, many clips). Handling: archive the current
	// Stages into StagesHistory (so end-of-job timing still has all
	// phases), clear Stages, and stamp CurrentFile / FileIndex / TotalFiles.
	fileMarkerRe = regexp.MustCompile(`^\[\[ENCODER-FILE index=(\d+) total=(\d+) name=(.+)\]\]$`)
	// ENCODER-BOOT reports the AMI the worker's instance actually booted
	// from (via IMDS). We compare it to WORKER_AMI_ID (the pre-baked worker
	// AMI) to flag whether the image was already resident — a definitive
	// "AMI cache hit" signal for the UI, independent of timing.
	bootMarkerRe = regexp.MustCompile(`^\[\[ENCODER-BOOT ami=(\S+)\]\]$`)
	// ENCODER-FFMPEG / ENCODER-CODECLIB record WHAT ENCODED THIS, reported by the
	// worker that actually ran ffmpeg. The image tracks BtbN's rolling `latest`
	// (a dated pin gets pruned upstream and breaks every build), so the version is
	// no longer knowable from the Dockerfile — it has to come from the run.
	//
	// CODECLIB is the one that matters for output: `ffmpeg -version` names
	// libx264/libx265/libsvtav1 but versions none of them, and an x265 bump under
	// a static ffmpeg build changes HEVC bitstreams while every other recorded
	// version stays identical.
	ffmpegMarkerRe   = regexp.MustCompile(`^\[\[ENCODER-FFMPEG version=(.+)\]\]$`)
	codecLibMarkerRe = regexp.MustCompile(`^\[\[ENCODER-CODECLIB codec=(\S+) lib=(\S+) version=(\S+)\]\]$`)
	// ENCODER-SPEED reports a completed encode's content-seconds vs wall-seconds
	// so the control plane can learn each variant's speed for the dynamic chunk
	// selector, cost, and ETA. Keyed by every dimension that moves encode time:
	// machine (mac/ubuntu/macmini local worker label, or "graviton" on cloud
	// batch), codec, height, pass, preset, fps. Consumed by Manager.learnSpeed.
	speedMarkerRe = regexp.MustCompile(`^\[\[ENCODER-SPEED machine=(\S+) codec=(\S+) height=(\d+) two_pass=([01]) preset=(\S+) fps=(\d+) content_s=([0-9.]+) encode_s=([0-9.]+)\]\]$`)
	// ENCODER-VMAF reports one rendition's VMAF (#24) — emitted per chunk (or
	// chunk=-1 for a whole-variant encode). frames + inv_sum let the control
	// plane recombine the frame-weighted mean and the harmonic mean correctly
	// across chunks (chunk means/harmonics can't just be averaged).
	// The trailing ` model=… common_h=…` is score provenance (#117), added later:
	// an OPTIONAL group so markers from an older worker image (no provenance) still
	// match and aggregate — their model/common_h simply read as unknown.
	vmafMarkerRe = regexp.MustCompile(`^\[\[ENCODER-VMAF codec=(\S+) label=(\S+) height=(\d+) chunk=(-?\d+) mean=([0-9.]+) harmonic=([0-9.]+) min=([0-9.]+) frames=(\d+) inv_sum=([0-9.]+)( model=(\S+) common_h=(\d+))?\]\]$`)
	// ENCODER-RECLAIM reports one encode chunk's spot-reclaim accounting:
	// count (# reclaimed attempts), lost_s (encode wall-time thrown away), and
	// total_s (all attempts' wall-time = the "total encoded" denominator).
	// Emitted per chunk (even zero-reclaim) so the job's %-wasted ratio is
	// exact. Per-stage values are cumulative; the Job max-merges them.
	reclaimMarkerRe = regexp.MustCompile(`^\[\[ENCODER-RECLAIM key=(\S+) count=(\d+) lost_s=([0-9.]+) total_s=([0-9.]+)\]\]$`)
	// ENCODER-COST reports one execution's spot-vs-on-demand cost + savings at
	// job end. Keyed by exec so a reattach that re-emits is idempotent (the Job
	// sums per-exec, last value wins). Drives "saved by using spot".
	// Matched as a PREFIX and then parsed key=value, not as one rigid pattern.
	// The rigid version listed every field in order, so adding egress_gb (#217)
	// would have stopped it matching AT ALL — and the failure mode of a
	// non-matching cost marker is silent: no error, just a run that reports $0.
	// Key=value also means old workers emitting the shorter form still parse.
	costMarkerRe = regexp.MustCompile(`^\[\[ENCODER-COST (.+)\]\]$`)
	// ENCODER-STATS reports one execution's run-efficiency stats at job end
	// (encode wall span, total vCPU-hours, the slowest single chunk = makespan
	// floor, job count, and the compute-env max vCPUs). The Job derives avg
	// concurrency + efficiency from these. Keyed by exec for idempotent reattach.
	statsMarkerRe = regexp.MustCompile(`^\[\[ENCODER-STATS exec=(\S+) wall_s=([0-9.]+) vcpu_h=([0-9.]+) longest_s=([0-9.]+) slowest=(\S+) jobs=(\d+) max_vcpus=(\d+)\]\]$`)
	// ENCODER-MACHINES reports what was RENTED against what jobs allocated —
	// the boot, image pull and scale-down tail that a job-derived cost figure
	// cannot see (#195). Captured so the per-output stats can show it.
	machinesMarkerRe = regexp.MustCompile(`^\[\[ENCODER-MACHINES exec=(\S+) instances=(\d+) machine_vcpu_h=([0-9.]+) allocated_vcpu_h=([0-9.]+) unallocated_pct=([0-9.]+)\]\]$`)
	// ENCODER-RENTAL is the per-instance detail behind ENCODER-MACHINES: exact
	// launch and termination for one box. The aggregate says 41% of the fleet was
	// rented-and-unallocated; this says which box and when, which is what the UI
	// needs to place lanes on a shared time axis.
	//
	// `end` is the load-bearing field. It is the one fact the browser cannot get
	// for itself — /api/aws/inventory drops terminated instances and is cached,
	// so a client can only infer "freed" from a box ceasing to appear, and that
	// inference billed six phantom minutes against machines EC2 had already
	// reclaimed. alive=1 means end is "as of now" and will grow.
	rentalMarkerRe = regexp.MustCompile(`^\[\[ENCODER-RENTAL exec=(\S+) id=(\S+) type=(\S+) vcpu=(\d+) launch=(\d+) end=(\d+) first_job=(\d+) last_job=(\d+) alive=([01]) chunks=(\d+)\]\]$`)
	// ENCODER-FLEET reports one distributed-local worker box's live CPU: busy =
	// logical cores currently busy (from /proc/stat), perf = its perf-core target
	// (4/4/8). Emitted by cli_local_dist from the workers' Temporal heartbeats,
	// once per machine per poll. Consumed by Manager.recordFleetCPU (not per-job)
	// to drive the local fleet CPU sparkline.
	fleetMarkerRe = regexp.MustCompile(`^\[\[ENCODER-FLEET machine=(\S+) busy=([0-9.]+) perf=([0-9.]+)( chunks=([^\]]*))?\]\]$`)
	// ENCODER-COMMERCIAL reports what this job's output ladder would have cost on
	// hosted transcoders — a generic commercial cloud encoder and AWS MediaConvert
	// — as comparison baselines against our own spot/local cost. Emitted once per
	// job by the orchestrator (commercial_cloud.py).
	commercialMarkerRe = regexp.MustCompile(`^\[\[ENCODER-COMMERCIAL commercial=([0-9.]+) mediaconvert=([0-9.]+) aws=([0-9.]+) aws_od=([0-9.]+)\]\]$`)
)

// learnSpeed folds an ENCODER-SPEED marker into the learned-speed model. Returns
// true when the line was such a marker (so the caller skips logging it).
func (m *Manager) learnSpeed(line string) bool {
	sm := speedMarkerRe.FindStringSubmatch(line)
	if sm == nil {
		return false
	}
	machine, codec := sm[1], sm[2]
	height, _ := strconv.Atoi(sm[3])
	twoPass := sm[4] == "1"
	preset := sm[5]
	fps, _ := strconv.Atoi(sm[6])
	contentS, _ := strconv.ParseFloat(sm[7], 64)
	encodeS, _ := strconv.ParseFloat(sm[8], 64)
	if m.Speeds != nil {
		m.Speeds.Update(machine, codec, height, twoPass, preset, fps, contentS, encodeS)
	}
	return true
}

// encodeStageKeyRe parses an encode stage key into codec + output height, for
// cost-weighting the overall progress. Matches chunked ("encode:hevc:1080p:chunk3")
// and whole-variant ("encode:h264:1080p") keys alike.
var encodeStageKeyRe = regexp.MustCompile(`^encode:([a-z0-9]+):(\d+)p?(:chunk\d+)?$`)

// computeProgress sets job.OverallProgress + job.ETASeconds. It weights each
// encode VARIANT by its expected wall-time (1 / learned content-per-wall speed),
// so a slow 4K HEVC 2-pass rendition dominates a fast h264 360p one — and the
// weighting self-corrects as the learned-speed model fills in from finished
// chunks. Grouped by variant so dynamic chunking's chunk COUNT doesn't double-
// count cost; pipeline stages (mezzanine/audio/package/…) share ~10% of the
// weight so the tail counts but never dominates. ETA is a linear extrapolation
// of the weighted progress over elapsed wall-time.
func (m *Manager) computeProgress(job *Job) {
	job.mu.Lock()
	defer job.mu.Unlock()
	if len(job.Stages) == 0 {
		return
	}
	twoPass := !job.Config.HevcSinglePass
	eff := func(s StageProgress) float64 {
		switch s.Status {
		case "done":
			return 100
		case "failed":
			return 0
		}
		return s.Percent
	}
	type vagg struct {
		sum, weight float64
		n           int
	}
	variants := map[string]*vagg{}
	var pipeline []float64
	for _, s := range job.Stages {
		mm := encodeStageKeyRe.FindStringSubmatch(s.Key)
		if mm == nil {
			pipeline = append(pipeline, eff(s))
			continue
		}
		vk := mm[1] + ":" + mm[2]
		v := variants[vk]
		if v == nil {
			height, _ := strconv.Atoi(mm[2])
			sp := 0.1
			if m.Speeds != nil {
				// Machine-agnostic: relative weights across variants cancel the
				// per-machine/preset/fps factors, so this need only rank variants
				// by cost. hevc honours the job's 2-pass setting; others 1-pass.
				sp = m.Speeds.RelativeSpeed(mm[1], height, mm[1] == "hevc" && twoPass, "", 0)
			}
			if sp <= 0 {
				sp = 0.001
			}
			v = &vagg{weight: 1.0 / sp}
			variants[vk] = v
		}
		v.sum += eff(s)
		v.n++
	}
	var num, den, variantTotal float64
	var outSum float64
	var outN int
	for _, v := range variants {
		if v.n > 0 {
			vp := v.sum / float64(v.n)
			num += v.weight * vp
			den += v.weight
			variantTotal += v.weight
			outSum += vp // output-time %: every rendition equal, cost-blind
			outN++
		}
	}
	if outN > 0 {
		job.OutputProgress = outSum / float64(outN)
	}
	if len(pipeline) > 0 {
		pw := 1.0
		if variantTotal > 0 {
			pw = 0.10 * variantTotal / float64(len(pipeline)) // pipeline ≈ 10% of the bar
		}
		for _, p := range pipeline {
			num += pw * p
			den += pw
		}
	}
	if den <= 0 {
		return
	}
	prog := num / den
	job.OverallProgress = prog
	if prog > 1 && prog < 99.5 && !job.StartedAt.IsZero() {
		job.ETASeconds = time.Since(job.StartedAt).Seconds() * (100 - prog) / prog
		// For a running LOCAL-DIST job, the local-hardware "cost" (predicted
		// end−start wall clock) is best given by the MEASURED total: elapsed +
		// ETA. Keep it consistent with the per-job ETA rather than the seed
		// makespan model (projectLocalWallSeconds), which over-predicts until
		// local speeds are learned. The model still covers the cloud "what-if"
		// and the pre-progress estimate.
		if job.Config.Target == TargetLocalDist {
			job.LocalWallSeconds = time.Since(job.StartedAt).Seconds() + job.ETASeconds
		}
	} else {
		job.ETASeconds = 0
	}
}

// upsertStage sets (or creates) a stage by key with the given label, status and
// percent, stamping StartedAt/EndedAt on transitions. For Go-side stages (the
// input upload) that aren't part of the worker's PLAN. Set before the worker's
// PLAN arrives, so the plan MERGE (parseMarker) keeps it as the leading stage.
func (j *Job) upsertStage(key, label, status string, percent float64) {
	now := time.Now()
	j.mu.Lock()
	defer j.mu.Unlock()
	for i := range j.Stages {
		if j.Stages[i].Key == key {
			if j.Stages[i].StartedAt == nil && status == "running" {
				j.Stages[i].StartedAt = &now
			}
			if j.Stages[i].EndedAt == nil && (status == "done" || status == "failed") {
				j.Stages[i].EndedAt = &now
			}
			j.Stages[i].Status = status
			j.Stages[i].Percent = percent
			return
		}
	}
	st := StageProgress{Key: key, Label: label, Status: status, Percent: percent}
	if status == "running" {
		st.StartedAt = &now
	}
	if status == "done" || status == "failed" {
		st.EndedAt = &now
	}
	j.Stages = append(j.Stages, st)
}

// awsCpProgressRe matches an `aws s3 cp` progress line, e.g.
// "Completed 256.0 MiB/1.2 GiB (50.0 MiB/s) with 1 file(s) remaining".
var awsCpProgressRe = regexp.MustCompile(`Completed ([\d.]+) (\w+)/([\d.]+) (\w+)`)

// parseAwsCpProgress extracts an overall percent from an aws-cli progress line.
func parseAwsCpProgress(line string) (float64, bool) {
	m := awsCpProgressRe.FindStringSubmatch(line)
	if m == nil {
		return 0, false
	}
	done := sizeToBytes(m[1], m[2])
	total := sizeToBytes(m[3], m[4])
	if total <= 0 {
		return 0, false
	}
	return math.Min(100, done/total*100), true
}

func sizeToBytes(num, unit string) float64 {
	f, err := strconv.ParseFloat(num, 64)
	if err != nil {
		return 0
	}
	switch unit {
	case "KiB":
		f *= 1 << 10
	case "MiB":
		f *= 1 << 20
	case "GiB":
		f *= 1 << 30
	case "TiB":
		f *= 1 << 40
	}
	return f
}

// parseMarker returns true when the line was a recognised progress marker
// (and was applied to the job), so the caller can skip appending it to
// the raw log buffer.
func (j *Job) parseMarker(line string) bool {
	if m := vmafMarkerRe.FindStringSubmatch(line); m != nil {
		codec, label := m[1], m[2]
		mean, _ := strconv.ParseFloat(m[5], 64)
		mn, _ := strconv.ParseFloat(m[7], 64)
		frames, _ := strconv.Atoi(m[8])
		invSum, _ := strconv.ParseFloat(m[9], 64)
		w := frames
		if w <= 0 {
			w = 1 // pooled-fallback marker (no per-frame data): weight as one sample
		}
		key := codec + "/" + label
		j.mu.Lock()
		if j.Vmaf == nil {
			j.Vmaf = map[string]*VmafScore{}
		}
		acc := j.Vmaf[key]
		if acc == nil {
			acc = &VmafScore{Min: mn}
			j.Vmaf[key] = acc
		}
		// Score provenance (#117), when the worker stamped it (optional group).
		// Uniform across a rung's chunks, so last-writer-wins is a no-op overwrite.
		if m[11] != "" {
			acc.Model = m[11]
			acc.CommonHeight, _ = strconv.Atoi(m[12])
		}
		acc.sumMeanW += mean * float64(w)
		acc.Frames += w
		acc.invSum += invSum
		if mn < acc.Min {
			acc.Min = mn
		}
		acc.Mean = acc.sumMeanW / float64(acc.Frames)
		if acc.invSum > 0 {
			acc.Harmonic = float64(acc.Frames) / acc.invSum
		} else {
			acc.Harmonic = acc.Mean
		}
		j.mu.Unlock()
		return true
	}
	if m := planMarkerRe.FindStringSubmatch(line); m != nil {
		type stageDesc struct {
			Key   string `json:"key"`
			Label string `json:"label"`
		}
		var desc []stageDesc
		if err := json.Unmarshal([]byte(m[1]), &desc); err != nil {
			return false
		}
		j.mu.Lock()
		// MERGE semantics: existing rows keep their status/percent (so
		// the remote's PLAN can't wipe cloud:upload's "done" state when
		// the local orchestrator finished it earlier).
		//
		// New keys from the incoming PLAN are inserted AFTER the last
		// currently-running-or-done stage — not appended blindly at
		// the end. This keeps chronological order when a second PLAN
		// arrives mid-job: a cloud encode's remote Python pipeline
		// emits its per-variant PLAN while `cloud:encode-remote` is
		// the currently-running stage, so those per-variant entries
		// slot in right after it and before any still-pending tail
		// stages (remote:sync-outputs / cloud:download / cloud:cleanup).
		existing := make(map[string]bool, len(j.Stages))
		for _, s := range j.Stages {
			existing[s.Key] = true
		}

		var newStages []StageProgress
		for _, d := range desc {
			if existing[d.Key] {
				continue
			}
			newStages = append(newStages, StageProgress{
				Key: d.Key, Label: d.Label, Status: "pending", Percent: 0,
			})
			existing[d.Key] = true
		}

		if len(newStages) > 0 {
			// Find the position right after the last non-pending stage.
			// Fall back to end-of-list when every existing stage is
			// still pending (e.g. the very first PLAN emission).
			insertIdx := len(j.Stages)
			for i := len(j.Stages) - 1; i >= 0; i-- {
				if j.Stages[i].Status != "pending" {
					insertIdx = i + 1
					break
				}
			}
			tail := append([]StageProgress{}, j.Stages[insertIdx:]...)
			j.Stages = append(j.Stages[:insertIdx], newStages...)
			j.Stages = append(j.Stages, tail...)
		}
		j.mu.Unlock()
		return true
	}
	if m := timingMarkerRe.FindStringSubmatch(line); m != nil {
		var key string
		var cpu, total, mem float64
		for _, f := range strings.Fields(m[1]) {
			k, v, ok := strings.Cut(f, "=")
			if !ok {
				continue
			}
			switch k {
			case "key":
				key = v
			case "cpu_s":
				cpu, _ = strconv.ParseFloat(v, 64)
			case "total_s":
				total, _ = strconv.ParseFloat(v, 64)
			case "mem_mib":
				mem, _ = strconv.ParseFloat(v, 64)
			}
		}
		if key != "" {
			j.mu.Lock()
			for i := range j.Stages {
				if j.Stages[i].Key == key {
					j.Stages[i].CPUSeconds = cpu
					j.Stages[i].WorkerSeconds = total
					j.Stages[i].PeakMemMiB = mem
					break
				}
			}
			j.mu.Unlock()
		}
		return true
	}
	if m := stageMarkerRe.FindStringSubmatch(line); m != nil {
		key, status := m[1], m[2]
		percent, _ := strconv.ParseFloat(m[3], 64)
		now := time.Now()
		j.mu.Lock()
		found := false
		for i := range j.Stages {
			if j.Stages[i].Key == key {
				// Stamp StartedAt the first time the stage goes running;
				// stamp EndedAt when it settles done/failed. This is
				// conservative — a stage going running → pending wouldn't
				// clear StartedAt, which is fine for timing analysis.
				if j.Stages[i].StartedAt == nil && status == "running" {
					t := now
					j.Stages[i].StartedAt = &t
				}
				if j.Stages[i].EndedAt == nil && (status == "done" || status == "failed") {
					t := now
					j.Stages[i].EndedAt = &t
				}
				j.Stages[i].Status = status
				j.Stages[i].Percent = percent
				found = true
				break
			}
		}
		if !found {
			// A stage key not in the plan — e.g. the per-chunk encode stages
			// (encode:<codec>:<tier>:chunk<N>) that the cloud-batch translator
			// emits dynamically as the Map fans out, which the UI groups into
			// the chunk grid. Append it so it renders instead of being dropped.
			st := StageProgress{Key: key, Label: key, Status: status, Percent: percent}
			if status == "running" {
				st.StartedAt = &now
			}
			if status == "done" || status == "failed" {
				st.EndedAt = &now
			}
			j.Stages = append(j.Stages, st)
		}
		j.mu.Unlock()
		return true
	}
	if m := hostMarkerRe.FindStringSubmatch(line); m != nil {
		key, inst := m[1], m[2]
		j.mu.Lock()
		for i := range j.Stages {
			if j.Stages[i].Key == key {
				j.Stages[i].Instance = inst
				break
			}
		}
		j.mu.Unlock()
		return true
	}
	if m := reusedMarkerRe.FindStringSubmatch(line); m != nil {
		key := m[1]
		j.mu.Lock()
		found := false
		for i := range j.Stages {
			if j.Stages[i].Key == key {
				j.Stages[i].Reused = true
				found = true
				break
			}
		}
		// The reused marker fires at startup, usually BEFORE the chunk's first
		// STAGE marker — seed the stage so the flag survives; the later STAGE
		// updates status/percent in place without clearing it.
		if !found {
			j.Stages = append(j.Stages, StageProgress{Key: key, Label: key, Status: "pending", Reused: true})
		}
		j.mu.Unlock()
		return true
	}
	if commercialMarkerRe.MatchString(line) {
		// ALL cost baselines are now computed server-side from the ladder + probe
		// (Manager.projectAndSetCosts) so cloud-batch and local-dist agree — the
		// old local-only ENCODER-COMMERCIAL marker is just swallowed (not logged).
		return true
	}
	if m := fileMarkerRe.FindStringSubmatch(line); m != nil {
		idx, _ := strconv.Atoi(m[1])
		total, _ := strconv.Atoi(m[2])
		j.startFile(strings.TrimSpace(m[3]), idx, total)
		return true
	}
	if m := ffmpegMarkerRe.FindStringSubmatch(line); m != nil {
		j.mu.Lock()
		j.FfmpegVersion = strings.TrimSpace(m[1])
		j.mu.Unlock()
		return true
	}
	if m := codecLibMarkerRe.FindStringSubmatch(line); m != nil {
		j.mu.Lock()
		if j.CodecLibs == nil {
			j.CodecLibs = map[string]string{}
		}
		// "libx265 4.2+37-b81f650e" — lib name kept with the version so the
		// record stays readable without knowing the codec->library mapping.
		j.CodecLibs[m[1]] = m[2] + " " + m[3]
		j.mu.Unlock()
		return true
	}
	if m := bootMarkerRe.FindStringSubmatch(line); m != nil {
		ami := m[1]
		j.mu.Lock()
		j.BootAMI = ami
		// Pre-baked when the instance's AMI matches the worker AMI we baked
		// (WORKER_AMI_ID, passed in by the Makefile). Empty env => unknown.
		j.PrebakedAMI = ami != "" && ami == os.Getenv("WORKER_AMI_ID")
		j.mu.Unlock()
		return true
	}
	if m := reclaimMarkerRe.FindStringSubmatch(line); m != nil {
		count, _ := strconv.ParseFloat(m[2], 64)
		lost, _ := strconv.ParseFloat(m[3], 64)
		total, _ := strconv.ParseFloat(m[4], 64)
		j.recordReclaim(m[1], count, lost, total)
		return true
	}
	if m := costMarkerRe.FindStringSubmatch(line); m != nil {
		kv := parseMarkerFields(m[1])
		if kv["exec"] == "" {
			return true // malformed; consumed, but nothing to record
		}
		num := func(k string) float64 { v, _ := strconv.ParseFloat(kv[k], 64); return v }
		j.recordCost(kv["exec"], execCost{
			Spot: num("spot_usd"), OnDemand: num("ondemand_usd"),
			Saved: num("saved_usd"), VCPUHours: num("vcpu_hours"),
			EgressGB: num("egress_gb"), EgressUSD: num("egress_usd"),
			EgressAvoidedGB: num("egress_avoided_gb"),
			SfnUSD:          num("sfn_usd"), RequestUSD: num("s3_request_usd"),
			StorageUSD: num("storage_usd"), PutEstUSD: num("s3_put_est_usd"),
		}, kv["unmodelled"])
		return true
	}
	if m := machinesMarkerRe.FindStringSubmatch(line); m != nil {
		inst, _ := strconv.Atoi(m[2])
		mh, _ := strconv.ParseFloat(m[3], 64)
		ah, _ := strconv.ParseFloat(m[4], 64)
		idle, _ := strconv.ParseFloat(m[5], 64)
		j.mu.Lock()
		j.Instances, j.MachineVCPUHours = inst, mh
		j.AllocatedVCPUHours, j.IdlePct = ah, idle
		j.mu.Unlock()
		return true
	}
	if m := rentalMarkerRe.FindStringSubmatch(line); m != nil {
		vcpu, _ := strconv.Atoi(m[4])
		launch, _ := strconv.ParseInt(m[5], 10, 64)
		end, _ := strconv.ParseInt(m[6], 10, 64)
		firstJob, _ := strconv.ParseInt(m[7], 10, 64)
		lastJob, _ := strconv.ParseInt(m[8], 10, 64)
		chunks, _ := strconv.Atoi(m[10])
		row := MachineRental{
			ID: m[2], Type: m[3], VCPUs: vcpu,
			LaunchedAt: launch, EndedAt: end,
			FirstJobAt: firstJob, LastJobAt: lastJob,
			Alive: m[9] == "1", Chunks: chunks,
		}
		j.mu.Lock()
		// Re-emitted on every rental summary as a run progresses, so replace the
		// row for this instance rather than appending a duplicate with a stale
		// end time.
		replaced := false
		for i := range j.Rentals {
			if j.Rentals[i].ID == row.ID {
				j.Rentals[i] = row
				replaced = true
				break
			}
		}
		if !replaced {
			j.Rentals = append(j.Rentals, row)
		}
		j.mu.Unlock()
		return true
	}
	if m := statsMarkerRe.FindStringSubmatch(line); m != nil {
		wall, _ := strconv.ParseFloat(m[2], 64)
		vcpuH, _ := strconv.ParseFloat(m[3], 64)
		longest, _ := strconv.ParseFloat(m[4], 64)
		jobs, _ := strconv.Atoi(m[6])
		maxV, _ := strconv.Atoi(m[7])
		slowest := m[5]
		if slowest == "-" { // driver's "no slowest" sentinel
			slowest = ""
		}
		j.recordRunStats(m[1], runStat{
			wallS: wall, vcpuH: vcpuH, slowestS: longest,
			slowest: slowest, jobs: jobs, maxVcpus: maxV,
		})
		return true
	}
	// Non-consuming matches: capture sideband info but still let the
	// line flow into the raw log buffer (return false).
	if m := cloudJobIDRe.FindStringSubmatch(line); m != nil {
		j.mu.Lock()
		j.CloudJobID = m[1]
		j.mu.Unlock()
	}
	if strings.Contains(line, "SPOT INTERRUPTION") {
		j.mu.Lock()
		j.FailureReason = "spot_interrupted"
		j.mu.Unlock()
	}
	return false
}

// recordReclaim folds one chunk's ENCODER-RECLAIM accounting into the job.
// Per-stage [count, lost_s, total_s] is cumulative and can arrive twice (the
// live poll while running + the final on completion), so max-merge per stage,
// then re-sum the job totals. WastePct = ReclaimLostS/EncodeTotalS.
func (j *Job) recordReclaim(key string, count, lost, total float64) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.reclaimByStage == nil {
		j.reclaimByStage = map[string][3]float64{}
	}
	cur := j.reclaimByStage[key]
	if count > cur[0] {
		cur[0] = count
	}
	if lost > cur[1] {
		cur[1] = lost
	}
	if total > cur[2] {
		cur[2] = total
	}
	j.reclaimByStage[key] = cur
	var c, l, t float64
	for _, v := range j.reclaimByStage {
		c += v[0]
		l += v[1]
		t += v[2]
	}
	j.ReclaimCount, j.ReclaimLostS, j.EncodeTotalS = int(c), l, t
}

// recordCost folds one execution's ENCODER-COST into the job. Keyed by exec so
// a reattach re-emitting is idempotent; job totals sum across files/executions.
// runStat is one execution's efficiency stats from ENCODER-STATS.
type runStat struct {
	wallS, vcpuH, slowestS float64
	slowest                string
	jobs, maxVcpus         int
}

// recordRunStats folds one execution's ENCODER-STATS into the job. Multi-file
// jobs run one execution per file (sequentially), so wall/vCPU-hours/jobs SUM,
// the slowest chunk is the MAX across files, and max-vCPUs is the last seen;
// concurrency + efficiency are recomputed from the summed totals. Keyed by exec
// so a reattach that re-emits the marker overwrites rather than double-counts.
func (j *Job) recordRunStats(exec string, s runStat) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.statsByExec == nil {
		j.statsByExec = map[string]runStat{}
	}
	j.statsByExec[exec] = s
	var wall, vcpuH, slowestS float64
	var jobs, maxV int
	var slowest string
	for _, v := range j.statsByExec {
		wall += v.wallS
		vcpuH += v.vcpuH
		jobs += v.jobs
		if v.maxVcpus > 0 {
			maxV = v.maxVcpus
		}
		if v.slowestS > slowestS {
			slowestS, slowest = v.slowestS, v.slowest
		}
	}
	j.EncodeWallS, j.CPUVcpuH, j.JobCount, j.MaxVcpus = wall, vcpuH, jobs, maxV
	j.SlowestJob, j.SlowestJobS = slowest, slowestS
	if wall > 0 {
		j.AvgConcurrency = vcpuH * 3600.0 / wall
		if maxV > 0 {
			j.EfficiencyPct = j.AvgConcurrency / float64(maxV) * 100.0
		}
	}
}

// recordCost folds one execution's ENCODER-COST into the job. Keyed by exec so a
// re-emitted marker replaces rather than double-counts.
//
// Egress is summed here with compute because a run's cost is ONE number to a
// person. Reporting compute alone is how this shipped for months quoting 21% of
// the bill as though it were all of it (#217).
func (j *Job) recordCost(exec string, c execCost, unmodelled string) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.costByExec == nil {
		j.costByExec = map[string]execCost{}
	}
	j.costByExec[exec] = c
	var s, o, sv, eg, eu, ea, sf, rq, st, pe float64
	for _, v := range j.costByExec {
		s += v.Spot
		o += v.OnDemand
		sv += v.Saved
		eg += v.EgressGB
		eu += v.EgressUSD
		ea += v.EgressAvoidedGB
		sf += v.SfnUSD
		rq += v.RequestUSD
		st += v.StorageUSD
		pe += v.PutEstUSD
	}
	j.SpotUSD, j.OnDemandUSD, j.SavedUSD = s, o, sv
	j.EgressGB, j.EgressUSD, j.EgressAvoidedGB = eg, eu, ea
	j.SfnUSD, j.RequestUSD, j.StorageUSD, j.PutEstUSD = sf, rq, st, pe
	// The headline: what the run cost at full rate, not what one component did.
	j.TotalUSD = s + eu + sf + rq + st + pe
	// Carried so the UI can say what the total EXCLUDES. A partial number that
	// looks complete is the whole reason #217 was filed.
	if unmodelled != "" {
		j.CostUnmodelled = unmodelled
	}
}

// execCost is one execution's contribution. A named struct rather than the old
// [4]float64 so adding a term cannot silently shift the meaning of an index.
type execCost struct {
	Spot, OnDemand, Saved, VCPUHours     float64
	EgressGB, EgressUSD, EgressAvoidedGB float64
	// Priced at FULL RATE with no free-tier discount, so the total answers
	// "what would this cost if every allowance were spent?" rather than "what
	// did AWS happen to charge this month". Step Functions is why that matters:
	// the account is already at 4,000/4,000 free transitions.
	SfnUSD, RequestUSD, StorageUSD float64
	// PutEstUSD is FITTED from staged bytes, not counted — Tier1 requests
	// happen across workers and the packager, which keep no counters. Kept in a
	// distinct field so the UI can label it an estimate rather than pass it off
	// as measured.
	PutEstUSD float64
}

// parseMarkerFields splits "a=1 b=2" into a map. Values cannot contain spaces,
// which every field here satisfies.
func parseMarkerFields(s string) map[string]string {
	out := map[string]string{}
	for _, tok := range strings.Fields(s) {
		if k, v, ok := strings.Cut(tok, "="); ok {
			out[k] = v
		}
	}
	return out
}

// startFile archives the current Stages (so end-of-job timing can still
// show every phase that ran) and resets for a fresh file. Called from
// parseMarker on [[ENCODER-FILE ...]], and directly by the local encode
// loop before each worker launch.
func (j *Job) startFile(name string, idx, total int) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if len(j.Stages) > 0 {
		// Tag each archived stage with the file it belonged to so the
		// timing table can group by file.
		tagged := make([]StageProgress, len(j.Stages))
		copy(tagged, j.Stages)
		j.StagesHistory = append(j.StagesHistory, FileStages{
			File:       j.CurrentFile,
			FileIndex:  j.CurrentFileIndex,
			TotalFiles: j.TotalFiles,
			Stages:     tagged,
		})
	}
	j.Stages = nil
	j.CurrentFile = name
	j.CurrentFileIndex = idx
	j.TotalFiles = total
}

// splitLinesOrCR is a bufio.Scanner SplitFunc that emits a token on either
// \n or \r. ffmpeg's -stats output uses \r to overwrite the same terminal
// line, so splitting only on \n would buffer the entire encode's stats
// into one token that only gets flushed when ffmpeg finally exits.
func splitLinesOrCR(data []byte, atEOF bool) (advance int, token []byte, err error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	for i, b := range data {
		if b == '\n' || b == '\r' {
			return i + 1, data[:i], nil
		}
	}
	if atEOF {
		return len(data), data, nil
	}
	return 0, nil, nil
}

type Target string

const (
	// TargetLocalDist ("local") fans one encode's chunks across the local
	// Temporal worker pool (Mac + any other boxes) via cli_local_dist
	// --backend temporal, with MinIO as the shared store.
	TargetLocalDist Target = "local"
	// TargetCloudBatch ("cloud") fans chunks across AWS Batch spot via Step
	// Functions. The retired single-box and one-box-EC2 targets are gone; the
	// old "local-dist"/"cloud-batch" values are still accepted as input aliases
	// (see NormalizeTarget).
	TargetCloudBatch Target = "cloud"
)

// NormalizeTarget maps the previous target values onto the current ones so
// existing .env (DEFAULT_TARGET), persisted job state, and old API callers keep
// working after the local-dist/cloud-batch -> local/cloud rename.
func NormalizeTarget(t Target) Target {
	switch t {
	case "local-dist":
		return TargetLocalDist
	case "cloud-batch":
		return TargetCloudBatch
	}
	return t
}

// envOr returns the env var value or a fallback.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

type JobStatus string

const (
	StatusQueued    JobStatus = "queued"
	StatusRunning   JobStatus = "running"
	StatusDone      JobStatus = "done"
	StatusFailed    JobStatus = "failed"
	StatusCancelled JobStatus = "cancelled"
)

type JobConfig struct {
	Files []string `json:"files"`
	Codec string   `json:"codec"`
	// Ladder selects the bitrate ladder by name (legacy | apple | apple-uniq |
	// any custom ladder). Empty means DefaultLadderName. Threaded to the local
	// encoder via --ladder and resolved by buildSFNInput for the cloud path.
	Ladder string `json:"ladder,omitempty"`
	MaxRes string `json:"max_res"`
	// MinRes is MaxRes's floor: rungs shorter than this tier are dropped, so
	// the pair selects a contiguous band of the ladder. Empty = no floor.
	MinRes          string `json:"min_res,omitempty"`
	Target          Target `json:"target"`
	Time            string `json:"time"`
	SegmentDuration string `json:"segment_duration"`
	PartialDuration string `json:"partial_duration"`
	// ChunkDuration tunes the cloud-batch encode granularity: "" or "dynamic"
	// = size each variant's chunks by learned encode speed (slow → 30s floor,
	// cheap → whole clip, so all variants finish ~together); "whole" = one job
	// per variant (max efficiency, no joins); or a fixed seconds value
	// (multiple of the 6s segment duration). On cloud-batch it sizes the Batch
	// fan-out; on local it sizes cli_local's parallel chunk pool (see
	// localChunkSeconds) — "dynamic"/"" default to 2×segment there.
	ChunkDuration string `json:"chunk_duration,omitempty"`
	GopDuration   string `json:"gop_duration"`
	// OutputTag is appended to the output dir name ("<stem>_<tag>_<codec>"). Not a
	// user field — filled from the selected ladder's output_tag by resolveTimings,
	// so a profile like apple-uniq-live-6s marks its outputs ("_6s") for go-live.
	OutputTag     string `json:"output_tag,omitempty"`
	HlsFormat     string `json:"hls_format"`
	Padding       string `json:"padding"`
	KeepMezzanine bool   `json:"keep_mezzanine"`
	ForceReencode bool   `json:"force_reencode"`
	// SkipMediaDownload leaves a cloud run's segments in S3 and brings back only
	// manifests and metadata — ~4 MB instead of ~2.6 GB (#214). Egress is billed
	// per GB and was 3x the compute on the 1-3 Aug bill; during iteration almost
	// every downloaded ladder is superseded within hours. Fetch the media later
	// from the Outputs tab. Costed at EgressUSDPerGB with no free-tier discount,
	// so the saving is the same figure on any day of the month.
	//
	// A pointer so nil means "use the server default" (SKIP_OUTPUT_MEDIA), and an
	// explicit true/false from the client wins. Cloud-batch only — the local and
	// local-dist paths write straight to disk with no egress to avoid.
	SkipMediaDownload *bool `json:"skip_media_download,omitempty"`
	// Burnin toggles the per-variant burnt-in text overlay (timecode / rate /
	// codec+res / encoder / watermark labels, plus the PADDING label). On by
	// default — a pointer so nil (older persisted jobs, or a client that omits
	// it) means "on", and only an explicit false disables it. Threaded to the
	// local encoder via --no-burnin and to cloud-batch via the BURNIN env in
	// each variant job's containerOverrides. See BurninEnabled.
	Burnin *bool `json:"burnin,omitempty"`
	// PromoteAfter rsyncs each produced output to the configured PROMOTE_DESTS
	// (local + remote live libraries) once the encode succeeds and moves to
	// OUTPUT_DIR — the automatic version of the per-output Promote button.
	PromoteAfter bool `json:"promote,omitempty"`
	// HevcSinglePass forces HEVC to encode single-pass. Two-pass is a codec
	// property, applied automatically: HEVC (libx265) is two-pass by default
	// (the accurate-average path — its single-pass VBR drifts below target),
	// H264 (libx264) is always single-pass (its VBV already hits target, so
	// two-pass just doubles time), AV1 has no two-pass path. So the default
	// (false) does the right thing per codec with no user decision. Set true
	// only to run HEVC single-pass (~2x faster) for a bitrate comparison.
	// Local passes --hevc-single-pass to cli_local.py; cloud bakes the
	// per-codec decision into each variant's TWO_PASS via the SFN input.
	HevcSinglePass bool `json:"hevc_single_pass,omitempty"`
	// MeasureVmaf enables the per-rendition VMAF quality audit (#24): after each
	// chunk encodes, the worker scores it against the mezzanine at source res and
	// emits an [[ENCODER-VMAF …]] marker; the control plane aggregates per rung.
	// Off by default (slow — see the cost notes on #24). Local only for now.
	MeasureVmaf bool `json:"measure_vmaf,omitempty"`
	// VmafPrescale (#109) builds a near-lossless pre-scaled VMAF reference once
	// per worker box, so the audit decodes a small fast H.264 file instead of
	// re-downscaling the native (4K/AV1) mezzanine on every chunk. Only helps
	// when VMAF is on; gated per-source (>= a few renditions, source slower to
	// decode+scale than the ref, fits locally) and falls back to the mezzanine.
	VmafPrescale bool `json:"vmaf_prescale,omitempty"`
	// CPU architecture for cloud encodes: "intel" | "amd" | "graviton".
	// Empty defaults to intel. Ignored for local encodes (which always
	// run on the host's native architecture).
	CpuArch string `json:"cpu_arch,omitempty"`
	// UseSpot controls EC2 purchasing mode for cloud encodes. Pointer
	// so `omitempty` works and an unset value lets cli_cloud.py apply
	// its env-var default (USE_SPOT=true). Ignored for local encodes.
	UseSpot *bool `json:"use_spot,omitempty"`
}

// MachineRental is one instance's slice of a cloud run: which box, how big, and
// the two instants that bound what you were charged for. Times are unix seconds,
// matching the marker; the UI converts.
//
// Alive means EndedAt is "as of the last rental summary" rather than a real
// termination, so it will grow. That distinction has to survive to the client:
// treating an un-terminated box as billing up to the present is how a timeline
// ends up charging for machines EC2 has already reclaimed.
type MachineRental struct {
	ID         string `json:"id"`
	Type       string `json:"type"`
	VCPUs      int    `json:"vcpus"`
	LaunchedAt int64  `json:"launched_at"`
	EndedAt    int64  `json:"ended_at"`
	// When Batch work began and ended on this box — NOT the first/last stage.
	// A job outlives its stage: pkgall's stages total 73s against a 177s job,
	// the remainder being the parallel fetch and the packaged-output upload.
	// A timeline drawn from stages alone paints that as idle.
	FirstJobAt int64 `json:"first_job_at,omitempty"`
	LastJobAt  int64 `json:"last_job_at,omitempty"`
	Alive      bool  `json:"alive,omitempty"`
	Chunks     int   `json:"chunks,omitempty"`
}

type StageProgress struct {
	Key     string  `json:"key"`
	Label   string  `json:"label"`
	Status  string  `json:"status"` // pending | running | done | failed
	Percent float64 `json:"percent"`
	// Instance is the machine (EC2 instance-id, or a stable ARN slug) this
	// stage's Batch job ran on — set from ENCODER-HOST, used by the UI to
	// colour the chunk plot by instance. Empty until the job is placed.
	Instance string `json:"instance,omitempty"`
	// Reused is true when this chunk's output was already staged from a prior
	// run and got skipped (not re-encoded) — set from ENCODER-REUSED. The UI
	// styles reused cells with a distinct neutral so they don't read as a
	// freshly-encoded machine colour (or the transient no-instance blue).
	Reused bool `json:"reused,omitempty"`
	// CPUSeconds / WorkerSeconds / PeakMemMiB come from ENCODER-TIMING, which
	// the worker emits from getrusage as the phase exits.
	//
	// WorkerSeconds is the worker's OWN wall clock for the phase, which is not
	// the same as EndedAt-StartedAt: those bracket the whole Batch job, so they
	// include image pull, the mezzanine fetch, and the upload. The gap between
	// the two is exactly the non-encoding overhead, which is why both are kept.
	//
	// CPUSeconds against WorkerSeconds gives cores actually used — the number
	// that says whether a reservation is earning its keep.
	CPUSeconds    float64 `json:"cpu_seconds,omitempty"`
	WorkerSeconds float64 `json:"worker_seconds,omitempty"`
	PeakMemMiB    float64 `json:"peak_mem_mib,omitempty"`
	// Timestamps of state transitions. StartedAt is set the first time
	// the stage sees `status=running`; EndedAt is set when it reaches
	// a terminal state (done|failed). Used to build the end-of-job
	// timing summary so the user can see where the wall-clock went.
	StartedAt *time.Time `json:"started_at,omitempty"`
	EndedAt   *time.Time `json:"ended_at,omitempty"`
}

// VmafScore is the aggregated VMAF for one rendition (#24). Mean/Harmonic/Min +
// Frames are the display values; sumMeanW/invSum are running accumulators (not
// serialized) that make the cross-chunk recombination correct.
type VmafScore struct {
	Mean     float64 `json:"mean"`
	Harmonic float64 `json:"harmonic"`
	Min      float64 `json:"min"`
	Frames   int     `json:"frames"`
	// Provenance (#117): the model and comparison height this score was measured
	// at. A VMAF number is only interpretable against these, and scores from
	// different sources (different comparison heights) must never be pooled.
	// Uniform across a rung's chunks (same source) — the worker stamps them onto
	// every ENCODER-VMAF marker; empty/0 when an older worker omitted them.
	Model        string  `json:"model,omitempty"`
	CommonHeight int     `json:"common_height,omitempty"`
	sumMeanW     float64 // Σ(chunk_mean × chunk_frames)
	invSum       float64 // Σ(chunk inv_sum)
}

// MarshalJSON serialises a Job under its own lock.
//
// The SSE stream sends *Job POINTERS to subscribers, so the handler marshals
// whatever the job holds AT SEND TIME rather than a snapshot taken when the
// notify fired. That is deliberate — it means a frame is always the latest
// state and can never deliver an older one after a newer one — but it also
// means json.Marshal walks j.Stages concurrently with upsertStage mutating it.
//
// Without this the read is unsynchronised, and the dangerous case is not a
// stale field: upsertStage APPENDS a StageProgress for a key it has not seen,
// and an append can reallocate the backing array while the marshaller is
// walking it. Appends happen while the grid is filling in, which is exactly
// when the UI was reported showing cells briefly wrong.
//
// The alias type is the standard idiom to avoid recursing into this method.
// Conversion is pointer-to-pointer, so the mutex is never copied.
func (j *Job) MarshalJSON() ([]byte, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	type alias Job
	return json.Marshal((*alias)(j))
}

type Job struct {
	ID        string     `json:"id"`
	Config    JobConfig  `json:"config"`
	Status    JobStatus  `json:"status"`
	StartedAt time.Time  `json:"started_at"`
	EndedAt   *time.Time `json:"ended_at,omitempty"`
	// FanOutAt is stamped when the cloud-batch execution first submits its encode
	// jobs (fan-out) — i.e. when actual encoding starts, after upload + mezzanine.
	// Lets timing anchor on encode start rather than submit (which includes the
	// upload + queue wait).
	FanOutAt *time.Time `json:"fan_out_at,omitempty"`
	Progress string     `json:"progress"`
	Error    string     `json:"error,omitempty"`

	// Outputs are the directory names this job moved into OutputDir, recorded
	// at the move rather than inferred. Set only on success, so its presence
	// also answers "is there anything to fetch". A client that needs the files
	// (encoder_cli --download) cannot derive these: resolveCodec may narrow the
	// codec list and the ladder may contribute an output_tag.
	Outputs []string `json:"outputs,omitempty"`

	// BootAMI is the AMI a cloud worker's instance actually booted from
	// (from the [[ENCODER-BOOT]] marker); PrebakedAMI is true when that
	// matches the pre-baked worker AMI, i.e. the image was already resident
	// and the ECR pull was skipped. Drives the "pre-baked AMI" UI badge.
	BootAMI     string `json:"boot_ami,omitempty"`
	PrebakedAMI bool   `json:"prebaked_ami,omitempty"`

	// What actually encoded this, reported by the worker that ran ffmpeg.
	// FfmpegVersion is the build id; CodecLibs maps our codec name to the
	// encoder library that produced its bitstream ("hevc" -> "libx265 4.2+37-…").
	//
	// h264 is ABSENT FROM CodecLibs BY DESIGN: libx264 prints no version at
	// init, unlike libx265 and SVT-AV1, and its core number lives only in the
	// bitstream SEI. FfmpegVersion is the proxy — a given BtbN build bundles a
	// fixed x264 — so the gap is bounded, not unbounded. Do not read an absent
	// h264 entry as "not recorded".
	FfmpegVersion string            `json:"ffmpeg_version,omitempty"`
	CodecLibs     map[string]string `json:"codec_libs,omitempty"`

	// Spot-reclaim accounting for this file's encode (from ENCODER-RECLAIM).
	// ReclaimCount = chunks reclaimed; ReclaimLostS = encode wall-seconds thrown
	// away; EncodeTotalS = all encode wall-seconds. The UI shows the waste ratio
	// ReclaimLostS/EncodeTotalS — how much of the encoding a spot reclaim cost.
	ReclaimCount int     `json:"reclaim_count,omitempty"`
	ReclaimLostS float64 `json:"reclaim_lost_s,omitempty"`
	EncodeTotalS float64 `json:"encode_total_s,omitempty"`
	// per-stage cumulative [count, lost_s, total_s], max-merged; not serialized.
	reclaimByStage map[string][3]float64

	// Spot-vs-on-demand cost (from ENCODER-COST). SpotUSD is what this job cost
	// on the spot fleet; SavedUSD = OnDemandUSD - SpotUSD (what spot avoided).
	// Machine rental vs allocation (from ENCODER-MACHINES). MachineVCPUHours is
	// what was rented — instance lifetimes — against AllocatedVCPUHours, the sum
	// of each job's reservation. IdlePct is the share that ran no job at all.
	MachineVCPUHours   float64 `json:"machine_vcpu_hours,omitempty"`
	AllocatedVCPUHours float64 `json:"allocated_vcpu_hours,omitempty"`
	IdlePct            float64 `json:"idle_pct,omitempty"`
	Instances          int     `json:"instances,omitempty"`
	// Rentals is the per-instance detail behind those aggregates (ENCODER-RENTAL):
	// which box, and exactly when it appeared and went away. The UI's machine
	// timeline needs absolute launch/end to place lanes on a shared axis — a
	// lifetime alone cannot say when a box appeared relative to the others, and
	// the browser cannot source termination for itself (the inventory endpoint
	// drops terminated instances and is cached).
	Rentals []MachineRental `json:"rentals,omitempty"`
	SpotUSD float64         `json:"spot_usd,omitempty"`
	// Egress this run paid for, and what --no-media left in S3 (#217). Priced
	// flat at EgressUSDPerGB with no free-tier discount, so two runs of the same
	// ladder quote the same figure whatever the date. TotalUSD = spot + egress:
	// the headline, because reporting compute alone quoted 21% of a real bill.
	EgressGB        float64 `json:"egress_gb,omitempty"`
	EgressUSD       float64 `json:"egress_usd,omitempty"`
	EgressAvoidedGB float64 `json:"egress_avoided_gb,omitempty"`
	// Step Functions, S3 requests and S3 storage, all at full rate.
	SfnUSD     float64 `json:"sfn_usd,omitempty"`
	RequestUSD float64 `json:"request_usd,omitempty"`
	StorageUSD float64 `json:"storage_usd,omitempty"`
	// Fitted, not measured — labelled as such in the UI.
	PutEstUSD float64 `json:"put_est_usd,omitempty"`
	TotalUSD  float64 `json:"total_usd,omitempty"`
	// Comma-separated list of cost lines the total does NOT include, so the UI
	// can show the gap rather than implying there isn't one.
	CostUnmodelled string  `json:"cost_unmodelled,omitempty"`
	OnDemandUSD    float64 `json:"ondemand_usd,omitempty"`
	SavedUSD       float64 `json:"saved_usd,omitempty"`
	// CommercialUSD / MediaConvertUSD are what this job's output ladder would have
	// cost on hosted transcoders (a generic commercial cloud encoder, and AWS
	// MediaConvert) — comparison baselines against our own spot/local cost. Both
	// are per-output-minute pricing, emitted once per job by the orchestrator
	// (scripts/infinite_streaming_encoder/commercial_cloud.py). For local jobs (no SpotUSD) they're
	// "what you'd have paid a hosted transcoder for the same work".
	CommercialUSD   float64 `json:"commercial_usd,omitempty"`
	MediaConvertUSD float64 `json:"mediaconvert_usd,omitempty"`
	// Two more transcode-only baselines (#229). Coconut applies no codec
	// multiplier at all; Bitmovin multiplies base x resolution x codec x passes,
	// which makes it the dearest despite the smallest headline rate.
	CoconutUSD  float64 `json:"coconut_usd,omitempty"`
	BitmovinUSD float64 `json:"bitmovin_usd,omitempty"`
	// AwsSpotUSD is what this ladder would cost on OUR AWS Batch spot fleet —
	// compute-based (encode vCPU-hours × spot rate), summed over every variant, so
	// unlike the per-output-minute SaaS baselines above it reflects each variant's
	// real encode work (a 4K HEVC 2-pass rendition ~200× a 360p h264 one).
	AwsSpotUSD     float64 `json:"aws_spot_usd,omitempty"`
	AwsOndemandUSD float64 `json:"aws_ondemand_usd,omitempty"` // on-demand (reclaim-proof) upper bound
	// LocalWallSeconds is the "cost" of encoding this ladder on YOUR local fleet:
	// its only downside is time, so it's reported as predicted wall-clock (end −
	// start), not dollars. A makespan estimate — total core-seconds of work over
	// the fleet's parallel cores, floored by the longest atomic chunk — computed
	// from the learned LOCAL speeds. Recomputed live as the encode learns, so a
	// running local encode's estimate converges on its own actual runtime.
	LocalWallSeconds float64 `json:"local_wall_s,omitempty"`
	// per-execution [spot, ondemand, saved, vcpu_h], summed across files; not serialized.
	costByExec map[string]execCost

	// Cached first-source probe (duration/width/fps) for live cost + local-wall
	// re-prediction, so the per-sample refresh needn't re-invoke ffprobe. Set once
	// by ensureSourceProbe; guarded by mu.
	probeDone     bool
	probeDuration float64
	probeWidth    int
	probeFps      int
	probeHasAudio bool
	probeSrcMbps  float64

	// Run efficiency stats (from ENCODER-STATS, computed by the driver from Batch
	// at job end). EncodeWallS = the Batch encode span (denominator for
	// concurrency, distinct from the end-to-end job wall which includes upload +
	// sync); CPUVcpuH = total vCPU-hours of compute; AvgConcurrency = vCPU-hours ÷
	// encode-wall-hours (avg vCPUs kept busy); EfficiencyPct = AvgConcurrency ÷
	// max vCPUs (how well the fleet ceiling was used); SlowestJob/SlowestJobS =
	// the single longest chunk (the makespan floor); JobCount = Batch jobs.
	EncodeWallS    float64 `json:"encode_wall_s,omitempty"`
	CPUVcpuH       float64 `json:"cpu_vcpu_h,omitempty"`
	AvgConcurrency float64 `json:"avg_concurrency,omitempty"`
	EfficiencyPct  float64 `json:"efficiency_pct,omitempty"`
	SlowestJob     string  `json:"slowest_job,omitempty"`
	SlowestJobS    float64 `json:"slowest_job_s,omitempty"`
	JobCount       int     `json:"job_count,omitempty"`
	MaxVcpus       int     `json:"max_vcpus,omitempty"`
	// per-execution stats, summed/maxed across files; not serialized.
	statsByExec map[string]runStat

	// launchComplete is set once this cloud job has submitted ALL its encode jobs
	// (fully fanned out); the age-ordered launch gate lets the next-oldest job
	// proceed only after this is true (or the job goes terminal). Not serialized.
	launchComplete bool

	// Stages is populated from [[ENCODER-PLAN]] + [[ENCODER-STAGE]]
	// markers the Python orchestrator emits. Empty when the job hasn't
	// reached the Python side yet (still queued, pre-run) or for old
	// jobs that pre-date the marker format. For multi-file jobs,
	// Stages reflects ONLY the currently-processing file; finished
	// files land in StagesHistory.
	Stages []StageProgress `json:"stages,omitempty"`

	// OverallProgress is a single 0–100% for the whole encode, WEIGHTED by each
	// variant's expected wall-time (1/learned-speed) so a slow 4K HEVC 2-pass
	// rendition counts far more than a fast h264 360p one — and it self-corrects
	// as the learned-speed model updates from finished chunks. ETASeconds is a
	// linear extrapolation of that weighted progress over elapsed time (0 when
	// not estimable). Both recomputed on every notify by Manager.computeProgress.
	OverallProgress float64 `json:"overall_progress,omitempty"`
	ETASeconds      float64 `json:"eta_seconds,omitempty"`
	// OutputProgress is the OTHER progress metric: % of the output video timeline
	// produced (content-seconds done ÷ total), every rendition weighted equally
	// regardless of encode cost. Differs from OverallProgress (compute-weighted)
	// because 4K HEVC is cheap in output-minutes but huge in compute. "Processing"
	// vs "output-time" — most people want OverallProgress; this is the companion.
	OutputProgress float64 `json:"output_progress,omitempty"`

	// Per-file progress indicators. CurrentFile/FileIndex/TotalFiles
	// populate on the first [[ENCODER-FILE]] marker (or the Go loop's
	// direct call for local encodes). UI renders "File N of M: name"
	// above the stages table.
	CurrentFile      string `json:"current_file,omitempty"`
	CurrentFileIndex int    `json:"current_file_index,omitempty"`
	TotalFiles       int    `json:"total_files,omitempty"`

	// Vmaf holds per-rendition VMAF scores (#24), keyed by "codec/label" (e.g.
	// "hevc/1080p"), aggregated from [[ENCODER-VMAF]] markers. Populated only
	// when the job ran with MeasureVmaf. Per-chunk markers fold together with a
	// frame-weighted mean + a correctly recombined harmonic mean.
	Vmaf map[string]*VmafScore `json:"vmaf,omitempty"`

	// StagesHistory holds the finished files' stages so end-of-job
	// timing can show every phase that ran, not just the last file's.
	StagesHistory []FileStages `json:"stages_history,omitempty"`

	// CloudJobID is the timestamp-based id cli_cloud.py uses for its
	// S3 prefix (e.g. 20260420T203910Z-1). Captured from the plan
	// header for cloud-job bookkeeping. Empty for local or old jobs.
	CloudJobID string `json:"cloud_job_id,omitempty"`

	// FailureReason categorises why a failed job failed, for UI
	// styling. Today: "spot_interrupted" | "" (generic).
	FailureReason string `json:"failure_reason,omitempty"`

	mu       sync.Mutex
	logLines []string
	// Diagnostic stream: its own file, never trimmed. See appendDiag.
	// Guarded by diagMu, deliberately NOT j.mu. Writing under j.mu meant every
	// diagnostic line — ~3,300 on a 336-chunk run, to an external volume —
	// blocked upsertStage and the SSE marshal for the length of a disk write.
	diagMu    sync.Mutex
	diagPath  string
	diagFile  *os.File
	cancelled bool
	// cloudExecArn is the SFN execution ARN of the CURRENT cloud-batch file's
	// running execution. Persisted so Reconcile can re-attach to it after a
	// restart (poll the running execution) instead of submitting a duplicate.
	// Empty for local jobs and before the first submit.
	cloudExecArn string
}

func (j *Job) CloudExecArn() string {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.cloudExecArn
}

func (j *Job) setCloudExecArn(arn string) {
	j.mu.Lock()
	j.cloudExecArn = arn
	j.mu.Unlock()
}

type FileStages struct {
	File       string          `json:"file"`
	FileIndex  int             `json:"file_index"`
	TotalFiles int             `json:"total_files"`
	Stages     []StageProgress `json:"stages"`
}

func (j *Job) MarkCancelled() {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.cancelled = true
}

func (j *Job) IsCancelled() bool {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.cancelled
}

// Diagnostic streams: high-volume, machine-oriented lines emitted for
// post-mortem rather than for a human watching the log viewer.
//
// They are kept OUT of logLines and streamed to their own file. Both halves
// matter. A 336-chunk run emits ~1,300 [state] and ~2,000 [fill] lines against
// a buffer that keeps the LAST 500 — so routing them through logLines both lost
// almost all of them AND evicted the human-readable narration they were meant
// to explain. A run was measured afterwards with 6 of its [state] lines
// surviving.
var diagPrefixes = []string{"[state] ", "[fill] ", "[census] ", "[host] "}

func isDiagLine(line string) bool {
	for _, p := range diagPrefixes {
		if strings.HasPrefix(line, p) {
			return true
		}
	}
	return false
}

func (j *Job) AppendLog(line string) {
	if isDiagLine(line) {
		j.appendDiag(line)
		return
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	j.logLines = append(j.logLines, line)
	if len(j.logLines) > 1000 {
		j.logLines = j.logLines[len(j.logLines)-500:]
	}
}

// appendDiag streams one diagnostic line straight to the job's .diag.log.
//
// Written as it arrives rather than buffered and flushed at the end: the runs
// worth investigating are the ones that crash, get killed, or are cancelled
// mid-flight, and a buffer held until terminal loses exactly those. The file
// handle is opened once and closed by closeDiag on the terminal path.
func (j *Job) appendDiag(line string) {
	// diagMu, NOT j.mu. Diagnostics must never make the job's own state slower
	// to read or write: j.mu serialises stage updates and the SSE marshal, and
	// holding it across a disk write put every one of those behind file I/O.
	j.diagMu.Lock()
	defer j.diagMu.Unlock()
	if j.diagFile == nil {
		if j.diagPath == "" {
			return // no path configured; drop rather than guess a location
		}
		f, err := os.OpenFile(j.diagPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return // diagnostics are never worth failing an encode for
		}
		j.diagFile = f
	}
	_, _ = j.diagFile.WriteString(line + "\n")
}

func (j *Job) closeDiag() {
	j.diagMu.Lock()
	defer j.diagMu.Unlock()
	if j.diagFile != nil {
		_ = j.diagFile.Close()
		j.diagFile = nil
	}
}

func (j *Job) LogLines() []string {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]string, len(j.logLines))
	copy(out, j.logLines)
	return out
}

type Manager struct {
	mu   sync.Mutex
	jobs []*Job

	// In-flight on-demand media fetches, keyed by output dir name (#214). Its
	// own lock because a fetch runs for minutes and must not block job
	// bookkeeping. See remote.go.
	fetchMu sync.Mutex
	fetches map[string]*FetchState

	SourceDir   string
	OutputDir   string
	TmpDir      string
	ScriptsDir  string
	DockerImage string

	// Host-side paths (as the Docker daemon sees them) for bind-mounting into
	// worker containers. When the Go server is itself inside a container,
	// `-v SourceDir:...` would refer to the wrong path because `docker run`
	// bind mounts are resolved by the daemon against the host filesystem.
	HostSourceDir string
	HostOutputDir string
	HostTmpDir    string
	HostAWSDir    string

	// Image used for detached worker containers.
	EncoderImage string

	// State Machine ARN for the cloud-batch target. Empty when no
	// Batch infrastructure is deployed yet (the cloud-batch target
	// then errors cleanly at submit time).
	StateMachineArn string

	// Ladders is the persisted ladder store (built-in + user-defined). Owns
	// $TmpDir/ladders.json; resolves the concrete rungs for the cloud path.
	Ladders *LadderStore

	// Curves holds measured VMAF-vs-bitrate points used to ESTIMATE a ladder
	// rung's quality without encoding ($TmpDir/quality-curves.json overlaying
	// the built-in seed). Design-time only — never a claim about a real encode.
	Curves *CurveStore

	// Speeds learns per-variant encode speed (content/wall) from completed
	// encodes ($TmpDir/encode_speeds.json); the dynamic chunk selector uses it.
	Speeds *EncodeSpeedStore

	// Persisted global settings (e.g. watcher on/off) owned at
	// $TmpDir/settings.json. Survives restarts so a UI toggle sticks.
	settingsMu sync.Mutex
	settings   Settings

	sem chan struct{}
	// launchMu/launchCond order the cloud-batch launch → fan-out phase by QUEUE
	// AGE: a job waits until every OLDER active cloud job has fully fanned out
	// (submitted all its jobs) before submitting its own execution. Age-ordered,
	// not first-come — so an earlier job floods the fleet with its higher-priority
	// jobs first regardless of upload timing (Batch never preempts).
	launchMu      sync.Mutex
	launchCond    *sync.Cond
	warmReconcile func()
	subscribers   []chan *Job
	subMu         sync.Mutex

	// fleetCPU holds recent per-machine CPU samples for the distributed-local
	// fleet view (machine -> ring of samples); fleetChunks holds the chunks
	// currently running on each machine (activity ids). Both fed by ENCODER-FLEET
	// markers off the orchestrator's stdout. Guarded by fleetMu.
	fleetMu     sync.Mutex
	fleetCPU    map[string][]fleetSample
	fleetChunks map[string][]string
	// fleetFirstSeen is when each machine first reported, used to order the
	// fleet view oldest-first so a box that has been encoding since the start
	// keeps its position and newly-scaled-up boxes append at the bottom.
	//
	// Kept separately rather than read off fleetCPU[m][0]: the sample ring is
	// trimmed to fleetHistoryLen, so its first element is the oldest RETAINED
	// sample, not the first ever. On a long run every machine's apparent
	// first-seen would keep advancing and the order would churn — the exact
	// thing this is meant to stop.
	fleetFirstSeen map[string]time.Time
}

// fleetSample is one CPU reading for a distributed-local worker box.
type fleetSample struct {
	T    time.Time
	Busy float64 // logical cores busy (from the box's /proc/stat)
	Perf float64 // its perf-core target (4/4/8); 100% = that many cores busy
}

// fleetHistoryLen caps the per-machine sample ring (~3 min at the orchestrator's
// ~2s emit cadence) — enough for a live sparkline without unbounded growth.
const fleetHistoryLen = 90

// fleetMinSampleGap is the minimum spacing between accepted samples for one
// machine. See recordFleetCPU: without it, N chunks on a cloud instance each
// report the same host CPU on their own timers and the history stops being a
// uniform time series.
const fleetMinSampleGap = 3 * time.Second

// recordFleetCPU folds one ENCODER-FLEET marker into the per-machine CPU history.
// Returns true when the line was such a marker (so the scanner suppresses it from
// the log buffer, like the other structured markers).
func (m *Manager) recordFleetCPU(line string) bool {
	mm := fleetMarkerRe.FindStringSubmatch(line)
	if mm == nil {
		return false
	}
	busy, _ := strconv.ParseFloat(mm[2], 64)
	perf, _ := strconv.ParseFloat(mm[3], 64)
	var chunks []string
	if mm[5] != "" {
		chunks = strings.Split(mm[5], "|")
	}
	m.fleetMu.Lock()
	if m.fleetCPU == nil {
		m.fleetCPU = map[string][]fleetSample{}
		m.fleetChunks = map[string][]string{}
		m.fleetFirstSeen = map[string]time.Time{}
	}
	if _, seen := m.fleetFirstSeen[mm[1]]; !seen {
		m.fleetFirstSeen[mm[1]] = time.Now()
	}
	// Throttle per machine. On the cloud path EVERY chunk container on a box
	// emits this — /proc/stat is the host's, so 4 concurrent chunks all report
	// the same instance figure, independently, on their own staggered timers.
	// Each reading is valid, but appending all of them turns one machine's
	// history into N interleaved series: clustered samples, then a gap, and
	// consecutive points measured over different intervals. Keeping the first
	// sample in each window makes the series uniform however many chunks are
	// running, so the sparkline means the same thing on both targets.
	//
	// The local path is unaffected — its orchestrator is a single reporter per
	// machine and emits slower than this window.
	if prev := m.fleetCPU[mm[1]]; len(prev) > 0 &&
		time.Since(prev[len(prev)-1].T) < fleetMinSampleGap {
		m.fleetChunks[mm[1]] = chunks // chunk list still refreshes
		m.fleetMu.Unlock()
		return true
	}
	s := append(m.fleetCPU[mm[1]], fleetSample{T: time.Now(), Busy: busy, Perf: perf})
	if len(s) > fleetHistoryLen {
		s = s[len(s)-fleetHistoryLen:]
	}
	m.fleetCPU[mm[1]] = s
	m.fleetChunks[mm[1]] = chunks
	m.fleetMu.Unlock()
	return true
}

// fleetEntryTTL drops a machine from the fleet view once it has been silent this
// long. Without it m.fleetCPU only ever grows: FleetCPU skipped machines with NO
// samples but never aged out old ones, so terminated cloud instances stayed on
// the panel at their final reading until the server restarted. Measured on a
// real run, the panel showed 11 machines averaging 60.3% CPU when ONE machine
// existed at 0.7% — the other ten were terminated 11-13 minutes earlier. Before
// a restart, entries 17 hours old were still being rendered.
//
// It also corrupted two things that are not the panel: localFleetPerfCores sums
// Perf across every entry, so dead CLOUD instances inflated the LOCAL fleet's
// core budget and shrank its wall-time projection.
//
// 10 minutes is deliberately generous — a machine that is encoding reports every
// ~5s, so this only catches boxes that are genuinely gone or idle. Callers that
// can do better should: the cloud panel cross-checks against the EC2 inventory
// (see attachFleetCPU), which is authoritative rather than a timeout.
const fleetEntryTTL = 10 * time.Minute

// FleetCPUEntry is one machine's CPU history for the local fleet view. History is
// the normalized busy% (busy/perf*100): 100 = the box at its perf-core target,
// >100 = SMT/E-core spill. Latest/Perf label the current reading; AgeS flags a
// stale machine (no recent sample = not currently encoding).
type FleetCPUEntry struct {
	Machine string    `json:"machine"`
	Perf    float64   `json:"perf"`
	Latest  float64   `json:"latest"`
	History []float64 `json:"history"`
	AgeS    float64   `json:"age_s"`
	Chunks  []string  `json:"chunks,omitempty"` // activity ids running on this box now
}

// FleetCPU returns each machine's recent CPU history for the fleet endpoint,
// normalized to its perf-core target. Machines with no samples are omitted.
func (m *Manager) FleetCPU() []FleetCPUEntry {
	m.fleetMu.Lock()
	defer m.fleetMu.Unlock()
	out := make([]FleetCPUEntry, 0, len(m.fleetCPU))
	now := time.Now()
	for machine, samples := range m.fleetCPU {
		if len(samples) == 0 {
			continue
		}
		if now.Sub(samples[len(samples)-1].T) > fleetEntryTTL {
			continue // silent too long — see fleetEntryTTL
		}
		hist := make([]float64, len(samples))
		for i, s := range samples {
			if s.Perf > 0 {
				hist[i] = s.Busy / s.Perf * 100
			}
		}
		last := samples[len(samples)-1]
		out = append(out, FleetCPUEntry{
			Machine: machine, Perf: last.Perf,
			Latest:  hist[len(hist)-1],
			History: hist,
			AgeS:    now.Sub(last.T).Seconds(),
			Chunks:  m.fleetChunks[machine],
		})
	}
	// Oldest-first by when the machine first reported. Sorting by machine name
	// (the previous behaviour) is effectively random for EC2 instance ids, so
	// the list reshuffled every time the fleet scaled and a box you were
	// watching jumped position. Ties and unknowns fall back to the name so the
	// order is still deterministic.
	sort.Slice(out, func(i, j int) bool {
		ti, oki := m.fleetFirstSeen[out[i].Machine]
		tj, okj := m.fleetFirstSeen[out[j].Machine]
		if oki && okj && !ti.Equal(tj) {
			return ti.Before(tj)
		}
		if oki != okj {
			return oki
		}
		return out[i].Machine < out[j].Machine
	})
	return out
}

// signalLaunch wakes any jobs waiting for their launch turn to re-check (called
// when a job fully fans out or finalizes, so the next-oldest can proceed).
func (m *Manager) signalLaunch() {
	m.launchMu.Lock()
	m.launchCond.Broadcast()
	m.launchMu.Unlock()
}

// waitForLaunchTurn blocks the caller until no OLDER active cloud job is still
// launching (queued/running without having fully fanned out). Guarantees cloud
// executions start in queue order.
func (m *Manager) waitForLaunchTurn(job *Job) {
	m.launchMu.Lock()
	defer m.launchMu.Unlock()
	// Also break on cancel so a job queued behind a long-uploading older job
	// doesn't sit here until its turn comes — Cancel broadcasts launchCond to
	// wake us, and the caller re-checks IsCancelled() right after this returns.
	for m.olderStillLaunching(job) && !job.IsCancelled() {
		m.launchCond.Wait()
	}
}

func (m *Manager) olderStillLaunching(job *Job) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, j := range m.jobs {
		if j == job || j.ID >= job.ID { // IDs are ascending timestamps → older = smaller
			continue
		}
		if j.Config.Target != TargetCloudBatch {
			continue
		}
		if (j.Status == StatusQueued || j.Status == StatusRunning) && !j.isLaunchComplete() {
			return true
		}
	}
	return false
}

// triggerWarm nudges the awswatch keep-warm loop to re-evaluate now (job start /
// finalize), so the floor doesn't lag a poll interval behind the queue.
func (m *Manager) triggerWarm() {
	if m.warmReconcile != nil {
		m.warmReconcile()
	}
}

type ManagerConfig struct {
	SourceDir       string
	OutputDir       string
	TmpDir          string
	ScriptsDir      string
	DockerImage     string
	HostSourceDir   string
	HostOutputDir   string
	HostTmpDir      string
	HostAWSDir      string
	EncoderImage    string
	StateMachineArn string
	MaxConcurrent   int
	// WarmReconcile, if set, is called on a job START and FINALIZE so the
	// awswatch keep-warm floor reacts immediately (raise on start, drop when the
	// queue empties) instead of waiting for its next poll. Non-blocking.
	WarmReconcile func()
}

func NewManager(cfg ManagerConfig) *Manager {
	if cfg.MaxConcurrent < 1 {
		cfg.MaxConcurrent = 1
	}
	if cfg.EncoderImage == "" {
		cfg.EncoderImage = "encoder:latest"
	}
	os.MkdirAll(cfg.TmpDir, 0755)
	m := &Manager{
		Ladders:         LoadLadderStore(filepath.Join(cfg.TmpDir, "ladders.json")),
		Curves:          LoadCurveStore(filepath.Join(cfg.TmpDir, "quality-curves.json")),
		Speeds:          LoadEncodeSpeedStore(filepath.Join(cfg.TmpDir, "encode_speeds.json")),
		SourceDir:       cfg.SourceDir,
		OutputDir:       cfg.OutputDir,
		TmpDir:          cfg.TmpDir,
		ScriptsDir:      cfg.ScriptsDir,
		DockerImage:     cfg.DockerImage,
		HostSourceDir:   cfg.HostSourceDir,
		HostOutputDir:   cfg.HostOutputDir,
		HostTmpDir:      cfg.HostTmpDir,
		HostAWSDir:      cfg.HostAWSDir,
		EncoderImage:    cfg.EncoderImage,
		StateMachineArn: cfg.StateMachineArn,
		sem:             make(chan struct{}, cfg.MaxConcurrent),
		warmReconcile:   cfg.WarmReconcile,
	}
	m.launchCond = sync.NewCond(&m.launchMu)
	return m
}

func (m *Manager) Subscribe() chan *Job {
	m.subMu.Lock()
	defer m.subMu.Unlock()
	ch := make(chan *Job, 16)
	m.subscribers = append(m.subscribers, ch)
	return ch
}

func (m *Manager) Unsubscribe(ch chan *Job) {
	m.subMu.Lock()
	defer m.subMu.Unlock()
	for i, s := range m.subscribers {
		if s == ch {
			m.subscribers = append(m.subscribers[:i], m.subscribers[i+1:]...)
			close(ch)
			return
		}
	}
}

func (m *Manager) notify(j *Job) {
	m.computeProgress(j) // refresh weighted progress + ETA before pushing to SSE
	m.subMu.Lock()
	defer m.subMu.Unlock()
	for _, ch := range m.subscribers {
		select {
		case ch <- j:
		default:
		}
	}
}

func (m *Manager) Jobs() []*Job {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]*Job, len(m.jobs))
	copy(out, m.jobs)
	return out
}

// ActiveCloudJobs counts submitted cloud jobs still queued or running — the
// app's own job queue. The awswatch keep-warm loop uses this so a box is held
// across the gap BETWEEN sequential jobs (one finishing before the next's AWS
// resources appear), not only while a single job's Batch work is live — so the
// next job in the queue doesn't cold-start.
func (m *Manager) ActiveCloudJobs() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	n := 0
	for _, j := range m.jobs {
		if (j.Status == StatusQueued || j.Status == StatusRunning) &&
			j.Config.Target == TargetCloudBatch {
			n++
		}
	}
	return n
}

// jobPriorityBase returns the SchedulingPriority band for a cloud job so EARLIER
// jobs in the queue outrank LATER ones on the shared Batch fleet: the oldest
// active job's phases all sit above the next job's, so a later job only gets
// capacity the earlier one can't use (its tail — or spare capacity on a big
// fleet). Bands are 1000 apart (oldest active job: 9000, next: 8000, …), with
// the within-job variant/phase priority (0-999) riding inside. Job IDs are
// ascending timestamps, so a smaller ID is older.
// markFanOut stamps FanOutAt once, the first time this job's execution submits
// encode jobs. Idempotent across reattaches / multiple detections.
func (j *Job) markFanOut() {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.FanOutAt == nil {
		now := time.Now()
		j.FanOutAt = &now
	}
}

// markLaunchComplete records that this job has submitted ALL its encode jobs
// (fully fanned out) — the signal that lets the next-oldest cloud job launch.
func (j *Job) markLaunchComplete() {
	j.mu.Lock()
	j.launchComplete = true
	j.mu.Unlock()
}

func (j *Job) isLaunchComplete() bool {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.launchComplete
}

func (m *Manager) jobPriorityBase(job *Job) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	older := 0
	for _, j := range m.jobs {
		if j == job {
			continue
		}
		if (j.Status == StatusQueued || j.Status == StatusRunning) &&
			j.Config.Target == TargetCloudBatch &&
			j.ID < job.ID {
			older++
		}
	}
	if older > 8 {
		older = 8
	}
	return 9000 - older*1000
}

// localJobRank returns this local-dist job's arrival rank among currently-active
// local-dist jobs — 0 for the oldest, 1 for the next, and so on. The Temporal
// orchestrator folds it into every chunk's priority_key (rank*bands + costBand)
// so ALL of an older job's chunks outrank any younger job's: the oldest job runs
// to completion and a younger one only backfills spare slots. This is the local
// mirror of jobPriorityBase's Batch bands. Job IDs are ascending timestamps, so a
// smaller ID is older. Capped at 9 so rank*5+5 stays within matching.priorityLevels
// (50); beyond 10 concurrent local jobs the extras share the lowest band.
func (m *Manager) localJobRank(job *Job) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	older := 0
	for _, j := range m.jobs {
		if j == job {
			continue
		}
		if (j.Status == StatusQueued || j.Status == StatusRunning) &&
			j.Config.Target == TargetLocalDist &&
			j.ID < job.ID {
			older++
		}
	}
	if older > 9 {
		older = 9
	}
	return older
}

func (m *Manager) GetJob(id string) *Job {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, j := range m.jobs {
		if j.ID == id {
			return j
		}
	}
	return nil
}

func (m *Manager) Submit(cfg JobConfig) *Job {
	job := &Job{
		ID:        fmt.Sprintf("%d", time.Now().UnixMilli()),
		Config:    cfg,
		Status:    StatusQueued,
		StartedAt: time.Now(),
		Progress:  "queued",
	}
	m.mu.Lock()
	m.jobs = append(m.jobs, job)
	m.mu.Unlock()
	m.persistState(job, 0)
	m.notify(job)

	go m.run(job, 0)
	return job
}

func (m *Manager) run(job *Job, startIdx int) {
	// Diagnostics stream to their own file from the first line. Set here rather
	// than at teardown so a run that crashes or is cancelled still leaves them.
	logsDir := filepath.Join(m.TmpDir, "logs")
	if err := os.MkdirAll(logsDir, 0o755); err == nil {
		job.mu.Lock()
		job.diagPath = filepath.Join(logsDir, job.ID+".diag.log")
		job.mu.Unlock()
	}
	if startIdx == 0 {
		job.Progress = "waiting for slot"
	} else {
		job.Progress = fmt.Sprintf("resuming at file %d/%d", startIdx+1, len(job.Config.Files))
	}
	m.notify(job)
	m.sem <- struct{}{}
	defer func() { <-m.sem }()
	// Job reached terminal state (any return path below) → nudge keep-warm to
	// re-evaluate now, so the floor drops the moment the queue empties.
	defer m.triggerWarm()

	// If cancel fired while this job was queued, skip straight to cleanup.
	if job.IsCancelled() {
		m.finalizeCancelled(job, filepath.Join(m.TmpDir, job.ID))
		return
	}

	job.Status = StatusRunning
	m.triggerWarm() // run starting → warm a box promptly, don't wait for the poll
	if startIdx == 0 {
		job.Progress = "starting"
	} else {
		job.Progress = fmt.Sprintf("resumed at file %d/%d", startIdx+1, len(job.Config.Files))
	}
	m.notify(job)

	// Encode into TmpDir so partial output never appears in OutputDir.
	// On success, move completed directories to OutputDir.
	jobTmpDir := filepath.Join(m.TmpDir, job.ID)
	os.MkdirAll(jobTmpDir, 0755)

	// local-dist is the only script-based (worker-container) path now; cloud-batch
	// branches to its SFN driver inside encodeFilesFrom. (Legacy single-box "local"
	// and direct-EC2 "cloud" targets were retired — cli_local.py survives only as
	// the phase entry point cloud-batch's Batch job-defs invoke.)
	script := filepath.Join(m.ScriptsDir, "infinite_streaming_encoder", "cli_local_dist.py")

	err := m.encodeFilesFrom(job, jobTmpDir, script, startIdx)

	now := time.Now()
	job.EndedAt = &now
	if job.IsCancelled() {
		m.finalizeCancelled(job, jobTmpDir)
		return
	}
	if err != nil {
		job.Status = StatusFailed
		job.Error = err.Error()
		job.Progress = "failed"
	} else {
		job.Progress = "moving to output"
		m.notify(job)
		moved, mvErr := m.moveTmpToOutput(jobTmpDir)
		if mvErr != nil {
			job.Status = StatusFailed
			job.Error = "encode succeeded but move failed: " + mvErr.Error()
			job.Progress = "failed"
		} else {
			job.Status = StatusDone
			job.Progress = "complete"
			// What this job actually produced. moveTmpToOutput already knows —
			// it just moved these — and every consumer that wanted the answer
			// was reconstructing it by globbing OutputDir for "<stem>_p*"
			// (make smoke did exactly that). A guess, because resolveCodec may
			// have narrowed the codec list and the ladder may have added an
			// output_tag, so the name is not derivable from the request.
			job.Outputs = moved
			// Write the profile metadata (encode.json + a one-line manifest
			// comment) into each output dir BEFORE promote, so a promoted copy
			// carries it too.
			m.writeEncodeMetaForDirs(job, moved)
			if job.Config.PromoteAfter {
				m.autoPromote(job, moved)
			}
		}
	}
	// On failure, preserve the job's tmp dir — it holds diagnostic
	// artifacts like the remote user-data.log (for cloud jobs) and any
	// partially-encoded variant MP4s (for local). Without this the
	// atexit cleanup and RemoveAll conspire to destroy every trace of
	// what went wrong before we can inspect it. On success everything's
	// already been moved to OutputDir, so clearing tmp is safe.
	if job.Status == StatusFailed {
		m.preserveTmpForFailure(job, jobTmpDir)
	} else {
		os.RemoveAll(jobTmpDir)
	}
	m.removePersistedState(job.ID)
	m.persistSpotSample(job)
	m.writeHistory(job)
	m.notify(job)
}

// persistSpotSample appends this finished cloud-batch job's spot-vs-on-demand
// cost and reclaim-waste to a rolling log in TmpDir (host-mounted, survives
// restarts) so the AWS view can show the accumulated "saved by spot" and the
// trailing-24h reclaim-waste %. No-op for jobs without cost/reclaim data.
func (m *Manager) persistSpotSample(job *Job) {
	if job.EncodeTotalS <= 0 && job.SavedUSD <= 0 {
		return
	}
	type sample struct {
		Ts          int64   `json:"ts"`
		LostS       float64 `json:"lost_s"`
		TotalS      float64 `json:"total_s"`
		SpotUSD     float64 `json:"spot_usd"`
		OnDemandUSD float64 `json:"ondemand_usd"`
		SavedUSD    float64 `json:"saved_usd"`
	}
	path := filepath.Join(m.TmpDir, "spot_samples.json")
	var samples []sample
	if data, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(data, &samples)
	}
	samples = append(samples, sample{
		Ts: time.Now().Unix(), LostS: job.ReclaimLostS, TotalS: job.EncodeTotalS,
		SpotUSD: job.SpotUSD, OnDemandUSD: job.OnDemandUSD, SavedUSD: job.SavedUSD,
	})
	if len(samples) > 5000 {
		samples = samples[len(samples)-5000:]
	}
	if data, err := json.MarshalIndent(samples, "", " "); err == nil {
		tmp := path + ".tmp"
		if os.WriteFile(tmp, data, 0644) == nil {
			_ = os.Rename(tmp, path)
		}
	}
}

// preserveTmpForFailure moves jobTmpDir to $TMP_DIR/failed/<job_id>/ so the
// user can inspect user-data.log, half-encoded variant MP4s, etc. after
// the job exits.
func (m *Manager) preserveTmpForFailure(job *Job, jobTmpDir string) {
	failedRoot := filepath.Join(m.TmpDir, "failed")
	os.MkdirAll(failedRoot, 0755)
	dst := filepath.Join(failedRoot, job.ID)
	// Collision shouldn't happen (job IDs are monotonic epoch ms) but
	// be defensive — rename the existing one out of the way.
	if _, err := os.Stat(dst); err == nil {
		_ = os.Rename(dst, dst+".old."+fmt.Sprint(time.Now().Unix()))
	}
	os.MkdirAll(dst, 0755)
	// Move whatever local tmp artifacts exist (cloud user-data.log, partial local
	// MP4s). For DIST jobs jobTmpDir is usually EMPTY — the work lives in MinIO +
	// the workers' /tmp — which is why this dir was previously useless. The
	// captured job log below is the real diagnostic in that case (#116).
	if entries, err := os.ReadDir(jobTmpDir); err == nil {
		for _, e := range entries {
			src := filepath.Join(jobTmpDir, e.Name())
			if os.Rename(src, filepath.Join(dst, e.Name())) != nil {
				_ = copyDir(src, filepath.Join(dst, e.Name()))
			}
		}
		os.RemoveAll(jobTmpDir)
	}
	// ALWAYS capture the job's log — orchestrator output plus the phase-failure
	// tail relayed up from the worker — so the failed dir is never empty (#116).
	if lines := job.LogLines(); len(lines) > 0 {
		_ = os.WriteFile(filepath.Join(dst, "job.log"),
			[]byte(strings.Join(lines, "\n")+"\n"), 0644)
	}
	job.AppendLog(fmt.Sprintf(
		"[preserved failure artifacts] %s", dst))
}

// finalizeCancelled handles the post-cancel bookkeeping: discard any partial
// tmp output, drop the persisted-state file, record the history entry, and
// flip the job to cancelled. Called from both the pre-slot-acquire path
// (job was queued when cancel fired) and the post-encode path (encode
// returned an error because we killed the worker container).
func (m *Manager) finalizeCancelled(job *Job, jobTmpDir string) {
	now := time.Now()
	job.EndedAt = &now
	job.Status = StatusCancelled
	job.Progress = "cancelled"
	job.Error = ""
	os.RemoveAll(jobTmpDir)
	m.removePersistedState(job.ID)
	m.writeHistory(job)
	m.notify(job)
}

// Cancel signals a job to stop. For a queued job this just marks it; when
// the goroutine picks up the semaphore slot it'll short-circuit into
// finalizeCancelled. For a running job we gracefully STOP the worker
// container (SIGTERM + grace period) so the Python orchestrator inside
// gets a chance to run its cleanup handlers — critical for cloud jobs,
// where cli_cloud.py's SIGTERM handler calls terminate_job() which
// terminates the EC2 instance via boto3 before exiting. Previously we
// did `docker rm -f` here, which SIGKILLs the container immediately
// and bypasses the Python cleanup, leaving the EC2 instance to bill
// until the awswatch watchdog (4h default) reaped it.
//
// Returns false if no job with that ID exists, or true in all other cases
// (idempotent — calling twice on the same job is a no-op).
func (m *Manager) Cancel(id string) bool {
	job := m.GetJob(id)
	if job == nil {
		return false
	}
	if job.Status == StatusDone || job.Status == StatusFailed || job.Status == StatusCancelled {
		return true
	}
	job.MarkCancelled()
	m.notify(job)
	// Wake any cloud job blocked in waitForLaunchTurn so it re-checks and, if
	// this is that job, exits the wait instead of holding until its turn.
	m.signalLaunch()

	// Discover worker containers for this job. The label filter matches
	// every file-index container we might have spawned; there will only
	// ever be one alive at a time under the current serial encode loop,
	// but the filter is defensive against future parallelism.
	out, err := exec.Command("docker", "ps", "-a",
		"--filter", "label=encoder.job_id="+id,
		"--format", "{{.Names}}",
	).Output()
	if err != nil {
		return true
	}
	names := strings.Fields(string(out))
	if len(names) == 0 {
		return true
	}

	// Stop strategy differs by target:
	//
	//   cloud: `docker stop --time 30` (SIGTERM + 30s grace). The
	//   Python process inside has a SIGTERM handler that calls
	//   terminate_job() to release the EC2 instance + spot request;
	//   that typically completes in 2-5s, so 30s is generous.
	//
	//   local: `docker kill` (SIGKILL, immediate). There's no cloud
	//   cleanup to run, and a graceful stop doesn't help anyway —
	//   cli_local.py blocks in subprocess.run(ffmpeg) so its SIGTERM
	//   handler (if any) can't fire until ffmpeg exits, which is
	//   exactly the thing Cancel is trying to interrupt. SIGKILL to
	//   PID 1 terminates the container and all children at once.
	cancelMode := "stop"
	go func() {
		for _, name := range names {
			if cancelMode == "kill" {
				exec.Command("docker", "kill", name).Run()
			} else {
				exec.Command("docker", "stop", "--time", "30", name).Run()
			}
			exec.Command("docker", "rm", name).Run()
		}
	}()
	return true
}

// Resume re-runs a failed or cancelled job IN PLACE — same *Job, same ID — so
// the jobs list keeps ONE row that transitions cancelled/failed → running and
// updates live, instead of spawning a fresh entry. This is Reconcile-on-demand:
// run() is already re-entrant (startup calls it on rebuilt jobs), and a job's
// cancel signal is a plain resettable bool, not a spent context.
//
// The payoff is automatic chunk reuse: because the ID is unchanged, so is
// DistJobPrefix, so the orchestrator's own per-chunk _object_exists check (see
// cli_local_dist.py) finds every chunk already staged in MinIO and skips
// straight to the unfinished ones. No cross-job prefix plumbing needed. A
// cancelled dist job never runs its own staging cleanup (the control plane
// docker-stops it), so its completed chunks are still there to reuse.
//
// The original config is re-used unchanged: an interrupted job never moved
// anything to OutputDir (that happens only on success), so there's nothing to
// archive and resolveCodec has nothing to skip — no ForceReencode needed.
// Returns false if no such job or it isn't failed/cancelled.
func (m *Manager) Resume(id string) bool {
	job := m.GetJob(id)
	if job == nil || (job.Status != StatusFailed && job.Status != StatusCancelled) {
		return false
	}
	// Cancel tears the worker container down asynchronously (stop + rm in a
	// goroutine, up to 30s), and a failed run's container can briefly linger
	// too. runFileContainer reattaches to any container of the deterministic
	// name instead of starting fresh — so a leftover would make it drain the
	// OLD run's exit code. Remove any container for this job id first.
	m.removeJobContainers(id)

	job.mu.Lock()
	job.cancelled = false
	job.Status = StatusQueued
	job.Progress = "queued"
	job.Error = ""
	job.FailureReason = ""
	job.EndedAt = nil
	job.StartedAt = time.Now() // fresh timing baseline for progress/ETA
	job.Stages = nil           // orchestrator re-emits; reused chunks report done
	job.StagesHistory = nil
	job.OverallProgress = 0
	job.OutputProgress = 0
	job.ETASeconds = 0
	job.launchComplete = false
	job.mu.Unlock()

	m.persistState(job, 0)
	m.notify(job)
	go m.run(job, 0)
	return true
}

// removeJobContainers force-removes any worker container(s) still labelled for
// this job — defensive cleanup before an in-place Resume, since Cancel's stop+rm
// runs asynchronously and may not have completed.
func (m *Manager) removeJobContainers(id string) {
	out, err := exec.Command("docker", "ps", "-a",
		"--filter", "label=encoder.job_id="+id, "--format", "{{.Names}}").Output()
	if err != nil {
		return
	}
	for _, name := range strings.Fields(string(out)) {
		exec.Command("docker", "rm", "-f", name).Run()
	}
}

func (m *Manager) writeHistory(job *Job) {
	logsDir := filepath.Join(m.TmpDir, "logs")
	os.MkdirAll(logsDir, 0755)
	job.closeDiag() // flush + release the diagnostic stream for this run

	// Write full log to a per-job file, stripping ANSI escape codes
	logPath := filepath.Join(logsDir, job.ID+".log")
	lines := job.LogLines()
	cleaned := stripANSI(strings.Join(lines, "\n") + "\n")
	os.WriteFile(logPath, []byte(cleaned), 0644)

	// Append entry to history.md
	historyPath := filepath.Join(m.TmpDir, "history.md")
	f, err := os.OpenFile(historyPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return
	}
	defer f.Close()

	started := job.StartedAt.Format("2006-01-02 15:04:05 MST")
	duration := "—"
	if job.EndedAt != nil {
		duration = job.EndedAt.Sub(job.StartedAt).Round(time.Second).String()
	}

	reason := "manual"
	if job.Config.ForceReencode {
		reason = "force re-encode (archives previous output)"
	}

	fmt.Fprintf(f, "## Job %s — %s\n\n", job.ID, string(job.Status))
	fmt.Fprintf(f, "- **Started:** %s\n", started)
	fmt.Fprintf(f, "- **Duration:** %s\n", duration)
	fmt.Fprintf(f, "- **Status:** %s\n", job.Status)
	fmt.Fprintf(f, "- **Reason:** %s\n", reason)
	fmt.Fprintf(f, "- **Target:** %s\n", job.Config.Target)
	fmt.Fprintf(f, "- **Files:** %s\n", strings.Join(job.Config.Files, ", "))
	fmt.Fprintf(f, "- **Codec:** %s\n", job.Config.Codec)
	fmt.Fprintf(f, "- **Ladder:** %s%s\n", EffectiveLadder(job.Config), ladderRungSummary(job))
	fmt.Fprintf(f, "- **Chunking:** %s\n", chunkModeLabel(job.Config.ChunkDuration))
	if job.Config.MaxRes != "" {
		fmt.Fprintf(f, "- **Max Res:** %s\n", job.Config.MaxRes)
	}
	if job.Config.MinRes != "" {
		fmt.Fprintf(f, "- **Min Res:** %s\n", job.Config.MinRes)
	}
	if job.Config.Time != "" {
		fmt.Fprintf(f, "- **Time limit:** %ss\n", job.Config.Time)
	}
	fmt.Fprintf(f, "- **Segment:** %ss / Partial: %ss / GOP: %ss\n",
		defaultVal(job.Config.SegmentDuration, "6"),
		defaultVal(job.Config.PartialDuration, "0.2"),
		defaultVal(job.Config.GopDuration, "1.0"))
	if job.Config.HlsFormat != "" && job.Config.HlsFormat != "fmp4" {
		fmt.Fprintf(f, "- **HLS Format:** %s\n", job.Config.HlsFormat)
	}
	if job.Config.Padding != "" {
		fmt.Fprintf(f, "- **Padding:** %s\n", job.Config.Padding)
	}
	if job.Error != "" {
		fmt.Fprintf(f, "- **Error:** %s\n", job.Error)
	}
	fmt.Fprintf(f, "- **Log:** [%s.log](logs/%s.log)\n", job.ID, job.ID)
	writeConfigBlock(f, job.Config)

	// Timing summary — per-stage wall-clock, so the user can see
	// where the job's total duration went. Particularly useful for
	// cloud jobs: how long was spent on boot vs docker pull vs
	// encode vs S3 sync — informs parallelisation decisions when
	// running multiple encodes.
	m.writeTimingSummary(f, job)

	fmt.Fprintf(f, "\n---\n\n")
}

// writeConfigBlock records the COMPLETE encode configuration into history.md as
// the JSON body that would resubmit the job through POST /api/encode.
//
// The prose lines above it are a readable summary of the options people usually
// care about. This block exists because that summary is a hand-maintained list,
// and a hand-maintained list is exactly what fails: #202 was a rerun rebuilt
// from history.md that silently used the default ladder, and the very next rerun
// — after the ladder line was added — silently used dynamic chunking instead of
// the fixed 12s the original ran with, cutting 41 chunks where the run being
// reproduced had 336. Two different fields, same root cause: the record was a
// subset of the config, and nothing made the omission visible.
//
// Marshalling JobConfig itself inverts that. A new option is recorded from the
// day the field lands rather than the day someone notices a rerun didn't match.
//
// Three fields are resolved to their effective values first, because for them an
// absent value does not mean "no value" — it means a default applied elsewhere
// (DefaultLadderName, "dynamic", burn-in on). Recording them raw would leave the
// reader deducing the default from code, which is the thing that went wrong.
// Every other omitempty field is honestly absent-means-default.
func writeConfigBlock(f *os.File, cfg JobConfig) {
	cfg.Ladder = EffectiveLadder(cfg)
	// "" -> "dynamic" only. NOT chunkModeLabel, which renders a fixed size as
	// "12s" — a display string that fails ParseFloat on the way back in and
	// silently becomes the 30s default. The block has to round-trip, so every
	// value written here must be one variantChunkSeconds accepts.
	if cfg.ChunkDuration == "" {
		cfg.ChunkDuration = "dynamic"
	}
	burnin := cfg.BurninEnabled()
	cfg.Burnin = &burnin

	body, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		// Never fail the history entry over its appendix; the prose above is
		// still the record, and a marshal error here is worth saying out loud.
		fmt.Fprintf(f, "\n<!-- config not recorded: %v -->\n", err)
		return
	}
	fmt.Fprintf(f, "\n<details><summary>Full config (POST /api/encode)</summary>\n\n"+
		"```json\n%s\n```\n\n</details>\n", body)
}

// ladderRungSummary describes the rungs a job ACTUALLY encoded, read back from
// its stage keys ("encode:<codec>:<res>:chunk<N>"), as " (12 rungs, 234p–2160p)".
//
// The ladder NAME on its own is not a reproducible description of a run.
// Ladders are user-editable through POST /api/ladders, so the definition behind
// a name can change after the fact — and MaxRes/MinRes narrow it per job
// anyway. The observed rungs are ground truth, and they are the thing that
// makes two runs comparable: #202 was caught by a stage count of 252 where 336
// was expected, which is precisely this number.
//
// Returns "" when no rungs are derivable (a job that failed before any encode
// stage), so the caller degrades to the bare ladder name rather than printing
// a misleading "0 rungs".
func ladderRungSummary(job *Job) string {
	job.mu.Lock()
	stages := append([]StageProgress(nil), job.Stages...)
	history := append([]FileStages(nil), job.StagesHistory...)
	job.mu.Unlock()
	for _, h := range history {
		stages = append(stages, h.Stages...)
	}

	heights := map[int]bool{}
	for _, s := range stages {
		parts := strings.Split(s.Key, ":")
		if len(parts) < 3 || parts[0] != "encode" {
			continue
		}
		// parts[2] is the rung ("1080p"); tolerate anything that isn't.
		if h, err := strconv.Atoi(strings.TrimSuffix(parts[2], "p")); err == nil && h > 0 {
			heights[h] = true
		}
	}
	if len(heights) == 0 {
		return ""
	}
	lo, hi := 0, 0
	for h := range heights {
		if lo == 0 || h < lo {
			lo = h
		}
		if h > hi {
			hi = h
		}
	}
	if lo == hi {
		return fmt.Sprintf(" (1 rung, %dp)", lo)
	}
	return fmt.Sprintf(" (%d rungs, %dp–%dp)", len(heights), lo, hi)
}

// PhaseStat is one row of the phase rollup: a stage, or a whole rung's chunks
// collapsed into one line.
//
// Three different times, because they answer different questions and are
// routinely conflated:
//
//	SpanS     first start -> last end. Real elapsed, so parallel chunks count once.
//	JobWallS  sum of per-stage Batch wall clock. Includes container start, the
//	          mezzanine fetch and the upload.
//	WorkerS   the worker's OWN timer over the same phases. The gap against
//	          JobWallS is the non-encoding overhead.
//
// Cores is CPUS/WorkerS — what the phase actually used against what it reserved.
type PhaseStat struct {
	Phase    string  `json:"phase"`
	N        int     `json:"n"`
	SpanS    float64 `json:"span_s"`
	JobWallS float64 `json:"job_wall_s"`
	WorkerS  float64 `json:"worker_s,omitempty"`
	CPUS     float64 `json:"cpu_s,omitempty"`
	Cores    float64 `json:"cores,omitempty"`
	PeakMiB  float64 `json:"peak_mib,omitempty"`
}

// PhaseRollup aggregates the job's stages by phase, collapsing a rung's chunks
// into one row. Shared by the on-disk timing summary and the per-output stats
// sidecar so the two can never disagree.
func (j *Job) PhaseRollup() []PhaseStat {
	j.mu.Lock()
	stages := append([]StageProgress(nil), j.Stages...)
	history := append([]FileStages(nil), j.StagesHistory...)
	j.mu.Unlock()
	var all []StageProgress
	for _, h := range history {
		all = append(all, h.Stages...)
	}
	all = append(all, stages...)

	type agg struct {
		n              int
		first, last    time.Time
		jobWall        time.Duration
		cpu, work, mem float64
	}
	groups := map[string]*agg{}
	var order []string
	for _, st := range all {
		g := st.Key
		if i := strings.LastIndex(g, ":chunk"); i > 0 {
			g = g[:i]
		}
		a := groups[g]
		if a == nil {
			a = &agg{}
			groups[g] = a
			order = append(order, g)
		}
		a.n++
		a.cpu += st.CPUSeconds
		a.work += st.WorkerSeconds
		if st.PeakMemMiB > a.mem {
			a.mem = st.PeakMemMiB
		}
		if st.StartedAt != nil {
			if a.first.IsZero() || st.StartedAt.Before(a.first) {
				a.first = *st.StartedAt
			}
			if st.EndedAt != nil {
				a.jobWall += st.EndedAt.Sub(*st.StartedAt)
				if st.EndedAt.After(a.last) {
					a.last = *st.EndedAt
				}
			}
		}
	}
	out := make([]PhaseStat, 0, len(order))
	for _, g := range order {
		a := groups[g]
		row := PhaseStat{Phase: g, N: a.n, JobWallS: a.jobWall.Seconds(),
			WorkerS: a.work, CPUS: a.cpu, PeakMiB: a.mem}
		if !a.first.IsZero() && !a.last.IsZero() {
			row.SpanS = a.last.Sub(a.first).Seconds()
		}
		if a.work > 0 {
			row.Cores = a.cpu / a.work
		}
		out = append(out, row)
	}
	return out
}

func (m *Manager) writeTimingSummary(f *os.File, job *Job) {
	// Snapshot under the lock. This runs at job teardown, when mutation is
	// unlikely but not impossible — and the SSE marshaller can be walking the
	// same slices. Copying the headers is enough: the elements are only ever
	// replaced wholesale, never mutated in place after this point.
	job.mu.Lock()
	stages := append([]StageProgress(nil), job.Stages...)
	history := append([]FileStages(nil), job.StagesHistory...)
	job.mu.Unlock()
	if len(stages) == 0 && len(history) == 0 {
		return
	}

	// Walk the history (finished files) + the current Stages (last file)
	// as one sequence so the timing table covers the whole job, not
	// just the last file.
	type stageRow struct {
		fileLabel string // "" for the current (only) file
		stage     StageProgress
	}
	var rows []stageRow
	for _, h := range history {
		label := h.File
		if h.TotalFiles > 1 {
			label = fmt.Sprintf("[%d/%d] %s", h.FileIndex, h.TotalFiles, h.File)
		}
		for _, s := range h.Stages {
			rows = append(rows, stageRow{fileLabel: label, stage: s})
		}
	}
	currentLabel := ""
	if job.TotalFiles > 1 && job.CurrentFile != "" {
		currentLabel = fmt.Sprintf("[%d/%d] %s", job.CurrentFileIndex, job.TotalFiles, job.CurrentFile)
	}
	for _, s := range stages {
		rows = append(rows, stageRow{fileLabel: currentLabel, stage: s})
	}

	var table strings.Builder

	// PHASE ROLLUP first. The detail table below is one row per stage, which on
	// a 336-chunk run is 350 rows nobody reads. This aggregates by phase and is
	// the part worth looking at.
	//
	// Three different times are reported because they answer different
	// questions and are routinely confused:
	//
	//   span   first start -> last end for the phase. Real elapsed time, so
	//          parallel chunks count once. This is "how long did this phase
	//          take".
	//   Σ job  sum of per-stage Batch wall clock. Includes container start, the
	//          mezzanine fetch and the upload.
	//   Σ work the worker's OWN measure of the same phases, from its timer.
	//          The gap against Σ job is the non-encoding overhead.
	//   Σ cpu  getrusage user+sys, so cores = Σ cpu / Σ work is what the phase
	//          actually used against what it reserved.
	type agg struct {
		n              int
		first, last    time.Time
		jobWall        time.Duration
		cpu, work, mem float64
	}
	groups := map[string]*agg{}
	var order []string
	for _, r := range rows {
		st := r.stage
		g := st.Key
		// encode:h264:1080p:chunk3 -> encode:h264:1080p, so a rung is one row
		// rather than 28.
		if i := strings.LastIndex(g, ":chunk"); i > 0 {
			g = g[:i]
		}
		a := groups[g]
		if a == nil {
			a = &agg{}
			groups[g] = a
			order = append(order, g)
		}
		a.n++
		a.cpu += st.CPUSeconds
		a.work += st.WorkerSeconds
		if st.PeakMemMiB > a.mem {
			a.mem = st.PeakMemMiB
		}
		if st.StartedAt != nil {
			if a.first.IsZero() || st.StartedAt.Before(a.first) {
				a.first = *st.StartedAt
			}
			if st.EndedAt != nil {
				a.jobWall += st.EndedAt.Sub(*st.StartedAt)
				if st.EndedAt.After(a.last) {
					a.last = *st.EndedAt
				}
			}
		}
	}
	table.WriteString("\n### Phase rollup\n\n")
	table.WriteString("| Phase | n | span | Σ job wall | Σ worker | Σ cpu | cores | peak MiB |\n")
	table.WriteString("|-------|---|------|------------|----------|-------|-------|----------|\n")
	var totCPU, totWork float64
	for _, g := range order {
		a := groups[g]
		span := "—"
		if !a.first.IsZero() && !a.last.IsZero() {
			span = a.last.Sub(a.first).Round(100 * time.Millisecond).String()
		}
		cores := "—"
		if a.work > 0 {
			cores = fmt.Sprintf("%.2f", a.cpu/a.work)
		}
		cpuStr, workStr, memStr := "—", "—", "—"
		if a.cpu > 0 {
			cpuStr = fmt.Sprintf("%.0fs", a.cpu)
		}
		if a.work > 0 {
			workStr = fmt.Sprintf("%.0fs", a.work)
		}
		if a.mem > 0 {
			memStr = fmt.Sprintf("%.0f", a.mem)
		}
		table.WriteString(fmt.Sprintf("| %s | %d | %s | %s | %s | %s | %s | %s |\n",
			g, a.n, span, a.jobWall.Round(100*time.Millisecond),
			workStr, cpuStr, cores, memStr))
		totCPU += a.cpu
		totWork += a.work
	}
	if totWork > 0 {
		table.WriteString(fmt.Sprintf(
			"| **total** |  |  |  | %.0fs | %.0fs | %.2f |  |\n",
			totWork, totCPU, totCPU/totWork))
	}

	// Also dump the same summary into the per-job log file so it's
	// trivially greppable later.
	table.WriteString("\n### Stage timing\n\n")
	if job.TotalFiles > 1 {
		table.WriteString("| File | Stage | Status | Started | Ended | Duration |\n")
		table.WriteString("|------|-------|--------|---------|-------|----------|\n")
	} else {
		table.WriteString("| Stage | Status | Started | Ended | Duration |\n")
		table.WriteString("|-------|--------|---------|-------|----------|\n")
	}

	var totalMeasured time.Duration
	var lastFileLabel string
	for _, r := range rows {
		s := r.stage
		startStr, endStr, durStr := "—", "—", "—"
		if s.StartedAt != nil {
			startStr = s.StartedAt.Format("15:04:05")
			if s.EndedAt != nil {
				endStr = s.EndedAt.Format("15:04:05")
				dur := s.EndedAt.Sub(*s.StartedAt).Round(100 * time.Millisecond)
				durStr = dur.String()
				totalMeasured += dur
			} else {
				// Still running at job end (unusual — log it anyway).
				endStr = "(still running)"
			}
		}
		if job.TotalFiles > 1 {
			// Only print the file label when it changes — keeps the
			// table less noisy.
			displayed := r.fileLabel
			if displayed == lastFileLabel {
				displayed = ""
			} else {
				lastFileLabel = r.fileLabel
			}
			table.WriteString(fmt.Sprintf(
				"| %s | %s | %s | %s | %s | %s |\n",
				displayed, s.Label, s.Status, startStr, endStr, durStr,
			))
		} else {
			table.WriteString(fmt.Sprintf(
				"| %s | %s | %s | %s | %s |\n",
				s.Label, s.Status, startStr, endStr, durStr,
			))
		}
	}

	// Totals row — includes both the sum of measured stage durations
	// (which may double-count parallel stages if any — unlikely but
	// noted) and the whole-job wall clock for reference.
	if job.EndedAt != nil {
		wall := job.EndedAt.Sub(job.StartedAt).Round(time.Second)
		if job.TotalFiles > 1 {
			table.WriteString(fmt.Sprintf(
				"|  | **total measured** |  |  |  | %s |\n",
				totalMeasured.Round(100*time.Millisecond),
			))
			table.WriteString(fmt.Sprintf(
				"|  | **wall clock** |  |  |  | %s |\n", wall,
			))
		} else {
			table.WriteString(fmt.Sprintf(
				"| **total measured** |  |  |  | %s |\n",
				totalMeasured.Round(100*time.Millisecond),
			))
			table.WriteString(fmt.Sprintf(
				"| **wall clock** |  |  |  | %s |\n", wall,
			))
		}
	}

	f.WriteString(table.String())

	// Echo into the per-job log file for quick greppability.
	logPath := filepath.Join(m.TmpDir, "logs", job.ID+".log")
	if lf, err := os.OpenFile(logPath, os.O_APPEND|os.O_WRONLY, 0644); err == nil {
		lf.WriteString("\n=== Stage timing ===\n")
		var lastFL string
		for _, r := range rows {
			if r.fileLabel != "" && r.fileLabel != lastFL {
				fmt.Fprintf(lf, "  --- %s ---\n", r.fileLabel)
				lastFL = r.fileLabel
			}
			s := r.stage
			dur := "—"
			if s.StartedAt != nil && s.EndedAt != nil {
				dur = s.EndedAt.Sub(*s.StartedAt).Round(100 * time.Millisecond).String()
			}
			fmt.Fprintf(lf, "  %-30s %-8s %s\n", s.Label, s.Status, dur)
		}
		lf.Close()
	}
}

func defaultVal(v, d string) string {
	if v == "" {
		return d
	}
	return v
}

// datedBackupRe matches an output dir a re-encode preserved: <name>_<YYYYMMDD>
// or <name>_<YYYYMMDD>_<HHMMSS>. Real outputs always end in _<codec>, so this
// never matches a live output.
var datedBackupRe = regexp.MustCompile(`_\d{8}(_\d{6})?$`)

// IsDatedBackup reports whether an output dir name is a datestamped backup left
// behind when a re-encode replaced a prior output. The Outputs list and the
// watcher skip these so a preserved old copy isn't treated as a live output.
func IsDatedBackup(name string) bool {
	return datedBackupRe.MatchString(name)
}

// moveTmpToOutput moves each top-level dir from tmpDir into OUTPUT_DIR and
// returns the names moved (so the caller can promote them). See the collision
// note on the in-place dated-backup behaviour.
func (m *Manager) moveTmpToOutput(tmpDir string) ([]string, error) {
	entries, err := os.ReadDir(tmpDir)
	if err != nil {
		return nil, err
	}
	var moved []string
	for _, e := range entries {
		src := filepath.Join(tmpDir, e.Name())
		dst := filepath.Join(m.OutputDir, e.Name())

		if fi, statErr := os.Stat(dst); statErr == nil {
			// Preserve the existing copy: rename it in place with the date it was
			// last modified appended (<name>_<YYYYMMDD>), so a re-encode never
			// destroys the prior output. A same-day re-encode disambiguates with a
			// time suffix. These dated backups are skipped by the Outputs list and
			// the watcher (IsDatedBackup).
			ts := fi.ModTime().Format("20060102")
			backup := filepath.Join(m.OutputDir, e.Name()+"_"+ts)
			if _, err := os.Stat(backup); err == nil {
				backup = filepath.Join(m.OutputDir, e.Name()+"_"+ts+"_"+fi.ModTime().Format("150405"))
			}
			os.Rename(dst, backup)
		}

		if err := os.Rename(src, dst); err != nil {
			if cpErr := copyDir(src, dst); cpErr != nil {
				return moved, fmt.Errorf("move %s: %w", e.Name(), cpErr)
			}
			os.RemoveAll(src)
		}
		moved = append(moved, e.Name())
	}
	return moved, nil
}

func copyDir(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, info.Mode())
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, info.Mode())
	})
}

func (cfg *JobConfig) OutputStem(filename string) string {
	stem := strings.TrimSuffix(filename, filepath.Ext(filename))
	suffix := "_p200"
	if cfg.PartialDuration != "" && cfg.PartialDuration != "0.2" {
		suffix = "_p" + strings.ReplaceAll(cfg.PartialDuration, ".", "")
	}
	stem += suffix
	switch cfg.Padding {
	case "black":
		stem += "_padblack"
	case "pink":
		stem += "_padpink"
	}
	// NOTE: the profile output tag (e.g. "xs") is NOT part of the stem — the
	// encode script appends it AFTER the codec ("<stem>_p200_<codec>_xs"), so the
	// "_p200_<codec>" shape smashing/go-live keys off stays intact (its clip-ID
	// regex is _p200_(codec)(_|$), which needs the codec right after _p200).
	return stem
}

// outputCodecDir is the final output directory name for one codec: the stem +
// "_<codec>" + the profile suffix appended LAST ("<stem>_p200_h264_xs"). The
// encode scripts build the same name; Go uses it for skip/matching.
func (cfg *JobConfig) outputCodecDir(filename, codec string) string {
	name := cfg.OutputStem(filename) + "_" + codec
	if cfg.OutputTag != "" {
		name += "_" + cfg.OutputTag
	}
	return name
}

// BurninEnabled reports whether the text overlay should be burnt in. Default is
// on: a nil Burnin (older jobs / omitted by the client) counts as enabled, and
// only an explicit false disables it.
func (cfg *JobConfig) BurninEnabled() bool {
	return cfg.Burnin == nil || *cfg.Burnin
}

// localChunkSeconds resolves the fixed chunk size for a LOCAL encode, driving
// cli_local's --local-chunk-duration. Unlike the cloud (per-variant, complexity
// aware), cli_local applies ONE chunk duration to every variant, so the cloud's
// "dynamic"/"per-variant" modes collapse here to a fixed default of 2×segment
// (12s by default): small enough that any real-length clip yields plenty of
// chunks to fill the cores — a lone x265 encode only ~half-fills a box, so
// chunking is what lets a single HEVC variant saturate the machine — yet large
// enough to amortize 2-pass startup. "whole" disables chunking (0). A numeric
// value is used verbatim. The result must stay a whole multiple of the segment
// duration (plan_chunks enforces this; 2×segment always is).
func (cfg *JobConfig) localChunkSeconds() float64 {
	seg := 6.0
	if cfg.SegmentDuration != "" {
		if v, err := strconv.ParseFloat(cfg.SegmentDuration, 64); err == nil && v > 0 {
			seg = v
		}
	}
	switch cfg.ChunkDuration {
	case "whole":
		return 0
	case "dynamic", "":
		return 2 * seg
	default:
		if v, err := strconv.ParseFloat(cfg.ChunkDuration, 64); err == nil && v > 0 {
			return v
		}
		return 2 * seg
	}
}

// vmafEstimateArgs returns --vmaf-estimate args for cli_local_dist — one per
// rung that has a design-time VMAF estimate in the quality curves, formatted
// "<codec>/<label>:<vmaf>:<clamped01>". The worker keys off "<codec>/<label>"
// and burns the value into the overlay; clamped=1 (rung above the measured
// range → nearest endpoint, not an interpolation) makes it render as VMAF≥N.
// Empty when there are no curves. clip="" → the store's current curve, so an
// estimate is available for every job (seeded), matching the UI's Ladders tab.
// vmafEstimates returns the design-time VMAF estimate per "<codec>/<label>",
// as (value, clamped). One source for both targets: vmafEstimateArgs formats
// these into cli_local_dist flags, and buildSFNInput copies them onto each
// sfnVariant for the cloud path — so the two cannot report different numbers
// for the same rung. Empty when there are no curves or no ladder.
func (m *Manager) vmafEstimates(cfg JobConfig) map[string][2]string {
	if m.Curves == nil || m.Ladders == nil {
		return nil
	}
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	out := map[string][2]string{}
	for _, codec := range parseCodecSel(cfg.Codec) {
		for _, e := range m.LadderEstimates(ladderName, codec, 0, "") {
			if e.Vmaf <= 0 {
				continue
			}
			clamped := "0"
			if e.Clamped {
				clamped = "1"
			}
			out[codec+"/"+e.Label] = [2]string{strconv.FormatFloat(e.Vmaf, 'f', 1, 64), clamped}
		}
	}
	return out
}

func (m *Manager) vmafEstimateArgs(cfg JobConfig) []string {
	if m.Curves == nil || m.Ladders == nil {
		return nil
	}
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	var out []string
	for _, codec := range parseCodecSel(cfg.Codec) {
		for _, e := range m.LadderEstimates(ladderName, codec, 0, "") {
			if e.Vmaf <= 0 {
				continue
			}
			clamped := 0
			if e.Clamped {
				clamped = 1
			}
			out = append(out, "--vmaf-estimate",
				fmt.Sprintf("%s/%s:%.1f:%d", codec, e.Label, e.Vmaf, clamped))
		}
	}
	return out
}

// distArgsForFile builds the CLI for cli_local_dist.py (the TargetLocalDist
// worker): plan + start the durable Temporal EncodeWorkflow, which fans the
// file's chunks across the local worker pool and downloads output_<codec>/ to
// <outputDir>/<stem>_<codec> — same layout as cli_local, so moveTmpToOutput
// picks it up unchanged. The S3 job-prefix embeds the unique job ID, so a
// re-encode always writes a fresh MinIO prefix (no cross-job reuse); force is
// implicit. MinIO endpoint/creds ride in as env (buildRunArgs).
func (cfg *JobConfig) distArgsForFile(sourceDir, outputDir, filename, jobID string, jobRank int) []string {
	stem := cfg.OutputStem(filename)
	args := []string{
		"--input", sourceDir + "/" + filename,
		"--output", stem,
		"--output-dir", outputDir,
		"--backend", "temporal",
		"--temporal-address", envOr("TEMPORAL_ADDRESS", "host.docker.internal:7233"),
		"--s3-bucket", envOr("DIST_S3_BUCKET", "encoder-local"),
		"--job-prefix", DistJobPrefix(jobID, filename),
	}
	if cfg.Codec != "" {
		args = append(args, "--codec", cfg.Codec)
	}
	if cfg.Ladder != "" {
		args = append(args, "--ladder", cfg.Ladder)
	}
	if cfg.MaxRes != "" {
		args = append(args, "--max-res", cfg.MaxRes)
	}
	if cfg.MinRes != "" {
		args = append(args, "--min-res", cfg.MinRes)
	}
	// Always pass it — including 0, which means "whole variant" (the UI's
	// "whole" option). Omitting the flag would let cli_local_dist's argparse
	// default (12s) silently re-chunk a "whole" request; cli_local_dist treats
	// <=0 as one chunk spanning the clip.
	args = append(args, "--chunk-duration",
		strconv.FormatFloat(cfg.localChunkSeconds(), 'f', -1, 64))
	// Profile timing overrides. Sent ONLY when the job set one — absent means
	// "use the ladder's", which cli_local_dist resolves. Cloud gets the same
	// three through the SFN container overrides; local had no route for them at
	// all until #172, so a ladder's gop/partial were silently replaced by
	// cli_phase's defaults.
	if cfg.SegmentDuration != "" {
		args = append(args, "--segment-duration", cfg.SegmentDuration)
	}
	if cfg.GopDuration != "" {
		args = append(args, "--gop-duration", cfg.GopDuration)
	}
	if cfg.PartialDuration != "" {
		args = append(args, "--partial-duration", cfg.PartialDuration)
	}
	if cfg.OutputTag != "" {
		args = append(args, "--output-tag", cfg.OutputTag)
	}
	if cfg.HevcSinglePass {
		args = append(args, "--hevc-single-pass")
	}
	if cfg.MeasureVmaf {
		args = append(args, "--measure-vmaf")
	}
	if cfg.VmafPrescale {
		args = append(args, "--vmaf-prescale")
	}
	if !cfg.BurninEnabled() {
		args = append(args, "--no-burnin")
	}
	// Cross-job priority: an older local-dist job's chunks all outrank a younger
	// job's on the shared Temporal fleet (see localJobRank). Always passed.
	args = append(args, "--job-rank", strconv.Itoa(jobRank))
	return args
}

// DistJobPrefix is the MinIO key prefix one local-dist file stages under.
// Shared by the orchestrator's --job-prefix argument and the staging GC's
// keep-list (ActiveDistPrefixes), so "which prefix belongs to this job" is
// defined once — the GC deleting a prefix an in-flight job is still writing
// would destroy a running encode.
func DistJobPrefix(jobID, filename string) string {
	base := strings.TrimSuffix(filename, filepath.Ext(filename))
	return fmt.Sprintf("jobs/%s-%s", jobID, base)
}

// ActiveDistPrefixes lists the staging prefixes of every queued/running
// local-dist job — the GC's keep-list. Covers all of a job's files, not just
// the current one: a multi-file job's later prefixes don't exist in MinIO yet
// (nothing to skip) and its earlier ones are already reclaimed by the
// orchestrator, so naming them all is harmless and keeps this independent of
// where the job has got to.
func (m *Manager) ActiveDistPrefixes() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []string
	for _, j := range m.jobs {
		if j.Status != StatusQueued && j.Status != StatusRunning {
			continue
		}
		if j.Config.Target != TargetLocalDist {
			continue
		}
		for _, f := range j.Config.Files {
			out = append(out, DistJobPrefix(j.ID, f))
		}
	}
	return out
}

// effectiveTiming resolves one profile timing value with precedence
// job > ladder > global default. "" means "unset" at both the job and ladder
// level; "0" is an explicit value (e.g. partial="0" turns LL-HLS parts off).
func effectiveTiming(jobVal, ladderVal, globalDefault string) string {
	if jobVal != "" {
		return jobVal
	}
	if ladderVal != "" {
		return ladderVal
	}
	return globalDefault
}

// resolveTimings fills a config's segment/partial/GOP from the selected ladder
// (then the global default) wherever the job didn't set them, so the ladder
// defines the profile's output timing. Mutates the passed (copied) cfg.
func (m *Manager) resolveTimings(cfg *JobConfig) {
	var def LadderDef
	if m.Ladders != nil {
		def, _ = m.Ladders.Get(cfg.Ladder)
	}
	cfg.SegmentDuration = effectiveTiming(cfg.SegmentDuration, def.SegmentDuration, "6")
	cfg.PartialDuration = effectiveTiming(cfg.PartialDuration, def.PartialDuration, "0.2")
	cfg.GopDuration = effectiveTiming(cfg.GopDuration, def.GopDuration, "1.0")
	// Output suffix: explicit (job or ladder output_tag) wins; otherwise only the
	// FLEXIBLE base (no pinned segment) is tagged "xs" — that's the master go-live
	// repackages into 1s/2s/6s. A fixed-segment profile is served as-is, so it
	// gets NO suffix (go-live doesn't repackage it or need its segment length).
	tag := cfg.OutputTag
	if tag == "" {
		tag = def.OutputTag
	}
	cfg.OutputTag = deriveOutputTag(tag, def.SegmentDuration)
}

// deriveOutputTag returns the explicit tag if set; else "xs" for the flexible
// base (no pinned segment — the repackage-into-1/2/6s master go-live treats
// specially), or "" for a fixed-segment profile (served as-is, no marker needed).
func deriveOutputTag(explicit, ladderSegment string) string {
	if explicit != "" {
		return explicit
	}
	if ladderSegment == "" {
		return "xs"
	}
	return ""
}

func (m *Manager) encodeFilesFrom(job *Job, tmpDir, script string, startIdx int) error {
	// Project the cost comparison up front from the first source file's probe +
	// the learned-speed model: AWS Batch (Graviton) compute cost AND predicted
	// local-fleet wall-clock time. Target-independent (both "what would this cost
	// on our cloud fleet?" and "how long on my own hardware?" apply to any run)
	// and idempotent on reattach; refreshed live below as speeds are learned.
	m.projectAndSetCosts(job)

	// Resolve profile timing + output tag from the selected ladder into the job's
	// effective config ONCE (job value wins, else ladder, else global default), so
	// both the encode args AND the skip/output naming (resolveCodec, OutputStem)
	// see the same values. Idempotent on reattach.
	m.resolveTimings(&job.Config)

	// cloud-batch fans the whole job onto AWS Batch via Step Functions; local-dist
	// runs one worker container per file below.
	if job.Config.Target == TargetCloudBatch {
		return m.encodeCloudBatchSFN(job, tmpDir, startIdx)
	}

	total := len(job.Config.Files)
	for i := startIdx; i < total; i++ {
		m.persistState(job, i)

		f := job.Config.Files[i]
		job.Progress = fmt.Sprintf("encoding %d/%d: %s", i+1, total, f)

		codec := job.Config.Codec
		if !job.Config.ForceReencode {
			codec = m.resolveCodec(job.Config, f)
			if codec == "" {
				job.AppendLog(fmt.Sprintf("skipping %s — all codecs already encoded", f))
				continue
			}
		}

		// Archive finished stages + reset bars so the UI shows a clean
		// slate for the next file. The worker's first ENCODER-PLAN
		// rebuilds the rows.
		job.startFile(f, i+1, total)
		m.notify(job)

		fileCfg := job.Config // already timing-resolved above
		fileCfg.Codec = codec
		if job.Config.Target != TargetLocalDist {
			// cloud-batch is handled earlier; anything else is an invalid or
			// retired target that should have been rejected at submit.
			return fmt.Errorf("unsupported target %q (use local or cloud)", job.Config.Target)
		}
		args := fileCfg.distArgsForFile(m.SourceDir, tmpDir, f, job.ID, m.localJobRank(job))
		// Per-rung design-time VMAF estimates for the burn-in overlay (only worth
		// passing when the overlay is on). cli_local_dist attaches them to the plan
		// rungs; the worker burns "VMAF~N"/"VMAF≥N" into each variant.
		if fileCfg.BurninEnabled() {
			args = append(args, m.vmafEstimateArgs(fileCfg)...)
		}
		if err := m.runFileContainer(job, i, script, args); err != nil {
			return fmt.Errorf("%s: %w", f, err)
		}
	}
	return nil
}

// encodeCloudBatchSFN drives the AWS Batch + Step Functions pipeline
// defined under infra/terraform/. For each input file:
//  1. Upload to s3://bucket/jobs/<go-job-id>-<idx>/input/<clip>
//  2. Build state-machine input JSON (s3_input, s3_prefix, variants[])
//  3. `cli_batch.py submit` -> prints execution ARN
//  4. `cli_batch.py poll` -> blocks, emits ENCODER-STAGE markers
//     until SUCCEEDED/FAILED, then downloads output_*/ trees into
//     the job's tmpDir for moveTmpToOutput to pick up
//
// Spot interrupts are handled inside the state machine's retry
// policies — Batch reruns the failing phase on fresh capacity. The
// Go side only cares about the terminal SUCCEEDED / FAILED outcome.
func (m *Manager) encodeCloudBatchSFN(job *Job, tmpDir string, startIdx int) error {
	if m.StateMachineArn == "" {
		return fmt.Errorf("STATE_MACHINE_ARN not configured; can't submit cloud-batch jobs")
	}
	bucket := os.Getenv("S3_BUCKET")
	if bucket == "" {
		return fmt.Errorf("S3_BUCKET not configured")
	}

	total := len(job.Config.Files)
	for i := startIdx; i < total; i++ {
		m.persistState(job, i)

		f := job.Config.Files[i]
		job.Progress = fmt.Sprintf("cloud-batch %d/%d: %s", i+1, total, f)
		job.startFile(f, i+1, total)
		m.notify(job)

		if err := m.runOneCloudBatchSFN(job, tmpDir, f, bucket, i); err != nil {
			return fmt.Errorf("%s: %w", f, err)
		}
		// This file's execution finished — clear the ARN so the next file
		// starts a fresh execution (and a restart between files won't reattach
		// to this now-complete one).
		job.setCloudExecArn("")
	}
	return nil
}

// cloudExecutionResumable reports whether a persisted SFN execution ARN is
// still worth re-attaching to: RUNNING (resume polling it) or SUCCEEDED (poll
// just downloads the finished outputs). Terminal-failure / aborted / not-found
// → false, so runOneCloudBatchSFN resubmits a fresh execution instead.
func (m *Manager) cloudExecutionResumable(arn string) bool {
	out, err := exec.Command("python3", "-m", "infinite_streaming_encoder.cloud.batch_admin",
		"execution-status", "--arn", arn).Output()
	if err != nil {
		return false
	}
	var r struct {
		Status string `json:"status"`
	}
	if json.Unmarshal(out, &r) != nil {
		return false
	}
	return r.Status == "RUNNING" || r.Status == "SUCCEEDED"
}

// runOneCloudBatchSFN submits + polls a single clip's state-machine
// execution. Shells out to cli_batch.py for both phases (rather than
// importing an AWS SDK for Go) to keep AWS usage in one language.
func (m *Manager) runOneCloudBatchSFN(job *Job, tmpDir, filename, bucket string, fileIdx int) error {
	// Unique S3 prefix per (manager-job, file-index) so retries of
	// the same Go job can reuse the prefix (input doesn't re-upload).
	prefix := fmt.Sprintf("jobs/%s-%s", job.ID, strings.TrimSuffix(filename, filepath.Ext(filename)))
	s3Prefix := fmt.Sprintf("s3://%s/%s", bucket, prefix)
	s3Input := fmt.Sprintf("%s/input/%s", s3Prefix, filename)

	// Re-attach to a still-running execution left by a pre-restart run instead
	// of re-uploading + re-submitting (which would orphan the old execution and
	// duplicate the work). Only if the persisted ARN is still RUNNING/SUCCEEDED.
	execArn := job.CloudExecArn()
	// Age-ordered launch: this job waits (below, before submit) until every older
	// cloud job has fully fanned out, then marks itself launch-complete once it has
	// submitted all its own encode jobs — releasing the next-oldest job. Counting
	// distinct encode stage keys vs the expected total detects "fully fanned out";
	// markLaunched is idempotent and always runs on return (so a job that fails
	// before fan-out still unblocks the queue).
	expectedEncodes := 0
	seenEncodes := map[string]bool{}
	var launchStart time.Time
	launchDone := false
	markLaunched := func() {
		if !launchDone {
			launchDone = true
			job.markLaunchComplete()
			m.signalLaunch()
		}
	}
	defer markLaunched()
	if execArn != "" && m.cloudExecutionResumable(execArn) {
		job.AppendLog("[cloud-batch] reattaching to execution " + execArn)
		m.notify(job)
	} else {

		// Source-keyed mezzanine: if a prior job of the SAME source already
		// produced the mezzanine (mezz/<key>/), skip BOTH the raw upload and the
		// mezzanine job — variants read it straight from there (one copy, no
		// duplicate storage). This applies even under ForceReencode: the mezzanine
		// is a stream-copy of the source, a pure function of the source file
		// (the cache key is sha1(name|size|mtime)), independent of codec/ladder —
		// so reusing it is always correct. ForceReencode redoes the variant
		// OUTPUTS, not the source mezzanine. To force the mezzanine to regenerate,
		// clear the mezz cache (source changes bump the key automatically).
		localSrc := filepath.Join(m.SourceDir, filename)
		s3Mezz := s3Prefix // fallback: mezzanine stays in the job prefix
		cacheHit := false
		if key, ok := sourceMezzKey(localSrc); ok {
			s3Mezz = fmt.Sprintf("s3://%s/mezz/%s", bucket, key)
			if s3ObjectExists(s3Mezz+"/mezzanine.mp4") &&
				s3ObjectExists(s3Mezz+"/mezzanine.mp4.done") {
				cacheHit = true
			}
		}
		if cacheHit {
			job.AppendLog(fmt.Sprintf("[cloud-batch] %s: mezzanine cache hit — reusing %s, skipping upload + mezzanine", filename, s3Mezz))
			job.upsertStage("upload:inputs", "upload inputs", "skipped", 100)
			m.notify(job)
		}
		if !cacheHit {
			// Upload the clip. Stream the aws-cli progress into an `upload:inputs`
			// stage so the UI shows a live bar for the local->S3 copy — the mirror
			// of the download:outputs bar on the sync-back.
			job.AppendLog(fmt.Sprintf("[cloud-batch] uploading %s → %s", filename, s3Input))
			job.upsertStage("upload:inputs", "upload inputs", "running", 0)
			m.notify(job)
			upload := exec.Command("aws", "s3", "cp", localSrc, s3Input) // no --only-show-errors: we want the progress lines
			upload.Env = os.Environ()
			// aws s3 cp writes its "Completed X/Y" progress to STDOUT (errors go
			// to stderr). Scan stdout for the progress bar; buffer stderr for the
			// failure message.
			stdoutPipe, pipeErr := upload.StdoutPipe()
			if pipeErr != nil {
				return fmt.Errorf("aws s3 cp stdout pipe: %w", pipeErr)
			}
			var stderrBuf bytes.Buffer
			upload.Stderr = &stderrBuf
			if err := upload.Start(); err != nil {
				job.upsertStage("upload:inputs", "upload inputs", "failed", 0)
				m.notify(job)
				return fmt.Errorf("aws s3 cp start: %w", err)
			}
			// Cancel watcher: this upload is a bare subprocess with no worker
			// container for Manager.Cancel to stop, so it watches the cancel flag
			// and kills the copy. A multi-GB UHD source can upload for minutes;
			// without this, Cancel is silently ignored until the copy finishes.
			upStop := make(chan struct{})
			go func() {
				t := time.NewTicker(time.Second)
				defer t.Stop()
				for {
					select {
					case <-upStop:
						return
					case <-t.C:
						if job.IsCancelled() && upload.Process != nil {
							upload.Process.Kill()
							return
						}
					}
				}
			}()
			// aws s3 cp updates its progress in place with '\r'; split on both \r and \n
			// so intermediate updates are seen, not just the final \n-terminated line.
			upScanner := bufio.NewScanner(stdoutPipe)
			upScanner.Buffer(make([]byte, 64*1024), 1<<20)
			upScanner.Split(func(data []byte, atEOF bool) (int, []byte, error) {
				if atEOF && len(data) == 0 {
					return 0, nil, nil
				}
				if i := bytes.IndexAny(data, "\r\n"); i >= 0 {
					return i + 1, data[:i], nil
				}
				if atEOF {
					return len(data), data, nil
				}
				return 0, nil, nil
			})
			for upScanner.Scan() {
				line := strings.TrimSpace(upScanner.Text())
				if pct, ok := parseAwsCpProgress(line); ok {
					job.upsertStage("upload:inputs", "upload inputs", "running", pct)
					m.notify(job)
				}
			}
			close(upStop) // scan drained → stop the watcher before Wait
			if err := upload.Wait(); err != nil {
				// A cancel that killed the copy isn't a real failure — return
				// clean so run() finalizes the job as cancelled (its deferred
				// markLaunched still unblocks the queue).
				if job.IsCancelled() {
					job.upsertStage("upload:inputs", "upload inputs", "cancelled", 0)
					m.notify(job)
					return nil
				}
				job.upsertStage("upload:inputs", "upload inputs", "failed", 0)
				m.notify(job)
				return fmt.Errorf("aws s3 cp: %w: %s", err, strings.TrimSpace(stderrBuf.String()))
			}
			job.upsertStage("upload:inputs", "upload inputs", "done", 100)
			m.notify(job)
		} // end if !cacheHit (upload)

		// Build the state-machine input document.
		// Probe the source duration so each variant can size its own chunks
		// (buildSFNInput does the per-variant planning). ffprobe runs in the
		// worker image; if unavailable, 0 duration collapses to whole variants.
		durationS := 0.0
		if d, err := probeDurationSeconds(localSrc); err != nil {
			job.AppendLog(fmt.Sprintf("[cloud-batch] ffprobe failed (%v); encoding %s as whole variants", err, filename))
		} else {
			durationS = d
			job.AppendLog(fmt.Sprintf("[cloud-batch] %s: %.1fs — chunking mode %q (per-variant by complexity)", filename, durationS, chunkModeLabel(job.Config.ChunkDuration)))
		}

		// Probe the source width so the ladder is capped at native resolution
		// (never upscale). 0 => unknown, don't cap.
		srcWidth := probeSourceWidth(localSrc)
		if srcWidth > 0 {
			job.AppendLog(fmt.Sprintf("[cloud-batch] %s: source width %dpx — ladder capped to native (no upscaling)", filename, srcWidth))
		}
		// Source fps: keys the speed model (encode time ∝ frame count), so chunk
		// sizes/priorities match the graviton keys learned at the same rate.
		srcFps := probeSourceFps(localSrc)
		inputJSON, expEnc := buildSFNInput(m.Ladders, m.Speeds, s3Input, s3Prefix, s3Mezz, job.Config.Ladder, job.Config.Codec, job.Config.MaxRes, job.Config.MinRes, job.Config.HevcSinglePass, cacheHit, job.Config.BurninEnabled(), srcWidth, srcFps, durationS, job.Config.ChunkDuration, job.Config.SegmentDuration, job.Config.PartialDuration, job.Config.GopDuration, m.jobPriorityBase(job), m.vmafEstimates(job.Config), job.AppendLog)
		expectedEncodes = expEnc
		inputPath := filepath.Join(tmpDir, fmt.Sprintf("sfn-input-%s.json", filename))
		if err := os.WriteFile(inputPath, []byte(inputJSON), 0644); err != nil {
			return fmt.Errorf("write input json: %w", err)
		}

		// Wait our turn: block until every OLDER queued/running cloud job has fully
		// fanned out, so an earlier job floods the fleet with its higher-priority
		// jobs before this execution starts (Batch never preempts, so a later job
		// that fanned out first would keep the vCPUs it grabbed).
		job.AppendLog("[cloud-batch] waiting for launch turn (older jobs fan out first)…")
		m.waitForLaunchTurn(job)
		launchStart = time.Now()
		// A cancel can fire while we were waiting our turn (minutes, if an earlier
		// job is still uploading/mezzanine). Bail before submitting so we don't spin
		// up an execution + Batch jobs for a cancelled job — run() finalizes it as
		// cancelled; the deferred markLaunched unblocks the queue.
		if job.IsCancelled() {
			return nil
		}

		// Submit.
		submit := exec.Command("python3", "-m", "infinite_streaming_encoder.cli_batch", "submit",
			"--state-machine-arn", m.StateMachineArn,
			"--input-json", inputPath)
		submit.Env = os.Environ()
		out, err := submit.Output()
		if err != nil {
			return fmt.Errorf("cli_batch submit: %w", err)
		}
		execArn = strings.TrimSpace(string(out))
		job.AppendLog(fmt.Sprintf("[cloud-batch] execution ARN: %s", execArn))
		// Persist the ARN so a restart re-attaches to THIS execution.
		job.setCloudExecArn(execArn)
		m.persistState(job, fileIdx)
	}

	// Poll + stream progress back into the job log.
	// Download into the job tmp with the stem passed separately: the downloader
	// lays each codec down as its own top-level <stem>_<codec>/ dir (not a shared
	// <stem>/output_<codec>/ wrapper), so moveTmpToOutput moves each codec
	// independently and codecs of the same clip COEXIST in OUTPUT_DIR — matching
	// the local pipeline + the OutputStem/resolveCodec/watcher naming contract.
	pollArgs := []string{"poll",
		"--execution-arn", execArn,
		"--s3-prefix", s3Prefix,
		"--local-dir", tmpDir,
		"--output-stem", job.Config.OutputStem(filename)}
	if job.Config.OutputTag != "" {
		pollArgs = append(pollArgs, "--output-tag", job.Config.OutputTag)
	}
	if m.skipMediaDownload(job.Config) {
		pollArgs = append(pollArgs, "--no-media")
	}
	poll := exec.Command("python3", append([]string{"-m", "infinite_streaming_encoder.cli_batch"}, pollArgs...)...)
	poll.Env = os.Environ()
	stdout, err := poll.StdoutPipe()
	if err != nil {
		return err
	}
	poll.Stderr = os.Stderr
	if err := poll.Start(); err != nil {
		return err
	}

	// Cancel watcher. A cloud-batch job's only live local process is this
	// poll subprocess — there's no worker container — so Manager.Cancel
	// can't stop it directly; it only sets the cancel flag. Watch that flag
	// here: on cancel, abort the Step Functions execution (which cascades
	// to its in-flight + queued Batch jobs) and kill the poll so the scan
	// loop below unblocks and run() finalizes the job as cancelled.
	stopWatch := make(chan struct{})
	defer close(stopWatch)
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-stopWatch:
				return
			case <-ticker.C:
				if job.IsCancelled() {
					job.AppendLog("[cloud-batch] cancel requested — aborting execution + terminating its Batch jobs")
					m.notify(job)
					// --terminate-jobs: stopping the execution alone leaves its
					// in-flight submitJob.sync jobs running (orphaned) until they
					// finish; terminate them so in-progress chunks stop now and
					// the fleet frees up promptly.
					stop := exec.Command("python3", "-m", "infinite_streaming_encoder.cloud.batch_admin",
						"stop-execution", "--arn", execArn, "--terminate-jobs")
					stop.Env = os.Environ()
					if o, e := stop.CombinedOutput(); e != nil {
						job.AppendLog(fmt.Sprintf("[cloud-batch] stop-execution failed: %v: %s",
							e, strings.TrimSpace(string(o))))
					}
					if poll.Process != nil {
						poll.Process.Kill()
					}
					return
				}
			}
		}
	}()

	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := stripANSI(scanner.Text())
		// Track encode-job submission for the launch gate: release only once ALL
		// expected encode jobs have been submitted (fully fanned out), so the next
		// job's execution can't start while this one still has higher-priority
		// jobs to submit. Batch never preempts, so a partial release would let the
		// later job grab vCPUs this job's remaining jobs should have had.
		if !launchDone {
			if m := stageMarkerRe.FindStringSubmatch(line); m != nil && strings.HasPrefix(m[1], "encode:") {
				job.markFanOut() // encode start (first job); idempotent
				seenEncodes[m[1]] = true
			}
			// Mark launch-complete once ALL expected encode jobs are submitted —
			// this releases the next-oldest job. Checked on every line so:
			// expectedEncodes==0 (source below the smallest rung → no encodes)
			// completes at once, and a Go/Python count divergence can't stall the
			// queue for the whole job — a safety timeout (10min, well past any
			// mezzanine + atomic fan-out) releases it.
			if len(seenEncodes) >= expectedEncodes || (!launchStart.IsZero() && time.Since(launchStart) > 10*time.Minute) {
				markLaunched()
				job.AppendLog(fmt.Sprintf("[cloud-batch] fully fanned out (%d/%d encode jobs) — next job may launch", len(seenEncodes), expectedEncodes))
			}
		}
		if m.learnSpeed(line) {
			m.projectAndSetCosts(job) // refine cost + local-wall from the new sample
			continue                  // telemetry for the dynamic chunk selector, not a log line
		}
		// Same fold as the local-dist scanner. Without it the cloud path emitted
		// and forwarded ENCODER-FLEET correctly (#145) but nothing consumed it:
		// the markers fell through to AppendLog, so the Cloud Fleet chart showed
		// ALLOCATED vCPU from the AWS inventory and no ACTUAL CPU at all.
		// Per-machine throttling (fleetMinSampleGap) lives inside recordFleetCPU,
		// which matters here because every chunk container on an instance reports
		// that instance's CPU independently.
		if m.recordFleetCPU(line) {
			continue // per-machine CPU sample for the fleet view, not a log line
		}
		if !job.parseMarker(line) {
			job.AppendLog(line)
		}
		m.notify(job)
	}
	// When cancelled we killed the poll on purpose — swallow the resulting
	// wait error and let run() observe IsCancelled() and finalize the job
	// as cancelled rather than failed.
	if err := poll.Wait(); err != nil && !job.IsCancelled() {
		return fmt.Errorf("cli_batch poll: %w", err)
	}
	return nil
}

// buildSFNInput renders the JSON doc the state machine expects. Picks
// which (codec, tier) combinations to fan out over based on the
// JobConfig's Codec + MaxRes.
// variantChunkSeconds resolves one variant's chunk length from the job's
// ChunkDuration config: "dynamic" (also the default when unset) sizes it by
// learned encode speed (slow → 12s floor, cheap → whole clip); "whole" → one
// chunk; a number → that fixed size. clipS==0 (duration unknown) collapses to
// whole downstream.
func variantChunkSeconds(cfg string, clipS float64, speeds *EncodeSpeedStore, codec string, height int, twoPass bool, preset string, fps int) float64 {
	switch cfg {
	case "dynamic", "":
		return dynamicChunkSeconds(speeds, codec, height, twoPass, preset, fps, clipS)
	case "whole":
		if clipS > 0 {
			return clipS
		}
		return wholeVariantChunkSeconds
	default:
		if v, err := strconv.ParseFloat(cfg, 64); err == nil && v > 0 {
			return v
		}
		return defaultChunkDurationSeconds
	}
}

// chunkPlanLine explains one variant's chunk-size decision for the job log:
// how many chunks, the per-chunk length, and — for the dynamic selector — the
// speed (learned or seeded) and target that produced it. This is what makes a
// "why is h264 1080p one whole chunk?" question answerable from the log alone.
func chunkPlanLine(cfg, codec, label string, height int, twoPass bool, preset string, fps int, speeds *EncodeSpeedStore, chunkS float64, count int, clipS float64) string {
	pass := "1-pass"
	if twoPass {
		pass = "2-pass"
	}
	mode := fmt.Sprintf("%d chunks", count)
	if count == 1 {
		mode = "1 chunk (whole)"
	}
	head := fmt.Sprintf("[cloud-batch] chunk plan %s %s %s: %s — %.0fs/chunk",
		codec, label, pass, mode, chunkS)
	switch cfg {
	case "dynamic", "":
		sp, n := speeds.SpeedDetail("graviton", codec, height, twoPass, preset, fps)
		src := fmt.Sprintf("seeded %.3gx", sp)
		if n > 0 {
			src = fmt.Sprintf("learned %.3gx (%d samples)", sp, n)
		}
		return fmt.Sprintf("%s [dynamic: %s realtime × %.0fs target → %.0fs, clip %.0fs, min %.0fs]",
			head, src, dynamicTargetWallSeconds, math.Round(dynamicTargetWallSeconds*sp), clipS, dynamicMinChunkSeconds)
	case "whole":
		return head + " [whole]"
	default:
		return head + " [fixed " + cfg + "s]"
	}
}

// chunkModeLabel is a human-readable name for a ChunkDuration config value.
func chunkModeLabel(cfg string) string {
	switch cfg {
	case "dynamic", "":
		return "dynamic"
	case "whole":
		return "whole"
	default:
		return cfg + "s"
	}
}

// sourceMezzKey derives a stable source-identity key for the mezzanine cache
// from the file's name + size + mtime — cheap (a stat, no reading the file).
// Any change to those re-mezzanines. Returns false if the file can't be stat'd.
func sourceMezzKey(path string) (string, bool) {
	fi, err := os.Stat(path)
	if err != nil {
		return "", false
	}
	sum := sha1.Sum([]byte(fmt.Sprintf("%s|%d|%d",
		filepath.Base(path), fi.Size(), fi.ModTime().UnixNano())))
	return hex.EncodeToString(sum[:])[:16], true
}

// s3ObjectExists reports whether an s3://bucket/key object exists (aws-cli
// head-object; exit 0 == present). Best-effort — any error reads as absent.
func s3ObjectExists(uri string) bool {
	bucket, key, ok := strings.Cut(strings.TrimPrefix(uri, "s3://"), "/")
	if !ok || bucket == "" || key == "" {
		return false
	}
	cmd := exec.Command("aws", "s3api", "head-object", "--bucket", bucket, "--key", key)
	cmd.Env = os.Environ()
	return cmd.Run() == nil
}

// parseCodecSel turns the codec selector into the ordered codec list. It accepts
// a comma-separated subset ("h264", "hevc,av1", "h264,hevc,av1") — the form the
// UI's codec checkboxes emit — plus the legacy named values ("all", "both", and
// "" → h264+hevc). Unknown tokens are ignored; an empty result falls back to
// h264+hevc. Output is always in canonical order (h264, hevc, av1).
func parseCodecSel(sel string) []string {
	switch sel {
	case "", "both":
		return []string{"h264", "hevc"}
	case "all":
		return []string{"h264", "hevc", "av1"}
	}
	want := map[string]bool{}
	for _, t := range strings.Split(sel, ",") {
		switch strings.TrimSpace(t) {
		case "h264", "hevc", "av1":
			want[strings.TrimSpace(t)] = true
		}
	}
	var out []string
	for _, c := range []string{"h264", "hevc", "av1"} {
		if want[c] {
			out = append(out, c)
		}
	}
	if len(out) == 0 {
		return []string{"h264", "hevc"}
	}
	return out
}

func buildSFNInput(store *LadderStore, speeds *EncodeSpeedStore, s3Input, s3Prefix, s3Mezz, ladderName, codecSel, maxRes, minRes string, hevcSinglePass, mezzCached, burnin bool, sourceWidth, sourceFps int, clipDurationS float64, chunkCfg, segDur, partDur, gopDur string, priorityBase int, vmafEst map[string][2]string, logf func(string)) (string, int) {
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	// Every Batch SchedulingPriorityOverride for this job rides inside a 1000-wide
	// band (priorityBase): an EARLIER queued job gets a higher band, so ALL its
	// phases (mezzanine/audio/chunks/package) outrank a later job's — the later
	// job only gets fleet capacity the earlier one can't use (its tail, or spare
	// capacity on a big fleet). Within-job priority (0-999) rides inside the band.
	clampPrio := func(v int) int {
		if v < 1 {
			return 1
		}
		if v > 9999 {
			return 9999
		}
		return v
	}
	// Ladder-level VBV, defaulted to match ladder.py (124% / 0.25×).
	ladderDef, _ := store.Get(ladderName)
	maxratePct := ladderDef.MaxratePercent
	if maxratePct <= 0 {
		maxratePct = 124
	}
	bufMult := ladderDef.BufsizeMultiplier
	if bufMult <= 0 {
		bufMult = 0.25
	}
	// Profile timing: job value wins, else the ladder's, else the global default.
	// So a VOD ladder (0 partial, 6s GOP) drives the cloud encode the same way it
	// drives local — cli_phase reads these off env (previously hardcoded).
	segDur = effectiveTiming(segDur, ladderDef.SegmentDuration, "6")
	partDur = effectiveTiming(partDur, ladderDef.PartialDuration, "0.2")
	gopDur = effectiveTiming(gopDur, ladderDef.GopDuration, "1.0")
	// Float form of the resolved segment duration, for planChunks. Chunk
	// boundaries must land on whole segments, so the SAME value the workers are
	// told to segment with has to drive the plan.
	segS := 6.0
	if v, err := strconv.ParseFloat(segDur, 64); err == nil && v > 0 {
		segS = v
	}
	codecs := parseCodecSel(codecSel)
	// Build the per-codec rung list from the ladder store. resolveLadderRungs
	// caps each codec at the source width (no upscale) and --max-res, and
	// assigns ordinal labels for repeated resolutions — identical to
	// ladder.select_rungs, so cloud and local agree on {codec}_{label}.
	var variants []sfnVariant
	var scores []float64 // predicted encode wall per variant, aligned with variants (for rank-based priority)
	doH264, doHevc, doAV1 := false, false, false
	for _, c := range codecs {
		rungs := store.resolveRungs(ladderName, c, maxRes, minRes, sourceWidth)
		if len(rungs) == 0 {
			continue
		}
		switch c {
		case "h264":
			doH264 = true
		case "hevc":
			doHevc = true
		case "av1":
			doAV1 = true
		}
		for _, r := range rungs {
			vcpu, mem := variantResourcesFor(c, r.Height)
			// Two-pass is now owned by the ladder profile's per-codec pass count
			// (LadderDef.Passes → passesFor: hevc defaults to 2, h264/av1 to 1;
			// h264:2/av1:2 are rejected at save time). The per-encode
			// hevcSinglePass flag is kept as an OVERRIDE to force a single-pass
			// HEVC comparison run without cloning the ladder.
			twoPass := ladderDef.passesFor(c) == 2
			if hevcSinglePass && c == "hevc" {
				twoPass = false
			}
			// Ranking score = predicted encode WALL (content ÷ graviton speed). The
			// banded SchedulingPriority is assigned by RANK of this score AFTER the
			// loop (not by clamping the raw score), so the heaviest variant strictly
			// outranks the merely-heavy — pixels, codec, and pass all fold into the
			// speed model, self-correcting as speeds are learned.
			score := clipDurationS
			if score <= 0 {
				score = 1
			}
			score /= speeds.Speed("graviton", c, r.Height, twoPass, r.Preset, sourceFps)
			scores = append(scores, score)
			// Per-variant chunking: size this variant's chunks (dynamic by
			// complexity, or the job's fixed/whole config), then enumerate them.
			cs := variantChunkSeconds(chunkCfg, clipDurationS, speeds, c, r.Height, twoPass, r.Preset, sourceFps)
			// Plan the actual boundaries here, not just a count: the worker is
			// handed (index, start, duration) and does not re-derive them. See
			// chunkplan.go for why the authority moved.
			spans := planChunks(clipDurationS, cs, segS)
			nc := len(spans)
			if logf != nil {
				logf(chunkPlanLine(chunkCfg, c, r.Label, r.Height, twoPass, r.Preset, sourceFps, speeds, cs, nc, clipDurationS))
			}
			variants = append(variants, sfnVariant{
				Codec:   c,
				Label:   r.Label,
				Width:   strconv.Itoa(r.Width),
				Height:  strconv.Itoa(r.Height),
				Bitrate: strconv.Itoa(r.Bitrate),
				Preset:  r.Preset,
				VCPU:    vcpu,
				Memory:  mem,
				// "" when the curves have no estimate for this rung; cli_phase
				// then omits the overlay row rather than failing.
				EstVmaf:        vmafEst[c+"/"+r.Label][0],
				EstVmafClamped: vmafEst[c+"/"+r.Label][1],
				Priority:       0, // assigned by rank below
				TwoPass:        strconv.FormatBool(twoPass),
				ExtraArgs:      ladderDef.extraArgsFor(c),
				Chunks:         spans,
				ChunkDuration:  strconv.FormatFloat(cs, 'f', -1, 64),
				Chunked:        strconv.FormatBool(nc > 1),
				// The duration the boundaries above were planned against. The
				// worker checks its own mezzanine probe against this and fails
				// rather than encoding a plan built for a different-length file.
				ContentDuration: formatSeconds(clipDurationS),
			})
		}
	}
	// Assign the banded SchedulingPriority by RANK of predicted encode wall: the
	// heaviest variant gets 999 within this job's band, the next 998, and so on —
	// strictly decreasing. Ranking (rather than clamping the raw score to 999) is
	// what stops 4K H.264 and 4K HEVC 2-pass — both far past the old 999 cap —
	// from tying at 999 and letting Batch start the cheaper H.264 first. A ladder
	// has ≤ ~40 variants, so ranks never collide within the 999-wide band.
	order := make([]int, len(variants))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(a, b int) bool { return scores[order[a]] > scores[order[b]] })
	for rank, vi := range order {
		// 998, not 999: the top of the band is reserved for audio (see
		// prio_audio). Ranks stay strictly decreasing and a ladder has <= ~40
		// variants, so nothing collides.
		within := 998 - rank
		if within < 1 {
			within = 1
		}
		variants[vi].Priority = clampPrio(priorityBase + within)
	}
	// Submit slowest-first too (Priority desc) so submission order reinforces the
	// scheduler's priority — the long poles enter the queue first.
	sortRungsSlowestFirst(variants)
	// chunk_indices / chunk_duration / chunked are PER-VARIANT now (on each
	// sfnVariant above): each variant sizes its own chunks by complexity, so the
	// SFN Map reads them off the item, not a shared top-level value. A variant
	// that resolves to a single chunk flows through the whole-variant path
	// (chunk_index=-1, no concat) via its own "chunked":"false".
	doc := map[string]any{
		"s3_input":  s3Input,
		"s3_prefix": s3Prefix,
		// Source-keyed mezzanine home (mezz/<key>/), reused across jobs so the
		// same source skips the upload + mezzanine job. Falls back to s3_prefix
		// when the key can't be computed. Read by mezzanine (out), variants +
		// audio (in); variant/output writes still go to s3_prefix.
		"s3_mezz": s3Mezz,
		// Cache hit: the mezzanine already exists at s3_mezz, so the SFN skips
		// the Mezzanine job entirely (Choice → FanOut) and variants/audio read
		// it straight from the cache.
		"mezz_cached": mezzCached,
		"variants":    variants,
		"do_h264":     doH264,
		"do_hevc":     doHevc,
		"do_av1":      doAV1,
		// Ladder-level VBV shaping, applied to every variant's encode (the
		// worker reads these as MAXRATE_PERCENT / BUFSIZE_MULT env). Threaded
		// so a custom ladder's VBV is honored in the cloud, not just locally.
		"maxrate_percent":    strconv.Itoa(maxratePct),
		"bufsize_multiplier": strconv.FormatFloat(bufMult, 'f', -1, 64),
		// Profile timing (ladder-resolved) → containerOverrides env → cli_phase.
		// Same for every variant/package job of a run (like the VBV knobs above).
		"segment_duration": segDur,
		"partial_duration": partDur,
		"gop_duration":     gopDur,
		// Text-overlay toggle → BURNIN env on every variant job (see the ASL
		// containerOverrides). "true"/"false" so cli_phase's _env_flag_default_on
		// reads it; on by default, only an explicit false disables it.
		"burnin": strconv.FormatBool(burnin),
		// Banded priorities for the fixed phases (chunks carry their own banded
		// priority per variant). Keeps a job's whole pipeline in one band so an
		// earlier job's package isn't starved by a later job's chunks.
		"prio_mezz": clampPrio(priorityBase + 99),
		// Audio outranks every variant, and has to.
		//
		// It runs in the same FanOut Parallel as the Variants map, so packaging
		// cannot start until BOTH finish — but at base+55 it sat below all 336
		// chunk jobs and was placed near the end. Measured on run
		// 1785781612611: the last chunk container stopped at 18:34:26.969 and
		// audio, an ~11s job with no dependency on any chunk, stopped at
		// 18:34:33.224. Six seconds of a 42.9s fan-in gap spent waiting for a
		// job that could have run at the very start.
		//
		// It is one small job per run, so promoting it above the ladder costs
		// the heaviest variant a few seconds of queue position at most, and buys
		// that back many times over by removing audio from the critical path.
		"prio_audio": clampPrio(priorityBase + 999),
		"prio_pkg":   clampPrio(priorityBase + 45),
		// NOTE: two_pass + chunk_* are per-variant now (see the variant struct),
		// not top-level — variants differ in codec/pass AND chunk length.
	}
	b, _ := json.Marshal(doc)
	// Expected number of encode Batch jobs this fan-out will submit (one per
	// chunk; a whole variant is one). The launch gate releases once it has seen
	// all of them submitted — i.e. the job has fully fanned out.
	expected := 0
	for _, v := range variants {
		if len(v.Chunks) > 0 {
			expected += len(v.Chunks)
		} else {
			expected++
		}
	}
	return string(b), expected
}

// resolveCodec checks which codecs are already encoded in OutputDir and returns
// the codec flag that covers only the missing ones. Returns "" if all exist.
func (m *Manager) resolveCodec(cfg JobConfig, filename string) string {
	want := map[string]bool{}
	for _, c := range parseCodecSel(cfg.Codec) { // handles "", both, all, comma-list, single
		want[c] = true
	}
	// The still-missing codecs, in canonical order.
	var missing []string
	for _, c := range []string{"h264", "hevc", "av1"} {
		if want[c] && !dirExistsWithFiles(filepath.Join(m.OutputDir, cfg.outputCodecDir(filename, c))) {
			missing = append(missing, c)
		}
	}
	if len(missing) == 0 {
		return "" // everything wanted is already encoded → skip the file
	}
	// Re-encode only the missing ones. Keep the friendly "all"/"both" names when
	// they match exactly (nicer logs/UI); otherwise a comma-list (or single).
	if len(missing) == 3 {
		return "all"
	}
	if len(missing) == 2 && missing[0] == "h264" && missing[1] == "hevc" {
		return "both"
	}
	if len(missing) == 1 {
		return missing[0]
	}
	return strings.Join(missing, ",")
}

func dirExistsWithFiles(path string) bool {
	entries, err := os.ReadDir(path)
	if err != nil {
		return false
	}
	for _, e := range entries {
		if !e.IsDir() {
			return true
		}
	}
	return false
}

// workerName is the deterministic docker container name for a given job+file.
// Reconciliation on startup relies on this being stable so we can reattach to
// a worker that was still running when the server died.
func workerName(jobID string, fileIdx int) string {
	return fmt.Sprintf("encoder_job_%s_f%d", jobID, fileIdx)
}

// runFileContainer encodes one file in a detached sibling container.
// If a container with the expected name already exists (reconciliation path
// after a server restart), it re-attaches to it instead of creating a new one.
func (m *Manager) runFileContainer(job *Job, fileIdx int, script string, args []string) error {
	name := workerName(job.ID, fileIdx)

	exists, _, err := containerState(name)
	if err != nil {
		return fmt.Errorf("inspect %s: %w", name, err)
	}

	if !exists {
		runArgs := m.buildRunArgs(job, name, script, args)
		out, err := exec.Command("docker", runArgs...).CombinedOutput()
		if err != nil {
			return fmt.Errorf("docker run %s: %w: %s", name, err, strings.TrimSpace(string(out)))
		}
		job.AppendLog(fmt.Sprintf("launched worker container %s", name))
	} else {
		job.AppendLog(fmt.Sprintf("reattaching to worker container %s", name))
	}
	m.notify(job)

	return m.attachAndWait(job, name)
}

func (m *Manager) buildRunArgs(job *Job, name, script string, scriptArgs []string) []string {
	runArgs := []string{
		"run", "-dt",
		"--name", name,
		// Resolve host.docker.internal on plain Linux too — Docker Desktop
		// provides it automatically, but Docker Engine needs host-gateway. Lets
		// the spawned orchestrator reach Temporal/MinIO published on the host
		// when the master runs on Linux (a Linux box, or a CI runner).
		"--add-host", "host.docker.internal:host-gateway",
		"--label", "encoder.job_id=" + job.ID,
		"--label", "encoder.role=encode-worker",
		"--label", fmt.Sprintf("encoder.target=%s", job.Config.Target),
		"-v", m.HostSourceDir + ":" + m.SourceDir + ":ro",
		"-v", m.HostOutputDir + ":" + m.OutputDir,
		"-v", m.HostTmpDir + ":" + m.TmpDir,
		// Pin TMPDIR to the mounted host tmp so cli_local.py's per-clip
		// work dir (at TMPDIR/encode_<stem>/) persists across worker
		// container lifetimes. That's what makes local Retry reuse
		// prior mezzanines / variants — the work_dir is on the host
		// filesystem, not inside the ephemeral container layer.
		"-e", "TMPDIR=" + m.TmpDir,
		// Point the Python encoder at the persisted ladder store (mounted via
		// TmpDir) so custom/edited ladders resolve for local encodes too. The
		// file is written by the control plane; cloud variant jobs don't need
		// it (they get concrete rungs from the SFN).
		"-e", "LADDER_STORE=" + filepath.Join(m.TmpDir, "ladders.json"),
		// Run the orchestrator via python3 rather than exec'ing the script
		// directly, so it works whether the script is baked into the image (+x)
		// or bind-mounted from the host working tree (HOST_SCRIPTS_DIR, where the
		// file keeps the host's non-executable mode). The script path is passed
		// as the first argument below, after the image.
		"--entrypoint", "python3",
	}
	// Dev: overlay the host's working-tree encoder package onto the image's, so
	// a spawned orchestrator (local-dist) runs current code without a rebuild.
	// Opt-in via HOST_SCRIPTS_DIR (set by `make farm-dev`); empty in normal runs,
	// where the container uses the image's baked scripts.
	if dir := os.Getenv("HOST_SCRIPTS_DIR"); dir != "" {
		runArgs = append(runArgs, "-v", dir+":"+filepath.Join(m.ScriptsDir, "infinite_streaming_encoder")+":ro")
	}
	// TargetLocalDist: the orchestrator reaches Temporal + MinIO. These are the
	// MinIO creds (as AWS_* for boto3) — kept under distinct MINIO_* server env
	// so they don't clobber the server's real AWS creds used by the cloud path.
	if job.Config.Target == TargetLocalDist {
		runArgs = append(runArgs,
			"-e", "TEMPORAL_ADDRESS="+envOr("TEMPORAL_ADDRESS", "host.docker.internal:7233"),
			"-e", "TEMPORAL_TASK_QUEUE="+envOr("TEMPORAL_TASK_QUEUE", "encode"),
			"-e", "S3_ENDPOINT_URL="+envOr("MINIO_ENDPOINT", "http://host.docker.internal:9000"),
			"-e", "AWS_ACCESS_KEY_ID="+envOr("MINIO_ACCESS_KEY", "encoder"),
			"-e", "AWS_SECRET_ACCESS_KEY="+envOr("MINIO_SECRET_KEY", "encoder-secret"),
			"-e", "AWS_REGION="+envOr("MINIO_REGION", "us-east-1"),
		)
	}
	runArgs = append(runArgs, m.EncoderImage)
	runArgs = append(runArgs, script)
	runArgs = append(runArgs, scriptArgs...)
	return runArgs
}

var cloudEnvPassthrough = []string{
	"AWS_REGION", "AWS_PROFILE",
	"S3_BUCKET",
	"INSTANCE_TYPE", "INSTANCE_TYPE_FALLBACKS",
	"USE_SPOT", "AMI_ID",
	"SUBNET_ID", "SECURITY_GROUP_ID", "INSTANCE_PROFILE",
	"GHCR_USERNAME", "GHCR_PAT",
	"DOCKER_IMAGE",
}

// attachAndWait streams the worker container's logs into the job log buffer
// and blocks until the container exits. Returns an error if the container
// exited with a non-zero status. The container itself is removed after exit.
//
// For already-exited containers we use plain `docker logs` (no -f) — some
// Docker daemon versions hang indefinitely when `docker logs -f` is
// started against an already-exited container, which would leave reconcile
// goroutines pinned on the restart-resilience path. Plain `docker logs`
// always drains the history and returns.
func (m *Manager) attachAndWait(job *Job, name string) error {
	_, running, _ := containerState(name)

	args := []string{"logs", name}
	if running {
		args = []string{"logs", "-f", name}
	}
	logs := exec.Command("docker", args...)
	stdout, err := logs.StdoutPipe()
	if err != nil {
		return fmt.Errorf("logs pipe: %w", err)
	}
	logs.Stderr = logs.Stdout
	if err := logs.Start(); err != nil {
		return fmt.Errorf("docker logs start: %w", err)
	}

	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 256*1024), 256*1024)
	// ffmpeg's `-stats` progress updates use \r (not \n) to overwrite the
	// same terminal line, so a plain ScanLines would never yield until the
	// encode ends. Treat both \r and \n as line terminators so the UI sees
	// live frame counts.
	scanner.Split(splitLinesOrCR)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		if m.learnSpeed(line) {
			m.projectAndSetCosts(job) // refine cost + local-wall from the new sample
			continue                  // dynamic-chunk-selector telemetry, not a log line
		}
		if m.recordFleetCPU(line) {
			continue // per-machine CPU sample for the fleet view, not a log line
		}
		// Structured progress markers update Job.Stages and are suppressed
		// from the log buffer so the viewer stays readable. Anything else
		// (ffmpeg stats, x265 info, package logs) goes through unchanged.
		if job.parseMarker(line) {
			m.notify(job)
			continue
		}
		job.AppendLog(line)
		job.Progress = stripANSI(line)
		m.notify(job)
	}
	logs.Wait()

	exitCode, err := containerExitCode(name)
	// Remove the worker regardless of exit outcome.
	exec.Command("docker", "rm", "-f", name).Run()

	if err != nil {
		return fmt.Errorf("inspect exit code: %w", err)
	}
	if exitCode != 0 {
		return fmt.Errorf("worker exited with code %d", exitCode)
	}
	return nil
}

// containerState reports whether a container with the given name exists and,
// if it does, whether it is currently running.
func containerState(name string) (exists bool, running bool, err error) {
	out, runErr := exec.Command("docker", "inspect",
		"-f", "{{.State.Running}}", name).Output()
	if runErr != nil {
		// `docker inspect` exits non-zero when the container doesn't exist;
		// distinguishing "not found" from a real daemon error would require
		// parsing stderr, which isn't worth it here — treat non-zero as absent.
		return false, false, nil
	}
	return true, strings.TrimSpace(string(out)) == "true", nil
}

func containerExitCode(name string) (int, error) {
	out, err := exec.Command("docker", "inspect",
		"-f", "{{.State.ExitCode}}", name).Output()
	if err != nil {
		return -1, err
	}
	code := 0
	fmt.Sscanf(strings.TrimSpace(string(out)), "%d", &code)
	return code, nil
}

// persistedState is written to $TmpDir/jobs/<id>.json while a job is active.
// Its presence on startup marks a job we need to reconcile; CurrentFileIdx
// tells us where the loop was when the server died.
type persistedState struct {
	ID             string    `json:"id"`
	Config         JobConfig `json:"config"`
	StartedAt      time.Time `json:"started_at"`
	CurrentFileIdx int       `json:"current_file_idx"`
	// CloudExecArn lets Reconcile re-attach to a cloud-batch job's still-running
	// SFN execution after a restart instead of submitting a duplicate.
	CloudExecArn string `json:"cloud_exec_arn,omitempty"`
}

func (m *Manager) statePath(jobID string) string {
	return filepath.Join(m.TmpDir, "jobs", jobID+".json")
}

func (m *Manager) persistState(job *Job, fileIdx int) {
	os.MkdirAll(filepath.Join(m.TmpDir, "jobs"), 0755)
	data, err := json.Marshal(persistedState{
		ID:             job.ID,
		Config:         job.Config,
		StartedAt:      job.StartedAt,
		CurrentFileIdx: fileIdx,
		CloudExecArn:   job.CloudExecArn(),
	})
	if err != nil {
		return
	}
	os.WriteFile(m.statePath(job.ID), data, 0644)
}

func (m *Manager) removePersistedState(jobID string) {
	os.Remove(m.statePath(jobID))
}

func (m *Manager) loadPersistedStates() []persistedState {
	entries, err := os.ReadDir(filepath.Join(m.TmpDir, "jobs"))
	if err != nil {
		return nil
	}
	var out []persistedState
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".json" {
			continue
		}
		data, err := os.ReadFile(filepath.Join(m.TmpDir, "jobs", e.Name()))
		if err != nil {
			continue
		}
		var s persistedState
		if err := json.Unmarshal(data, &s); err != nil {
			continue
		}
		out = append(out, s)
	}
	return out
}

// Reconcile rebuilds in-memory jobs from persisted state files and resumes
// them. For each job, the starting file index is taken from the state file;
// if a worker container still exists for that file (running or already
// exited), runFileContainer will reattach via `docker logs -f` rather than
// launching a duplicate.
//
// Call this before accepting new submissions — it re-pushes jobs into the
// semaphore just like Submit would, so concurrency limits still apply.
func (m *Manager) Reconcile() {
	states := m.loadPersistedStates()
	for _, s := range states {
		// A job persisted before the local-dist/cloud-batch -> local/cloud rename
		// carries the old target value; normalize so it dispatches correctly.
		s.Config.Target = NormalizeTarget(s.Config.Target)
		job := &Job{
			ID:        s.ID,
			Config:    s.Config,
			Status:    StatusQueued,
			StartedAt: s.StartedAt,
			Progress:  "resuming after restart",
		}
		// Seed the cloud-batch execution ARN so runOneCloudBatchSFN re-attaches
		// to the still-running SFN execution rather than submitting a duplicate.
		job.setCloudExecArn(s.CloudExecArn)
		m.mu.Lock()
		m.jobs = append(m.jobs, job)
		m.mu.Unlock()
		m.notify(job)
		go m.run(job, s.CurrentFileIdx)
	}
}
