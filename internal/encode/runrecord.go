package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// RunRecordFile is the per-output run record, written next to encode.json.
//
// encode.json answers "what profile made this rendition" — it is about the
// ARTIFACT, and every field in it is derivable from the config plus the files
// on disk. This answers "what run made this", which is not derivable from
// anything afterwards: stage timings, cost, the machines, the config that was
// actually submitted. All of that lives in the in-memory Job and Reconcile
// rebuilds only ACTIVE jobs, so a server restart takes it with it — the raw
// text log and a history.md entry keyed by job id are what survive today, and
// neither is reachable from an output dir.
//
// Written at move time from the same hook as encode.json, so a promoted copy
// carries it too. Best-effort: an output with no run.json renders exactly as it
// did before this existed, the same way a missing encode.json does.
const RunRecordFile = "run.json"

// runRecordSchema is bumped when a field changes meaning (not when one is
// added — a reader that ignores unknown fields is unaffected by growth). It is
// here so a record written by a future server is identifiable as such rather
// than being read with today's assumptions.
const runRecordSchema = 1

// RunCost mirrors the Job cost fields ONE FOR ONE, including their JSON names,
// so the page's existing cost renderer takes this struct unmodified. Two
// renderers for the same numbers is how a run's cost ends up reported one way
// in the Jobs tab and another in Outputs.
type RunCost struct {
	SpotUSD         float64 `json:"spot_usd,omitempty"`
	EgressUSD       float64 `json:"egress_usd,omitempty"`
	EgressGB        float64 `json:"egress_gb,omitempty"`
	EgressAvoidedGB float64 `json:"egress_avoided_gb,omitempty"`
	SfnUSD          float64 `json:"sfn_usd,omitempty"`
	RequestUSD      float64 `json:"request_usd,omitempty"`
	StorageUSD      float64 `json:"storage_usd,omitempty"`
	PutEstUSD       float64 `json:"put_est_usd,omitempty"`
	TotalUSD        float64 `json:"total_usd,omitempty"`
	SavedUSD        float64 `json:"saved_usd,omitempty"`
	CostUnmodelled  string  `json:"cost_unmodelled,omitempty"`
}

// RunEfficiency is the fleet-side view of the same run: what compute it used
// against what it reserved. Same one-for-one JSON naming as RunCost, and the
// durable home #94 asks for these numbers to have.
type RunEfficiency struct {
	EncodeWallS    float64 `json:"encode_wall_s,omitempty"`
	CPUVcpuH       float64 `json:"cpu_vcpu_h,omitempty"`
	AvgConcurrency float64 `json:"avg_concurrency,omitempty"`
	EfficiencyPct  float64 `json:"efficiency_pct,omitempty"`
	SlowestJob     string  `json:"slowest_job,omitempty"`
	SlowestJobS    float64 `json:"slowest_job_s,omitempty"`
	JobCount       int     `json:"job_count,omitempty"`
	MaxVcpus       int     `json:"max_vcpus,omitempty"`
	Instances      int     `json:"instances,omitempty"`
	// Machine rental vs allocation — the work-you-got-for-what-you-paid view,
	// and the only one here whose denominator is the bill.
	//
	// EfficiencyPct above is AvgConcurrency ÷ MaxVcpus: how full the fleet ran
	// against the ceiling you configured. That is a real number but it is not a
	// property of the run — raise max_vcpus and it halves with nothing else
	// changing. These three are anchored to what AWS actually charged for:
	// MachineVCPUHours is instance lifetimes (launch to termination),
	// AllocatedVCPUHours is the sum of each job's reservation, and IdlePct is
	// the share of rented time with no job on it at all.
	//
	// Persisted here because they were already computed per run and written
	// only to $TMP_DIR/spot_samples.json, which is keyed by time and not by
	// output — so the one place someone asks "what did this encode cost me in
	// machine time" could not answer. Same #94 reasoning as the fields above.
	//
	// IdlePct is a LOWER BOUND: boxes still alive at terminal have their
	// lifetime measured to now, and the scale-down tail after that is never
	// seen. It errs low, which under-states idle rather than inventing it.
	MachineVCPUHours   float64 `json:"machine_vcpu_hours,omitempty"`
	AllocatedVCPUHours float64 `json:"allocated_vcpu_hours,omitempty"`
	IdlePct            float64 `json:"idle_pct,omitempty"`
	LocalWallS         float64 `json:"local_wall_s,omitempty"`
	ReclaimCount       int     `json:"reclaim_count,omitempty"`
	ReclaimLostS       float64 `json:"reclaim_lost_s,omitempty"`
	EncodeTotalS       float64 `json:"encode_total_s,omitempty"`
}

// RunRecord is what one output dir's encode run was.
//
// Scoped to THIS dir, not the job: a job encoding h264 and hevc writes two
// dirs, and each gets its own phases, rungs and VMAF. An hevc output claiming
// the h264 encode time would be a plausible-looking lie, and the numbers most
// worth having here are exactly the ones people compare between runs.
type RunRecord struct {
	SchemaVersion int        `json:"schema_version"`
	JobID         string     `json:"job_id"`
	Status        string     `json:"status"`
	Target        Target     `json:"target"`
	Codec         string     `json:"codec,omitempty"`
	Source        string     `json:"source,omitempty"`
	StartedAt     time.Time  `json:"started_at"`
	EndedAt       *time.Time `json:"ended_at,omitempty"`
	WallS         float64    `json:"wall_s,omitempty"`
	Error         string     `json:"error,omitempty"`
	// LogFile is the per-job log's name under TmpDir/logs, served at
	// /logs/<name>. The record is the index into the raw log, not a replacement
	// for it — everything not summarised here is still in that file.
	LogFile string `json:"log_file,omitempty"`
	// Rungs are the rungs this dir's codec ACTUALLY encoded, read back from
	// stage keys. The ladder NAME does not answer this: ladders are editable
	// through the API after the fact, and MaxRes/MinRes narrow them per job.
	Rungs []string `json:"rungs,omitempty"`
	// Config is the COMPLETE JobConfig with defaults resolved — the body that
	// would resubmit this encode through POST /api/encode. Marshalled whole
	// rather than field-picked, so an option added tomorrow is recorded from
	// the day its field lands. See EffectiveConfig.
	//
	// A POINTER because absent has to be distinguishable from empty. A record
	// reconstructed from a source that never captured the config (see
	// RunRecovery) would otherwise marshal `{}` here, and an empty config is a
	// perfectly valid-looking config — a reader would take defaults for facts,
	// which is #202 with extra steps.
	Config *JobConfig  `json:"config,omitempty"`
	Phases []PhaseStat `json:"phases,omitempty"`
	// Stages is the per-chunk detail behind Phases: which chunk ran when, on
	// which machine, and whether it was reused from an earlier run. What the
	// timeline is drawn from, and the reason it survives a restart at all —
	// this only ever existed in the running Job before.
	Stages []StageProgress `json:"stages,omitempty"`
	// FfmpegArgv is the literal command per rung (see Job.FfmpegArgv), narrowed
	// to this output's codec.
	FfmpegArgv map[string][]string   `json:"ffmpeg_argv,omitempty"`
	Cost       *RunCost              `json:"cost,omitempty"`
	Efficiency *RunEfficiency        `json:"efficiency,omitempty"`
	Vmaf       map[string]*VmafScore `json:"vmaf,omitempty"`
	BootAMI    string                `json:"boot_ami,omitempty"`
	// Recovered is set ONLY on a record reconstructed after the fact. Absent
	// means the run wrote this itself. See RunRecovery.
	Recovered *RunRecovery `json:"recovered,omitempty"`
}

// RunRecovery marks a record that was reconstructed from a secondary source
// rather than written by the run that produced the output, and states what that
// source could not supply.
//
// Present because a reconstructed record is otherwise indistinguishable from a
// first-hand one — same filename, same fields, same shape — and the difference
// matters the moment anyone compares two runs. Missing is spelled out rather
// than left as absent fields for the same reason encode.json says h264 has no
// library entry BY DESIGN: an unexplained gap reads as "we forgot", and someone
// eventually treats it as zero.
type RunRecovery struct {
	// From names the source, e.g. "history.md".
	From string `json:"from"`
	// At is when the reconstruction ran (RFC3339), not when the encode ran.
	At string `json:"at"`
	// Missing lists what this source cannot supply at all — "cost",
	// "efficiency", "config", "phases".
	Missing []string `json:"missing,omitempty"`
	// Caveats are things present but weaker than a first-hand record's, e.g.
	// phases that cover a whole multi-file job rather than this output's file.
	Caveats []string `json:"caveats,omitempty"`
}

// EffectiveConfig resolves the three fields whose absence does NOT mean "no
// value" — it means a default applied somewhere else — and returns a config
// safe to record as the run's own settings.
//
// Shared by history.md's config block and the run record so the two records of
// the same run cannot disagree. #202 was a rerun rebuilt from a config record
// that was a subset of the config: it silently used the default ladder, and
// then — after the ladder was added — silently used dynamic chunking instead of
// the fixed 12s, cutting 41 chunks where the run being reproduced cut 336.
func EffectiveConfig(cfg JobConfig) JobConfig {
	cfg.Ladder = EffectiveLadder(cfg)
	// "" -> "dynamic" only. NOT chunkModeLabel, which renders a fixed size as
	// "12s" — a display string that fails ParseFloat on the way back in and
	// silently becomes the default. Every value written here must be one
	// variantChunkSeconds accepts, or the record does not round-trip.
	if cfg.ChunkDuration == "" {
		cfg.ChunkDuration = "dynamic"
	}
	burnin := cfg.BurninEnabled()
	cfg.Burnin = &burnin
	return cfg
}

// stageCodec picks the codec out of a stage key ("encode:hevc:1080p:chunk3",
// "package:hevc"), or "" for a phase that serves every codec (mezzanine,
// audio, upload:source).
//
// Scans for a known codec token rather than matching per-prefix, so a stage key
// shape added on the Python side is classified correctly without this needing
// to have heard of it. The failure mode of the alternative is silent: an
// unrecognised key would read as shared and be attributed to every codec's dir.
func stageCodec(key string) string {
	for _, p := range strings.Split(key, ":") {
		switch p {
		case "h264", "hevc", "av1":
			return p
		}
	}
	return ""
}

// sourceForDir returns the config file that produced dirName, or "" when the
// job has no single unambiguous source for it. Match is by OutputStem prefix —
// the same naming contract parseOutputMeta and the watcher read.
func sourceForDir(cfg JobConfig, dirName string) string {
	switch len(cfg.Files) {
	case 0:
		return ""
	case 1:
		return cfg.Files[0]
	}
	for _, f := range cfg.Files {
		if strings.HasPrefix(dirName, cfg.OutputStem(filepath.Base(f))) {
			return f
		}
	}
	return ""
}

// rungsForCodec lists the rungs one codec encoded, tallest first, from the
// job's stage keys. The chunk suffix is dropped, so a rung appears once however
// many chunks it was cut into.
func rungsForCodec(phases []PhaseStat, codec string) []string {
	seen := map[string]int{}
	for _, p := range phases {
		parts := strings.Split(p.Phase, ":")
		if len(parts) < 3 || parts[0] != "encode" || parts[1] != codec {
			continue
		}
		label := parts[2]
		h, err := strconv.Atoi(strings.TrimSuffix(strings.SplitN(label, "_", 2)[0], "p"))
		if err != nil {
			h = 0
		}
		seen[label] = h
	}
	if len(seen) == 0 {
		return nil
	}
	out := make([]string, 0, len(seen))
	for label := range seen {
		out = append(out, label)
	}
	sort.Slice(out, func(i, j int) bool {
		if seen[out[i]] != seen[out[j]] {
			return seen[out[i]] > seen[out[j]]
		}
		return out[i] < out[j]
	})
	return out
}

// vmafForCodec narrows the job's aggregated VMAF map to one codec's rungs.
func vmafForCodec(vmaf map[string]*VmafScore, codec string) map[string]*VmafScore {
	if len(vmaf) == 0 || codec == "" {
		return nil
	}
	out := map[string]*VmafScore{}
	for k, v := range vmaf {
		if v != nil && strings.HasPrefix(k, codec+"/") {
			out[k] = v
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// argvForCodec narrows the job's recorded ffmpeg commands to one codec's rungs.
// Keys are "<codec>/<rung>" with an optional " (pass N)" suffix, so the codec
// prefix is the whole test.
func argvForCodec(argv map[string][]string, codec string) map[string][]string {
	if len(argv) == 0 || codec == "" {
		return nil
	}
	out := map[string][]string{}
	for k, v := range argv {
		if strings.HasPrefix(k, codec+"/") {
			out[k] = v
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// buildRunRecord assembles one output dir's record from the finished job.
// Exported behaviour is tested through this rather than the file write, so the
// attribution rules (which phases, which rungs, which source) are checked
// without a filesystem.
func buildRunRecord(job *Job, cfg JobConfig, dirName string) RunRecord {
	codec := ""
	for _, c := range []string{"h264", "hevc", "av1"} {
		if strings.Contains(dirName, "_"+c) {
			codec = c
			break
		}
	}
	source := sourceForDir(cfg, dirName)
	// Only filter by file when there is more than one to tell apart; a
	// single-file job's stages carry a label the config may spell differently
	// (basename vs path) and filtering on it could empty the rollup.
	fileFilter := ""
	if len(cfg.Files) > 1 {
		fileFilter = source
	}
	phases := job.PhaseRollupFor(fileFilter, codec)

	job.mu.Lock()
	status, jobErr, startedAt, endedAt := string(job.Status), job.Error, job.StartedAt, job.EndedAt
	bootAMI := job.BootAMI
	cost := RunCost{
		SpotUSD: job.SpotUSD, EgressUSD: job.EgressUSD, EgressGB: job.EgressGB,
		EgressAvoidedGB: job.EgressAvoidedGB, SfnUSD: job.SfnUSD,
		RequestUSD: job.RequestUSD, StorageUSD: job.StorageUSD,
		PutEstUSD: job.PutEstUSD, TotalUSD: job.TotalUSD, SavedUSD: job.SavedUSD,
		CostUnmodelled: job.CostUnmodelled,
	}
	eff := RunEfficiency{
		EncodeWallS: job.EncodeWallS, CPUVcpuH: job.CPUVcpuH,
		AvgConcurrency: job.AvgConcurrency, EfficiencyPct: job.EfficiencyPct,
		SlowestJob: job.SlowestJob, SlowestJobS: job.SlowestJobS,
		JobCount: job.JobCount, MaxVcpus: job.MaxVcpus, Instances: job.Instances,
		MachineVCPUHours:   job.MachineVCPUHours,
		AllocatedVCPUHours: job.AllocatedVCPUHours, IdlePct: job.IdlePct,
		LocalWallS: job.LocalWallSeconds, ReclaimCount: job.ReclaimCount,
		ReclaimLostS: job.ReclaimLostS, EncodeTotalS: job.EncodeTotalS,
	}
	vmaf := vmafForCodec(job.Vmaf, codec)
	argv := argvForCodec(job.FfmpegArgv, codec)
	job.mu.Unlock()

	effCfg := EffectiveConfig(cfg)
	rec := RunRecord{
		SchemaVersion: runRecordSchema,
		JobID:         job.ID,
		Status:        status,
		Target:        cfg.Target,
		Codec:         codec,
		Source:        source,
		StartedAt:     startedAt,
		EndedAt:       endedAt,
		Error:         jobErr,
		LogFile:       job.ID + ".log",
		Rungs:         rungsForCodec(phases, codec),
		Config:        &effCfg,
		Phases:        phases,
		Stages:        job.StagesFor(fileFilter, codec),
		FfmpegArgv:    argv,
		Vmaf:          vmaf,
		BootAMI:       bootAMI,
	}
	if endedAt != nil {
		rec.WallS = endedAt.Sub(startedAt).Seconds()
	}
	// Cost and efficiency are cloud-only and stay absent rather than being
	// written as a wall of zeros — a $0.00 total on a local run reads as "this
	// was free", which is a different claim from "not applicable".
	if cost != (RunCost{}) {
		rec.Cost = &cost
	}
	if eff != (RunEfficiency{}) {
		rec.Efficiency = &eff
	}
	return rec
}

// writeRunRecord writes one output dir's run.json. Best-effort — a record is
// never worth failing a finished encode over.
func (m *Manager) writeRunRecord(dirName string, cfg JobConfig, job *Job) {
	if job == nil {
		return
	}
	dir := filepath.Join(m.OutputDir, dirName)
	if fi, err := os.Stat(dir); err != nil || !fi.IsDir() {
		return
	}
	b, err := json.MarshalIndent(buildRunRecord(job, cfg, dirName), "", "  ")
	if err != nil {
		return
	}
	_ = os.WriteFile(filepath.Join(dir, RunRecordFile), b, 0644)
}

// ReadRunRecord loads an output dir's run record, or nil when it has none
// (every output encoded before this shipped, and any dir whose write failed).
// Callers must degrade to the pre-record view rather than reporting an error:
// absent is the normal case for old outputs, not a fault.
func ReadRunRecord(dir string) *RunRecord {
	b, err := os.ReadFile(filepath.Join(dir, RunRecordFile))
	if err != nil {
		return nil
	}
	var rec RunRecord
	if err := json.Unmarshal(b, &rec); err != nil {
		return nil
	}
	return &rec
}
