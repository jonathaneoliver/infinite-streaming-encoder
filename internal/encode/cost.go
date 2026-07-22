package encode

import (
	"path/filepath"
	"strconv"
)

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

// projectAndSetCloudCost probes the job's first source file and stores the
// compute-based AWS Batch (Graviton) cost projection on the Job. Runs for every
// target — "what would this cost on our cloud fleet?" is a target-independent
// comparison — and is idempotent (a reattach recomputes the same value). This is
// authoritative for AwsSpotUSD / AwsOndemandUSD; the Python ENCODER-COMMERCIAL
// marker now only supplies the SaaS commercial + MediaConvert baselines. Best-
// effort: a probe failure or unsized ladder leaves the fields untouched.
func (m *Manager) projectAndSetCloudCost(job *Job) {
	if len(job.Config.Files) == 0 {
		return
	}
	src := filepath.Join(m.SourceDir, job.Config.Files[0])
	dur, err := probeDurationSeconds(src)
	if err != nil || dur <= 0 {
		return
	}
	spot, ondemand := m.projectCloudCost(job.Config, probeSourceWidth(src), probeSourceFps(src), dur)
	if spot <= 0 {
		return
	}
	job.mu.Lock()
	job.AwsSpotUSD = spot
	job.AwsOndemandUSD = ondemand
	job.mu.Unlock()
	m.notify(job)
}
