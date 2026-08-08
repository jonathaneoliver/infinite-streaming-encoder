package encode

import (
	"fmt"
	"math"
	"path/filepath"
	"sort"
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

// assumedFleetIdleFraction prices the gap between allocated and rented time
// before any cloud run has been measured on this install.
//
// 0.40 is the midpoint of the 38-44% band measured across every run tabulated
// in #208 and #237. It is an ASSUMPTION, labelled as one everywhere it is
// shown, and it is self-correcting: the first finished cloud run replaces it
// with that run's own figure, so nobody ever has to edit this constant to track
// a fleet that got better at packing.
const assumedFleetIdleFraction = 0.40

// maxFleetIdleFraction caps what the measurement is allowed to claim. A sample
// approaching 1.0 divides the estimate towards infinity, and the sample most
// likely to do that is a degenerate one — a run whose instances were almost
// entirely someone else's. Clamping keeps a bad measurement merely wrong
// instead of catastrophic.
const maxFleetIdleFraction = 0.85

// fleetIdleRuns is how many recent cloud runs the allowance is taken over. Small
// enough that a fleet-packing improvement shows up within a working session,
// large enough that one unusual run cannot swing it. The MEDIAN, not the mean —
// #208's own tabulation spans 38-44% with outliers well outside it, and a single
// pathological run must not set the price of every future estimate.
const fleetIdleRuns = 10

// fleetIdleFraction is the share of rented machine time that no job allocated,
// measured over recent cloud runs. Returns (fraction, runs, measured) — `runs`
// is how many samples backed it, and `measured` is false when the answer is the
// stated assumption rather than an observation.
//
// This exists because the estimate and the finished run were computed on
// different bases and disagreed by 1.6-1.8x (#237). projectCloudCost predicts
// ALLOCATED vCPU-hours; AWS bills the INSTANCE, launch to termination, boot and
// image pull and scale-down tail included. The two reconcile through exactly one
// term — machine = allocated / (1 - idle) — and this measures it.
//
// OVERLAPPING RUNS ARE EXCLUDED. _emit_machine_rental attributes a concurrent
// run's time on a shared instance to whichever run is reporting, so a sample
// taken while another encode was live reads high for a reason that says nothing
// about how the fleet packs one run. Including those biases the allowance up and
// turns a systematic undercount into a systematic overcount.
func (m *Manager) fleetIdleFraction() (fraction float64, runs int, measured bool) {
	samples := m.readRunSamples()
	var usable []float64
	for i, s := range samples {
		if s.IdlePct <= 0 || s.MachineVCPUHours <= 0 {
			continue // not a cloud run, or rental could not be measured
		}
		if runOverlapsAny(s, samples, i) {
			continue
		}
		usable = append(usable, s.IdlePct/100.0)
	}
	if len(usable) == 0 {
		return assumedFleetIdleFraction, 0, false
	}
	if len(usable) > fleetIdleRuns {
		usable = usable[len(usable)-fleetIdleRuns:]
	}
	f := median(usable)
	if f < 0 {
		f = 0
	}
	if f > maxFleetIdleFraction {
		f = maxFleetIdleFraction
	}
	return f, len(usable), true
}

// runOverlapsAny reports whether samples[i]'s run span intersects any other
// sample's. A sample missing its span is treated as NOT overlapping: those are
// records written before the span was recorded, and dropping the whole back
// catalogue would leave the allowance on its assumption for ten more runs.
func runOverlapsAny(s RunSample, all []RunSample, i int) bool {
	if s.StartedAt <= 0 || s.EndedAt <= s.StartedAt {
		return false
	}
	for j, o := range all {
		if j == i || o.StartedAt <= 0 || o.EndedAt <= o.StartedAt {
			continue
		}
		if s.StartedAt < o.EndedAt && o.StartedAt < s.EndedAt {
			return true
		}
	}
	return false
}

// median of a non-empty slice. Copies before sorting — the caller's slice is a
// window into the sample log and must not be reordered under it.
func median(v []float64) float64 {
	c := append([]float64(nil), v...)
	sort.Float64s(c)
	n := len(c)
	if n%2 == 1 {
		return c[n/2]
	}
	return (c[n/2-1] + c[n/2]) / 2
}

// projectCloudCost estimates what a job's full output ladder would cost to
// encode on our AWS Batch Graviton fleet, and is authoritative for the UI's
// "AWS spot / on-demand" numbers (the Python marker only supplies the SaaS
// commercial + MediaConvert baselines now). For every variant (codec × ladder
// rung) it takes the LEARNED graviton encode speed — content-seconds per
// wall-second, seeded from the model until observed — turns the clip's content
// duration into encode wall-hours, and multiplies by the variant's vCPU request.
// Summed over all variants, that is ALLOCATED vCPU-hours; the fleet-idle
// allowance below converts it to the MACHINE hours AWS actually bills. As the
// graviton speed table and the idle history fill in from real encodes, this
// sharpens automatically.
//
// Returns (spot, ondemand); (0,0) if it can't size the ladder (unknown duration,
// no store, empty ladder).
//
// TWO THINGS HERE ARE DELIBERATE AND WILL LOOK LIKE BUGS:
//
// "graviton" is hardcoded, and must stay that way while there is one compute
// environment. infra/terraform/modules/compute/main.tf launches c8g/c7g only, so
// a cloud-batch job CANNOT land on Intel or AMD — quoting JobConfig.CpuArch's
// speeds would price hardware the run can never reach. (The form's cpu-arch
// control is hidden as retired legacy for the same reason, and the estimate
// request does not send the field.) Wire it up when a second compute env exists,
// not before.
//
// variantResourcesFor is the RESERVATION, not measured utilisation, and pricing
// the reservation is correct here rather than merely convenient. The idle
// allowance is defined as (machine - allocated)/machine where `allocated` sums
// each job's RESERVED vCPU × duration, so the reservation appears on both sides
// of the calibration and cancels. Substituting measured busy-cores would price
// against a ratio that was never measured that way and reintroduce the very
// undercount this fixes. Batch also packs by reservation, so it is the term that
// actually drives instance lifetime — the thing being billed.
func (m *Manager) projectCloudCost(cfg JobConfig, sourceWidth, fps int, durationS float64) (spot, ondemand float64) {
	spot, ondemand, _, _, _ = m.projectCloudCostDetail(cfg, sourceWidth, fps, durationS)
	return spot, ondemand
}

// projectCloudCostDetail is projectCloudCost plus the idle allowance it applied,
// for callers that must SHOW the allowance rather than fold it in silently. An
// invisible 1.7x multiplier on a number sitting next to the Encode button is how
// this went unnoticed in the first place.
func (m *Manager) projectCloudCostDetail(cfg JobConfig, sourceWidth, fps int, durationS float64) (spot, ondemand, idle float64, idleRuns int, idleMeasured bool) {
	idle, idleRuns, idleMeasured = m.fleetIdleFraction()
	if m.Speeds == nil || m.Ladders == nil || durationS <= 0 {
		return 0, 0, idle, idleRuns, idleMeasured
	}
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	// machine = allocated / (1 - idle). Guarded because a clamped-but-still-high
	// fraction divides hard; maxFleetIdleFraction keeps this well away from zero.
	scale := 1.0
	if d := 1.0 - idle; d > 0 {
		scale = 1.0 / d
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
			spot += wallHours * vcpu * awsSpotVCPUHourUSD * scale
			ondemand += wallHours * vcpu * awsOndemandVCPUHourUSD * scale
		}
	}
	return spot, ondemand, idle, idleRuns, idleMeasured
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

// --- Coconut (https://www.coconut.co/pricing, verified 2026-08-07) -----------
//
// Flat per output-minute by resolution, with NO codec multiplier — AV1 costs the
// same as H.264 there, which is unusual and is why it undercuts Qencode on wide
// multi-codec ladders despite a higher H.264 rate.
const (
	_coconutSD    = 0.00750 // < 720p
	_coconutHD    = 0.01500 // 720p-1080p
	_coconutUHD   = 0.03000 // > 1080p
	_coconutAudio = 0.00325 // per output-minute, one track
)

func _coconutRate(height int) float64 {
	switch {
	case height <= 719:
		return _coconutSD
	case height <= 1080:
		return _coconutHD
	}
	return _coconutUHD
}

// --- Bitmovin (https://bitmovin.com/pricing/, verified 2026-08-07) -----------
//
// A low base rate multiplied three ways, which is what makes it the most
// expensive of the four despite the smallest headline number: $0.02/output-min
// x resolution x codec x passes. A 21-rendition 5-minute clip bills as 360
// output-minutes.
//
// The 2,000 free minutes/month are DELIBERATELY NOT MODELLED, consistent with
// how AWS free tiers are treated everywhere else here (see pricing.py): a figure
// that is zero only because an allowance has not run out tells you nothing about
// what the work costs, and makes two runs incomparable.
const _bitmovinBasePerMin = 0.02

// Resolution factors, from their published table.
func _bitmovinResFactor(height int) float64 {
	switch {
	case height <= 719:
		return 1
	case height <= 1080:
		return 2
	case height <= 2160:
		return 4
	}
	return 120 // 8K
}

// Codec factor. HEVC is published as 2x. AV1's factor is NOT on the pricing
// page — it points at a separate methodology document — so 2x is assumed by
// analogy with HEVC and the result is a FLOOR, not a quote. Given HEVC is 2x,
// AV1 is very unlikely to be cheaper.
func _bitmovinCodecFactor(codec string) float64 {
	switch codec {
	case "hevc", "av1":
		return 2
	}
	return 1
}

// 2-pass is 1.25x (3-pass 2.0x, unused here).
func _bitmovinPassFactor(twoPass bool) float64 {
	if twoPass {
		return 1.25
	}
	return 1
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
func (m *Manager) projectSaaSCosts(cfg JobConfig, sourceWidth, fps int, durationS float64, hasAudio bool, srcMbps float64) (commercial, mediaconvert, coconut, bitmovin float64) {
	if m.Ladders == nil || durationS <= 0 {
		return 0, 0, 0, 0
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
			coconut += minutes * _coconutRate(r.Height)
			bitmovin += minutes * _bitmovinBasePerMin *
				_bitmovinResFactor(r.Height) * _bitmovinCodecFactor(c) *
				_bitmovinPassFactor(c == "hevc" && !cfg.HevcSinglePass)
		}
	}
	if hasAudio {
		commercial += minutes * _commercialAudioPerMin
		coconut += minutes * _coconutAudio
	}
	return commercial, mediaconvert, coconut, bitmovin
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
	commercial, mediaconvert, coconut, bitmovin := m.projectSaaSCosts(job.Config, width, fps, dur, hasAudio, srcMbps)

	job.mu.Lock()
	if commercial > 0 {
		job.CommercialUSD = commercial
	}
	if mediaconvert > 0 {
		job.MediaConvertUSD = mediaconvert
	}
	if coconut > 0 {
		job.CoconutUSD = coconut
	}
	if bitmovin > 0 {
		job.BitmovinUSD = bitmovin
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

// EstimateResult is a pre-flight cost projection for a config that has not been
// submitted. Same machinery projectAndSetCosts uses after a job starts — the
// only difference is that it probes a filename instead of reading a Job.
type EstimateResult struct {
	DurationS   float64 `json:"duration_s"`
	Width       int     `json:"width"`
	Fps         int     `json:"fps"`
	SpotUSD     float64 `json:"spot_usd"`
	OnDemandUSD float64 `json:"ondemand_usd"`
	// Egress the run would pay to bring output home. Zero when the config
	// leaves media in S3 (#214) — which is most of the point of showing this
	// before you press the button rather than after.
	EgressGB  float64 `json:"egress_gb"`
	EgressUSD float64 `json:"egress_usd"`
	TotalUSD  float64 `json:"total_usd"`
	// SaaS baselines for the same ladder, so the comparison is visible at the
	// moment of choosing rather than only in the finished job.
	CommercialUSD   float64 `json:"commercial_usd"`
	MediaConvertUSD float64 `json:"mediaconvert_usd"`
	CoconutUSD      float64 `json:"coconut_usd"`
	BitmovinUSD     float64 `json:"bitmovin_usd"`
	// The fleet-idle allowance folded into SpotUSD/OnDemandUSD, surfaced so the
	// UI can state it. IdleMeasured false means IdlePct is the documented
	// assumption, not an observation, and the UI must say which — an allowance
	// nobody can see is the defect this fixes, not the fix (#237).
	//
	// PERCENT (0-100), matching ENCODER-MACHINES' unallocated_pct and what the
	// UI prints, not the 0-1 fraction the arithmetic uses.
	IdlePct      float64 `json:"idle_pct"`
	IdleRuns     int     `json:"idle_runs"`
	IdleMeasured bool    `json:"idle_measured"`
	// Local targets write straight to disk: no egress, no AWS spend at all.
	Local bool `json:"local"`
}

// EstimateCost projects what a config WOULD cost, before it is submitted.
//
// Deliberately reuses projectCloudCost / projectSaaSCosts rather than
// approximating in JavaScript: an estimate that disagrees with the figure the
// finished job reports is worse than no estimate, because the difference reads
// as a bug in the encode rather than in the estimator.
//
// Probes the source with ffprobe, so it costs one subprocess per distinct file.
// Callers should debounce.
func (m *Manager) EstimateCost(cfg JobConfig) (EstimateResult, error) {
	var out EstimateResult
	if len(cfg.Files) == 0 {
		return out, fmt.Errorf("no files selected")
	}
	// Sum durations across every selected file — the form can select several,
	// and quoting only the first would understate a batch.
	var totalDur float64
	for _, f := range cfg.Files {
		src := filepath.Join(m.SourceDir, f)
		d, err := probeDurationSeconds(src)
		if err != nil || d <= 0 {
			continue
		}
		totalDur += d
		if out.Width == 0 {
			out.Width, out.Fps = probeSourceWidth(src), probeSourceFps(src)
		}
	}
	if totalDur <= 0 {
		return out, fmt.Errorf("could not probe duration")
	}
	// A --time limit caps what actually gets encoded, per file.
	if lim, err := strconv.ParseFloat(cfg.Time, 64); err == nil && lim > 0 {
		capped := lim * float64(len(cfg.Files))
		if capped < totalDur {
			totalDur = capped
		}
	}
	out.DurationS = totalDur

	src0 := filepath.Join(m.SourceDir, cfg.Files[0])
	hasAudio, srcMbps := probeHasAudio(src0), probeSourceMbps(src0)
	out.CommercialUSD, out.MediaConvertUSD, out.CoconutUSD, out.BitmovinUSD =
		m.projectSaaSCosts(cfg, out.Width, out.Fps, totalDur, hasAudio, srcMbps)

	out.Local = cfg.Target != "cloud" && cfg.Target != "cloud-batch"
	if out.Local {
		// Nothing is billed: local and local-dist write to disk. Say $0 rather
		// than omitting the line, so the contrast with cloud is visible.
		return out, nil
	}

	var idle float64
	out.SpotUSD, out.OnDemandUSD, idle, out.IdleRuns, out.IdleMeasured =
		m.projectCloudCostDetail(cfg, out.Width, out.Fps, totalDur)
	out.IdlePct = idle * 100
	// Output size = the ladder's total bitrate x duration. The same bytes drive
	// egress and (via the fitted per-GB rate) the Tier1 estimate, so a run that
	// leaves media in S3 correctly shows near-zero here.
	if !m.skipMediaDownload(cfg) {
		out.EgressGB = m.ladderOutputGB(cfg, out.Width, totalDur)
		out.EgressUSD = out.EgressGB * EgressUSDPerGB
	}
	out.TotalUSD = out.SpotUSD + out.EgressUSD
	return out, nil
}

// ladderOutputGB is the ladder's summed bitrate over the duration — what the
// encode will actually produce, and therefore what would be transferred.
func (m *Manager) ladderOutputGB(cfg JobConfig, sourceWidth int, durationS float64) float64 {
	var kbps int
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	if m.Ladders == nil {
		return 0
	}
	for _, codec := range parseCodecSel(cfg.Codec) {
		for _, r := range m.Ladders.resolveRungs(ladderName, codec, cfg.MaxRes, cfg.MinRes, sourceWidth) {
			kbps += r.Bitrate
		}
	}
	return float64(kbps) * 1000 * durationS / 8 / 1e9
}
