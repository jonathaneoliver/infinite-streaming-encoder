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
// wants a number) and is the predicted encode wall time — see predictedPriority.
type sfnVariant struct {
	Codec    string `json:"codec"`
	Label    string `json:"label"`
	Width    string `json:"width"`
	Height   string `json:"height"`
	Bitrate  string `json:"bitrate"`
	Preset   string `json:"preset"`
	VCPU     string `json:"vcpu"`
	Memory   string `json:"memory"`
	Priority int    `json:"priority"`
	TwoPass  string `json:"two_pass"`
	// Per-variant chunking (dynamic chunk selector): each variant sizes its own
	// chunks by complexity, so a slow 4K HEVC gets many 30s chunks while a cheap
	// H264 runs whole. ChunkIndices is [0..n-1]; ChunkDuration is the chunk size
	// in seconds (string, for the container env); Chunked is "true"/"false" for
	// the SFN Choice. The SFN reads these off each Map item, not top-level.
	ChunkIndices  []int  `json:"chunk_indices"`
	ChunkDuration string `json:"chunk_duration"`
	Chunked       string `json:"chunked"`
}

// maxResHeight maps a --max-res tier name to its pixel height for capping.
var maxResHeight = map[string]int{
	"360p": 360, "540p": 540, "720p": 720,
	"1080p": 1080, "1440p": 1440, "2160p": 2160,
}

// variantResourcesFor sizes a variant/chunk encode job by CODEC, not just
// resolution — because on Batch the vCPU request is a packing + fair-share
// weight (Batch uses CPU shares, not a hard cap), so the right value is how
// many cores that encoder actually drives. Measured on Graviton .2xlarge:
//   - x265 (HEVC) core scaling RISES with resolution — ~2 cores at 1080p but
//     many more at 1440p/4K (more CTUs + WPP rows, especially 2-pass). So the
//     vCPU request is resolution-aware: 2 (≤1080p) / 4 (1440p) / 8 (≥2160p).
//     This both gives the 4K long pole its cores AND stops Batch's bin-packer
//     from stacking heavy chunks onto one box (a flat 2 vCPU let ~8 4K chunks
//     pack a 16-vCPU .4xlarge, where they oversubscribed cores and serialized).
//     The vCPU request is the only anti-affinity lever Batch exposes.
//   - x264 (H264) scales to ~7 cores → 4 vCPU (packs 2 per box).
//   - SVT-AV1 self-parallelizes across many cores → give it a whole box (8).
//
// Small resolutions are cheap for every codec, so a 2-vCPU floor applies.
// Memory is kept well below the naive 1:2 vCPU:GiB ratio so jobs pack by vCPU;
// even 4K HEVC peaked at ~2.2 GiB and h264 1080p at ~0.9 GiB (measured via
// ru_maxrss), so 3 GiB is generous and never the binding constraint.
func variantResourcesFor(codec string, height int) (vcpu, memory string) {
	// h264: 4 vCPU so two encodes pack per 8-vCPU .2xlarge. On Graviton3/4 a
	// single x264 encode only drives ~4-5 cores (a dedicated 8-vCPU box idles at
	// 50-60%), so isolating one per box wastes half the machine. Packing two
	// fills it with only mild contention — each encode wanted ~4 cores anyway.
	// (An earlier 8-vCPU/whole-box setting was to measure uncontended speed and
	// dodge c6g's much worse contention; c6g is now dropped from the fleet.)
	if codec == "h264" {
		return "4", "3072"
	}
	if height <= 540 {
		return "2", "3072" // small res is cheap for any codec
	}
	switch codec {
	case "hevc":
		// Resolution-aware: x265 uses more cores as resolution climbs, and a
		// bigger request also forces Batch to spread the heavy chunks (≤1-2 per
		// instance) instead of stacking them.
		switch {
		case height <= 1080:
			return "2", "3072" // x265 genuinely caps ~2 cores here → pack ~4 per box
		case height <= 1440:
			return "4", "3072"
		default: // 2160p+: ~a whole .2xlarge / half a .4xlarge → forced spread + cores
			return "8", "4096" // 2-pass 4K peaked ~2.2 GiB; 4 GiB leaves headroom, still packs by vCPU
		}
	case "av1":
		return "8", "6144" // SVT-AV1 scales → give it a whole .2xlarge
	default:
		return "4", "3072"
	}
}

// sortRungsSlowestFirst orders variants by descending Priority (predicted
// encode wall time) so the long poles enter the queue first — matching the
// SchedulingPriorityOverride Batch sees. Submission order only breaks ties that
// the scheduler leaves open; HEVC wins an exact tie (typically slower to seek).
func sortRungsSlowestFirst(v []sfnVariant) {
	sort.SliceStable(v, func(i, j int) bool {
		if v[i].Priority != v[j].Priority {
			return v[i].Priority > v[j].Priority
		}
		return v[i].Codec == "hevc" && v[j].Codec != "hevc"
	})
}
