package api

import (
	"bufio"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// Mirror of scripts/encoder/ladder.py's LADDER. Changing the kbps
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
	// ActualKbps comes from the master playlist's BANDWIDTH attribute,
	// which is ffmpeg's computed peak over the whole stream. May be 0
	// when the master has no BANDWIDTH line for this rendition.
	ActualKbps int   `json:"actual_kbps"`
	SizeBytes  int64 `json:"size_bytes"`
}

type ladderDoc struct {
	Codec string       `json:"codec"`
	Tiers []ladderTier `json:"tiers"`
}

var (
	bandwidthRe  = regexp.MustCompile(`BANDWIDTH=(\d+)`)
	resolutionRe = regexp.MustCompile(`RESOLUTION=(\d+)x(\d+)`)
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

	// Parse the master to pull BANDWIDTH per rendition. Fall back to
	// zero if the master is missing or malformed; the UI handles it.
	actualByRes := readMasterBandwidth(dirPath)

	var tiers []ladderTier
	for _, t := range ladderSpec {
		resDir := filepath.Join(dirPath, t.name)
		info, err := os.Stat(resDir)
		if err != nil || !info.IsDir() {
			continue
		}
		size, _ := dirStats(resDir)
		tiers = append(tiers, ladderTier{
			Res:        t.name,
			Width:      t.width,
			Height:     t.height,
			TargetKbps: tierTargetKbps(t, meta.codec),
			ActualKbps: actualByRes[t.name],
			SizeBytes:  size,
		})
	}

	writeJSON(w, ladderDoc{Codec: meta.codec, Tiers: tiers})
}

// readMasterBandwidth returns a map of "<res>p" → kbps from the dir's
// master.m3u8. Finds a RESOLUTION=WxH alongside BANDWIDTH= on each
// EXT-X-STREAM-INF line and maps Y (height) back to the tier name.
func readMasterBandwidth(dirPath string) map[string]int {
	out := map[string]int{}
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
		return out
	}
	f, err := os.Open(path)
	if err != nil {
		return out
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
		bw, _ := strconv.Atoi(bwMatch[1])
		h, _ := strconv.Atoi(resMatch[2])
		out[heightToRes(h)] = bw / 1000 // bps → kbps
	}
	return out
}

func heightToRes(h int) string {
	switch h {
	case 360, 540, 720, 1080, 1440, 2160:
		return strconv.Itoa(h) + "p"
	}
	return ""
}
