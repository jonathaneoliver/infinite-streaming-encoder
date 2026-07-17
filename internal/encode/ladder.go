package encode

import (
	"fmt"
	"sort"
)

// Data-driven ladder resolution for the cloud-batch path. The control plane
// resolves each variant's concrete rung (label + resolution + bitrate + preset)
// from the ladder definition and passes it to the worker via the SFN input, so
// the worker needs no ladder knowledge — which is what lets user-defined
// ladders work in the cloud.
//
// NOTE: the seed geometry + bitrates below mirror scripts/encoder/ladder.py's
// SEED_LADDERS. They are duplicated here because Go builds the SFN input up
// front (bitrates included) while Python encodes locally. Keep the two in sync;
// a later stage unifies them behind a single persisted ladders.json store that
// both read. res_name is derived as "{height}p" and labels get an ordinal
// suffix when a codec repeats a resolution — identical to ladder.py so cloud
// and local produce the same {codec}_{label} outputs.

type ladderRung struct {
	Label   string
	ResName string
	Width   int
	Height  int
	Bitrate int // kbps
	Preset  string
}

// sfnVariant is one entry in the SFN input's "variants" list. All the
// worker-facing fields are strings because Batch job Parameters (Ref::x) are
// string substitutions; Priority stays an int (SchedulingPriorityOverride
// wants a number). heightRank is internal (submission-order sort only) and is
// not marshaled.
type sfnVariant struct {
	Codec      string `json:"codec"`
	Label      string `json:"label"`
	Width      string `json:"width"`
	Height     string `json:"height"`
	Bitrate    string `json:"bitrate"`
	Preset     string `json:"preset"`
	VCPU       string `json:"vcpu"`
	Memory     string `json:"memory"`
	Priority   int    `json:"priority"`
	TwoPass    string `json:"two_pass"`
	heightRank int    `json:"-"`
}

// rawRung is [width, height, bitrate_kbps]; preset defaults to "medium".
type rawRung [3]int

// seedLadders mirrors ladder.py SEED_LADDERS (av1 == hevc). Bitrate columns
// are per-codec kbps.
var seedLadders = map[string]map[string][]rawRung{
	"legacy": {
		"h264": {{640, 360, 600}, {960, 540, 1722}, {1280, 720, 2779}, {1920, 1080, 6957}, {2560, 1440, 16995}, {3840, 2160, 26453}},
		"hevc": {{640, 360, 300}, {960, 540, 1001}, {1280, 720, 1662}, {1920, 1080, 4273}, {2560, 1440, 10547}, {3840, 2160, 16458}},
	},
	"apple": {
		"h264": {{416, 234, 145}, {640, 360, 365}, {768, 432, 730}, {768, 432, 1100}, {960, 540, 2000}, {1280, 720, 3000}, {1280, 720, 4500}, {1920, 1080, 6000}, {1920, 1080, 7800}},
		"hevc": {{640, 360, 145}, {768, 432, 300}, {960, 540, 600}, {960, 540, 900}, {960, 540, 1600}, {1280, 720, 2400}, {1280, 720, 3400}, {1920, 1080, 4500}, {1920, 1080, 5800}, {2560, 1440, 8100}, {3840, 2160, 11600}, {3840, 2160, 16800}},
	},
	"apple-uniq": {
		"h264": {{416, 234, 145}, {640, 360, 365}, {704, 396, 730}, {768, 432, 1100}, {960, 540, 2000}, {1216, 684, 3000}, {1280, 720, 4500}, {1856, 1044, 6000}, {1920, 1080, 7800}},
		"hevc": {{640, 360, 145}, {768, 432, 300}, {832, 468, 600}, {896, 504, 900}, {960, 540, 1600}, {1216, 684, 2400}, {1280, 720, 3400}, {1856, 1044, 4500}, {1920, 1080, 5800}, {2560, 1440, 8100}, {3776, 2124, 11600}, {3840, 2160, 16800}},
	},
}

// maxResHeight maps a --max-res tier name to its pixel height for capping.
var maxResHeight = map[string]int{
	"360p": 360, "540p": 540, "720p": 720,
	"1080p": 1080, "1440p": 1440, "2160p": 2160,
}

// ladderExists reports whether a ladder name is known.
func ladderExists(name string) bool {
	_, ok := seedLadders[name]
	return ok
}

// resolveLadderRungs returns the rungs to encode for a (ladder, codec),
// filtered to those that fit the source (no upscale) and --max-res, with
// ordinal labels for repeated resolutions. Mirrors ladder.select_rungs. av1
// reuses the hevc column. Returns nil for an unknown ladder/codec.
func resolveLadderRungs(ladderName, codec, maxRes string, sourceWidth int) []ladderRung {
	ladder, ok := seedLadders[ladderName]
	if !ok {
		return nil
	}
	col := codec
	if codec == "av1" {
		col = "hevc"
	}
	rows, ok := ladder[col]
	if !ok {
		return nil
	}

	// Count resolution occurrences (by height) for label disambiguation over
	// the FULL ladder, so filtering never changes a rung's label.
	counts := map[string]int{}
	for _, r := range rows {
		rn := fmt.Sprintf("%dp", r[1])
		counts[rn]++
	}

	maxH, capByRes := maxResHeight[maxRes]
	idx := map[string]int{}
	var out []ladderRung
	for _, r := range rows {
		w, h, b := r[0], r[1], r[2]
		rn := fmt.Sprintf("%dp", h)
		label := rn
		if counts[rn] > 1 {
			idx[rn]++
			label = fmt.Sprintf("%s_%d", rn, idx[rn])
		}
		// Filter after label assignment (labels stay stable).
		if sourceWidth > 0 && w > sourceWidth {
			continue
		}
		if capByRes && h > maxH {
			continue
		}
		out = append(out, ladderRung{
			Label: label, ResName: rn, Width: w, Height: h,
			Bitrate: b, Preset: "medium",
		})
	}
	return out
}

// resHeightRank maps a resolution name to a small rank for scheduling priority
// (higher resolution = higher rank = scheduled first).
func resHeightRank(height int) int {
	switch {
	case height <= 360:
		return 1
	case height <= 540:
		return 2
	case height <= 720:
		return 3
	case height <= 1080:
		return 4
	case height <= 1440:
		return 5
	default:
		return 6
	}
}

// variantResourcesForHeight sizes a chunk encode job to its resolution,
// returning Batch resourceRequirements (strings). Mirrors the old
// variantResources but keyed on pixel height so it works for any ladder's
// rung resolutions (incl. apple's non-standard heights).
func variantResourcesForHeight(height int) (vcpu, memory string) {
	switch {
	case height <= 540:
		return "2", "4096"
	case height <= 1080:
		return "4", "8192"
	default: // 1440p, 2160p
		return "4", "8192"
	}
}

// sortRungsSlowestFirst orders variants so the long poles (high resolution,
// then HEVC over H.264) enter the queue first. Scheduling priority enforces
// it, but submission order helps too.
func sortRungsSlowestFirst(v []sfnVariant) {
	sort.SliceStable(v, func(i, j int) bool {
		if v[i].heightRank != v[j].heightRank {
			return v[i].heightRank > v[j].heightRank
		}
		return v[i].Codec == "hevc" && v[j].Codec != "hevc"
	})
}
