package encode

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]|\x1b[()][0-9A-B]|\x1b\][^\x07]*\x07|\r`)

func stripANSI(s string) string {
	return ansiRe.ReplaceAllString(s, "")
}

// Markers the Python orchestrator emits for structured progress. See
// scripts/encoder/progress.py for the producer side.
var (
	planMarkerRe  = regexp.MustCompile(`^\[\[ENCODER-PLAN (.+)\]\]$`)
	stageMarkerRe = regexp.MustCompile(`^\[\[ENCODER-STAGE key=(\S+) status=(\S+) percent=([0-9.]+)\]\]$`)
	// ENCODER-FILE signals the start of a per-file encode within a
	// multi-file job. Emitted by the Go server for local encodes (one
	// worker per file) and by the remote userdata bash loop for cloud
	// batches (one worker, many clips). Handling: archive the current
	// Stages into StagesHistory (so end-of-job timing still has all
	// phases), clear Stages, and stamp CurrentFile / FileIndex / TotalFiles.
	fileMarkerRe = regexp.MustCompile(`^\[\[ENCODER-FILE index=(\d+) total=(\d+) name=(.+)\]\]$`)
)

// parseMarker returns true when the line was a recognised progress marker
// (and was applied to the job), so the caller can skip appending it to
// the raw log buffer.
func (j *Job) parseMarker(line string) bool {
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
	if m := stageMarkerRe.FindStringSubmatch(line); m != nil {
		key, status := m[1], m[2]
		percent, _ := strconv.ParseFloat(m[3], 64)
		now := time.Now()
		j.mu.Lock()
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
				break
			}
		}
		j.mu.Unlock()
		return true
	}
	if m := fileMarkerRe.FindStringSubmatch(line); m != nil {
		idx, _ := strconv.Atoi(m[1])
		total, _ := strconv.Atoi(m[2])
		j.startFile(strings.TrimSpace(m[3]), idx, total)
		return true
	}
	return false
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
	TargetCloud Target = "cloud"
	TargetLocal Target = "local"
)

type JobStatus string

const (
	StatusQueued    JobStatus = "queued"
	StatusRunning   JobStatus = "running"
	StatusDone      JobStatus = "done"
	StatusFailed    JobStatus = "failed"
	StatusCancelled JobStatus = "cancelled"
)

type JobConfig struct {
	Files           []string `json:"files"`
	Codec           string   `json:"codec"`
	MaxRes          string   `json:"max_res"`
	Target          Target   `json:"target"`
	Time            string   `json:"time"`
	SegmentDuration string   `json:"segment_duration"`
	PartialDuration string   `json:"partial_duration"`
	GopDuration     string   `json:"gop_duration"`
	HlsFormat       string   `json:"hls_format"`
	Padding         string   `json:"padding"`
	KeepMezzanine   bool     `json:"keep_mezzanine"`
	ForceReencode   bool     `json:"force_reencode"`
	// CPU architecture for cloud encodes: "intel" | "amd" | "graviton".
	// Empty defaults to intel. Ignored for local encodes (which always
	// run on the host's native architecture).
	CpuArch string `json:"cpu_arch,omitempty"`
	// UseSpot controls EC2 purchasing mode for cloud encodes. Pointer
	// so `omitempty` works and an unset value lets cli_cloud.py apply
	// its env-var default (USE_SPOT=true). Ignored for local encodes.
	UseSpot *bool `json:"use_spot,omitempty"`
}

type StageProgress struct {
	Key     string  `json:"key"`
	Label   string  `json:"label"`
	Status  string  `json:"status"` // pending | running | done | failed
	Percent float64 `json:"percent"`
	// Timestamps of state transitions. StartedAt is set the first time
	// the stage sees `status=running`; EndedAt is set when it reaches
	// a terminal state (done|failed). Used to build the end-of-job
	// timing summary so the user can see where the wall-clock went.
	StartedAt *time.Time `json:"started_at,omitempty"`
	EndedAt   *time.Time `json:"ended_at,omitempty"`
}

type Job struct {
	ID        string     `json:"id"`
	Config    JobConfig  `json:"config"`
	Status    JobStatus  `json:"status"`
	StartedAt time.Time  `json:"started_at"`
	EndedAt   *time.Time `json:"ended_at,omitempty"`
	Progress  string     `json:"progress"`
	Error     string     `json:"error,omitempty"`

	// Stages is populated from [[ENCODER-PLAN]] + [[ENCODER-STAGE]]
	// markers the Python orchestrator emits. Empty when the job hasn't
	// reached the Python side yet (still queued, pre-run) or for old
	// jobs that pre-date the marker format. For multi-file jobs,
	// Stages reflects ONLY the currently-processing file; finished
	// files land in StagesHistory.
	Stages []StageProgress `json:"stages,omitempty"`

	// Per-file progress indicators. CurrentFile/FileIndex/TotalFiles
	// populate on the first [[ENCODER-FILE]] marker (or the Go loop's
	// direct call for local encodes). UI renders "File N of M: name"
	// above the stages table.
	CurrentFile      string `json:"current_file,omitempty"`
	CurrentFileIndex int    `json:"current_file_index,omitempty"`
	TotalFiles       int    `json:"total_files,omitempty"`

	// StagesHistory holds the finished files' stages so end-of-job
	// timing can show every phase that ran, not just the last file's.
	StagesHistory []FileStages `json:"stages_history,omitempty"`

	mu        sync.Mutex
	logLines  []string
	cancelled bool
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

func (j *Job) AppendLog(line string) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.logLines = append(j.logLines, line)
	if len(j.logLines) > 1000 {
		j.logLines = j.logLines[len(j.logLines)-500:]
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

	sem         chan struct{}
	subscribers []chan *Job
	subMu       sync.Mutex
}

type ManagerConfig struct {
	SourceDir     string
	OutputDir     string
	TmpDir        string
	ScriptsDir    string
	DockerImage   string
	HostSourceDir string
	HostOutputDir string
	HostTmpDir    string
	HostAWSDir    string
	EncoderImage  string
	MaxConcurrent int
}

func NewManager(cfg ManagerConfig) *Manager {
	if cfg.MaxConcurrent < 1 {
		cfg.MaxConcurrent = 1
	}
	if cfg.EncoderImage == "" {
		cfg.EncoderImage = "encoder:latest"
	}
	os.MkdirAll(cfg.TmpDir, 0755)
	return &Manager{
		SourceDir:     cfg.SourceDir,
		OutputDir:     cfg.OutputDir,
		TmpDir:        cfg.TmpDir,
		ScriptsDir:    cfg.ScriptsDir,
		DockerImage:   cfg.DockerImage,
		HostSourceDir: cfg.HostSourceDir,
		HostOutputDir: cfg.HostOutputDir,
		HostTmpDir:    cfg.HostTmpDir,
		HostAWSDir:    cfg.HostAWSDir,
		EncoderImage:  cfg.EncoderImage,
		sem:           make(chan struct{}, cfg.MaxConcurrent),
	}
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
	if startIdx == 0 {
		job.Progress = "waiting for slot"
	} else {
		job.Progress = fmt.Sprintf("resuming at file %d/%d", startIdx+1, len(job.Config.Files))
	}
	m.notify(job)
	m.sem <- struct{}{}
	defer func() { <-m.sem }()

	// If cancel fired while this job was queued, skip straight to cleanup.
	if job.IsCancelled() {
		m.finalizeCancelled(job, filepath.Join(m.TmpDir, job.ID))
		return
	}

	job.Status = StatusRunning
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

	script := filepath.Join(m.ScriptsDir, "encoder", "cli_local.py")
	if job.Config.Target == TargetCloud {
		script = filepath.Join(m.ScriptsDir, "encoder", "cli_cloud.py")
	}

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
		if mvErr := m.moveTmpToOutput(jobTmpDir, job.Config.ForceReencode); mvErr != nil {
			job.Status = StatusFailed
			job.Error = "encode succeeded but move failed: " + mvErr.Error()
			job.Progress = "failed"
		} else {
			job.Status = StatusDone
			job.Progress = "complete"
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
	m.writeHistory(job)
	m.notify(job)
}

// preserveTmpForFailure moves jobTmpDir to $TMP_DIR/failed/<job_id>/ so the
// user can inspect user-data.log, half-encoded variant MP4s, etc. after
// the job exits.
func (m *Manager) preserveTmpForFailure(job *Job, jobTmpDir string) {
	if _, err := os.Stat(jobTmpDir); os.IsNotExist(err) {
		return
	}
	failedRoot := filepath.Join(m.TmpDir, "failed")
	os.MkdirAll(failedRoot, 0755)
	dst := filepath.Join(failedRoot, job.ID)
	// Collision shouldn't happen (job IDs are monotonic epoch ms) but
	// be defensive — rename the existing one out of the way.
	if _, err := os.Stat(dst); err == nil {
		_ = os.Rename(dst, dst+".old."+fmt.Sprint(time.Now().Unix()))
	}
	if err := os.Rename(jobTmpDir, dst); err != nil {
		// Cross-device or permission issue — fall back to copy + remove.
		if cpErr := copyDir(jobTmpDir, dst); cpErr == nil {
			os.RemoveAll(jobTmpDir)
		}
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

	// Graceful stop runs in the background so the UI's POST returns
	// immediately and the Cancel action feels instant. `docker stop
	// --time 30` sends SIGTERM, waits up to 30s for the container to
	// exit, then SIGKILLs if it didn't. boto3 TerminateInstances +
	// CancelSpotInstanceRequests typically complete in 2-5s, so the
	// budget is generous. After stop, `docker rm` cleans up the
	// container record so runFileContainer's reattach-if-exists path
	// can't pick it up on a subsequent retry.
	go func() {
		for _, name := range names {
			exec.Command("docker", "stop", "--time", "30", name).Run()
			exec.Command("docker", "rm", name).Run()
		}
	}()
	return true
}

func (m *Manager) writeHistory(job *Job) {
	logsDir := filepath.Join(m.TmpDir, "logs")
	os.MkdirAll(logsDir, 0755)

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
	if job.Config.MaxRes != "" {
		fmt.Fprintf(f, "- **Max Res:** %s\n", job.Config.MaxRes)
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

	// Timing summary — per-stage wall-clock, so the user can see
	// where the job's total duration went. Particularly useful for
	// cloud jobs: how long was spent on boot vs docker pull vs
	// encode vs S3 sync — informs parallelisation decisions when
	// running multiple encodes.
	m.writeTimingSummary(f, job)

	fmt.Fprintf(f, "\n---\n\n")
}

func (m *Manager) writeTimingSummary(f *os.File, job *Job) {
	if len(job.Stages) == 0 && len(job.StagesHistory) == 0 {
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
	for _, h := range job.StagesHistory {
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
	for _, s := range job.Stages {
		rows = append(rows, stageRow{fileLabel: currentLabel, stage: s})
	}

	// Also dump the same summary into the per-job log file so it's
	// trivially greppable later.
	var table strings.Builder
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

func (m *Manager) moveTmpToOutput(tmpDir string, archive bool) error {
	entries, err := os.ReadDir(tmpDir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		src := filepath.Join(tmpDir, e.Name())
		dst := filepath.Join(m.OutputDir, e.Name())

		if _, statErr := os.Stat(dst); statErr == nil {
			if archive {
				archiveDir := filepath.Join(m.OutputDir, ".archive")
				os.MkdirAll(archiveDir, 0755)
				ts := time.Now().Format("20060102_150405")
				archiveDst := filepath.Join(archiveDir, e.Name()+"_"+ts)
				os.Rename(dst, archiveDst)
			} else {
				os.RemoveAll(dst)
			}
		}

		if err := os.Rename(src, dst); err != nil {
			if cpErr := copyDir(src, dst); cpErr != nil {
				return fmt.Errorf("move %s: %w", e.Name(), cpErr)
			}
			os.RemoveAll(src)
		}
	}
	return nil
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
	return stem
}

func (cfg *JobConfig) encodeArgsForFile(sourceDir, outputDir, filename string) []string {
	args := []string{
		"--input", sourceDir + "/" + filename,
		"--output", cfg.OutputStem(filename),
		"--output-dir", outputDir,
	}
	if cfg.Codec != "" {
		args = append(args, "--codec", cfg.Codec)
	}
	if cfg.MaxRes != "" {
		args = append(args, "--max-res", cfg.MaxRes)
	}
	if cfg.Time != "" {
		args = append(args, "--time", cfg.Time)
	}
	if cfg.SegmentDuration != "" {
		args = append(args, "--segment-duration", cfg.SegmentDuration)
	}
	if cfg.PartialDuration != "" {
		args = append(args, "--partial-duration", cfg.PartialDuration)
	}
	if cfg.GopDuration != "" {
		args = append(args, "--gop-duration", cfg.GopDuration)
	}
	if cfg.HlsFormat != "" {
		args = append(args, "--hls-format", cfg.HlsFormat)
	}
	switch cfg.Padding {
	case "black":
		args = append(args, "--padding")
	case "pink":
		args = append(args, "--padding-pink")
	case "none":
		args = append(args, "--no-padding")
	}
	if cfg.KeepMezzanine {
		args = append(args, "--keep-mezzanine")
	}
	// CPU arch only makes sense for cloud encodes (local runs on the
	// host's own architecture), and cli_local.py doesn't accept the
	// flag. Gate by target so we don't hand an unknown arg to argparse.
	if cfg.Target == TargetCloud && cfg.CpuArch != "" {
		args = append(args, "--cpu-arch", cfg.CpuArch)
	}
	if cfg.Target == TargetCloud && cfg.UseSpot != nil && !*cfg.UseSpot {
		args = append(args, "--no-spot")
	}
	return args
}

// cloudBatchArgs builds the CLI for cli_cloud.py when it's handling
// multiple clips in one invocation. Differences vs encodeArgsForFile:
//
//   - `--input` appears once per filename (cli_cloud.py uses
//     action="append" on that flag)
//   - `--output` is omitted — each clip's output stem is derived on
//     the remote inside the user-data loop as `${stem}_p200`
//   - `--output-dir` is the local tmp dir where outputs sync back
//     before moveTmpToOutput picks them up
func (cfg *JobConfig) cloudBatchArgs(sourceDir, outputDir string, filenames []string) []string {
	var args []string
	for _, f := range filenames {
		args = append(args, "--input", sourceDir+"/"+f)
	}
	args = append(args, "--output-dir", outputDir)

	// These get passed through cli_cloud.py to the remote cli_local.py
	// via the parse_known_args passthrough channel — same flags the
	// local path uses.
	if cfg.Codec != "" {
		args = append(args, "--codec", cfg.Codec)
	}
	if cfg.MaxRes != "" {
		args = append(args, "--max-res", cfg.MaxRes)
	}
	if cfg.Time != "" {
		args = append(args, "--time", cfg.Time)
	}
	if cfg.SegmentDuration != "" {
		args = append(args, "--segment-duration", cfg.SegmentDuration)
	}
	if cfg.PartialDuration != "" {
		args = append(args, "--partial-duration", cfg.PartialDuration)
	}
	if cfg.GopDuration != "" {
		args = append(args, "--gop-duration", cfg.GopDuration)
	}
	if cfg.HlsFormat != "" {
		args = append(args, "--hls-format", cfg.HlsFormat)
	}
	switch cfg.Padding {
	case "black":
		args = append(args, "--padding")
	case "pink":
		args = append(args, "--padding-pink")
	case "none":
		args = append(args, "--no-padding")
	}
	if cfg.KeepMezzanine {
		args = append(args, "--keep-mezzanine")
	}
	if cfg.CpuArch != "" {
		args = append(args, "--cpu-arch", cfg.CpuArch)
	}
	if cfg.UseSpot != nil && !*cfg.UseSpot {
		args = append(args, "--no-spot")
	}
	return args
}

func (m *Manager) encodeFilesFrom(job *Job, tmpDir, script string, startIdx int) error {
	// Cloud jobs batch every remaining file into a single cli_cloud.py
	// invocation so one EC2 instance handles the whole job — boot once,
	// docker pull once, amortize the launch + pull overhead across N
	// clips. Local jobs still run one worker per file.
	if job.Config.Target == TargetCloud {
		return m.encodeCloudBatch(job, tmpDir, script, startIdx)
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

		fileCfg := job.Config
		fileCfg.Codec = codec
		args := fileCfg.encodeArgsForFile(m.SourceDir, tmpDir, f)
		if err := m.runFileContainer(job, i, script, args); err != nil {
			return fmt.Errorf("%s: %w", f, err)
		}
	}
	return nil
}

// encodeCloudBatch runs ALL remaining files through a single
// cli_cloud.py invocation, which launches one EC2 instance and its
// user-data bash loops through every clip on that same machine. The
// EC2 boot + docker-pull overhead is paid once per job instead of
// per clip.
//
// Pre-filters files via resolveCodec so we don't ship work for
// already-fully-encoded clips to the cloud, same as the local path.
func (m *Manager) encodeCloudBatch(job *Job, tmpDir, script string, startIdx int) error {
	// Figure out which files genuinely need work. When !ForceReencode
	// and everything's already in OutputDir, the whole batch collapses
	// to zero clips — skip the instance launch entirely.
	var filesToEncode []string
	chosenCodec := job.Config.Codec
	for i := startIdx; i < len(job.Config.Files); i++ {
		f := job.Config.Files[i]
		if job.Config.ForceReencode {
			filesToEncode = append(filesToEncode, f)
			continue
		}
		c := m.resolveCodec(job.Config, f)
		if c == "" {
			job.AppendLog(fmt.Sprintf("skipping %s — all codecs already encoded", f))
			continue
		}
		filesToEncode = append(filesToEncode, f)
		// If clips in the same job need different codec subsets, the
		// batch uses the first non-empty resolved codec; edge case,
		// only relevant when some clips partially exist in OutputDir.
		if c != job.Config.Codec {
			chosenCodec = c
		}
	}

	if len(filesToEncode) == 0 {
		return nil
	}

	job.Progress = fmt.Sprintf("cloud encode: %d clip(s) on one instance",
		len(filesToEncode))
	m.notify(job)
	m.persistState(job, startIdx)

	batchCfg := job.Config
	batchCfg.Codec = chosenCodec
	args := batchCfg.cloudBatchArgs(m.SourceDir, tmpDir, filesToEncode)

	if err := m.runFileContainer(job, startIdx, script, args); err != nil {
		return fmt.Errorf("cloud batch: %w", err)
	}
	return nil
}

// resolveCodec checks which codecs are already encoded in OutputDir and returns
// the codec flag that covers only the missing ones. Returns "" if all exist.
func (m *Manager) resolveCodec(cfg JobConfig, filename string) string {
	if cfg.Codec == "" {
		cfg.Codec = "both"
	}
	stem := cfg.OutputStem(filename)

	wantH264 := cfg.Codec == "h264" || cfg.Codec == "both" || cfg.Codec == "all"
	wantHEVC := cfg.Codec == "hevc" || cfg.Codec == "both" || cfg.Codec == "all"
	wantAV1 := cfg.Codec == "av1" || cfg.Codec == "all"

	hasH264 := dirExistsWithFiles(filepath.Join(m.OutputDir, stem+"_h264"))
	hasHEVC := dirExistsWithFiles(filepath.Join(m.OutputDir, stem+"_hevc"))
	hasAV1 := dirExistsWithFiles(filepath.Join(m.OutputDir, stem+"_av1"))

	needH264 := wantH264 && !hasH264
	needHEVC := wantHEVC && !hasHEVC
	needAV1 := wantAV1 && !hasAV1

	if !needH264 && !needHEVC && !needAV1 {
		return ""
	}
	if cfg.Codec == "all" {
		missing := []string{}
		if needH264 { missing = append(missing, "h264") }
		if needHEVC { missing = append(missing, "hevc") }
		if needAV1 { missing = append(missing, "av1") }
		if len(missing) == 3 { return "all" }
		if len(missing) == 2 && needH264 && needHEVC { return "both" }
		if len(missing) == 1 { return missing[0] }
		// 2 of 3 but not h264+hevc — encode individually
		return strings.Join(missing, ",")
	}
	if cfg.Codec == "both" {
		if needH264 && needHEVC { return "both" }
		if needH264 { return "h264" }
		if needHEVC { return "hevc" }
		return ""
	}
	return cfg.Codec
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
		"--label", "encoder.job_id=" + job.ID,
		"--label", "encoder.role=encode-worker",
		"--label", fmt.Sprintf("encoder.target=%s", job.Config.Target),
		"-v", m.HostSourceDir + ":" + m.SourceDir + ":ro",
		"-v", m.HostOutputDir + ":" + m.OutputDir,
		"-v", m.HostTmpDir + ":" + m.TmpDir,
		"--entrypoint", script,
	}
	// Cloud jobs drive AWS from inside the worker; pass the credentials
	// directory and the AWS / GHCR env vars the wrapper expects.
	if job.Config.Target == TargetCloud {
		if m.HostAWSDir != "" {
			runArgs = append(runArgs, "-v", m.HostAWSDir+":/root/.aws:ro")
		}
		for _, key := range cloudEnvPassthrough {
			if v := os.Getenv(key); v != "" {
				runArgs = append(runArgs, "-e", key+"="+v)
			}
		}
	}
	runArgs = append(runArgs, m.EncoderImage)
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
		job := &Job{
			ID:        s.ID,
			Config:    s.Config,
			Status:    StatusQueued,
			StartedAt: s.StartedAt,
			Progress:  "resuming after restart",
		}
		m.mu.Lock()
		m.jobs = append(m.jobs, job)
		m.mu.Unlock()
		m.notify(job)
		go m.run(job, s.CurrentFileIdx)
	}
}
