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
	"sync"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/imageinfo"
)

type Server struct {
	Manager *encode.Manager
	Mux     *http.ServeMux
	// Version, GitSha, ImageTag are stamped by cmd/server from -ldflags-injected
	// main.version / main.gitSha / main.imageTag. GitSha is the real HEAD;
	// ImageTag is the content hash the image was published under. CloudImage is
	// the DOCKER_IMAGE env var — what the worker pulls on job start. The About
	// tab pulls the image's OCI labels from GHCR to compare local vs cloud.
	Version    string
	GitSha     string
	ImageTag   string
	CloudImage string
	// GHCRImage is the GHCR image (no tag) whose OCI labels stand in for the
	// cloud worker image's — the worker image is an ECR ref imageinfo can't
	// query, but publish keeps ECR + GHCR in sync by tag.
	GHCRImage string
	// DevMount is set when the compose dev overlay bind-mounts working-tree code
	// over the image's copy (make farm-dev-up). The version stamps below then
	// describe the base image only — the Python actually running is whatever is
	// on disk, committed or not — so the About tab has to say so.
	DevMount bool

	imageInfo *imageinfo.Client

	// distMu guards distDisabled — the set of distributed-local worker machines
	// the user has toggled off from the UI (drained). A disabled machine's
	// worker container is stopped; the flag gives the pill immediate off-state
	// feedback without waiting for the Temporal poller list to expire.
	distMu       sync.Mutex
	distDisabled map[string]bool
}

func NewServer(mgr *encode.Manager) *Server {
	s := &Server{
		Manager: mgr,
		Mux:     http.NewServeMux(),
		imageInfo: imageinfo.NewClient(
			os.Getenv("GHCR_USERNAME"),
			os.Getenv("GHCR_PAT"),
		),
		distDisabled: map[string]bool{},
	}
	s.Mux.HandleFunc("GET /api/version", s.getVersion)
	s.Mux.HandleFunc("GET /api/dist/workers", s.distWorkers)
	s.Mux.HandleFunc("POST /api/dist/workers/{machine}", s.toggleDistWorker)
	s.Mux.HandleFunc("GET /api/settings", s.getSettings)
	s.Mux.HandleFunc("POST /api/settings", s.putSettings)
	s.Mux.HandleFunc("GET /api/sources", s.listSources)
	s.Mux.HandleFunc("POST /api/sources/upload", s.uploadSource)
	s.Mux.HandleFunc("GET /api/ladders", s.getLadders)
	s.Mux.HandleFunc("GET /api/ladders/{name}/estimates", s.ladderEstimates)
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
	s.Mux.HandleFunc("POST /api/jobs/{id}/redo", s.redoJob)
	s.Mux.HandleFunc("GET /api/jobs/{id}/workdir", s.jobWorkdir)
	// AWS inventory + cleanup (issue #5)
	s.Mux.HandleFunc("GET /api/aws/inventory", s.awsInventory)
	s.Mux.HandleFunc("GET /api/aws/image-state", s.awsImageState)
	s.Mux.HandleFunc("POST /api/aws/clear", s.awsClearAll)
	s.Mux.HandleFunc("POST /api/aws/jobs/{id}/cleanup", s.awsCleanupJob)
	s.Mux.HandleFunc("POST /api/aws/s3/delete-prefix", s.awsDeleteS3Prefix)
	s.Mux.HandleFunc("POST /api/aws/max-vcpus", s.awsSetMaxVCPUs)
	// local-dist (MinIO) staging usage + manual reclaim (#93) — the local twin
	// of the AWS S3 staging controls above.
	s.Mux.HandleFunc("GET /api/dist/staging", s.distStagingUsage)
	s.Mux.HandleFunc("POST /api/dist/staging/gc", s.distStagingGC)
	s.Mux.HandleFunc("POST /api/dist/staging/delete-prefix", s.distStagingDeletePrefix)
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

// imageTagOf extracts the tag from an image ref (e.g. ".../worker:a044a7e" →
// "a044a7e"), defaulting to "latest". Strips the registry/repo first so a
// registry port (host:5000/...) is never mistaken for the tag.
func imageTagOf(ref string) string {
	rest := ref
	if i := strings.LastIndex(rest, "/"); i >= 0 {
		rest = rest[i+1:]
	}
	if i := strings.LastIndex(rest, ":"); i >= 0 {
		return rest[i+1:]
	}
	return "latest"
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	out := map[string]any{
		"local": map[string]string{
			"version":   s.Version,
			"revision":  s.GitSha,
			"image_tag": s.ImageTag,
		},
		"cloud_image": s.CloudImage,
		// Whether the cloud-batch target is usable on this host. The UI disables
		// the option when false — mirrors the submit-time guard in job.go so a
		// local-only install doesn't offer a target that can't work.
		"cloud_configured": s.Manager.StateMachineArn != "",
		// Working-tree code is mounted over the image's: the stamps above
		// describe the base image, not what is executing.
		"dev_mount": s.DevMount,
	}
	if s.CloudImage != "" {
		// imageinfo reads OCI labels only from GHCR. The cloud image is an ECR
		// ref, so read the labels from its GHCR twin at the same tag (publish
		// keeps them in sync). Non-ECR / already-GHCR refs pass through.
		ref := s.CloudImage
		if !strings.HasPrefix(ref, "ghcr.io/") && s.GHCRImage != "" {
			ref = s.GHCRImage + ":" + imageTagOf(s.CloudImage)
		}
		out["cloud"] = s.imageInfo.Get(r.Context(), ref)
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
		cfg.Target = encode.TargetLocalDist
	}
	// Accept the old local-dist / cloud-batch values as aliases for local / cloud.
	cfg.Target = encode.NormalizeTarget(cfg.Target)
	if cfg.Codec == "" {
		cfg.Codec = "both"
	}
	// An inverted min/max band selects no rungs at all, which would otherwise
	// surface as a job that "succeeded" with an empty ladder. Reject it here.
	if minH, ok := encode.ResHeight(cfg.MinRes); ok {
		if maxH, ok2 := encode.ResHeight(cfg.MaxRes); ok2 && minH > maxH {
			http.Error(w, fmt.Sprintf("min resolution %s is above max resolution %s — no rungs would be encoded",
				cfg.MinRes, cfg.MaxRes), http.StatusBadRequest)
			return
		}
	}
	// A non-inverted band can still select zero rungs for a chosen codec whose
	// column doesn't reach that band (e.g. a 1800p floor on h264, which tops at
	// 1080p). That used to fail deep in the worker as "no ladder rungs fit this
	// source"; catch it here with a codec-specific message (issue #115).
	if err := s.Manager.ValidateResBand(cfg); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	// Reject unknown targets up front with a clear message, rather than letting
	// a bad target string fail cryptically deep in the encode path.
	switch cfg.Target {
	case encode.TargetLocalDist, encode.TargetCloudBatch:
	default:
		http.Error(w, fmt.Sprintf("unknown target %q — use local or cloud", cfg.Target), http.StatusBadRequest)
		return
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
	// Resume re-runs this same job in place (same ID → same MinIO prefix), so
	// the orchestrator reuses every chunk already staged and only encodes the
	// unfinished ones — one job row, not a new entry. See Manager.Resume.
	if !s.Manager.Resume(id) {
		http.Error(w, "job is not in a resumable state", 400)
		return
	}
	writeJSON(w, orig)
}

// redoJob submits a NEW job with the same config as `id` but re-encodes the WHOLE
// thing from scratch — no reuse. Unlike Resume (which re-runs the same job in
// place, reusing chunks already staged under its prefix), Redo mints a fresh job
// id → a fresh MinIO prefix, and sets ForceReencode so every rendition is
// produced again. Works on any terminal job (done, failed, or cancelled) —
// "do it all over" as a brand-new entry.
func (s *Server) redoJob(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	orig := s.Manager.GetJob(id)
	if orig == nil {
		http.Error(w, "job not found", 404)
		return
	}
	switch orig.Status {
	case encode.StatusDone, encode.StatusFailed, encode.StatusCancelled:
	default:
		http.Error(w, "job is still active — cancel it before redoing", 400)
		return
	}
	cfg := orig.Config
	cfg.ForceReencode = true // re-encode every rendition, ignore existing outputs
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

// ladderEstimates projects a ladder's rungs against the measured VMAF curve —
// the design-time answer to "is every rung earning its place?", available
// before anything is encoded.
//
// Kept OFF GET /api/ladders deliberately: that response round-trips through the
// ladder editor's save, so adding derived fields to it risks writing estimates
// back into the stored definition.
func (s *Server) ladderEstimates(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if _, ok := s.Manager.Ladders.Get(name); !ok {
		http.Error(w, "no such ladder", 404)
		return
	}
	reference, _ := strconv.Atoi(r.URL.Query().Get("reference"))
	if reference == 0 {
		reference = encode.DefaultCurveReference
	}
	// Curves are per-clip because quality-vs-bitrate is content-dependent. The
	// caller picks which content to estimate against; default is the store's
	// current clip (the seed until an audit overwrites it).
	clip := r.URL.Query().Get("clip")
	if clip == "" {
		clip = s.Manager.Curves.Clip
	}
	byCodec := map[string][]encode.RungEstimate{}
	for _, c := range []string{"h264", "hevc", "av1"} {
		if est := s.Manager.LadderEstimates(name, c, reference, clip); len(est) > 0 {
			byCodec[c] = est
		}
	}
	writeJSON(w, map[string]any{
		"ladder":    name,
		"reference": reference,
		// clip + the full list so the UI can name whose quality this is and
		// offer the others; estimated=true so it's never read as a measurement
		// of any particular encode.
		"clip":      clip,
		"clips":     s.Manager.Curves.Clips(),
		"estimated": true,
		"codecs":    byCodec,
	})
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

// runPythonCloud invokes `python3 -m infinite_streaming_encoder.cloud.<module> <args>` and
// returns the captured stdout on exit-0, or an error containing stderr.
func runPythonCloud(module string, args ...string) ([]byte, error) {
	fullArgs := append([]string{"-m", "infinite_streaming_encoder.cloud." + module, "--json"}, args...)
	cmd := exec.Command("python3", fullArgs...)
	// The Python modules read AWS_REGION, S3_BUCKET, etc. from the
	// server process's environment — same vars the encoder has been
	// forwarding into worker containers all along.
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return nil, fmt.Errorf("python3 -m infinite_streaming_encoder.cloud.%s exited %d: %s",
				module, ee.ExitCode(), strings.TrimSpace(string(ee.Stderr)))
		}
		return nil, fmt.Errorf("python3 -m infinite_streaming_encoder.cloud.%s: %w", module, err)
	}
	return out, nil
}

func (s *Server) awsInventory(w http.ResponseWriter, r *http.Request) {
	out, err := runPythonCloud("inventory")
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	out = s.attachFleetCPU(out)
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

// attachFleetCPU adds the per-machine CPU history from ENCODER-FLEET markers to
// the cloud inventory, under the SAME `fleet` key /api/dist/workers uses for
// local machines — so both targets carry identical data in an identical shape
// and the UI renders them with one code path.
//
// This replaces the `cw_cpu` field inventory.py used to fill from AWS/EC2
// CPUUtilization, which was paid for by enabling detailed monitoring on every
// instance: 44.8% of the AWS bill on a busy day, for a series then read at
// 5-minute granularity anyway (#137). The marker carries the same quantity —
// whole-instance busy %, from /proc/stat — with no lag and no cost.
//
// Best-effort: on any parse failure the inventory passes through untouched,
// because a missing sparkline must never cost the fleet view.
func (s *Server) attachFleetCPU(out []byte) []byte {
	fleet := s.Manager.FleetCPU()
	if len(fleet) == 0 {
		return out
	}
	var doc map[string]any
	if json.Unmarshal(out, &doc) != nil {
		return out
	}
	doc["fleet"] = fleet
	merged, err := json.Marshal(doc)
	if err != nil {
		return out
	}
	return merged
}

// awsImageState reports the cloud worker-image + AMI state for the About tab
// (#80): the job-def-pinned tag, whether it's in ECR, and whether a matching
// AMI is baked + wired (warm) vs baked-only vs pull-on-boot vs dangling.
func (s *Server) awsImageState(w http.ResponseWriter, r *http.Request) {
	out, err := runPythonCloud("image_state")
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

// runPythonDist is runPythonCloud's local-dist twin: it drives the MinIO
// staging module instead of the AWS cloud package. Same env-passthrough
// contract — the module reads MINIO_ENDPOINT / MINIO_ACCESS_KEY /
// MINIO_SECRET_KEY / DIST_S3_BUCKET from the server's own environment.
func runPythonDist(args ...string) ([]byte, error) {
	full := append([]string{"-m", "infinite_streaming_encoder.dist_staging", "--json"}, args...)
	cmd := exec.Command("python3", full...)
	cmd.Env = os.Environ()
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			// Exit 1 means some prefix failed to delete but the report was
			// still printed — hand it to the UI so it can show the partial
			// result, same as the AWS panel does for a partial sweep.
			if ee.ExitCode() == 1 && len(out) > 0 {
				return out, nil
			}
			return nil, fmt.Errorf("python3 -m infinite_streaming_encoder.dist_staging exited %d: %s",
				ee.ExitCode(), strings.TrimSpace(string(ee.Stderr)))
		}
		return nil, fmt.Errorf("python3 -m infinite_streaming_encoder.dist_staging: %w", err)
	}
	return out, nil
}

// distStagingUsage reports what the local-dist MinIO bucket is holding, per job
// prefix — the "why is the disk full" view.
func (s *Server) distStagingUsage(w http.ResponseWriter, r *http.Request) {
	out, err := runPythonDist("--usage")
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

// distStagingGC reclaims job staging idle longer than max_age_s (default 24h),
// on demand rather than waiting for the diststage watchdog's next sweep. The
// keep-list is always the current queued/running jobs, so a manual reclaim —
// even with max_age_s 0 — can't delete a running encode's staging.
func (s *Server) distStagingGC(w http.ResponseWriter, r *http.Request) {
	var body struct {
		MaxAgeS *float64 `json:"max_age_s"`
		DryRun  bool     `json:"dry_run"`
	}
	// An empty body is fine — it means "use the defaults".
	_ = json.NewDecoder(r.Body).Decode(&body)
	maxAge := 86400.0
	if body.MaxAgeS != nil && *body.MaxAgeS >= 0 {
		maxAge = *body.MaxAgeS
	}
	args := []string{"--gc", "--max-age-s", strconv.FormatFloat(maxAge, 'f', 0, 64)}
	if body.DryRun {
		args = append(args, "--dry-run")
	}
	for _, p := range s.Manager.ActiveDistPrefixes() {
		args = append(args, "--keep", p)
	}
	out, err := runPythonDist(args...)
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(out)
}

// distStagingDeletePrefix removes one job's staging. dist_staging restricts the
// prefix to jobs/<id>/, and an active job's prefix is refused here so the UI
// can't reclaim an encode out from under itself.
//
// The request's prefix is never handed to the subprocess: it's matched against
// the bucket's own listing and the *listed* prefix is what gets deleted. Job
// prefixes carry the source filename stem, so a character allowlist would
// reject legitimate names; resolving through the listing is both the stricter
// check and the one that can't put request data on an argv.
func (s *Server) distStagingDeletePrefix(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Prefix string `json:"prefix"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Prefix == "" {
		http.Error(w, `bad request: {"prefix": "..."} required`, 400)
		return
	}
	want := strings.Trim(body.Prefix, "/")
	for _, p := range s.Manager.ActiveDistPrefixes() {
		if strings.Trim(p, "/") == want {
			http.Error(w, "refused: that job is still queued or running", 409)
			return
		}
	}
	listed, err := runPythonDist("--usage")
	if err != nil {
		http.Error(w, err.Error(), 502)
		return
	}
	var doc struct {
		Prefixes []struct {
			Prefix string `json:"prefix"`
		} `json:"prefixes"`
	}
	if err := json.Unmarshal(listed, &doc); err != nil {
		http.Error(w, "could not list staging prefixes: "+err.Error(), 502)
		return
	}
	target := ""
	for _, p := range doc.Prefixes {
		if strings.Trim(p.Prefix, "/") == want {
			target = p.Prefix
			break
		}
	}
	if target == "" {
		http.Error(w, "no such staging prefix", 404)
		return
	}
	out, err := runPythonDist("--delete-prefix", target)
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
