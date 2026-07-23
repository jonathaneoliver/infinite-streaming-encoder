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

	// Parse the master to pull BANDWIDTH + width per rendition. Fall back to
	// zero if the master is missing or malformed; the UI handles it.
	actualByRes, widthByRes := readMasterBandwidth(dirPath)
	// Targets: prefer the profile's own rungs recorded in encode.json (handles
	// ANY resolution, incl. apple-uniq's unique heights); fall back to the
	// standard-tier table for older outputs without it.
	jsonTargets := readEncodeJSONTargets(dirPath)

	// Enumerate the ACTUAL resolution dirs (<N>p) rather than a fixed standard
	// list, so unique-resolution ladders (432p/468p/504p/684p …) show every rung.
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
		target := jsonTargets[h]
		if target == 0 {
			target = standardTargetByHeight(h, meta.codec) // legacy fallback
		}
		width := widthByRes[e.Name()]
		if width == 0 {
			width = h * 16 / 9 // 16:9 derive (apple-uniq rungs are 16:9)
		}
		size, _ := dirStats(filepath.Join(dirPath, e.Name()))
		tiers = append(tiers, ladderTier{
			Res:        e.Name(),
			Width:      width,
			Height:     h,
			TargetKbps: target,
			ActualKbps: actualByRes[e.Name()],
			SizeBytes:  size,
		})
	}
	sort.Slice(tiers, func(i, j int) bool { return tiers[i].Height < tiers[j].Height })

	writeJSON(w, ladderDoc{Codec: meta.codec, Tiers: tiers})
}

// readEncodeJSONTargets reads the per-rung target bitrates from encode.json
// (height → kbps). Empty when the file is absent (older outputs).
func readEncodeJSONTargets(dirPath string) map[int]int {
	out := map[int]int{}
	data, err := os.ReadFile(filepath.Join(dirPath, "encode.json"))
	if err != nil {
		return out
	}
	var m struct {
		Rungs []struct {
			Height      int `json:"height"`
			BitrateKbps int `json:"bitrate_kbps"`
		} `json:"rungs"`
	}
	if json.Unmarshal(data, &m) == nil {
		for _, r := range m.Rungs {
			if r.Height > 0 {
				out[r.Height] = r.BitrateKbps
			}
		}
	}
	return out
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
func readMasterBandwidth(dirPath string) (map[string]int, map[string]int) {
	out := map[string]int{}
	widths := map[string]int{}
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
		return out, widths
	}
	f, err := os.Open(path)
	if err != nil {
		return out, widths
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
		wdt, _ := strconv.Atoi(resMatch[1])
		h, _ := strconv.Atoi(resMatch[2])
		res := heightToRes(h)
		out[res] = bw / 1000 // bps → kbps
		widths[res] = wdt
	}
	return out, widths
}

// heightToRes names a rendition by its height ("<N>p") — for ANY height, so
// apple-uniq's unique resolutions (432p/468p/504p/684p) map correctly, not just
// the standard tiers.
func heightToRes(h int) string {
	if h <= 0 {
		return ""
	}
	return strconv.Itoa(h) + "p"
}
