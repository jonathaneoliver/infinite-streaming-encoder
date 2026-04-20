package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/jonathaneoliver/encoder/internal/encode"
	"github.com/jonathaneoliver/encoder/internal/imageinfo"
)

type Server struct {
	Manager *encode.Manager
	Mux     *http.ServeMux
	// Version + GitSha are stamped by cmd/server from -ldflags-injected
	// main.version / main.gitSha. CloudImage is the DOCKER_IMAGE env var
	// — what the EC2 worker user-data pulls on job start. The About tab
	// pulls the image's OCI labels from GHCR to compare local vs cloud.
	Version    string
	GitSha     string
	CloudImage string

	imageInfo *imageinfo.Client
}

func NewServer(mgr *encode.Manager) *Server {
	s := &Server{
		Manager: mgr,
		Mux:     http.NewServeMux(),
		imageInfo: imageinfo.NewClient(
			os.Getenv("GHCR_USERNAME"),
			os.Getenv("GHCR_PAT"),
		),
	}
	s.Mux.HandleFunc("GET /api/version", s.getVersion)
	s.Mux.HandleFunc("GET /api/sources", s.listSources)
	s.Mux.HandleFunc("GET /api/outputs", s.listOutputs)
	s.Mux.HandleFunc("GET /api/outputs/{name}", s.listOutputContents)
	s.Mux.HandleFunc("GET /api/outputs/{name}/playlists", s.listPlaylists)
	s.Mux.HandleFunc("GET /api/outputs/{name}/ladder", s.ladder)
	s.Mux.HandleFunc("GET /api/outputs/{name}/logs", s.outputLogs)
	s.Mux.HandleFunc("POST /api/encode", s.startEncode)
	s.Mux.HandleFunc("GET /api/jobs", s.listJobs)
	s.Mux.HandleFunc("GET /api/jobs/{id}/logs", s.jobLogs)
	s.Mux.HandleFunc("GET /api/jobs/stream", s.streamJobs)
	s.Mux.HandleFunc("POST /api/jobs/{id}/cancel", s.cancelJob)
	s.Mux.HandleFunc("POST /api/jobs/{id}/retry", s.retryJob)
	s.Mux.HandleFunc("POST /api/jobs/{id}/simulate-interrupt", s.simulateInterrupt)
	// AWS inventory + cleanup (issue #5)
	s.Mux.HandleFunc("GET /api/aws/inventory", s.awsInventory)
	s.Mux.HandleFunc("POST /api/aws/clear", s.awsClearAll)
	s.Mux.HandleFunc("POST /api/aws/jobs/{id}/cleanup", s.awsCleanupJob)
	// Serve encode logs
	s.Mux.Handle("GET /logs/", http.StripPrefix("/logs/", http.FileServer(http.Dir(filepath.Join(mgr.TmpDir, "logs")))))
	// Serve encoded output files (segments, manifests) for HLS.js playback
	s.Mux.Handle("GET /content/", http.StripPrefix("/content/", mediaFileServer(mgr.OutputDir)))
	// Serve source files for direct playback
	s.Mux.Handle("GET /sources/", http.StripPrefix("/sources/", mediaFileServer(mgr.SourceDir)))
	// The SPA is under active iteration; browser caches of index.html
	// have bitten the user mid-session (new form fields silently not
	// sent). Force revalidation so reloads always see current markup.
	s.Mux.Handle("GET /", noCache(http.FileServer(http.Dir("static"))))
	return s
}

// noCache wraps a handler so responses can't be cached by browsers.
// Only applied to the SPA (index.html, JS bundle); media assets keep
// their normal caching since they're immutable once written.
func noCache(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-cache, no-store, must-revalidate")
		w.Header().Set("Pragma", "no-cache")
		w.Header().Set("Expires", "0")
		h.ServeHTTP(w, r)
	})
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	out := map[string]any{
		"local": map[string]string{
			"version":  s.Version,
			"revision": s.GitSha,
		},
		"cloud_image": s.CloudImage,
	}
	if s.CloudImage != "" {
		out["cloud"] = s.imageInfo.Get(r.Context(), s.CloudImage)
	}
	writeJSON(w, out)
}

type sourceFile struct {
	Name    string `json:"name"`
	Size    int64  `json:"size"`
	ModTime int64  `json:"mod_time"`
}

func (s *Server) listSources(w http.ResponseWriter, r *http.Request) {
	entries, err := os.ReadDir(s.Manager.SourceDir)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	var files []sourceFile
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		ext := filepath.Ext(e.Name())
		if !isVideo(ext) {
			continue
		}
		info, _ := e.Info()
		if info == nil {
			continue
		}
		files = append(files, sourceFile{Name: e.Name(), Size: info.Size(), ModTime: info.ModTime().UnixMilli()})
	}
	writeJSON(w, files)
}

func (s *Server) startEncode(w http.ResponseWriter, r *http.Request) {
	var cfg encode.JobConfig
	if err := json.NewDecoder(r.Body).Decode(&cfg); err != nil {
		http.Error(w, "bad request: "+err.Error(), 400)
		return
	}
	if len(cfg.Files) == 0 {
		http.Error(w, "no files specified", 400)
		return
	}
	if cfg.Target == "" {
		cfg.Target = encode.TargetLocal
	}
	if cfg.Codec == "" {
		cfg.Codec = "both"
	}
	job := s.Manager.Submit(cfg)
	writeJSON(w, job)
}

func (s *Server) listJobs(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.Manager.Jobs())
}

func (s *Server) cancelJob(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.Manager.Cancel(id) {
		http.Error(w, "job not found", 404)
		return
	}
	w.WriteHeader(204)
}

// simulateInterrupt drops an empty `_SIMULATE_INTERRUPT` sentinel
// into the running job's S3 prefix. The remote user-data polls for
// it every 5s and, on seeing it, invokes the same trigger_interrupt
// bash path that a real spot reclaim hits — writes the distinctive
// _FAILED body, rsyncs /work/tmp + /work/output, exits. Lets us
// test the Retry + partial-file flow without waiting for AWS.
func (s *Server) simulateInterrupt(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	job := s.Manager.GetJob(id)
	if job == nil {
		http.Error(w, "job not found", 404)
		return
	}
	if job.Config.Target != encode.TargetCloud || job.Status != encode.StatusRunning {
		http.Error(w, "job must be a running cloud job", 400)
		return
	}
	bucket := os.Getenv("S3_BUCKET")
	region := os.Getenv("AWS_REGION")
	if bucket == "" {
		http.Error(w, "S3_BUCKET not configured", 500)
		return
	}
	cloudID := job.CloudJobID
	if cloudID == "" {
		http.Error(w, "cloud_job_id not yet known for this job", 409)
		return
	}
	key := fmt.Sprintf("s3://%s/jobs/%s/_SIMULATE_INTERRUPT", bucket, cloudID)
	cmd := exec.Command("aws", "s3", "cp", "-", key, "--region", region)
	cmd.Stdin = strings.NewReader("")
	out, err := cmd.CombinedOutput()
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to write sentinel: %s", strings.TrimSpace(string(out))), 500)
		return
	}
	w.WriteHeader(204)
}

// retryJob submits a new job with the same config as `id`, wired to
// resume from that job's S3 staging (inputs, mezzanines, completed
// variants). Only meaningful for cloud failures — local encodes have
// no shared staging to reuse.
func (s *Server) retryJob(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	orig := s.Manager.GetJob(id)
	if orig == nil {
		http.Error(w, "job not found", 404)
		return
	}
	if orig.Status != encode.StatusFailed && orig.Status != encode.StatusCancelled {
		http.Error(w, "job is not in a retryable state", 400)
		return
	}
	cfg := orig.Config
	cfg.ForceReencode = true
	if cfg.Target == encode.TargetCloud {
		// The JobID used as the S3 prefix by cli_cloud.py isn't the
		// Manager's internal ID — it's the timestamp-based one the
		// Python tool computes itself. We stash that into Job.CloudJobID
		// when the remote plan prints job_id:<X>; fall back to the
		// manager ID (close enough for new-style jobs).
		prior := orig.CloudJobID
		if prior == "" {
			prior = orig.ID
		}
		cfg.ResumeFromJobID = prior
	}
	job := s.Manager.Submit(cfg)
	writeJSON(w, job)
}

func (s *Server) jobLogs(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	job := s.Manager.GetJob(id)
	if job == nil {
		http.Error(w, "job not found", 404)
		return
	}
	writeJSON(w, job.LogLines())
}

func (s *Server) streamJobs(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", 500)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	ch := s.Manager.Subscribe()
	defer s.Manager.Unsubscribe(ch)

	// Send current state immediately
	for _, j := range s.Manager.Jobs() {
		data, _ := json.Marshal(j)
		fmt.Fprintf(w, "data: %s\n\n", data)
	}
	flusher.Flush()

	for {
		select {
		case job, ok := <-ch:
			if !ok {
				return
			}
			data, _ := json.Marshal(job)
			fmt.Fprintf(w, "data: %s\n\n", data)
			flusher.Flush()
		case <-r.Context().Done():
			return
		}
	}
}

type outputDir struct {
	Name        string   `json:"name"`
	Size        int64    `json:"size"`
	NumFiles    int      `json:"num_files"`
	ModTime     int64    `json:"mod_time"`
	Codec       string   `json:"codec"`
	Resolutions []string `json:"resolutions"`
	HlsFormat   string   `json:"hls_format"`
	Partial     string   `json:"partial"`
	Padding     string   `json:"padding"`
}

func (s *Server) listOutputs(w http.ResponseWriter, r *http.Request) {
	entries, err := os.ReadDir(s.Manager.OutputDir)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	var dirs []outputDir
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		name := e.Name()
		if strings.HasPrefix(name, ".") || strings.HasSuffix(name, "_tmp") {
			continue
		}
		info, _ := e.Info()
		if info == nil {
			continue
		}
		dirPath := filepath.Join(s.Manager.OutputDir, e.Name())
		size, count := dirStats(dirPath)
		meta := parseOutputMeta(e.Name(), dirPath)
		dirs = append(dirs, outputDir{
			Name: e.Name(), Size: size, NumFiles: count, ModTime: info.ModTime().UnixMilli(),
			Codec: meta.codec, Resolutions: meta.resolutions, HlsFormat: meta.hlsFormat,
			Partial: meta.partial, Padding: meta.padding,
		})
	}
	writeJSON(w, dirs)
}

type outputFile struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
	Dir  bool   `json:"dir"`
}

func (s *Server) listOutputContents(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if name == "" || name == ".." || name == "." {
		http.Error(w, "invalid name", 400)
		return
	}
	dirPath := filepath.Join(s.Manager.OutputDir, name)
	entries, err := os.ReadDir(dirPath)
	if err != nil {
		http.Error(w, "not found", 404)
		return
	}
	var files []outputFile
	for _, e := range entries {
		info, _ := e.Info()
		if info == nil {
			continue
		}
		f := outputFile{Name: e.Name(), Size: info.Size(), Dir: e.IsDir()}
		if e.IsDir() {
			sz, _ := dirStats(filepath.Join(dirPath, e.Name()))
			f.Size = sz
		}
		files = append(files, f)
	}
	writeJSON(w, files)
}

type outputMeta struct {
	codec       string
	resolutions []string
	hlsFormat   string
	partial     string
	padding     string
}

var knownRes = []string{"2160p", "1440p", "1080p", "720p", "540p", "360p"}

func parseOutputMeta(name, dirPath string) outputMeta {
	m := outputMeta{}

	// Codec from directory name suffix
	switch {
	case strings.HasSuffix(name, "_h264") || strings.Contains(name, "_h264_"):
		m.codec = "h264"
	case strings.HasSuffix(name, "_hevc") || strings.Contains(name, "_hevc_"):
		m.codec = "hevc"
	case strings.HasSuffix(name, "_av1") || strings.Contains(name, "_av1_"):
		m.codec = "av1"
	}

	// Partial duration from name (e.g. _p200_ = 200ms)
	if idx := strings.Index(name, "_p"); idx >= 0 {
		rest := name[idx+2:]
		end := strings.IndexByte(rest, '_')
		if end < 0 {
			end = len(rest)
		}
		pval := rest[:end]
		allDigit := len(pval) > 0
		for _, c := range pval {
			if c < '0' || c > '9' {
				allDigit = false
				break
			}
		}
		if allDigit {
			m.partial = pval + "ms"
		}
	}

	// Padding from name
	if strings.Contains(name, "_padblack") {
		m.padding = "black"
	} else if strings.Contains(name, "_padpink") {
		m.padding = "pink"
	}

	// Resolutions: check which subdirectories exist
	for _, res := range knownRes {
		if info, err := os.Stat(filepath.Join(dirPath, res)); err == nil && info.IsDir() {
			m.resolutions = append(m.resolutions, res)
		}
	}

	// HLS format: check for .ts vs .m4s segments
	hasM4S := false
	hasTS := false
	entries, _ := os.ReadDir(dirPath)
	for _, e := range entries {
		if e.IsDir() {
			subEntries, _ := os.ReadDir(filepath.Join(dirPath, e.Name()))
			for _, se := range subEntries {
				switch filepath.Ext(se.Name()) {
				case ".m4s":
					hasM4S = true
				case ".ts":
					if se.Name() != "playlist.m3u8" {
						hasTS = true
					}
				}
				if hasM4S && hasTS {
					break
				}
			}
		}
		if hasM4S && hasTS {
			break
		}
	}
	switch {
	case hasM4S && hasTS:
		m.hlsFormat = "both"
	case hasTS:
		m.hlsFormat = "ts"
	case hasM4S:
		m.hlsFormat = "fmp4"
	}

	return m
}

func dirStats(path string) (totalSize int64, fileCount int) {
	filepath.Walk(path, func(_ string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		totalSize += info.Size()
		fileCount++
		return nil
	})
	return
}

func (s *Server) outputLogs(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if name == "" || name == ".." || name == "." {
		http.Error(w, "invalid name", 400)
		return
	}

	// Search job history for any job whose output stem matches this directory.
	// The output dir name is typically <stem>_<codec>, so we strip the codec suffix
	// to find matching jobs.
	type logEntry struct {
		JobID   string `json:"job_id"`
		Status  string `json:"status"`
		Started string `json:"started"`
		LogFile string `json:"log_file"`
	}
	var logs []logEntry

	// Check in-memory jobs first
	for _, j := range s.Manager.Jobs() {
		for _, f := range j.Config.Files {
			stem := j.Config.OutputStem(f)
			if len(name) >= len(stem) && name[:len(stem)] == stem {
				entry := logEntry{
					JobID:   j.ID,
					Status:  string(j.Status),
					Started: j.StartedAt.Format("2006-01-02 15:04:05"),
				}
				logPath := filepath.Join(s.Manager.TmpDir, "logs", j.ID+".log")
				if _, err := os.Stat(logPath); err == nil {
					entry.LogFile = j.ID + ".log"
				}
				logs = append(logs, entry)
			}
		}
	}

	// Also scan the logs directory for any log files (covers jobs from previous runs)
	logsDir := filepath.Join(s.Manager.TmpDir, "logs")
	if entries, err := os.ReadDir(logsDir); err == nil {
		seen := make(map[string]bool)
		for _, l := range logs {
			seen[l.JobID] = true
		}
		for _, e := range entries {
			if e.IsDir() || filepath.Ext(e.Name()) != ".log" {
				continue
			}
			jobID := e.Name()[:len(e.Name())-4]
			if seen[jobID] {
				continue
			}
			// Read first few lines of the log to see if it mentions this output name
			logPath := filepath.Join(logsDir, e.Name())
			data, err := os.ReadFile(logPath)
			if err != nil {
				continue
			}
			content := string(data)
			if len(content) > 4096 {
				content = content[:4096]
			}
			if !contains(content, name) {
				continue
			}
			info, _ := e.Info()
			started := ""
			if info != nil {
				started = info.ModTime().Format("2006-01-02 15:04:05")
			}
			logs = append(logs, logEntry{
				JobID:   jobID,
				Status:  "historical",
				Started: started,
				LogFile: e.Name(),
			})
		}
	}

	writeJSON(w, logs)
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(substr) == 0 ||
		(len(s) > 0 && len(substr) > 0 && searchString(s, substr)))
}

func searchString(s, sub string) bool {
	for i := 0; i <= len(s)-len(sub); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

func (s *Server) listPlaylists(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if name == "" || name == ".." || name == "." {
		http.Error(w, "invalid name", 400)
		return
	}
	dirPath := filepath.Join(s.Manager.OutputDir, name)
	var playlists []string
	filepath.Walk(dirPath, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		if filepath.Ext(path) == ".m3u8" {
			rel, _ := filepath.Rel(s.Manager.OutputDir, path)
			playlists = append(playlists, rel)
		}
		return nil
	})
	writeJSON(w, playlists)
}

// mediaFileServer wraps http.FileServer with correct MIME types for media files
// that Go's built-in MIME database doesn't recognize (.m3u8, .mpd, .m4s, .ts).
func mediaFileServer(root string) http.Handler {
	fs := http.FileServer(http.Dir(root))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch filepath.Ext(r.URL.Path) {
		case ".m3u8":
			w.Header().Set("Content-Type", "application/vnd.apple.mpegurl")
		case ".mpd":
			w.Header().Set("Content-Type", "application/dash+xml")
		case ".m4s":
			w.Header().Set("Content-Type", "video/iso.segment")
		case ".ts":
			w.Header().Set("Content-Type", "video/mp2t")
		}
		fs.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func isVideo(ext string) bool {
	switch ext {
	case ".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts":
		return true
	}
	return false
}

// ---------------------------------------------------------------------------
// AWS inventory + cleanup (issue #5)
//
// We delegate to the Python cloud modules via subprocess rather than
// wiring aws-sdk-go-v2 into the server. The Python side already has
// the boto3 client plumbing, the tagging contract, and the same error
// handling the CLI uses — there's no gain from duplicating all that in
// Go. The subprocess boundary also neatly sandboxes any AWS API timeout
// or crash away from the main server goroutine.
// ---------------------------------------------------------------------------

// runPythonCloud invokes `python3 -m encoder.cloud.<module> <args>` and
// returns the captured stdout on exit-0, or an error containing stderr.
func runPythonCloud(module string, args ...string) ([]byte, error) {
	fullArgs := append([]string{"-m", "encoder.cloud." + module, "--json"}, args...)
	cmd := exec.Command("python3", fullArgs...)
	// The Python modules read AWS_REGION, S3_BUCKET, etc. from the
	// server process's environment — same vars the encoder has been
	// forwarding into worker containers all along.
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return nil, fmt.Errorf("python3 -m encoder.cloud.%s exited %d: %s",
				module, ee.ExitCode(), strings.TrimSpace(string(ee.Stderr)))
		}
		return nil, fmt.Errorf("python3 -m encoder.cloud.%s: %w", module, err)
	}
	return out, nil
}

func (s *Server) awsInventory(w http.ResponseWriter, r *http.Request) {
	out, err := runPythonCloud("inventory")
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

type clearRequest struct {
	// Literal string "CLEAR AWS" — anything else is rejected.
	// This is the user-confirmation gate; the UI prompts for the
	// exact string before enabling the confirm button.
	Confirm string `json:"confirm"`
}

func (s *Server) awsClearAll(w http.ResponseWriter, r *http.Request) {
	var body clearRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad request: "+err.Error(), 400)
		return
	}
	if body.Confirm != "CLEAR AWS" {
		http.Error(w,
			`confirmation required: POST body must contain {"confirm": "CLEAR AWS"}`,
			400)
		return
	}
	out, err := runPythonCloud("cleanup", "--sweep-all")
	if err != nil {
		// Non-zero exit from cleanup means at least one action failed —
		// we still want to return the structured report so the UI can
		// render a partial-success state.
		if strings.Contains(err.Error(), "exited 1") {
			// cleanup.py prints the JSON report to stdout before
			// exit(1), so we should re-invoke WITHOUT piping stderr
			// to get that output. Simpler: just fall back to returning
			// the error message as a plain-text response.
		}
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

func (s *Server) awsCleanupJob(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" || strings.ContainsAny(id, "/\\") {
		http.Error(w, "invalid job id", 400)
		return
	}
	out, err := runPythonCloud("cleanup", "--job-id", id)
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}
