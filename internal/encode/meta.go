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
	Profile           string     `json:"profile"`
	Codec             string     `json:"codec"`
	MaxratePercent    int        `json:"maxrate_percent"`
	BufsizeMultiplier float64    `json:"bufsize_multiplier"`
	SegmentS          string     `json:"segment_s"`
	PartialS          string     `json:"partial_s"`
	GopS              string     `json:"gop_s"`
	OutputTag         string     `json:"output_tag,omitempty"`
	// Extra job config used to make this output.
	MaxRes         string `json:"max_res,omitempty"`
	HevcSinglePass bool   `json:"hevc_single_pass,omitempty"`
	Padding        string `json:"padding,omitempty"`
	ChunkDuration  string `json:"chunk_duration,omitempty"`
	ForceReencode  bool   `json:"force_reencode,omitempty"`
	Source         string `json:"source,omitempty"`
	EncodedAt      string `json:"encoded_at"`
	Rungs          []metaRung `json:"rungs,omitempty"`
}

type metaRung struct {
	Height      int `json:"height"`
	BitrateKbps int `json:"bitrate_kbps"`
	// Vmaf is populated later by the VMAF audit (#24); 0/absent = not measured.
	Vmaf float64 `json:"vmaf,omitempty"`
}

// writeEncodeMetaForDirs writes encode.json + a one-line manifest comment into
// each just-moved output dir. Best-effort — never fails the job.
func (m *Manager) writeEncodeMetaForDirs(job *Job, dirs []string) {
	for _, name := range dirs {
		if IsDatedBackup(name) || strings.HasPrefix(name, ".") {
			continue
		}
		m.writeEncodeMeta(name, job.Config)
	}
}

func (m *Manager) writeEncodeMeta(dirName string, cfg JobConfig) {
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
		ladderName = "apple-uniq-live"
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
			rungs = append(rungs, metaRung{Height: r[1], BitrateKbps: r[2]})
		}
	}
	meta := encodeMeta{
		Profile:           ladderName,
		Codec:             codec,
		MaxratePercent:    maxrate,
		BufsizeMultiplier: buf,
		SegmentS:          defaultVal(cfg.SegmentDuration, "6"),
		PartialS:          defaultVal(cfg.PartialDuration, "0.2"),
		GopS:              defaultVal(cfg.GopDuration, "1.0"),
		OutputTag:         cfg.OutputTag,
		MaxRes:            cfg.MaxRes,
		HevcSinglePass:    cfg.HevcSinglePass,
		Padding:           cfg.Padding,
		ChunkDuration:     cfg.ChunkDuration,
		ForceReencode:     cfg.ForceReencode,
		Source:            strings.Join(cfg.Files, ", "),
		EncodedAt:         time.Now().UTC().Format(time.RFC3339),
		Rungs:             rungs,
	}
	if b, err := json.MarshalIndent(meta, "", "  "); err == nil {
		_ = os.WriteFile(filepath.Join(dir, "encode.json"), b, 0644)
	}

	// One human-readable line for eyeballing, injected into the master manifests.
	line := fmt.Sprintf("encoder: profile=%s codec=%s maxrate=%d%% bufsize=%gx segment=%ss partial=%ss gop=%ss%s",
		ladderName, codec, maxrate, buf, meta.SegmentS, meta.PartialS, meta.GopS,
		tagNote(cfg.OutputTag))
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
