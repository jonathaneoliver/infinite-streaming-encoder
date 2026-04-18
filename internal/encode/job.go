package encode

import (
	"bufio"
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
	StatusQueued   JobStatus = "queued"
	StatusRunning  JobStatus = "running"
	StatusDone     JobStatus = "done"
	StatusFailed   JobStatus = "failed"
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

	mu       sync.Mutex
	logLines []string
	cancel   func()
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

	SourceDir    string
	OutputDir    string
	TmpDir       string
	ScriptsDir   string
	DockerImage  string

	sem         chan struct{}
	subscribers []chan *Job
	subMu       sync.Mutex
}

func NewManager(sourceDir, outputDir, tmpDir, scriptsDir, dockerImage string, maxConcurrent int) *Manager {
	if maxConcurrent < 1 {
		maxConcurrent = 1
	}
	os.MkdirAll(tmpDir, 0755)
	return &Manager{
		SourceDir:   sourceDir,
		OutputDir:   outputDir,
		TmpDir:      tmpDir,
		ScriptsDir:  scriptsDir,
		DockerImage: dockerImage,
		sem:         make(chan struct{}, maxConcurrent),
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
	m.notify(job)

	go m.run(job)
	return job
}

func (m *Manager) run(job *Job) {
	job.Progress = "waiting for slot"
	m.notify(job)
	m.sem <- struct{}{}
	defer func() { <-m.sem }()

	job.Status = StatusRunning
	job.Progress = "starting"
	m.notify(job)

	// Encode into TmpDir so partial output never appears in OutputDir.
	// On success, move completed directories to OutputDir.
	jobTmpDir := filepath.Join(m.TmpDir, job.ID)
	os.MkdirAll(jobTmpDir, 0755)

	var err error
	if job.Config.Target == TargetCloud {
		err = m.runCloud(job, jobTmpDir)
	} else {
		err = m.runLocal(job, jobTmpDir)
	}

	now := time.Now()
	job.EndedAt = &now
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
	m.writeHistory(job)
	m.notify(job)
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

func (m *Manager) runCloud(job *Job, tmpDir string) error {
	return m.encodeFiles(job, tmpDir, m.ScriptsDir+"/cloud_encode.sh")
}

func (m *Manager) runLocal(job *Job, tmpDir string) error {
	return m.encodeFiles(job, tmpDir, m.ScriptsDir+"/create_abr_ladder.sh")
}

func (m *Manager) encodeFiles(job *Job, tmpDir, script string) error {
	for i, f := range job.Config.Files {
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
		if err := m.execScript(job, script, args); err != nil {
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

func (m *Manager) execScript(job *Job, script string, args []string) error {
	cmd := exec.Command(script, args...)
	cmd.Dir = m.OutputDir

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("stdout pipe: %w", err)
	}
	cmd.Stderr = cmd.Stdout

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start: %w", err)
	}

	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 256*1024), 256*1024)
	for scanner.Scan() {
		line := scanner.Text()
		job.AppendLog(line)
		job.Progress = stripANSI(line)
		m.notify(job)
	}

	return cmd.Wait()
}
