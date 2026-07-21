package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
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
	s.Mux.HandleFunc("GET /api/settings", s.getSettings)
	s.Mux.HandleFunc("POST /api/settings", s.putSettings)
	s.Mux.HandleFunc("GET /api/sources", s.listSources)
	s.Mux.HandleFunc("POST /api/sources/upload", s.uploadSource)
	s.Mux.HandleFunc("GET /api/ladders", s.getLadders)
	s.Mux.HandleFunc("POST /api/ladders", s.putLadder)
	s.Mux.HandleFunc("DELETE /api/ladders/{name}", s.deleteLadder)
	s.Mux.HandleFunc("GET /api/outputs", s.listOutputs)
	s.Mux.HandleFunc("GET /api/outputs/{name}", s.listOutputContents)
	s.Mux.HandleFunc("GET /api/outputs/{name}/playlists", s.listPlaylists)
	s.Mux.HandleFunc("GET /api/outputs/{name}/ladder", s.ladder)
	s.Mux.HandleFunc("GET /api/outputs/{name}/logs", s.outputLogs)
	s.Mux.HandleFunc("POST /api/outputs/{name}/promote", s.promoteOutput)
	s.Mux.HandleFunc("GET /api/promote", s.getPromote)
	s.Mux.HandleFunc("POST /api/encode", s.startEncode)
	s.Mux.HandleFunc("GET /api/jobs", s.listJobs)
	s.Mux.HandleFunc("GET /api/jobs/{id}/logs", s.jobLogs)
	s.Mux.HandleFunc("GET /api/jobs/stream", s.streamJobs)
	s.Mux.HandleFunc("POST /api/jobs/{id}/cancel", s.cancelJob)
	s.Mux.HandleFunc("POST /api/jobs/{id}/retry", s.retryJob)
	s.Mux.HandleFunc("POST /api/jobs/{id}/simulate-interrupt", s.simulateInterrupt)
	s.Mux.HandleFunc("GET /api/jobs/{id}/workdir", s.jobWorkdir)
	// AWS inventory + cleanup (issue #5)
	s.Mux.HandleFunc("GET /api/aws/inventory", s.awsInventory)
	s.Mux.HandleFunc("POST /api/aws/clear", s.awsClearAll)
	s.Mux.HandleFunc("POST /api/aws/jobs/{id}/cleanup", s.awsCleanupJob)
	s.Mux.HandleFunc("POST /api/aws/s3/delete-prefix", s.awsDeleteS3Prefix)
	s.Mux.HandleFunc("POST /api/aws/max-vcpus", s.awsSetMaxVCPUs)
	// Cloud-batch release controls (Step Functions executions + Batch jobs).
	s.Mux.HandleFunc("POST /api/aws/executions/stop", s.awsStopExecution)
	s.Mux.HandleFunc("POST /api/aws/batch-jobs/terminate", s.awsTerminateBatchJob)
	s.Mux.HandleFunc("POST /api/aws/batch/stop-all", s.awsBatchStopAll)
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

// uploadSource streams dropped video file(s) straight to SOURCE_DIR. Uses the
// streaming MultipartReader (never ParseMultipartForm — sources are multi-GB and
// must not buffer in memory), writes to <name>.uploading, then renames on
// completion so the watcher never picks up a partial file. Rejects non-video and
// path-traversal names.
func (s *Server) uploadSource(w http.ResponseWriter, r *http.Request) {
	mr, err := r.MultipartReader()
	if err != nil {
		http.Error(w, "expected a multipart upload: "+err.Error(), http.StatusBadRequest)
		return
	}
	var saved []string
	for {
		part, err := mr.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			http.Error(w, "read part: "+err.Error(), http.StatusBadRequest)
			return
		}
		if part.FormName() != "file" {
			part.Close()
			continue
		}
		name := filepath.Base(part.FileName()) // strip any client path components
		if name == "" || name == "." || strings.ContainsAny(name, `/\`) {
			http.Error(w, "invalid filename", http.StatusBadRequest)
			return
		}
		if !isVideo(strings.ToLower(filepath.Ext(name))) {
			http.Error(w, "not a video file: "+name, http.StatusBadRequest)
			return
		}
		dst := filepath.Join(s.Manager.SourceDir, name)
		tmp := dst + ".uploading"
		f, cerr := os.Create(tmp)
		if cerr != nil {
			http.Error(w, "create: "+cerr.Error(), http.StatusInternalServerError)
			return
		}
		_, werr := io.Copy(f, part)
		f.Close()
		part.Close()
		if werr != nil {
			os.Remove(tmp)
			http.Error(w, "write: "+werr.Error(), http.StatusInternalServerError)
			return
		}
		if rerr := os.Rename(tmp, dst); rerr != nil {
			os.Remove(tmp)
			http.Error(w, "finalize: "+rerr.Error(), http.StatusInternalServerError)
			return
		}
		saved = append(saved, name)
	}
	writeJSON(w, map[string]any{"saved": saved})
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
	// One job per file: each selected file becomes its own independent job, so
	// they run concurrently (up to MAX_CONCURRENT), each with its own log,
	// history, cancel and retry — instead of a single batched job that processes
	// the files strictly sequentially. Returns the list of created jobs (the UI
	// tracks them via the SSE stream, so it doesn't depend on this body).
	jobs := make([]*encode.Job, 0, len(cfg.Files))
	for _, f := range cfg.Files {
		c := cfg
		c.Files = []string{f}
		jobs = append(jobs, s.Manager.Submit(c))
	}
	writeJSON(w, jobs)
}

func (s *Server) listJobs(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.Manager.Jobs())
}

// getSettings returns the persisted global settings (currently just the
// watcher on/off toggle).
func (s *Server) getSettings(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.Manager.Settings())
}

// putSettings applies a partial settings update. Only fields present in the
// body are changed; the watcher toggle persists across restarts.
func (s *Server) putSettings(w http.ResponseWriter, r *http.Request) {
	// Pointer fields so we can tell "set to false" from "omitted".
	var body struct {
		WatcherEnabled *bool `json:"watcher_enabled"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad request: "+err.Error(), 400)
		return
	}
	if body.WatcherEnabled != nil {
		s.Manager.SetWatcherEnabled(*body.WatcherEnabled)
	}
	writeJSON(w, s.Manager.Settings())
}

func (s *Server) cancelJob(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.Manager.Cancel(id) {
		http.Error(w, "job not found", 404)
		return
	}
	w.WriteHeader(204)
}

// jobWorkdir lists files in cli_local.py's per-clip persistent work
// dir ($TMP_DIR/encode_<stem>/) and flags each as complete (has a
// matching .done sidecar) or partial. Lets the UI preview what a
// Retry will reuse vs re-encode. Works for both local and cloud
// encodes — cloud's /work/tmp is rsynced to/from S3, but during an
// encode the same paths are mounted on the host.
func (s *Server) jobWorkdir(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	job := s.Manager.GetJob(id)
	if job == nil {
		http.Error(w, "job not found", 404)
		return
	}
	type workdirEntry struct {
		Name     string `json:"name"`
		Size     int64  `json:"size"`
		Complete bool   `json:"complete"`
	}
	type workdirResp struct {
		Stem    string         `json:"stem"`
		Path    string         `json:"path"`
		Entries []workdirEntry `json:"entries"`
	}
	// Use the first file's stem. Multi-file jobs each have their own
	// work dir; if we're asked mid-job we could be partway through
	// clip N, so pick the current one from CurrentFile if set.
	var filename string
	if job.CurrentFile != "" {
		filename = job.CurrentFile
	} else if len(job.Config.Files) > 0 {
		filename = job.Config.Files[0]
	}
	if filename == "" {
		writeJSON(w, workdirResp{})
		return
	}
	stem := job.Config.OutputStem(filename)
	dir := filepath.Join(s.Manager.TmpDir, "encode_"+stem)
	entries, err := os.ReadDir(dir)
	if err != nil {
		writeJSON(w, workdirResp{Stem: stem, Path: dir})
		return
	}
	resp := workdirResp{Stem: stem, Path: dir}
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".mp4") {
			continue
		}
		info, _ := e.Info()
		mp4 := filepath.Join(dir, e.Name())
		complete := false
		if marker, err := os.ReadFile(mp4 + ".done"); err == nil {
			if recorded, perr := strconv.ParseInt(strings.TrimSpace(string(marker)), 10, 64); perr == nil {
				if info != nil && recorded == info.Size() {
					complete = true
				}
			}
		}
		size := int64(0)
		if info != nil {
			size = info.Size()
		}
		resp.Entries = append(resp.Entries, workdirEntry{
			Name: e.Name(), Size: size, Complete: complete,
		})
	}
	writeJSON(w, resp)
}

// simulateInterrupt fakes a mid-encode failure so the Retry flow
// can be exercised without waiting for a real interrupt.
//
// Cloud jobs: writes an empty _SIMULATE_INTERRUPT sentinel to the
// job's S3 prefix. The remote user-data polls for it every 5s and,
// on seeing it, invokes the same trigger_interrupt bash path a real
// spot reclaim hits — writes SPOT INTERRUPTION: to _FAILED, rsyncs
// /work/tmp + /work/output, exits.
//
// Local jobs: docker kill on the worker container. SIGKILLs the
// Python process before it can clean up, so any in-flight variant
// has no .done sidecar written (exactly what happens on spot
// interrupt) — cli_local.py's preflight sweep deletes those on
// retry, leaving only the fully-complete files for reuse.
func (s *Server) simulateInterrupt(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	job := s.Manager.GetJob(id)
	if job == nil {
		http.Error(w, "job not found", 404)
		return
	}
	if job.Status != encode.StatusRunning {
		http.Error(w, "job is not running", 400)
		return
	}
	if job.Config.Target == encode.TargetCloud {
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
		return
	}
	// Local: find and SIGKILL the worker container(s) for this job.
	out, err := exec.Command("docker", "ps",
		"--filter", "label=encoder.job_id="+id,
		"--format", "{{.Names}}").Output()
	if err != nil {
		http.Error(w, "docker ps failed: "+err.Error(), 500)
		return
	}
	names := strings.Fields(string(out))
	if len(names) == 0 {
		http.Error(w, "no running worker container for this job", 404)
		return
	}
	for _, name := range names {
		exec.Command("docker", "kill", name).Run()
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
	// Local resume is automatic: cli_local.py's per-clip work dir
	// lives at TMPDIR/encode_<stem>/ on the host filesystem, so
	// variants + mezzanine from a prior partial run are still there
	// when the new worker starts. No config plumbing needed.
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

// getLadders returns all ladder definitions (built-in + user-defined) keyed by
// name. The UI populates the encode-options dropdown and the Ladders tab from
// this. Seed ladders carry "seed": true and are read-only.
func (s *Server) getLadders(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.Manager.Ladders.List())
}

// putLadder creates or replaces a user-defined ladder. Body is a LadderDef
// plus a "name". Built-in ladders are read-only (the store rejects them).
func (s *Server) putLadder(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name string `json:"name"`
		encode.LadderDef
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON: "+err.Error(), 400)
		return
	}
	if err := s.Manager.Ladders.Put(req.Name, req.LadderDef); err != nil {
		http.Error(w, err.Error(), 400)
		return
	}
	writeJSON(w, s.Manager.Ladders.List())
}

// deleteLadder removes a user-defined ladder. Built-in ladders can't be deleted.
func (s *Server) deleteLadder(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if err := s.Manager.Ladders.Delete(name); err != nil {
		http.Error(w, err.Error(), 400)
		return
	}
	writeJSON(w, s.Manager.Ladders.List())
}

// getPromote reports the configured promote destinations so the UI can show/hide
// the Promote button + "promote after encode" checkbox and label them.
func (s *Server) getPromote(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{"dests": encode.PromoteDests()})
}

// promoteOutput rsyncs one staged output to every configured destination and
// returns the per-destination results (200 with a mix of ok/failed dests; 400
// only when nothing could run — bad name or no PROMOTE_DESTS).
func (s *Server) promoteOutput(w http.ResponseWriter, r *http.Request) {
	results, err := s.Manager.Promote(r.PathValue("name"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	writeJSON(w, results)
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
		if strings.HasPrefix(name, ".") || strings.HasSuffix(name, "_tmp") || encode.IsDatedBackup(name) {
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

// awsStopExecution stops one Step Functions execution (aborts its Batch jobs).
// awsDeleteS3Prefix deletes every object under one S3 staging prefix — a job's
// staging or a single mezz-cache entry. cleanup.py restricts it to jobs/ or
// mezz/, so a bad prefix can't reach arbitrary keys.
func (s *Server) awsDeleteS3Prefix(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Prefix string `json:"prefix"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Prefix == "" {
		http.Error(w, `bad request: {"prefix": "..."} required`, 400)
		return
	}
	out, err := runPythonCloud("cleanup", "--delete-prefix", body.Prefix)
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

// awsSetMaxVCPUs changes the Batch compute env's maxvCpus ceiling live (the AWS
// panel's current-vs-2x toggle). Terraform ignores max_vcpus so it isn't reset.
func (s *Server) awsSetMaxVCPUs(w http.ResponseWriter, r *http.Request) {
	var body struct {
		MaxVCPUs int `json:"max_vcpus"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.MaxVCPUs <= 0 {
		http.Error(w, `bad request: {"max_vcpus": N} required`, 400)
		return
	}
	out, err := runPythonCloud("compute_env", "--set-max-vcpus", strconv.Itoa(body.MaxVCPUs))
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

func (s *Server) awsStopExecution(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Arn string `json:"arn"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Arn == "" {
		http.Error(w, `bad request: {"arn": "..."} required`, 400)
		return
	}
	// Guard against arg injection: execution ARNs are a fixed shape.
	if !strings.HasPrefix(body.Arn, "arn:aws:states:") {
		http.Error(w, "invalid execution arn", 400)
		return
	}
	out, err := runPythonCloud("batch_admin", "stop-execution", "--arn", body.Arn)
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

// awsTerminateBatchJob terminates one Batch job.
func (s *Server) awsTerminateBatchJob(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ID string `json:"id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.ID == "" {
		http.Error(w, `bad request: {"id": "..."} required`, 400)
		return
	}
	if strings.ContainsAny(body.ID, "/\\ ") {
		http.Error(w, "invalid job id", 400)
		return
	}
	out, err := runPythonCloud("batch_admin", "terminate-job", "--id", body.ID)
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

// awsBatchStopAll stops every running execution and terminates every active
// Batch job — the cloud-batch equivalent of the legacy "clear all" sweep.
func (s *Server) awsBatchStopAll(w http.ResponseWriter, r *http.Request) {
	out, err := runPythonCloud("batch_admin", "stop-all")
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}
