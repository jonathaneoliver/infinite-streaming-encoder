package api

import (
	"bufio"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Mirror of scripts/infinite_streaming_encoder/ladder.py's LADDER. Changing the kbps
// values here is a visualization-only concern; the encoder's real
// targets are the Python ones. Keeping them in sync is a manual step
// — trivial but required when the Python ladder shifts.
type tierSpec struct {
	name   string
	width  int
	height int
	h264   int // kbps
	hevc   int
	av1    int
}

var ladderSpec = []tierSpec{
	{"360p", 640, 360, 600, 300, 300},
	{"540p", 960, 540, 1200, 900, 900},
	{"720p", 1280, 720, 2400, 1500, 1500},
	{"1080p", 1920, 1080, 5000, 4500, 4500},
	{"1440p", 2560, 1440, 11000, 7500, 7500},
	{"2160p", 3840, 2160, 21700, 15000, 15000},
}

func tierTargetKbps(t tierSpec, codec string) int {
	switch codec {
	case "h264":
		return t.h264
	case "hevc":
		return t.hevc
	case "av1":
		return t.av1
	}
	return 0
}

type ladderTier struct {
	Res        string `json:"res"`
	Width      int    `json:"width"`
	Height     int    `json:"height"`
	TargetKbps int    `json:"target_kbps"`
	// Three bandwidths: PeakKbps from the master's BANDWIDTH (peak segment
	// bitrate), AvgKbps from AVERAGE-BANDWIDTH (the encoder's declared average),
	// and ActualKbps measured from size × 8 ÷ duration (the true delivered
	// average). 0 when the source data is missing.
	PeakKbps   int   `json:"peak_kbps"`
	AvgKbps    int   `json:"avg_kbps"`
	ActualKbps int   `json:"actual_kbps"`
	SizeBytes  int64 `json:"size_bytes"`
	// Vmaf from encode.json (0 = not measured yet).
	Vmaf float64 `json:"vmaf,omitempty"`
}

type ladderDoc struct {
	Codec   string       `json:"codec"`
	Profile *profileInfo `json:"profile,omitempty"` // from encode.json (nil for older outputs)
	Tiers   []ladderTier `json:"tiers"`
}

// profileInfo is the encode.json profile record surfaced to the ladder view —
// the ladder/profile AND the extra job config used to make the output.
type profileInfo struct {
	Name              string  `json:"name"`
	MaxratePercent    int     `json:"maxrate_percent"`
	BufsizeMultiplier float64 `json:"bufsize_multiplier"`
	SegmentS          string  `json:"segment_s"`
	PartialS          string  `json:"partial_s"`
	GopS              string  `json:"gop_s"`
	OutputTag         string  `json:"output_tag,omitempty"`
	MaxRes            string  `json:"max_res,omitempty"`
	MinRes            string  `json:"min_res,omitempty"`
	HevcSinglePass    bool    `json:"hevc_single_pass,omitempty"`
	Padding           string  `json:"padding,omitempty"`
	ChunkDuration     string  `json:"chunk_duration,omitempty"`
	ForceReencode     bool    `json:"force_reencode,omitempty"`
	// Burnin is nil when the overlay was on (default) and &false when disabled,
	// mirroring encode.json — so the UI flags only the exception.
	Burnin    *bool  `json:"burnin,omitempty"`
	Source    string `json:"source,omitempty"`
	EncodedAt string `json:"encoded_at,omitempty"`
}

// encodeJSON mirrors the encode.json written by the encoder (internal/encode/meta.go).
type encodeJSON struct {
	Profile           string  `json:"profile"`
	MaxratePercent    int     `json:"maxrate_percent"`
	BufsizeMultiplier float64 `json:"bufsize_multiplier"`
	SegmentS          string  `json:"segment_s"`
	PartialS          string  `json:"partial_s"`
	GopS              string  `json:"gop_s"`
	OutputTag         string  `json:"output_tag"`
	MaxRes            string  `json:"max_res"`
	MinRes            string  `json:"min_res"`
	HevcSinglePass    bool    `json:"hevc_single_pass"`
	Padding           string  `json:"padding"`
	ChunkDuration     string  `json:"chunk_duration"`
	ForceReencode     bool    `json:"force_reencode"`
	Burnin            *bool   `json:"burnin"`
	Source            string  `json:"source"`
	EncodedAt         string  `json:"encoded_at"`
	Rungs             []struct {
		Height      int     `json:"height"`
		BitrateKbps int     `json:"bitrate_kbps"`
		Vmaf        float64 `json:"vmaf"`
	} `json:"rungs"`
}

var (
	bandwidthRe    = regexp.MustCompile(`[^-]BANDWIDTH=(\d+)`)
	avgBandwidthRe = regexp.MustCompile(`AVERAGE-BANDWIDTH=(\d+)`)
	resolutionRe   = regexp.MustCompile(`RESOLUTION=(\d+)x(\d+)`)
	extinfRe       = regexp.MustCompile(`^#EXTINF:([0-9.]+)`)
)

// ladder returns a per-tier view of an output dir: the ladder targets
// for the dir's codec plus the actually-encoded bitrate and size for
// each tier present. The UI renders this as a matrix so overshoots
// (x265 commonly exceeds its target at low bitrates) or missing tiers
// stand out immediately.
func (s *Server) ladder(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if name == "" || strings.Contains(name, "..") {
		http.Error(w, "invalid name", 400)
		return
	}
	dirPath := filepath.Join(s.Manager.OutputDir, name)
	meta := parseOutputMeta(name, dirPath)
	if meta.codec == "" {
		http.Error(w, "output dir has no codec suffix", 400)
		return
	}

	// Master → peak (BANDWIDTH), avg (AVERAGE-BANDWIDTH), width per rendition.
	peakByRes, avgByRes, widthByRes := readMasterBandwidth(dirPath)
	// encode.json (new): the profile + extra config + per-rung targets/vmaf,
	// handling ANY resolution. nil for older outputs.
	ej := readEncodeJSON(dirPath)
	jsonTargets := map[int]int{}
	jsonVmaf := map[int]float64{}
	var profile *profileInfo
	if ej != nil {
		for _, r := range ej.Rungs {
			if r.Height > 0 {
				jsonTargets[r.Height] = r.BitrateKbps
				jsonVmaf[r.Height] = r.Vmaf
			}
		}
		profile = &profileInfo{
			Name: ej.Profile, MaxratePercent: ej.MaxratePercent,
			BufsizeMultiplier: ej.BufsizeMultiplier, SegmentS: ej.SegmentS,
			PartialS: ej.PartialS, GopS: ej.GopS, OutputTag: ej.OutputTag,
			MaxRes: ej.MaxRes, MinRes: ej.MinRes, HevcSinglePass: ej.HevcSinglePass,
			Padding:       ej.Padding,
			ChunkDuration: ej.ChunkDuration, ForceReencode: ej.ForceReencode,
			Burnin: ej.Burnin,
			Source: ej.Source, EncodedAt: ej.EncodedAt,
		}
	}

	// The manifest's BANDWIDTH/AVERAGE-BANDWIDTH bundle the audio group, but the
	// ladder Target and the measured Actual are video-only — so subtract the audio
	// bitrate from Peak/Avg to keep all four columns comparable (video-only).
	audioKbps := 0
	audioDir := filepath.Join(dirPath, "audio")
	if fi, err := os.Stat(audioDir); err == nil && fi.IsDir() {
		if sz, _ := dirStats(audioDir); sz > 0 {
			dur := rungDurationS(audioDir)
			if dur <= 0 { // audio playlist may lack EXTINF — borrow a video rung's
				dur = firstRungDuration(dirPath)
			}
			if dur > 0 {
				audioKbps = int(float64(sz) * 8 / dur / 1000)
			}
		}
	}
	deAudio := func(v int) int {
		if v > audioKbps {
			return v - audioKbps
		}
		return v
	}

	// Enumerate the ACTUAL resolution dirs (<N>p) rather than a fixed standard
	// list, so unique-resolution ladders (432p/468p/504p/594p …) show every rung.
	resDirRe := regexp.MustCompile(`^(\d+)p$`)
	entries, _ := os.ReadDir(dirPath)
	var tiers []ladderTier
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		mm := resDirRe.FindStringSubmatch(e.Name())
		if mm == nil {
			continue // skips "audio" and any non-rung dir
		}
		h, _ := strconv.Atoi(mm[1])
		res := e.Name()
		target := jsonTargets[h]
		if target == 0 {
			target = standardTargetByHeight(h, meta.codec) // legacy fallback
		}
		width := widthByRes[res]
		if width == 0 {
			width = h * 16 / 9 // 16:9 derive (apple-uniq rungs are 16:9)
		}
		resDir := filepath.Join(dirPath, res)
		size, _ := dirStats(resDir)
		// True average bitrate = size × 8 ÷ duration.
		actual := 0
		if dur := rungDurationS(resDir); dur > 0 {
			actual = int(float64(size) * 8 / dur / 1000)
		}
		tiers = append(tiers, ladderTier{
			Res: res, Width: width, Height: h, TargetKbps: target,
			PeakKbps: deAudio(peakByRes[res]), AvgKbps: deAudio(avgByRes[res]),
			ActualKbps: actual, SizeBytes: size, Vmaf: jsonVmaf[h],
		})
	}
	sort.Slice(tiers, func(i, j int) bool { return tiers[i].Height < tiers[j].Height })

	writeJSON(w, ladderDoc{Codec: meta.codec, Profile: profile, Tiers: tiers})
}

// readEncodeJSON reads the full encode.json profile record (nil for older
// outputs that predate it).
func readEncodeJSON(dirPath string) *encodeJSON {
	data, err := os.ReadFile(filepath.Join(dirPath, "encode.json"))
	if err != nil {
		return nil
	}
	var ej encodeJSON
	if json.Unmarshal(data, &ej) != nil {
		return nil
	}
	return &ej
}

// standardTargetByHeight is the legacy fallback: the standard-tier target for an
// exact standard height, else 0 (unique heights show "—" until re-encoded with
// encode.json).
func standardTargetByHeight(h int, codec string) int {
	for _, t := range ladderSpec {
		if t.height == h {
			return tierTargetKbps(t, codec)
		}
	}
	return 0
}

// readMasterBandwidth returns a map of "<res>p" → kbps from the dir's
// master.m3u8. Finds a RESOLUTION=WxH alongside BANDWIDTH= on each
// EXT-X-STREAM-INF line and maps Y (height) back to the tier name.
func readMasterBandwidth(dirPath string) (peak, avg, widths map[string]int) {
	peak = map[string]int{}
	avg = map[string]int{}
	widths = map[string]int{}
	// Masters can live at the dir root as master.m3u8 or master_ts.m3u8.
	candidates := []string{"master.m3u8", "master_ts.m3u8"}
	var path string
	for _, c := range candidates {
		p := filepath.Join(dirPath, c)
		if _, err := os.Stat(p); err == nil {
			path = p
			break
		}
	}
	if path == "" {
		return peak, avg, widths
	}
	f, err := os.Open(path)
	if err != nil {
		return peak, avg, widths
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "#EXT-X-STREAM-INF") {
			continue
		}
		bwMatch := bandwidthRe.FindStringSubmatch(line)
		resMatch := resolutionRe.FindStringSubmatch(line)
		if bwMatch == nil || resMatch == nil {
			continue
		}
		wdt, _ := strconv.Atoi(resMatch[1])
		h, _ := strconv.Atoi(resMatch[2])
		res := heightToRes(h)
		bw, _ := strconv.Atoi(bwMatch[1])
		peak[res] = bw / 1000 // bps → kbps
		widths[res] = wdt
		if am := avgBandwidthRe.FindStringSubmatch(line); am != nil {
			a, _ := strconv.Atoi(am[1])
			avg[res] = a / 1000
		}
	}
	return peak, avg, widths
}

// firstRungDuration returns the content duration from the first <N>p rung found
// (they share one timeline) — a fallback when the audio playlist has no EXTINF.
func firstRungDuration(dirPath string) float64 {
	entries, _ := os.ReadDir(dirPath)
	rre := regexp.MustCompile(`^\d+p$`)
	for _, e := range entries {
		if e.IsDir() && rre.MatchString(e.Name()) {
			if d := rungDurationS(filepath.Join(dirPath, e.Name())); d > 0 {
				return d
			}
		}
	}
	return 0
}

// rungDurationS sums the #EXTINF segment durations in a rung's playlist to get
// the content duration (for the true-average bitrate). Tries playlist.m3u8 then
// any *.m3u8 in the rung dir; 0 if none found.
func rungDurationS(resDir string) float64 {
	path := filepath.Join(resDir, "playlist.m3u8")
	if _, err := os.Stat(path); err != nil {
		matches, _ := filepath.Glob(filepath.Join(resDir, "*.m3u8"))
		if len(matches) == 0 {
			return 0
		}
		path = matches[0]
	}
	f, err := os.Open(path)
	if err != nil {
		return 0
	}
	defer f.Close()
	var total float64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if m := extinfRe.FindStringSubmatch(sc.Text()); m != nil {
			d, _ := strconv.ParseFloat(m[1], 64)
			total += d
		}
	}
	return total
}

// heightToRes names a rendition by its height ("<N>p") — for ANY height, so
// apple-uniq's unique resolutions (432p/468p/504p/594p) map correctly, not just
// the standard tiers.
func heightToRes(h int) string {
	if h <= 0 {
		return ""
	}
	return strconv.Itoa(h) + "p"
}
