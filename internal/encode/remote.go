package encode

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"time"
)

// RemoteSidecar is the file cli_batch.py writes into an output dir whose media
// was deliberately left in S3 (#214). Its presence IS the remote state: it is
// written by the metadata-only sync-back and removed by a completed fetch, so
// the two sides never need to agree on anything but this filename.
//
// A file rather than a stdout marker because it moves with the directory
// through moveTmpToOutput and survives a restart of the server.
const RemoteSidecar = ".remote.json"

// RemoteInfo mirrors the sidecar's JSON. Field names are a contract with
// _write_remote_sidecar in scripts/infinite_streaming_encoder/cli_batch.py.
type RemoteInfo struct {
	S3Prefix     string `json:"s3_prefix"`
	PendingFiles int    `json:"pending_files"`
	PendingBytes int64  `json:"pending_bytes"`
	RecordedAt   string `json:"recorded_at"`
	// ExpiresAt is advisory: the bucket's lifecycle clock runs from each
	// object's own creation, so this is the floor rather than a guarantee.
	ExpiresAt  string `json:"expires_at"`
	ExpiryDays int    `json:"expiry_days"`

	// Gone records that the prefix was OBSERVED empty — a manual staging
	// clear, a console delete, or the lifecycle firing early (#225). Expiry
	// alone was being used as a proxy for existence, and the two are different
	// claims: everything that removes objects other than the clock produced an
	// output that looked perfectly healthy right up until the click.
	//
	// Set rather than deleting the sidecar. Deleting it would reclassify the
	// output as COMPLETE, which is the one wrong answer available: the UI would
	// offer Play, hls.js would load the playlist, and every segment would 404.
	Gone bool `json:"gone,omitempty"`
	// GoneDetectedAt/GoneReason keep the provenance — which prefix, how much,
	// when it was expected to expire, and what noticed it had not.
	GoneDetectedAt string `json:"gone_detected_at,omitempty"`
	GoneReason     string `json:"gone_reason,omitempty"`
}

// Expired reports whether the staging prefix is past its advertised expiry, in
// which case the media is gone and Download must not be offered.
func (r *RemoteInfo) Expired() bool {
	if r == nil || r.ExpiresAt == "" {
		return false
	}
	t, err := time.Parse(time.RFC3339, r.ExpiresAt)
	if err != nil {
		return false // unparseable: assume still fetchable, let the fetch say
	}
	return time.Now().After(t)
}

// Fetchable reports whether Download can still succeed. The three states —
// available, expired, deleted — used to render as two, and the missing one
// degraded the worst, so callers ask this rather than re-deriving it from
// whichever field they happen to know about.
func (r *RemoteInfo) Fetchable() bool {
	return r != nil && !r.Gone && !r.Expired()
}

// MarkRemoteGone records on an output's sidecar that its staging prefix no
// longer holds the media. Returns false when there was nothing to mark (no
// sidecar, or already marked), so a sweep can report only what it changed.
//
// Written via a temp file and rename: the sidecar IS the state, and a partial
// write would leave an output that is neither remote nor local.
func MarkRemoteGone(dir, reason string) (bool, error) {
	info := ReadRemote(dir)
	if info == nil || info.Gone {
		return false, nil
	}
	info.Gone = true
	info.GoneDetectedAt = time.Now().UTC().Format(time.RFC3339)
	info.GoneReason = reason

	b, err := json.MarshalIndent(info, "", "  ")
	if err != nil {
		return false, err
	}
	final := filepath.Join(dir, RemoteSidecar)
	tmp := final + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return false, err
	}
	if err := os.Rename(tmp, final); err != nil {
		os.Remove(tmp)
		return false, err
	}
	return true, nil
}

// MarkGoneUnderPrefix marks every output whose staging lives under an S3 prefix
// that was just deleted, and returns how many it changed.
//
// This is the "invalidate on purpose" half of #225: whatever cleared the
// staging already knows which prefixes it removed, so the common case is
// detected immediately instead of on a user's click days later. Discovery at
// fetch time stays as the backstop for everything the server did not do
// itself — a console delete, or the lifecycle firing early.
//
// Deliberately NOT on the /api/outputs path: this runs once per clear, over
// local files only, and costs no S3 call.
func (m *Manager) MarkGoneUnderPrefix(s3Prefix, reason string) int {
	s3Prefix = strings.TrimRight(strings.TrimSpace(s3Prefix), "/")
	if !strings.HasPrefix(s3Prefix, "s3://") {
		return 0
	}
	entries, err := os.ReadDir(m.OutputDir)
	if err != nil {
		return 0
	}
	marked := 0
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		dir := filepath.Join(m.OutputDir, e.Name())
		info := ReadRemote(dir)
		if info == nil || info.Gone {
			continue
		}
		// The sidecar points at a codec subdirectory INSIDE the job prefix
		// (…/jobs/<id>-<base>/output_h264), so an equality test would never
		// match what the cleanup actually deletes (…/jobs/<id>-<base>/). The
		// "/" guard keeps jobs/12-clip from matching jobs/12-clip2.
		p := strings.TrimRight(info.S3Prefix, "/")
		if p != s3Prefix && !strings.HasPrefix(p, s3Prefix+"/") {
			continue
		}
		if ok, err := MarkRemoteGone(dir, reason); err == nil && ok {
			marked++
		}
	}
	return marked
}

// ReadRemote returns the output dir's remote record, or nil when the media is
// local (the common case — a normal full download leaves no sidecar).
func ReadRemote(dir string) *RemoteInfo {
	b, err := os.ReadFile(filepath.Join(dir, RemoteSidecar))
	if err != nil {
		return nil
	}
	var info RemoteInfo
	if json.Unmarshal(b, &info) != nil || info.S3Prefix == "" {
		return nil
	}
	return &info
}

// FetchState is one output's in-flight download. Its existence answers the
// question the sidecar cannot: an output mid-fetch is neither remote nor
// complete, and Play must stay disabled for that whole window.
type FetchState struct {
	Name      string  `json:"name"`
	State     string  `json:"state"` // "fetching" | "failed"
	Percent   float64 `json:"percent"`
	Error     string  `json:"error,omitempty"`
	StartedAt int64   `json:"started_at"`
}

var fetchStageRe = regexp.MustCompile(
	`^\[\[ENCODER-STAGE key=fetch:media status=(\S+) percent=([0-9.]+)\]\]$`)

// FetchOutput pulls an output's media down from S3 in the background.
//
// Shells out to cli_batch.py rather than importing an AWS SDK for Go, matching
// how the manager already calls cloud.batch_admin — and reusing the tested
// parallel download rather than reimplementing it.
//
// Idempotent at both layers: a second call while one is running returns
// ErrFetchInFlight, and cli_batch.py's fetch skips objects already on disk at
// the right size, so an interrupted fetch resumes rather than re-paying.
func (m *Manager) FetchOutput(name string) error {
	dir, err := m.outputDirFor(name)
	if err != nil {
		return err
	}
	info := ReadRemote(dir)
	if info == nil {
		return fmt.Errorf("%s: nothing pending — media is already local", name)
	}
	if info.Gone {
		return fmt.Errorf("%s: the media is no longer in S3 (%s, detected %s); "+
			"it cannot be fetched — re-encode to recreate it",
			name, info.GoneReason, info.GoneDetectedAt)
	}
	if info.Expired() {
		return fmt.Errorf("%s: staging expired %s; the media is gone from S3",
			name, info.ExpiresAt)
	}

	m.fetchMu.Lock()
	if m.fetches == nil {
		m.fetches = map[string]*FetchState{}
	}
	if st := m.fetches[name]; st != nil && st.State == "fetching" {
		m.fetchMu.Unlock()
		return ErrFetchInFlight
	}
	m.fetches[name] = &FetchState{
		Name: name, State: "fetching", StartedAt: time.Now().UnixMilli(),
	}
	m.fetchMu.Unlock()

	go m.runFetch(name, dir)
	return nil
}

// SkipMediaDownloadDefault is the server-wide default for new cloud jobs,
// from SKIP_OUTPUT_MEDIA. Off unless deliberately turned on: the surprising
// outcome is an encode whose media silently is not on disk, so that has to be
// asked for rather than inherited.
var SkipMediaDownloadDefault = func() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("SKIP_OUTPUT_MEDIA"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}()

// skipMediaDownload resolves the per-job override against the server default.
func (m *Manager) skipMediaDownload(cfg JobConfig) bool {
	if cfg.SkipMediaDownload != nil {
		return *cfg.SkipMediaDownload
	}
	return SkipMediaDownloadDefault
}

// PackageOnHostDefault moves the packaging chain (join → Shaka → byteranges →
// HLS) off Batch and onto the control plane (#197). Same shape of knob as
// SkipMediaDownloadDefault above, and the tail-side twin of #266's host
// mezzanine.
//
// Default ON. On two measured runs the post-encode tail was 4m18s, of which
// 141s was dead time bracketing the pkgall Batch job — 43s of queue wait and
// container start before it, ~1m55s of state machine exit and poll latency
// after it — plus a 26s `download:outputs` re-fetching what that job had just
// uploaded. None of that survives packaging locally, and the host is the faster
// machine per-stream anyway (median 1.58x over Graviton across 12 rungs,
// measured in encode_speeds.json).
var PackageOnHostDefault = func() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("PACKAGE_ON_HOST"))) {
	case "0", "false", "no", "off":
		return false
	}
	return true
}()

// packageOnHost decides whether THIS job packages locally.
//
// It is forced off when the run is leaving its media in S3. Those two features
// want opposite things: skip-media-download exists so the segments never come
// down the link, and host packaging cannot produce a playable output without
// pulling every chunk. Honouring both would mean fetching the whole ladder and
// then uploading the packaged result back so a later `fetch` could retrieve it —
// strictly more transfer than either option alone.
func (m *Manager) packageOnHost(cfg JobConfig) bool {
	return PackageOnHostDefault && !m.skipMediaDownload(cfg)
}

// ErrFetchInFlight is returned when a download is already running for an
// output — the second click on the Download button, which must be a no-op
// rather than a second concurrent transfer.
var ErrFetchInFlight = fmt.Errorf("a fetch is already running for this output")

func (m *Manager) runFetch(name, dir string) {
	// PYTHONPATH=/app/scripts is baked into the image, so no working directory
	// is needed — same invocation shape as cloudExecutionResumable.
	cmd := exec.Command("python3", "-m", "infinite_streaming_encoder.cli_batch",
		"fetch", "--dir", dir)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		m.finishFetch(name, err)
		return
	}
	// Merge stderr into the same pipe: a traceback is the only diagnostic when a
	// fetch dies, and it must not be discarded.
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		m.finishFetch(name, err)
		return
	}
	sc := bufio.NewScanner(stdout)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		line := strings.TrimRight(sc.Text(), "\r")
		if mt := fetchStageRe.FindStringSubmatch(line); mt != nil {
			pct, _ := strconv.ParseFloat(mt[2], 64)
			m.fetchMu.Lock()
			if st := m.fetches[name]; st != nil {
				st.Percent = pct
			}
			m.fetchMu.Unlock()
		}
	}
	m.finishFetch(name, fetchError(cmd.Wait()))
}

// exitStagingGone is cli_batch.py's EXIT_STAGING_GONE — the prefix listed
// empty, so there is nothing to fetch and never will be. A distinct code
// rather than a message match: the message is for people, and a fetch that
// dies mid-transfer also prints about S3.
const exitStagingGone = 4

// fetchError turns cli_batch's exit status into something the UI can show.
// Without this the badge reads "exit status 4", which tells the user nothing
// about the one thing that matters — that clicking again will not help.
func fetchError(err error) error {
	var ee *exec.ExitError
	if errors.As(err, &ee) && ee.ExitCode() == exitStagingGone {
		return fmt.Errorf("media no longer in S3 — the staging prefix is empty, " +
			"so it cannot be downloaded; re-encode to recreate it")
	}
	return err
}

func (m *Manager) finishFetch(name string, err error) {
	m.fetchMu.Lock()
	defer m.fetchMu.Unlock()
	if err != nil {
		if st := m.fetches[name]; st != nil {
			st.State = "failed"
			st.Error = err.Error()
		}
		return
	}
	// Success: drop the entry entirely. The sidecar is gone too, so the output
	// is now indistinguishable from one downloaded automatically — which is the
	// acceptance criterion.
	delete(m.fetches, name)
}

// FetchStateFor returns the in-flight (or last failed) fetch for an output.
func (m *Manager) FetchStateFor(name string) *FetchState {
	m.fetchMu.Lock()
	defer m.fetchMu.Unlock()
	if st := m.fetches[name]; st != nil {
		cp := *st
		return &cp
	}
	return nil
}

// outputDirFor resolves an output name to a directory inside OutputDir,
// rejecting anything that escapes it. The name arrives from a request path.
func (m *Manager) outputDirFor(name string) (string, error) {
	if err := ValidPathSegment("output name", name); err != nil {
		return "", err
	}
	dir := filepath.Join(m.OutputDir, name)
	fi, err := os.Stat(dir)
	if err != nil || !fi.IsDir() {
		return "", fmt.Errorf("no such output: %s", name)
	}
	return dir, nil
}

// DetectHLSFormat reports "fmp4", "ts", "both", or "" for a packaged output dir.
//
// One definition, in encode, because BOTH the writer of encode.json
// (writeEncodeMeta) and the reader that renders the Outputs badge
// (api.parseOutputMeta) need it. Two copies would be free to disagree, and the
// disagreement would show up as a badge that contradicts the JSON beside it.
//
// Prefers real segment files; falls back to the PLAYLISTS when there are none.
// That fallback is what makes a metadata-only output (#214) still report its
// format: the .m4s are in S3, but the .m3u8 that references them is on disk.
func DetectHLSFormat(dirPath string) string {
	hasM4S, hasTS := false, false
	entries, _ := os.ReadDir(dirPath)
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		subEntries, _ := os.ReadDir(filepath.Join(dirPath, e.Name()))
		for _, se := range subEntries {
			switch filepath.Ext(se.Name()) {
			case ".m4s":
				hasM4S = true
			case ".ts":
				hasTS = true
			}
		}
		if hasM4S && hasTS {
			break
		}
	}
	if !hasM4S && !hasTS {
		hasM4S, hasTS = hlsFormatFromPlaylists(dirPath)
	}
	switch {
	case hasM4S && hasTS:
		return "both"
	case hasTS:
		return "ts"
	case hasM4S:
		return "fmp4"
	}
	return ""
}

// hlsFormatFromPlaylists infers the format from the .m3u8 files, which stay
// local even when the segments do not.
//
//	EXT-X-MAP:URI=... or a .m4s URI  -> fMP4
//	a .ts URI                        -> TS
//
// Bails at the first hit of each kind: this runs per output dir on every
// /api/outputs call, which already costs ~0.8s over 30 outputs and must not
// grow into a full manifest parse.
func hlsFormatFromPlaylists(dirPath string) (m4s, ts bool) {
	entries, _ := os.ReadDir(dirPath)
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		sub := filepath.Join(dirPath, e.Name())
		subEntries, _ := os.ReadDir(sub)
		for _, se := range subEntries {
			if filepath.Ext(se.Name()) != ".m3u8" {
				continue
			}
			b, err := os.ReadFile(filepath.Join(sub, se.Name()))
			if err != nil {
				continue
			}
			body := string(b)
			if strings.Contains(body, "EXT-X-MAP:") || strings.Contains(body, ".m4s") {
				m4s = true
			}
			if strings.Contains(body, ".ts\n") || strings.Contains(body, ".ts\r") {
				ts = true
			}
			if m4s && ts {
				return
			}
		}
	}
	return
}
