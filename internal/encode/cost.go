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
// we quote alongside it.
//
// These mirror scripts/infinite_streaming_encoder/pricing.py, which holds the
// single Python-side definition and documents how the spot figure was measured
// (7-day blended mean over the instance types the compute env launches, 479
// price points on 2026-08-07). Two languages cannot share a constant, so these
// MUST be changed together — #217 exists because three copies drifted apart.
const (
	awsSpotVCPUHourUSD     = 0.014
	awsOndemandVCPUHourUSD = 0.036
)

// EgressUSDPerGB prices S3 -> internet transfer, us-west-2.
//
// **The 100 GB/month free tier is deliberately ignored.** Applying it would make
// the same run cost $0.00 on the 1st and $0.24 on the 20th, so no two runs would
// be comparable and the cheapest-looking run would be whichever happened to go
// first. This is the long-run marginal rate: past the allowance — which heavy
// iteration now clears within days of each month starting — it is what every GB
// actually costs. Under the allowance it over-states, which is the safe
// direction for a number meant to discourage needless transfer.
//
// The single definition. The UI reads it from /api/settings rather than
// hardcoding a copy — #217 exists because three spot-rate constants drifted
// apart in three files, and this must not become the fourth.
const EgressUSDPerGB = 0.09

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
		ladderName = DefaultLadderName
	}
	for _, c := range parseCodecSel(cfg.Codec) {
		for _, r := range m.Ladders.resolveRungs(ladderName, c, cfg.MaxRes, cfg.MinRes, sourceWidth) {
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
		ladderName = DefaultLadderName
	}
	cores := m.localFleetPerfCores()
	if cores <= 0 {
		cores = 1
	}
	var coreSeconds, floor float64
	for _, c := range parseCodecSel(cfg.Codec) {
		for _, r := range m.Ladders.resolveRungs(ladderName, c, cfg.MaxRes, cfg.MinRes, sourceWidth) {
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

// --- SaaS baseline pricing (per OUTPUT minute). Ported from commercial_cloud.py
//     so BOTH targets compute the commercial + MediaConvert baselines the same
//     way, server-side (they're a pure function of the ladder + probe, need no
//     learned data). Previously only the local orchestrator emitted these, so
//     cloud-batch jobs showed $0. ---

var _commercialTiers = []struct {
	maxH int
	rate map[string]float64
}{
	{719, map[string]float64{"h264": 0.005, "hevc": 0.008, "av1": 0.010}},     // SD (<720)
	{1080, map[string]float64{"h264": 0.010, "hevc": 0.015, "av1": 0.020}},    // HD
	{1440, map[string]float64{"h264": 0.025, "hevc": 0.038, "av1": 0.050}},    // 1440p
	{1 << 30, map[string]float64{"h264": 0.045, "hevc": 0.068, "av1": 0.090}}, // 4K+
}

const (
	_commercialRepackPerMin = 0.003 // package each rendition to HLS/DASH
	_commercialAudioPerMin  = 0.001 // one audio track when the source has audio
)

func _commercialRate(height int, codec string) float64 {
	for _, t := range _commercialTiers {
		if height <= t.maxH {
			if r, ok := t.rate[codec]; ok {
				return r
			}
			return t.rate["h264"]
		}
	}
	return _commercialTiers[len(_commercialTiers)-1].rate["h264"]
}

// _fpsMult = ceil(fps/30): +100% per additional 30fps (30→1, 60→2).
func _fpsMult(fps int) int {
	if fps <= 0 {
		fps = 30
	}
	m := (fps + 29) / 30
	if m < 1 {
		m = 1
	}
	return m
}

// _bitrateMult = +25% per 50 Mbps of source bitrate over 50.
func _bitrateMult(srcMbps float64) float64 {
	if srcMbps <= 50 {
		return 1.0
	}
	return 1.0 + 0.25*math.Ceil((srcMbps-50)/50)
}

// MediaConvert: Basic tier for h264, Professional for hevc/av1; ×2 above 30fps.
var (
	_mcBasic = []struct {
		maxH int
		rate float64
	}{{719, 0.0075}, {1080, 0.0150}, {1 << 30, 0.0300}}
	_mcPro = []struct {
		maxH int
		rate float64
	}{{719, 0.0170}, {1080, 0.0340}, {1 << 30, 0.0680}}
)

func _mcRate(height int, codec string) float64 {
	t := _mcPro
	if codec == "h264" {
		t = _mcBasic
	}
	for _, e := range t {
		if height <= e.maxH {
			return e.rate
		}
	}
	return t[len(t)-1].rate
}

// projectSaaSCosts returns what this ladder would cost on a hosted transcoder —
// commercial cloud (per-output-minute tiers + repack + audio, × fps × bitrate)
// and AWS MediaConvert (Basic/Pro tiers × 2 over 30fps). Target-independent.
func (m *Manager) projectSaaSCosts(cfg JobConfig, sourceWidth, fps int, durationS float64, hasAudio bool, srcMbps float64) (commercial, mediaconvert float64) {
	if m.Ladders == nil || durationS <= 0 {
		return 0, 0
	}
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	minutes := durationS / 60.0
	cMult := float64(_fpsMult(fps)) * _bitrateMult(srcMbps)
	mcMult := 1.0
	if fps > 30 {
		mcMult = 2.0
	}
	for _, c := range parseCodecSel(cfg.Codec) {
		for _, r := range m.Ladders.resolveRungs(ladderName, c, cfg.MaxRes, cfg.MinRes, sourceWidth) {
			commercial += minutes*_commercialRate(r.Height, c)*cMult + minutes*_commercialRepackPerMin
			mediaconvert += minutes * _mcRate(r.Height, c) * mcMult
		}
	}
	if hasAudio {
		commercial += minutes * _commercialAudioPerMin
	}
	return commercial, mediaconvert
}

// ensureSourceProbe probes the job's first source file once (duration/width/fps
// + audio presence + bitrate) and caches the result on the Job, so live re-
// prediction — called on every encode-speed sample — needn't re-invoke ffprobe.
// Returns false when the clip can't be sized (no files / ffprobe failure / zero
// duration).
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
	hasAudio := false
	var mbps float64
	if err == nil && dur > 0 {
		width = probeSourceWidth(src)
		fps = probeSourceFps(src)
		hasAudio = probeHasAudio(src)
		mbps = probeSourceMbps(src)
	}
	job.mu.Lock()
	job.probeDone = true
	job.probeDuration, job.probeWidth, job.probeFps = dur, width, fps
	job.probeHasAudio, job.probeSrcMbps = hasAudio, mbps
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
	hasAudio, srcMbps := job.probeHasAudio, job.probeSrcMbps
	job.mu.Unlock()

	spot, ondemand := m.projectCloudCost(job.Config, width, fps, dur)
	localWall := m.projectLocalWallSeconds(job.Config, width, fps, dur)
	commercial, mediaconvert := m.projectSaaSCosts(job.Config, width, fps, dur, hasAudio, srcMbps)

	job.mu.Lock()
	if commercial > 0 {
		job.CommercialUSD = commercial
	}
	if mediaconvert > 0 {
		job.MediaConvertUSD = mediaconvert
	}
	if spot > 0 {
		job.AwsSpotUSD, job.AwsOndemandUSD = spot, ondemand
	}
	if localWall > 0 {
		// Don't overwrite the MEASURED local wall (elapsed+ETA) that computeProgress
		// keeps for a running local-dist job — the model is only the pre-progress /
		// cloud "what-if" estimate.
		measured := job.Config.Target == TargetLocalDist && job.ETASeconds > 0
		if !measured {
			job.LocalWallSeconds = localWall
		}
	}
	job.mu.Unlock()
	m.notify(job)
}
