package encode

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// encodeMeta is the profile record written next to each output as encode.json —
// what ladder/profile and properties produced this rendition. Structured so
// go-live / dashboards can read it; the natural home for VMAF scores later.
type encodeMeta struct {
	Profile           string  `json:"profile"`
	Codec             string  `json:"codec"`
	MaxratePercent    int     `json:"maxrate_percent"`
	BufsizeMultiplier float64 `json:"bufsize_multiplier"`
	SegmentS          string  `json:"segment_s"`
	PartialS          string  `json:"partial_s"`
	GopS              string  `json:"gop_s"`
	OutputTag         string  `json:"output_tag,omitempty"`
	// Extra job config used to make this output.
	MaxRes         string `json:"max_res,omitempty"`
	MinRes         string `json:"min_res,omitempty"`
	HevcSinglePass bool   `json:"hevc_single_pass,omitempty"`
	// Per-codec encoding policy from the ladder profile (this rendition's codec):
	// ExtraArgs are the raw ffmpeg tokens appended after rate control; Passes is
	// the effective pass count (with the HevcSinglePass override applied). Both
	// omitted when at defaults, so existing outputs' encode.json is unchanged.
	ExtraArgs string `json:"extra_args,omitempty"`
	Passes    int    `json:"passes,omitempty"`
	Padding   string `json:"padding,omitempty"`
	// HlsFormat is OBSERVED from the packaged output ("fmp4" | "ts" | "both"),
	// not copied from the request — what was produced can differ from what was
	// asked for. Same DetectHLSFormat the Outputs badge uses, so the badge and
	// this field cannot contradict each other.
	HlsFormat     string `json:"hls_format,omitempty"`
	ChunkDuration string `json:"chunk_duration,omitempty"`
	ForceReencode bool   `json:"force_reencode,omitempty"`
	// Burnin records the text-overlay toggle only when it DEVIATES from the
	// default (on): a pointer left nil when burn-in was on, so existing outputs'
	// encode.json is byte-unchanged; set to &false when the overlay was disabled.
	Burnin *bool  `json:"burnin,omitempty"`
	Source string `json:"source,omitempty"`
	// TimeLimitS is JobConfig.Time — the ffmpeg `-t` cap. Recorded because it's
	// the ONE mezzanine-re-derivation input encode.json otherwise lacked (#117):
	// the mezzanine is a stream copy, byte-identical given source + time limit, so
	// a post-hoc VMAF audit can rebuild the exact reference only if this is known.
	// Absent = no limit (whole source).
	TimeLimitS string `json:"time_limit_s,omitempty"`
	// SourceSize/SourceModified fingerprint the input at encode time (#117): a
	// source edited or replaced afterward silently yields a wrong audit reference.
	// Size+mtime (not a content hash — sources run to multi-GB and this is written
	// on every encode) makes that mismatch detectable rather than invisible.
	// Recorded only when THIS output maps to a single unambiguous source file.
	SourceSize     int64  `json:"source_size,omitempty"`
	SourceModified string `json:"source_modified,omitempty"`
	// VMAF score provenance + lifecycle (#117). VmafModel/VmafComparisonHeight are
	// the scale a recorded score sits on (see VmafScore); uniform per output since
	// one encode.json is one source. VmafState lets the UI tell "never audited"
	// (pending) from "can't be" (ineligible: a burn-in overlay biases VMAF) from
	// done/failed. Never "running" — encode.json is written post-encode.
	VmafModel            string `json:"vmaf_model,omitempty"`
	VmafComparisonHeight int    `json:"vmaf_comparison_height,omitempty"`
	VmafState            string `json:"vmaf_state,omitempty"`
	EncodedAt            string `json:"encoded_at"`
	// What encoded this. FfmpegVersion is the build id; CodecLibs maps codec ->
	// encoder library ("hevc" -> "libx265 4.2+37-b81f650e"). Recorded because
	// the image tracks ffmpeg's rolling `latest`, so the Dockerfile no longer
	// names a version — and because the LIBRARY, not ffmpeg, determines the
	// bitstream: an x265 bump under a static ffmpeg changes HEVC output while
	// every other version string stays put.
	//
	// h264 is absent from CodecLibs by design — libx264 prints no version at
	// init and its core lives only in the bitstream SEI. FfmpegVersion is the
	// proxy for it. Absent != unrecorded.
	FfmpegVersion string            `json:"ffmpeg_version,omitempty"`
	CodecLibs     map[string]string `json:"codec_libs,omitempty"`
	Rungs         []metaRung        `json:"rungs,omitempty"`
}

type metaRung struct {
	Height      int `json:"height"`
	BitrateKbps int `json:"bitrate_kbps"`
	// Vmaf is populated later by the VMAF audit (#24); 0/absent = not measured.
	Vmaf float64 `json:"vmaf,omitempty"`
}

// writeEncodeMetaForDirs writes encode.json + a one-line manifest comment into
// each just-moved output dir, plus the run record (run.json) describing the run
// that produced it. Best-effort — never fails the job.
//
// Both are written here, before promote, so a promoted copy carries them. They
// answer different questions and neither subsumes the other: encode.json is
// about the ARTIFACT (the profile and properties of these renditions, all of it
// re-derivable), run.json is about the RUN (config, timings, cost, machines —
// none of which survives the server process, see RunRecordFile).
func (m *Manager) writeEncodeMetaForDirs(job *Job, dirs []string) {
	for _, name := range dirs {
		if IsDatedBackup(name) || strings.HasPrefix(name, ".") {
			continue
		}
		m.writeEncodeMeta(name, job.Config, job.Vmaf, job)
		m.writeRunRecord(name, job.Config, job)
	}
}

// vmafForRung returns the measured VMAF mean for a codec+height rung from the
// job's aggregated ENCODER-VMAF scores (#24), or 0 when unmeasured. Matches the
// base "<codec>/<h>p" label first, then a dup-resolution "<codec>/<h>p_*".
func vmafForRung(vmaf map[string]*VmafScore, codec string, height int) float64 {
	if vmaf == nil {
		return 0
	}
	base := fmt.Sprintf("%s/%dp", codec, height)
	if v := vmaf[base]; v != nil {
		return v.Mean
	}
	prefix := base + "_"
	for k, v := range vmaf {
		if v != nil && strings.HasPrefix(k, prefix) {
			return v.Mean
		}
	}
	return 0
}

// vmafProvenance returns the model + comparison height any measured rung of this
// codec was scored at (#117). Uniform per source, so the first non-empty score
// answers for the whole output. ("", 0) when nothing measured or an older worker
// emitted no provenance.
func vmafProvenance(vmaf map[string]*VmafScore, codec string) (string, int) {
	prefix := codec + "/"
	for k, v := range vmaf {
		if v != nil && v.Model != "" && strings.HasPrefix(k, prefix) {
			return v.Model, v.CommonHeight
		}
	}
	return "", 0
}

// hasVmafForCodec reports whether the aggregated map holds a measured score for
// any rung of this codec (#117) — the "done" signal, independent of whether the
// ladder could be resolved to build metaRungs.
func hasVmafForCodec(vmaf map[string]*VmafScore, codec string) bool {
	prefix := codec + "/"
	for k, v := range vmaf {
		if v != nil && v.Mean > 0 && strings.HasPrefix(k, prefix) {
			return true
		}
	}
	return false
}

// vmafStateFor derives the audit lifecycle state written to encode.json (#117):
//
//	done       — a measured score is present for this codec
//	failed     — the audit was requested (MeasureVmaf) but produced no score
//	ineligible — not requested and a burn-in overlay is on (it biases VMAF)
//	pending    — not requested, eligible, awaiting a future audit
//
// Never "running": encode.json is written only after the encode completes.
func vmafStateFor(cfg JobConfig, hasScores bool) string {
	switch {
	case hasScores:
		return "done"
	case cfg.MeasureVmaf:
		return "failed"
	case cfg.BurninEnabled():
		return "ineligible"
	default:
		return "pending"
	}
}

// sourceFingerprint stats the single source file that produced dirName and
// returns its size + mtime (RFC3339) for #117. Returns (0, "") when the job has
// no single unambiguous source for this dir (a multi-file job with no stem match,
// or an unstatable file) — an absent fingerprint reads as unknown, never as a
// default. Size+mtime, not a content hash: this runs on every encode and sources
// reach multi-GB, but a swapped/edited source still changes one of the two.
func (m *Manager) sourceFingerprint(cfg JobConfig, dirName string) (int64, string) {
	src := sourceForDir(cfg, dirName)
	if src == "" {
		return 0, ""
	}
	// A source is always a plain name resolved WITHIN SourceDir (the encoder joins
	// it the same way). Reject an absolute path or any "../" traversal so a crafted
	// Files entry can't aim the stat at an arbitrary path (go/path-injection, #200).
	if !filepath.IsLocal(src) {
		return 0, ""
	}
	fi, err := os.Stat(filepath.Join(m.SourceDir, src))
	if err != nil || fi.IsDir() {
		return 0, ""
	}
	return fi.Size(), fi.ModTime().UTC().Format(time.RFC3339)
}

func (m *Manager) writeEncodeMeta(dirName string, cfg JobConfig, vmaf map[string]*VmafScore,
	job *Job) {
	dir := filepath.Join(m.OutputDir, dirName)
	if fi, err := os.Stat(dir); err != nil || !fi.IsDir() {
		return
	}
	codec := ""
	for _, c := range []string{"h264", "hevc", "av1"} {
		if strings.Contains(dirName, "_"+c) {
			codec = c
			break
		}
	}
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	var def LadderDef
	if m.Ladders != nil {
		def, _ = m.Ladders.Get(ladderName)
	}
	maxrate := def.MaxratePercent
	if maxrate <= 0 {
		maxrate = 124
	}
	buf := def.BufsizeMultiplier
	if buf <= 0 {
		buf = 0.25
	}
	var rungs []metaRung
	for _, r := range def.Codecs[codec] { // [width, height, bitrate_kbps]
		if len(r) >= 3 {
			rungs = append(rungs, metaRung{
				Height: r[1], BitrateKbps: r[2],
				Vmaf: vmafForRung(vmaf, codec, r[1]),
			})
		}
	}
	// Effective pass count = the profile's, with the per-encode HevcSinglePass
	// override forcing HEVC to 1. Record only when it DEVIATES from the
	// codec default (2 for every codec) so a plain ladder's encode.json stays
	// unchanged; 0 is omitted by omitempty.
	//
	// NOTE the default moved: h264 was 1. An encode.json written before that
	// change records nothing for a single-pass h264 rung, so absence means 1
	// there and 2 here. Anything reading `passes` back for reproduction has to
	// date the file — which is an argument for recording it unconditionally,
	// not for keeping the omission. Left as-is for now because nothing reads it
	// yet; #287 is where that changes.
	defaultPasses := 2
	effectivePasses := def.passesFor(codec)
	if cfg.HevcSinglePass && codec == "hevc" {
		effectivePasses = 1
	}
	metaPasses := 0
	if effectivePasses != defaultPasses {
		metaPasses = effectivePasses
	}
	// Record burn-in only when disabled (default is on) — keeps the common
	// case's encode.json unchanged.
	var burnin *bool
	if !cfg.BurninEnabled() {
		off := false
		burnin = &off
	}
	// VMAF provenance + state (#117). A score is present iff the aggregated VMAF
	// map holds a measured mean for this codec — read from the map, not the built
	// metaRungs, since those depend on the ladder being resolvable.
	vmafModel, vmafCommonH := vmafProvenance(vmaf, codec)
	hasScores := hasVmafForCodec(vmaf, codec)
	vmafState := vmafStateFor(cfg, hasScores)
	// Fingerprint the single source that produced this dir, when unambiguous, so a
	// later audit can detect a source swapped out from under it (#117).
	srcSize, srcMod := m.sourceFingerprint(cfg, dirName)
	meta := encodeMeta{
		Profile:              ladderName,
		Codec:                codec,
		MaxratePercent:       maxrate,
		BufsizeMultiplier:    buf,
		SegmentS:             defaultVal(cfg.SegmentDuration, "6"),
		PartialS:             defaultVal(cfg.PartialDuration, "0.2"),
		GopS:                 defaultVal(cfg.GopDuration, "1.0"),
		OutputTag:            cfg.OutputTag,
		MaxRes:               cfg.MaxRes,
		MinRes:               cfg.MinRes,
		HevcSinglePass:       cfg.HevcSinglePass,
		ExtraArgs:            def.extraArgsFor(codec),
		Passes:               metaPasses,
		Padding:              cfg.Padding,
		HlsFormat:            DetectHLSFormat(dir),
		ChunkDuration:        cfg.ChunkDuration,
		ForceReencode:        cfg.ForceReencode,
		Burnin:               burnin,
		Source:               strings.Join(cfg.Files, ", "),
		TimeLimitS:           cfg.Time,
		SourceSize:           srcSize,
		SourceModified:       srcMod,
		VmafModel:            vmafModel,
		VmafComparisonHeight: vmafCommonH,
		VmafState:            vmafState,
		EncodedAt:            time.Now().UTC().Format(time.RFC3339),
		Rungs:                rungs,
	}
	if job != nil {
		job.mu.Lock()
		meta.FfmpegVersion = job.FfmpegVersion
		// Record only THIS output's codec. A job encoding h264+hevc writes two
		// dirs, and copying the whole map into both would claim each was made
		// with a library that never touched it.
		if v := job.CodecLibs[codec]; v != "" {
			meta.CodecLibs = map[string]string{codec: v}
		}
		job.mu.Unlock()
	}
	if b, err := json.MarshalIndent(meta, "", "  "); err == nil {
		_ = os.WriteFile(filepath.Join(dir, "encode.json"), b, 0644)
	}

	// One human-readable line for eyeballing, injected into the master manifests.
	line := fmt.Sprintf("encoder: profile=%s codec=%s maxrate=%d%% bufsize=%gx segment=%ss partial=%ss gop=%ss%s",
		ladderName, codec, maxrate, buf, meta.SegmentS, meta.PartialS, meta.GopS,
		tagNote(cfg.OutputTag))
	if metaPasses != 0 {
		line += fmt.Sprintf(" passes=%d", metaPasses)
	}
	if ea := def.extraArgsFor(codec); ea != "" {
		line += " extra_args=[" + ea + "]"
	}
	if !cfg.BurninEnabled() {
		line += " burnin=off"
	}
	// Provenance in the manifest itself, so a bare output dir handed to someone
	// else still answers "what encoded this?" without encode.json.
	if meta.FfmpegVersion != "" {
		line += " ffmpeg=" + meta.FfmpegVersion
	}
	if v := meta.CodecLibs[codec]; v != "" {
		line += " lib=[" + v + "]"
	}
	injectM3U8Comment(filepath.Join(dir, "master.m3u8"), line)
	injectMPDComment(filepath.Join(dir, "manifest.mpd"), line)
}

func tagNote(tag string) string {
	if tag == "" {
		return ""
	}
	return " tag=" + tag
}

// injectM3U8Comment inserts (or refreshes) a "# encoder: …" comment right after
// the #EXTM3U header. Idempotent; best-effort.
func injectM3U8Comment(path, line string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	lines := strings.Split(string(data), "\n")
	out := make([]string, 0, len(lines)+1)
	inserted := false
	for _, l := range lines {
		if strings.HasPrefix(l, "# encoder:") { // drop any prior one (idempotent)
			continue
		}
		out = append(out, l)
		if !inserted && strings.HasPrefix(strings.TrimSpace(l), "#EXTM3U") {
			out = append(out, "# "+line)
			inserted = true
		}
	}
	if !inserted { // no #EXTM3U found — prepend
		out = append([]string{"# " + line}, out...)
	}
	_ = os.WriteFile(path, []byte(strings.Join(out, "\n")), 0644)
}

// injectMPDComment inserts (or refreshes) an <!-- encoder: … --> comment right
// after the <?xml …?> declaration (before <MPD>). Idempotent; best-effort.
func injectMPDComment(path, line string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	s := string(data)
	// Strip a prior comment so repeated writes don't accumulate.
	if i := strings.Index(s, "<!-- encoder:"); i >= 0 {
		if j := strings.Index(s[i:], "-->"); j >= 0 {
			end := i + j + len("-->")
			s = s[:i] + strings.TrimLeft(s[end:], "\n")
		}
	}
	comment := "<!-- " + strings.ReplaceAll(line, "--", "—") + " -->\n"
	if k := strings.Index(s, "?>"); k >= 0 { // after the XML declaration
		insert := k + len("?>")
		s = s[:insert] + "\n" + comment + strings.TrimLeft(s[insert:], "\n")
	} else {
		s = comment + s
	}
	_ = os.WriteFile(path, []byte(s), 0644)
}
