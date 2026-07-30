package encode

import (
	"sort"
	"strconv"
	"strings"
)

// Shared ladder types + helpers for the cloud-batch path. The control plane
// resolves each variant's concrete rung (label + resolution + bitrate + preset)
// from the ladder store (see ladder_store.go) and passes it to the worker via
// the SFN input, so the worker needs no ladder knowledge — which is what lets
// user-defined ladders work in the cloud. res_name is derived as "{height}p"
// and labels get an ordinal suffix when a codec repeats a resolution —
// identical to scripts/infinite_streaming_encoder/ladder.py so cloud and local produce the same
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
// wants a number) and is a banded, rank-of-predicted-wall priority — assigned in
// buildSFNInput so the heaviest variant strictly outranks the merely-heavy.
type sfnVariant struct {
	Codec   string `json:"codec"`
	Label   string `json:"label"`
	Width   string `json:"width"`
	Height  string `json:"height"`
	Bitrate string `json:"bitrate"`
	Preset  string `json:"preset"`
	VCPU    string `json:"vcpu"`
	Memory  string `json:"memory"`
	// Design-time VMAF estimate from the quality curves, for the burn-in overlay
	// — the cloud twin of local-dist's --est-vmaf (see Manager.vmafEstimateArgs).
	// Strings because Batch container-override Environment values must be, and
	// NEVER omitempty: the state machine references $$.Map.Item.Value.est_vmaf
	// unconditionally, so a missing key fails the execution rather than
	// degrading. "" means no estimate, which cli_phase reads as no overlay row.
	EstVmaf        string `json:"est_vmaf"`
	EstVmafClamped string `json:"est_vmaf_clamped"`
	Priority       int    `json:"priority"`
	TwoPass        string `json:"two_pass"`
	// ExtraArgs is the ladder profile's per-codec raw ffmpeg args for THIS
	// variant's codec (resolved from LadderDef.ExtraArgs[codec]; "" = none).
	// Travels to the worker as the EXTRA_ARGS container env, alongside TWO_PASS.
	// (#59's per-codec pass count needs no new field — it's folded into TwoPass.)
	ExtraArgs string `json:"extra_args"`
	// Per-variant chunking (dynamic chunk selector): each variant sizes its own
	// chunks by complexity, so a slow 4K HEVC gets many 30s chunks while a cheap
	// H264 runs whole. ChunkDuration is the chunk size in seconds (string, for
	// the container env); Chunked is "true"/"false" for the SFN Choice. The SFN
	// reads these off each Map item, not top-level.
	//
	// Chunks carries the planned boundaries themselves, one object per chunk,
	// and the chunk Map iterates it — so each worker is TOLD its (index, start,
	// duration) instead of re-deriving the plan from its own probe. It replaced
	// a bare []int of indices; see chunkplan.go for why the authority moved.
	Chunks        []chunkSpan `json:"chunks"`
	ChunkDuration string      `json:"chunk_duration"`
	Chunked       string      `json:"chunked"`
	// ContentDuration is the clip length the boundaries were planned against,
	// for the worker's validation. Same for every variant; carried per-variant
	// because the chunk Map's ItemSelector can only project what the variant
	// item holds.
	ContentDuration string `json:"content_duration"`
}

// maxResHeight maps a legacy tier name to its pixel height. Only a fallback
// for any name that isn't literally "<height>p" — see resHeight.
var maxResHeight = map[string]int{
	"360p": 360, "540p": 540, "720p": 720,
	"1080p": 1080, "1440p": 1440, "2160p": 2160,
}

// resHeight parses a --min-res/--max-res tier name: "1080p" -> 1080, true.
// An empty or unparseable name means "no bound" (0, false).
//
// Parsed rather than table-driven because the UI derives its tier options from
// the SELECTED LADDER's actual rung heights, and the Apple-uniq ladders carry
// non-standard ones (954p, 1800p, 594p...). A fixed table silently ignored
// every tier it didn't know about. Mirrors ladder.res_height.
// ResHeight is resHeight for callers outside the package (the API's min/max
// band validation).
func ResHeight(name string) (int, bool) { return resHeight(name) }

func resHeight(name string) (int, bool) {
	n := strings.TrimSpace(name)
	if n == "" {
		return 0, false
	}
	if strings.HasSuffix(n, "p") {
		if h, err := strconv.Atoi(n[:len(n)-1]); err == nil && h > 0 {
			return h, true
		}
	}
	h, ok := maxResHeight[n]
	return h, ok
}

// variantResourcesFor sizes a variant/chunk encode job by CODEC, not just
// resolution — because on Batch the vCPU request is a packing + fair-share
// weight (Batch uses CPU shares, not a hard cap), so the right value is how
// many cores that encoder actually drives. Measured on Graviton .2xlarge:
//   - x265 (HEVC) tops out ~2 cores even at 4K 2-pass → 2 vCPU so ~4 pack per
//     8-core box. (An A/B — run 1784578218094 vs 1784565875622 — disproved
//     resolution-scaling: 4K at 8 vCPU cost 3.6x the CPU for a 1.1x speedup and
//     barely moved the chunk floor. Lower the floor with smaller chunks instead.)
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
		return "4", memForHeight(height)
	}
	if height <= 540 {
		return "2", "3072" // small res is cheap for any codec (peak ~500 MiB)
	}
	switch codec {
	case "hevc":
		// Flat 2 vCPU at every resolution. A/B on run 1784578218094 disproved the
		// "x265 scales at 4K" assumption: bumping 4K to 8 vCPU cost 3.6x the CPU
		// for a 1.1x wall speedup (and barely moved the chunk floor — a 2-vCPU 12s
		// 4K chunk ~11.9 min vs the 8-vCPU's 10.9). x265 2-pass tops out ~2 cores
		// even at 4K, so 2 vCPU is the efficient point → pack ~4 per box. Lower the
		// makespan floor with smaller CHUNKS, not more vCPU.
		return "2", memForHeight(height)
	case "av1":
		return "8", "6144" // SVT-AV1 scales → give it a whole .2xlarge
	default:
		return "4", memForHeight(height)
	}
}

// memForHeight sizes a variant's container memory from its encoded height.
//
// Measured peaks (ENCODER-TIMING mem_mib, this clip, 2026-07-30):
//
//	h264  540p  502    h264 1080p  1112    h264 2160p  2271+
//	hevc  720p  758                        hevc 2160p  2559
//
// The 4K figures are CENSORED: a chunk needing more than its limit is killed
// before it emits a marker, so the true peak is above what we can see. h264
// 2160p read 2271 and still OOM'd at 3072.
//
// A flat 3072 was safe only because apple-uniq-live stops h264 at 1080p. The
// first apple-uniq-live-full run put x264 on 3840x2160 at 27 Mbps and Batch
// killed every 2160p chunk twice. hevc 2160p was already within ~20% of the
// same cliff without having crossed it.
//
// 6144 above 1080p is ~2.4x the highest observed peak — deliberate headroom,
// because the ceiling is unobservable from this side and an OOM costs the whole
// job while over-allocating only costs packing density. It matches what av1
// already asks for.
func memForHeight(height int) string {
	if height > 1080 {
		return "6144"
	}
	return "3072"
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
