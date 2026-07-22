package encode

import (
	"math"
	"path/filepath"
	"strconv"
)

// defaultLocalPerfCores is the assumed local-fleet parallelism when no worker has
// reported live CPU yet — the full fleet (MacBook 4 + Mac Mini 4 + ubuntu 8 perf
// cores). Once workers report via ENCODER-FLEET, the real live sum is used
// instead, so a smaller live fleet (e.g. only the Mac up) predicts proportionally
// slower.
const defaultLocalPerfCores = 16.0

// AWS Batch per-vCPU-hour pricing for the Graviton (c/m-7g) fleet. Spot is the
// steady-state rate we actually pay; on-demand is the reclaim-proof upper bound
// we quote alongside it. These mirror commercial_cloud.py's _AWS_*_VCPU_HR so
// the Go-side projection and the Python SaaS baselines agree on units.
const (
	awsSpotVCPUHourUSD     = 0.013
	awsOndemandVCPUHourUSD = 0.036
)

// projectCloudCost estimates what a job's full output ladder would cost to
// encode on our AWS Batch Graviton fleet, and is authoritative for the UI's
// "AWS spot / on-demand" numbers (the Python marker only supplies the SaaS
// commercial + MediaConvert baselines now). Cost is COMPUTE-based, exactly how
// AWS bills: for every variant (codec × ladder rung) it takes the LEARNED
// graviton encode speed — content-seconds per wall-second, seeded from the model
// until observed — turns the clip's content duration into encode wall-hours,
// and multiplies by the variant's vCPU request and the $/vCPU-hour rate. Summed
// over all variants. As the graviton speed table fills in from real encodes,
// this sharpens automatically. Returns (spot, ondemand); (0,0) if it can't size
// the ladder (unknown duration, no store, empty ladder).
func (m *Manager) projectCloudCost(cfg JobConfig, sourceWidth, fps int, durationS float64) (spot, ondemand float64) {
	if m.Speeds == nil || m.Ladders == nil || durationS <= 0 {
		return 0, 0
	}
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = "apple-uniq-live"
	}
	for _, c := range parseCodecSel(cfg.Codec) {
		for _, r := range m.Ladders.resolveRungs(ladderName, c, cfg.MaxRes, sourceWidth) {
			twoPass := c == "hevc" && !cfg.HevcSinglePass
			sp := m.Speeds.Speed("graviton", c, r.Height, twoPass, r.Preset, fps)
			if sp <= 0 {
				continue
			}
			vcpuStr, _ := variantResourcesFor(c, r.Height)
			vcpu, _ := strconv.ParseFloat(vcpuStr, 64)
			wallHours := (durationS / sp) / 3600.0
			spot += wallHours * vcpu * awsSpotVCPUHourUSD
			ondemand += wallHours * vcpu * awsOndemandVCPUHourUSD
		}
	}
	return spot, ondemand
}

// localFleetPerfCores is the local fleet's total parallel perf-core budget: the
// live sum reported by workers (ENCODER-FLEET), or the default full-fleet
// assumption before any worker has checked in.
func (m *Manager) localFleetPerfCores() float64 {
	var sum float64
	for _, e := range m.FleetCPU() {
		sum += e.Perf
	}
	if sum <= 0 {
		return defaultLocalPerfCores
	}
	return sum
}

// projectLocalWallSeconds predicts the wall-clock time (end − start) to encode
// the whole ladder on the LOCAL fleet — the "cost" of the local option, whose
// only downside is time. Makespan model: sum each variant's CORE-seconds of work
// (encode wall on a local box × the vCPUs it drives), divide by the fleet's total
// parallel cores (perfect-packing lower bound, which LPT scheduling + per-variant
// chunking approaches), and floor by the longest atomic chunk (~a dynamic-target
// chunk's wall) since no job finishes faster than its single largest piece. Uses
// LEARNED local speeds, so a running local encode's estimate converges on reality.
func (m *Manager) projectLocalWallSeconds(cfg JobConfig, sourceWidth, fps int, durationS float64) float64 {
	if m.Speeds == nil || m.Ladders == nil || durationS <= 0 {
		return 0
	}
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = "apple-uniq-live"
	}
	cores := m.localFleetPerfCores()
	if cores <= 0 {
		cores = 1
	}
	var coreSeconds, floor float64
	for _, c := range parseCodecSel(cfg.Codec) {
		for _, r := range m.Ladders.resolveRungs(ladderName, c, cfg.MaxRes, sourceWidth) {
			twoPass := c == "hevc" && !cfg.HevcSinglePass
			sp := m.Speeds.LocalSpeed(c, r.Height, twoPass, r.Preset, fps)
			if sp <= 0 {
				continue
			}
			wall := durationS / sp // serial wall to encode this whole variant on one box
			vcpuStr, _ := variantResourcesFor(c, r.Height)
			vcpu, _ := strconv.ParseFloat(vcpuStr, 64)
			coreSeconds += wall * vcpu
			// Atomic floor: chunking splits a variant into ~dynamic-target-wall
			// pieces, so the smallest indivisible unit is min(whole variant,
			// dynamicTargetWallSeconds). The makespan can't beat the longest one.
			if atomic := math.Min(wall, dynamicTargetWallSeconds); atomic > floor {
				floor = atomic
			}
		}
	}
	return math.Max(coreSeconds/cores, floor)
}

// ensureSourceProbe probes the job's first source file once (duration/width/fps)
// and caches the result on the Job, so live re-prediction — called on every
// encode-speed sample — needn't re-invoke ffprobe. Returns false when the clip
// can't be sized (no files / ffprobe failure / zero duration).
func (m *Manager) ensureSourceProbe(job *Job) bool {
	job.mu.Lock()
	if job.probeDone {
		ok := job.probeDuration > 0
		job.mu.Unlock()
		return ok
	}
	job.mu.Unlock()

	if len(job.Config.Files) == 0 {
		return false
	}
	src := filepath.Join(m.SourceDir, job.Config.Files[0])
	dur, err := probeDurationSeconds(src)
	width, fps := 0, 0
	if err == nil && dur > 0 {
		width = probeSourceWidth(src)
		fps = probeSourceFps(src)
	}
	job.mu.Lock()
	job.probeDone = true
	job.probeDuration, job.probeWidth, job.probeFps = dur, width, fps
	ok := dur > 0
	job.mu.Unlock()
	return ok
}

// projectAndSetCosts (re)computes and stores the whole cost comparison that needs
// the learned-speed model: the AWS Batch Graviton cost projection (authoritative
// for AwsSpotUSD/AwsOndemandUSD — the Python ENCODER-COMMERCIAL marker supplies
// only the SaaS commercial + MediaConvert baselines) and the predicted local-
// fleet wall-clock time. Target-independent (both comparisons apply to any run).
// Cheap once the source is probed, so it's called both at job start AND on every
// ENCODER-SPEED sample — the estimates then sharpen live as the running encode
// learns real speeds (a local run refines LocalWallSeconds; a cloud run refines
// the graviton cost). Idempotent; a reattach recomputes the same values.
func (m *Manager) projectAndSetCosts(job *Job) {
	if !m.ensureSourceProbe(job) {
		return
	}
	job.mu.Lock()
	dur, width, fps := job.probeDuration, job.probeWidth, job.probeFps
	job.mu.Unlock()

	spot, ondemand := m.projectCloudCost(job.Config, width, fps, dur)
	localWall := m.projectLocalWallSeconds(job.Config, width, fps, dur)

	job.mu.Lock()
	if spot > 0 {
		job.AwsSpotUSD, job.AwsOndemandUSD = spot, ondemand
	}
	if localWall > 0 {
		job.LocalWallSeconds = localWall
	}
	job.mu.Unlock()
	m.notify(job)
}
