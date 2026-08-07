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
	m.finishFetch(name, cmd.Wait())
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
	if name == "" || name == "." || name == ".." ||
		strings.ContainsAny(name, `/\`) {
		return "", fmt.Errorf("invalid output name")
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
