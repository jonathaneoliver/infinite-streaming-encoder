package encode

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]|\x1b[()][0-9A-B]|\x1b\][^\x07]*\x07|\r`)

func stripANSI(s string) string {
	return ansiRe.ReplaceAllString(s, "")
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
}

type Job struct {
	ID        string    `json:"id"`
	Config    JobConfig `json:"config"`
	Status    JobStatus `json:"status"`
	StartedAt time.Time `json:"started_at"`
	EndedAt   *time.Time `json:"ended_at,omitempty"`
	Progress  string    `json:"progress"`
	Error     string    `json:"error,omitempty"`

	mu        sync.Mutex
	logLines  []string
	cancelled bool
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
	os.RemoveAll(jobTmpDir)
	m.removePersistedState(job.ID)
	m.writeHistory(job)
	m.notify(job)
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
// finalizeCancelled. For a running job we additionally force-remove any
// worker container carrying the matching label — that makes `docker logs`
// return in attachAndWait, which unwinds the encode loop and lands us in
// the same finalize path.
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

	// Kill any worker container for this job. The label filter matches
	// every file-index container we might have spawned; there will only
	// ever be one alive at a time under the current serial encode loop,
	// but the filter is defensive against future parallelism.
	out, err := exec.Command("docker", "ps", "-a",
		"--filter", "label=encoder.job_id="+id,
		"--format", "{{.Names}}",
	).Output()
	if err == nil {
		for _, name := range strings.Fields(string(out)) {
			exec.Command("docker", "rm", "-f", name).Run()
		}
	}
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
	fmt.Fprintf(f, "\n---\n\n")
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
	return args
}

func (m *Manager) encodeFilesFrom(job *Job, tmpDir, script string, startIdx int) error {
	for i := startIdx; i < len(job.Config.Files); i++ {
		m.persistState(job, i)

		f := job.Config.Files[i]
		job.Progress = fmt.Sprintf("encoding %d/%d: %s", i+1, len(job.Config.Files), f)
		m.notify(job)

		codec := job.Config.Codec
		if !job.Config.ForceReencode {
			codec = m.resolveCodec(job.Config, f)
			if codec == "" {
				job.AppendLog(fmt.Sprintf("skipping %s — all codecs already encoded", f))
				continue
			}
		}

		fileCfg := job.Config
		fileCfg.Codec = codec
		args := fileCfg.encodeArgsForFile(m.SourceDir, tmpDir, f)
		if err := m.runFileContainer(job, i, script, args); err != nil {
			return fmt.Errorf("%s: %w", f, err)
		}
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
		"run", "-d",
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
	for scanner.Scan() {
		line := scanner.Text()
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
