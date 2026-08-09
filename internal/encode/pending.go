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

// PendingSidecar marks an output that was ENCODED but never packaged (#272).
// Its chunks are still in S3 and the packaging chain — join, Shaka, byteranges,
// HLS — has not run yet. Packaging happens when someone asks for the content.
//
// Presence IS the state, exactly as with RemoteSidecar: written by the
// orchestrator when a deferred run succeeds, removed by a completed package. The
// two languages agree on a filename and nothing else.
//
// It is a SEPARATE file from .remote.json rather than a flag inside it, because
// the two describe different situations and offer different actions:
//
//	.remote.json  packaged; the SEGMENTS are in S3      -> Download
//	.pending.json not packaged; the CHUNKS are in S3    -> Package
//
// #225 is the reason that distinction is a file and not a field. Its finding was
// that a metadata-only output is indistinguishable from a complete one by every
// other signal — right name, right rung subdirs, manifests present. A pending
// output is further from complete still: it has no manifests and no rung dirs at
// all. Collapsing the three states into two degraded the worst one; this is the
// fourth, and it gets its own name so nothing has to infer it.
const PendingSidecar = ".pending.json"

// PendingInfo mirrors the sidecar's JSON. Field names are a contract with
// _write_pending_sidecar in scripts/infinite_streaming_encoder/cli_batch.py.
type PendingInfo struct {
	S3Prefix string `json:"s3_prefix"`
	Codec    string `json:"codec"`

	// The packaging parameters, carried because they must OUTLIVE the execution.
	// cmd_poll reads them from describe_execution, but Step Functions history
	// ages out and this output may be packaged months later. They are recorded
	// rather than re-derived: the chunks were encoded to this segment duration,
	// and packaging to a different one yields playlists whose boundaries do not
	// land on the media's keyframes.
	SegmentDuration string `json:"segment_duration"`
	PartialDuration string `json:"partial_duration"`

	RecordedAt string `json:"recorded_at"`
	// ExpiresAt is advisory and is a FLOOR, not a guarantee — the lifecycle
	// clock runs from each object's own creation. It matters more here than on
	// .remote.json: there, expiry costs the media but the packaged manifests
	// survive; here the chunks are the ONLY copy, so expiry means the run must
	// be re-encoded from the source.
	ExpiresAt  string `json:"expires_at"`
	ExpiryDays int    `json:"expiry_days"`

	// Gone records that the prefix was OBSERVED empty, same semantics and same
	// reason as RemoteInfo.Gone: set, never delete the file. Deleting it would
	// reclassify the output as COMPLETE — and a pending dir has no media at all,
	// so the UI would offer Play on a directory containing one JSON file.
	Gone           bool   `json:"gone,omitempty"`
	GoneDetectedAt string `json:"gone_detected_at,omitempty"`
	GoneReason     string `json:"gone_reason,omitempty"`
}

// Expired reports whether the chunk staging is past its advertised expiry.
func (p *PendingInfo) Expired() bool {
	if p == nil {
		return false
	}
	return stagingExpired(p.ExpiresAt)
}

// Packageable reports whether Package can still succeed — the twin of
// RemoteInfo.Fetchable, and asked rather than re-derived for the same reason:
// available / expired / deleted is three states, and callers that check only
// the one they know about get the worst of them wrong.
func (p *PendingInfo) Packageable() bool {
	return p != nil && !p.Gone && !p.Expired()
}

// Unrecoverable reports that the chunks are provably gone, so this output can
// never be produced from staging and the source must be re-encoded.
//
// Distinct from !Packageable(): that is also true of an output already being
// packaged. This is the terminal one, and it is what the UI must say instead of
// offering a button that cannot work.
func (p *PendingInfo) Unrecoverable() bool {
	return p != nil && (p.Gone || p.Expired())
}

// ReadPending returns the pending sidecar for an output dir, or nil when there
// is none (the ordinary case: the output is packaged).
func ReadPending(dir string) *PendingInfo {
	b, err := os.ReadFile(filepath.Join(dir, PendingSidecar))
	if err != nil {
		return nil
	}
	var p PendingInfo
	if err := json.Unmarshal(b, &p); err != nil {
		return nil
	}
	return &p
}

// MarkPendingGone records that an output's chunk staging no longer holds the
// chunks. Returns false when there was nothing to mark.
//
// Written via temp file and rename for the same reason as MarkRemoteGone: the
// sidecar IS the state, and a torn write leaves an output that is neither
// pending nor packaged.
func MarkPendingGone(dir, reason string) (bool, error) {
	info := ReadPending(dir)
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
	final := filepath.Join(dir, PendingSidecar)
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

// DeferPackagingDefault leaves the chunks in S3 and packages on demand (#272),
// from DEFER_PACKAGING.
//
// Off unless asked for. The surprising outcome is an encode that finishes
// without producing anything playable, and — unlike skip-media-download, where
// the packaged manifests still land — a deferred run leaves a directory holding
// one JSON file. That has to be chosen, not inherited.
var DeferPackagingDefault = func() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("DEFER_PACKAGING"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}()

// deferPackaging resolves the per-job override against the server default.
//
// Deferring SUPERSEDES skip-media-download rather than combining with it: both
// exist to keep bytes in S3 until wanted, and deferring keeps strictly more
// (the packaged output is never created, so there is nothing to leave behind).
// Honouring both would mean writing a .remote.json for media that does not
// exist — two sidecars in one directory, describing incompatible states, which
// is precisely the collapse #225 spent two attempts undoing.
func (m *Manager) deferPackaging(cfg JobConfig) bool {
	if cfg.DeferPackaging != nil {
		return *cfg.DeferPackaging
	}
	return DeferPackagingDefault
}

// ErrPackageInFlight is returned when packaging is already running for an
// output — the second click on the Package button.
var ErrPackageInFlight = fmt.Errorf("packaging is already running for this output")

// PackageOutput packages a deferred output in the background (#272).
//
// Deliberately shares FetchState and the m.fetches map with FetchOutput rather
// than growing a parallel subsystem. The two are the same shape of thing from
// the UI's point of view — one long background operation per output, with a
// percentage, that ends by removing a sidecar — and an output can only ever be
// in one of them, because .pending.json and .remote.json describe mutually
// exclusive states. A second map would have to be kept consistent with the first
// for no gain. The State string is what distinguishes them.
func (m *Manager) PackageOutput(name string) error {
	dir, err := m.outputDirFor(name)
	if err != nil {
		return err
	}
	info := ReadPending(dir)
	if info == nil {
		return fmt.Errorf("%s: nothing pending — it is already packaged", name)
	}
	if info.Gone {
		return fmt.Errorf("%s: the chunks are no longer in S3 (%s, detected %s); "+
			"it cannot be packaged — re-encode to recreate it",
			name, info.GoneReason, info.GoneDetectedAt)
	}
	if info.Expired() {
		return fmt.Errorf("%s: chunk staging expired %s; there is nothing left to "+
			"package — re-encode to recreate it", name, info.ExpiresAt)
	}

	m.fetchMu.Lock()
	if m.fetches == nil {
		m.fetches = map[string]*FetchState{}
	}
	if st := m.fetches[name]; st != nil &&
		(st.State == "fetching" || st.State == "packaging") {
		m.fetchMu.Unlock()
		return ErrPackageInFlight
	}
	m.fetches[name] = &FetchState{
		Name: name, State: "packaging", StartedAt: time.Now().UnixMilli(),
	}
	m.fetchMu.Unlock()

	go m.runPackage(name, dir)
	return nil
}

// runPackage shells out to the same cli_batch.py the encode path uses, for the
// same reason FetchOutput does: the packaging is already implemented and tested
// there, and a Go reimplementation would be a second thing to keep in step with
// the chunk layout.
func (m *Manager) runPackage(name, dir string) {
	cmd := exec.Command("python3", "-m", "infinite_streaming_encoder.cli_batch",
		"package", "--dir", dir)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		m.finishPackage(name, dir, err)
		return
	}
	// Merge stderr in: a traceback is the only diagnostic when packaging dies.
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		m.finishPackage(name, dir, err)
		return
	}
	sc := bufio.NewScanner(stdout)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		line := strings.TrimRight(sc.Text(), "\r")
		// cli_phase's package stage marker, the same one the encode path emits.
		if mt := packageStageRe.FindStringSubmatch(line); mt != nil {
			pct, _ := strconv.ParseFloat(mt[1], 64)
			m.fetchMu.Lock()
			if st := m.fetches[name]; st != nil {
				st.Percent = pct
			}
			m.fetchMu.Unlock()
		}
	}
	m.finishPackage(name, dir, cmd.Wait())
}

// packageStageRe matches cli_phase's package stage marker, e.g.
// [[ENCODER-STAGE key=package:h264 status=running percent=42.0]]
var packageStageRe = regexp.MustCompile(
	`ENCODER-STAGE\s+key=package:\S+\s+status=\S+\s+percent=([\d.]+)`)

func (m *Manager) finishPackage(name, dir string, err error) {
	// EXIT_STAGING_GONE means the chunks are provably absent, so record it on the
	// sidecar. Otherwise the next click pays for the same listing to learn the
	// same thing, and the UI keeps offering a button that cannot work — which is
	// exactly the state #225 exists to prevent on the fetch side.
	var ee *exec.ExitError
	if errors.As(err, &ee) && ee.ExitCode() == exitStagingGone {
		MarkPendingGone(dir, "package found the chunk prefix empty")
		err = fmt.Errorf("chunks no longer in S3 — the staging prefix is empty, " +
			"so this cannot be packaged; re-encode to recreate it")
	}

	m.fetchMu.Lock()
	defer m.fetchMu.Unlock()
	if err != nil {
		if st := m.fetches[name]; st != nil {
			st.State = "failed"
			st.Error = err.Error()
		}
		return
	}
	// Success: drop the entry. The sidecar is gone too, so the output is now
	// indistinguishable from one packaged at encode time.
	delete(m.fetches, name)
}
