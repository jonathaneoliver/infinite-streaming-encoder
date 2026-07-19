package encode

import (
	"sort"
)

// Shared ladder types + helpers for the cloud-batch path. The control plane
// resolves each variant's concrete rung (label + resolution + bitrate + preset)
// from the ladder store (see ladder_store.go) and passes it to the worker via
// the SFN input, so the worker needs no ladder knowledge — which is what lets
// user-defined ladders work in the cloud. res_name is derived as "{height}p"
// and labels get an ordinal suffix when a codec repeats a resolution —
// identical to scripts/encoder/ladder.py so cloud and local produce the same
// {codec}_{label} outputs.

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

// maxResHeight maps a --max-res tier name to its pixel height for capping.
var maxResHeight = map[string]int{
	"360p": 360, "540p": 540, "720p": 720,
	"1080p": 1080, "1440p": 1440, "2160p": 2160,
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

// variantResourcesFor sizes a variant/chunk encode job by CODEC, not just
// resolution — because on Batch the vCPU request is a packing + fair-share
// weight (Batch uses CPU shares, not a hard cap), so the right value is how
// many cores that encoder actually drives. Measured on Graviton .2xlarge:
//   - x265 (HEVC) tops out ~2-2.4 cores even at 4K → request 2 vCPU so ~4 pack
//     per 8-core box, filling the cores a single x265 can't use.
//   - x264 (H264) scales to ~7 cores → 4 vCPU (packs 2 per box).
//   - SVT-AV1 self-parallelizes across many cores → give it a whole box (8).
// Small resolutions are cheap for every codec, so a 2-vCPU floor applies.
// Memory is kept well below the naive 1:2 vCPU:GiB ratio so jobs pack by vCPU;
// even 4K HEVC peaked at ~2.2 GiB and h264 1080p at ~0.9 GiB (measured via
// ru_maxrss), so 3 GiB is generous and never the binding constraint.
func variantResourcesFor(codec string, height int) (vcpu, memory string) {
	if height <= 540 {
		return "2", "3072" // small res is cheap for any codec
	}
	switch codec {
	case "hevc":
		return "2", "3072" // x265 caps ~2 cores → pack ~4 per box
	case "av1":
		return "8", "6144" // SVT-AV1 scales → give it a whole .2xlarge
	default: // h264
		return "4", "3072" // x264 scales to ~7 → pack 2 per box
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
