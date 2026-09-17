package encode

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// LadderStore is the control plane's source of truth for encoding ladders.
// It owns a persisted ladders.json (seeded from the built-ins on first run),
// serves the API, and resolves the concrete rungs the SFN hands to workers —
// so a user-defined ladder flows through to both local and cloud encodes.
//
// The on-disk format is shared with the Python encoder (scripts/infinite_streaming_encoder/
// ladder.py reads the same file), so a rung is a [width, height, bitrate_kbps]
// triple and burn-in geometry is derived from height on both sides.

// DefaultLadderName is the ladder a job encodes with when it names none.
//
// It has to be defined once. The same literal used to sit in eight call sites
// (job.go, meta.go, cost.go, ladder_store.go) plus the -default-ladder flag,
// and #202 is exactly what that costs: a run's ladder is only worth recording
// if the recorded name is the one that actually ran, which means the fallback
// the recorder uses and the fallback the encoder uses must be the same value.
//
// Note the server flag (-default-ladder / DEFAULT_LADDER) can override the
// default for jobs it seeds; EffectiveLadder describes what a given JobConfig
// resolves to, which is what history.md needs.
const DefaultLadderName = "apple-uniq-live-xs"

// EffectiveLadder is the ladder a config actually encodes with — the one it
// names, or the default when it names none.
func EffectiveLadder(cfg JobConfig) string {
	if cfg.Ladder != "" {
		return cfg.Ladder
	}
	return DefaultLadderName
}

// LadderDef is one ladder: per-codec rung lists plus optional VBV shaping.
// JSON-shaped for the store file and the API.
type LadderDef struct {
	Description       string  `json:"description,omitempty"`
	Seed              bool    `json:"seed,omitempty"`
	MaxratePercent    int     `json:"maxrate_percent,omitempty"`
	BufsizeMultiplier float64 `json:"bufsize_multiplier,omitempty"`
	// Output timing that defines the profile as much as its bitrates: HLS segment
	// length, LL-HLS partial length ("0" = off → plain VOD, no parts), and GOP /
	// keyframe interval. Strings so "" means "inherit the global default" while
	// "0" is an explicit value; a per-encode job value still overrides. Live
	// profiles set 6/0.2/1.0; a VOD profile sets 6/0/6.
	SegmentDuration string `json:"segment_duration,omitempty"`
	PartialDuration string `json:"partial_duration,omitempty"`
	GopDuration     string `json:"gop_duration,omitempty"`
	// OutputTag, when set, is appended to the output directory name AFTER the
	// codec (e.g. "6s" → "<stem>_<codec>_6s") — last, so the `_p200_<codec>`
	// shape that OutputStem / resolveCodec / parseOutputMeta / the watcher all
	// key off stays intact. It marks the profile in the filename so a downstream
	// consumer (e.g. go-live) can tell a repackage-once profile from the default
	// repackage-into-1s/2s/6s one. Empty = no tag (dir names unchanged).
	OutputTag string `json:"output_tag,omitempty"`
	// Codecs maps a codec ("h264"/"hevc"/"av1") to its rungs, each a
	// [width, height, bitrate_kbps] triple. Preset defaults to "medium".
	Codecs map[string][][]int `json:"codecs"`
	// ExtraArgs maps a codec to a raw ffmpeg args string appended AFTER the
	// ladder's rate-control block and BEFORE the output — e.g. "hevc":
	// "-x265-params aq-mode=3:psy-rd=2.0", "av1": "-svtav1-params film-grain=8".
	// Empty/absent = none (the default; existing ladders are unchanged). The
	// Python side shlex-splits it to argv (never shell-eval'd); shell
	// metacharacters are rejected at save time (validateLadderDef).
	ExtraArgs map[string]string `json:"extra_args,omitempty"`
	// Passes maps a codec to its encode pass count (1 or 2). Absent/missing key
	// = the codec-intrinsic default (h264:1, hevc:2, av1:1), so existing ladders
	// are unchanged. Only HEVC is a real choice: h264 is always 1-pass and av1
	// has no 2-pass path, so h264:2 / av1:2 are rejected at save time. This
	// generalizes the old per-encode JobConfig.HevcSinglePass into the profile.
	Passes map[string]int `json:"passes,omitempty"`
}

type ladderFile struct {
	Version int                  `json:"version"`
	Ladders map[string]LadderDef `json:"ladders"`
}

type LadderStore struct {
	mu      sync.RWMutex
	path    string
	ladders map[string]LadderDef
}

// defaultSeedLadders returns the built-in read-only ladders. Mirrors
// scripts/infinite_streaming_encoder/ladder.py SEED_LADDERS (av1 == hevc). Kept in sync by hand;
// the store persists a copy so both languages read the same file thereafter.
func defaultSeedLadders() map[string]LadderDef {
	appleH264 := [][]int{{416, 234, 145}, {640, 360, 365}, {768, 432, 730}, {768, 432, 1100}, {960, 540, 2000}, {1280, 720, 3000}, {1280, 720, 4500}, {1920, 1080, 6000}, {1920, 1080, 7800}}
	appleHEVC := [][]int{{640, 360, 145}, {768, 432, 300}, {960, 540, 600}, {960, 540, 900}, {960, 540, 1600}, {1280, 720, 2400}, {1280, 720, 3400}, {1920, 1080, 4500}, {1920, 1080, 5800}, {2560, 1440, 8100}, {3840, 2160, 11600}, {3840, 2160, 16800}}
	appleUniqH264 := [][]int{{416, 234, 145}, {640, 360, 365}, {704, 396, 730}, {768, 432, 1100}, {960, 540, 2000}, {1056, 594, 3000}, {1280, 720, 4500}, {1696, 954, 6000}, {1920, 1080, 7800}}
	appleUniqHEVC := [][]int{{640, 360, 145}, {768, 432, 300}, {832, 468, 600}, {896, 504, 900}, {960, 540, 1600}, {1056, 594, 2400}, {1280, 720, 3400}, {1696, 954, 4500}, {1920, 1080, 5800}, {2560, 1440, 8100}, {3200, 1800, 11600}, {3840, 2160, 16800}}
	// apple-uniq H.264 extended to 4K (max-compat high-bitrate H.264): the three
	// extra tiers mirror the HEVC-uniq top resolutions (1440/1800/2160) with
	// H.264-appropriate (higher) bitrates. Keeps h264/hevc/av1 rung-parallel.
	appleUniqH264Full := append(append([][]int{}, appleUniqH264...), []int{2560, 1440, 13500}, []int{3200, 1800, 19000}, []int{3840, 2160, 27000})
	// Netflix's own rungs, read out of a live player (#394) — the bitrates are
	// exactly as measured; only repeated resolutions are nudged (see below).
	netflixAV1 := [][]int{{576, 324, 63}, {608, 342, 97}, {768, 432, 152}, {960, 540, 209}, {1280, 720, 322}, {1888, 1062, 543}, {1920, 1080, 1020}, {3840, 2160, 1962}}
	netflixDVHEVC := [][]int{{608, 342, 111}, {768, 432, 113}, {960, 540, 138}, {1280, 720, 232}, {1920, 1080, 351}, {2560, 1440, 528}, {3712, 2088, 778}, {3744, 2106, 1154}, {3776, 2124, 4213}, {3808, 2142, 8366}, {3840, 2160, 11846}}
	return map[string]LadderDef{
		"apple": {
			Description: "Apple HLS Authoring Spec bitrates — per-codec, multi-rung.",
			Seed:        true,
			Codecs: map[string][][]int{
				"h264": appleH264,
				"hevc": appleHEVC,
				"av1":  appleHEVC,
			},
		},
		"apple-uniq": {
			Description: "Apple bitrates with every rung given a unique 16:9 resolution.",
			Seed:        true,
			Codecs: map[string][][]int{
				"h264": appleUniqH264,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-live-xs": {
			Description:       "The FLEXIBLE base: no pinned segment length, so go-live repackages one encode into 1s/2s/6s. Apple's live/linear VBV (peak <= 1.25x avg) split as maxrate 100% + a 0.25x buffer, so the bound holds EVEN AT 1s (1.00 + 0.25) — that is what makes it safe to re-chop. The split matters as much as the bound: at 110%/0.10x the same 1.25x ceiling left only 3 frames of buffer and the encoder delivered just 64-68% of target, because a 3-frame buffer cannot absorb a keyframe and x264 stays conservative rather than violate VBV. Measured, the peak never reached 86-92% of that maxrate at any rung, so the ceiling was never the constraint — trading it for buffer costs nothing and yields 94-99% of target with the 1s peak still inside the cap. H.264 climbs to 4K (1440p/1800p/2160p): Apple caps H.264 at 1080p and puts HEVC above, so this trades spec-compliance for max-compatibility high-bitrate 4K H.264, matching the rung set of the fixed-segment ladders it is compared against.",
			Seed:              true,
			MaxratePercent:    100,
			BufsizeMultiplier: 0.25,
			// Flexible base: no pinned segment_duration → suffix derives to _xs.
			PartialDuration: "0.2",
			GopDuration:     "1.0",
			Codecs: map[string][][]int{
				"h264": appleUniqH264Full,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-live-1s": {
			Description:       "apple-uniq bitrates encoded NATIVELY for 1s segments. Delivered peak (maxrate + bufsize/T) is held at 1.25x avg — Apple's live/linear guidance — the SAME as the other apple-uniq-live-Ns ladders, so a comparison between them is not confounded by peak. Split as maxrate 100% + 0.25x rather than 110% + 0.15x: both satisfy the bound at T=1s, but the first gives 7.5 frames of buffer instead of 4.5, which lifts delivery from 91% to 94-99% of target AND brings the measured 1s peak back under the cap (110%/0.15x breached it at 540p). GOP matched to the segment (1s), which is what makes this a different ENCODE rather than a repackaging. NOTE gop == segment means LL-HLS parts are INDEPENDENT only at segment boundaries, so a player cannot join mid-segment: the low-latency cost of a long GOP.",
			Seed:              true,
			MaxratePercent:    100,
			BufsizeMultiplier: 0.25,
			SegmentDuration:   "1", // fixed → suffix auto-derives to "_1s"
			PartialDuration:   "0.2",
			GopDuration:       "1",
			Codecs: map[string][][]int{
				"h264": appleUniqH264Full,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-live-2s": {
			Description:       "apple-uniq bitrates encoded NATIVELY for 2s segments. Delivered peak (maxrate + bufsize/T) is held at 1.25x avg — Apple's live/linear guidance — the SAME as the other apple-uniq-live-Ns ladders, so a comparison between them is not confounded by peak. Committing to 2s is what buys the bigger buffer: 0.3x here versus 0.10x on the flexible base (apple-uniq-live-xs), which must survive re-chopping to 1s and so pays the 1s price at every length — that difference IS the cost of re-choppability. GOP matched to the segment (2s), which is what makes this a different ENCODE rather than a repackaging. NOTE gop == segment means LL-HLS parts are INDEPENDENT only at segment boundaries, so a player cannot join mid-segment: the low-latency cost of a long GOP.",
			Seed:              true,
			MaxratePercent:    110,
			BufsizeMultiplier: 0.3,
			SegmentDuration:   "2", // fixed → suffix auto-derives to "_2s"
			PartialDuration:   "0.2",
			GopDuration:       "2",
			Codecs: map[string][][]int{
				"h264": appleUniqH264Full,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-live-6s": {
			Description:       "apple-uniq bitrates encoded NATIVELY for 6s segments. Delivered peak (maxrate + bufsize/T) is held at 1.25x avg — Apple's live/linear guidance — the SAME as the other apple-uniq-live-Ns ladders, so a comparison between them is not confounded by peak. Committing to 6s is what buys the bigger buffer: 0.9x here versus 0.10x on the flexible base (apple-uniq-live-xs), which must survive re-chopping to 1s and so pays the 1s price at every length — that difference IS the cost of re-choppability. GOP matched to the segment (6s), which is what makes this a different ENCODE rather than a repackaging. NOTE gop == segment means LL-HLS parts are INDEPENDENT only at segment boundaries, so a player cannot join mid-segment: the low-latency cost of a long GOP.",
			Seed:              true,
			MaxratePercent:    110,
			BufsizeMultiplier: 0.9,
			SegmentDuration:   "6", // fixed → suffix auto-derives to "_6s"
			PartialDuration:   "0.2",
			GopDuration:       "6",
			Codecs: map[string][][]int{
				"h264": appleUniqH264Full,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-vod": {
			Description:       "apple-uniq bitrates tuned for VOD: 6s segments, NO LL-HLS parts, long 6s GOP (fewer keyframes -> better efficiency), and a relaxed VBV (peak <= 2x avg per Apple's VOD guidance, 2.0x buffer). Bits redistribute toward complex scenes; average bitrate and size are unchanged.",
			Seed:              true,
			MaxratePercent:    200,
			BufsizeMultiplier: 2.0,
			SegmentDuration:   "6",
			PartialDuration:   "0",
			GopDuration:       "6",
			// Explicit, because the derived tag would be "6s" — the same as
			// apple-uniq-live-6s, which is a different encode entirely (gop 6 vs
			// 1.0, no parts vs 0.2s, 200%/2x vs 150%/1x). Two encodes into one
			// output directory, second overwrites first. Segment duration is a
			// good DEFAULT name, not a unique one.
			OutputTag: "vod",
			Codecs: map[string][][]int{
				"h264": appleUniqH264,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		// The two MEASURED Netflix ladders (#394). Seeds rather than user
		// ladders because reference data that exists on one box is not a
		// reference: a state wipe, a fresh install or a second machine would
		// each lose it, and the measurement cost most of a day on real
		// hardware and is not cheaply repeatable.
		//
		// They are the only ladders here whose rungs were not chosen. Both
		// descriptions therefore carry the provenance and the DO-NOT-COPY
		// warning inline, because the description is the only free-text field
		// that survives the store round-trip (unknown JSON keys are dropped by
		// the Go struct, so there is no comment field to put it in).
		//
		// Two deliberate departures from what was measured, both in
		// docs/ladders-and-delivery.md § "The Netflix ladders":
		//   - Resolutions NUDGED where Netflix repeated one, because HLS
		//     variant directories are named <height>p and duplicates collide.
		//   - The delivery profile (segment/partial/GOP/VBV) is OURS. None of
		//     it is observable from a player, so it had to be chosen.
		"netflix-av1": {
			Description:       "MEASURED from a live Netflix player, not designed (Ladder B in issue #394). NETFLIX'S OWN STREAM TAGS: EVEAV1, MCCLEAREN_AV1, av1-hd-bitrate-capped, identPOI. 'av1-hd-bitrate-capped' is undocumented and is the best available explanation for how low this ladder tops out: every AV1 stream seen carrying it was served far cheaper at the same resolution than an untagged one (875 vs 2409 kbps at 1080p on two different titles). STREAM: av01.0.04M.08 (AV1 Main, profile 0, 8-bit, SDR), 24.000 fps, audio mp4a.40.5 HE-AAC 2.0 at 128 kbps. Served when the DISPLAY is SDR - dynamic range alone selects this ladder over the Dolby Vision one, live, mid-playback. MEASURED: title 81646429, 2026-09-13/14, 59 cap steps at 5% from 0.2 to 3.39 Mbit/s, every step guarded on title + codec + tags. RUNG NOTES: steps 1.38x-1.92x with no outlier. 63 + 97 share 342p as measured (a hold-resolution rung). 152->209 is the TIGHTEST step (1.38x) and therefore sets the largest peak/avg a VBR encode can carry before a rung's peak overlaps the next rung's average. 543->1020 is the 1080p hold rung: 1.88x the bits for +1 VMAF. 1020->1962 is the widest step (1.92x) and jumps 1080p straight to 4K - there is no 1440p rung, unlike the Dolby Vision ladder. Top-rung VMAF 103 is SATURATED; do not read it as quality headroom. The top rung may also be an artifact of the sweep stopping at a 3.39 Mbit/s cap, just above it. RESOLUTIONS NUDGED: Netflix repeats a resolution on two pairs (608x342 at 63+97, 1920x1080 at 543+1020). HLS variant dirs are named <height>p, so duplicates collide - the LOWER rung of each pair is stepped down by the smallest exact-16:9 increment (18px of height: 324p, 1062p) and the measured resolution is kept on the upper rung. Same device apple-uniq uses. DO NOT COPY THE BITRATES: they are the OUTPUT of per-shot optimisation on one piece of Netflix material. Three adjacent episodes of that show already disagree by up to 16%. Our encoder at 543 kbps/1080p will not reach their reported VMAF 95. DELIVERY PROFILE IS OURS - none of it is observable from a player: 6s segments, 0.2s LL-HLS parts, 6s GOP, VOD VBV (200% / 2.0x). With gop 6 == segment 6, parts are INDEPENDENT only at segment boundaries.",
			Seed:              true,
			MaxratePercent:    200,
			BufsizeMultiplier: 2.0,
			SegmentDuration:   "6",
			PartialDuration:   "0.2",
			GopDuration:       "6",
			// Explicit for the same reason apple-uniq-vod carries "vod": the
			// derived tag would be "6s", which apple-uniq-live-6s already owns.
			// Both Netflix ladders can share this one because their codecs
			// differ, and the codec is already in the directory name.
			OutputTag: "nf6s",
			Codecs: map[string][][]int{
				"av1": netflixAV1,
			},
		},
		"netflix-dv-hevc": {
			Description:       "MEASURED from a live Netflix player, not designed (Ladder A in issue #394). NETFLIX'S OWN STREAM TAGS: CE4_DoVi_DO_v1, MCCLEAREN_DV, NOP, identPOI; content keys issued for 540/1080/2160. STREAM: dvhe.05.01 (HEVC Main 10, Dolby Vision profile 5), 24.000 fps, audio mp4a.40.5 HE-AAC 2.0 at 128 kbps. Served only when the DISPLAY is HDR - same machine, monitor, cable and session: toggling macOS HDR switched the ladder live, mid-playback. Browser, connector and colour gamut were each tested and none of them moved it. MEASURED: title 81646429, 2026-09-13/14, 5% cap steps, with 26 steps from 1.3 to 4.4 Mbit/s across the big gap alone. RUNG NOTES: the ladder climbs resolution fast at the bottom, then holds 4K and buys quality - five rungs at 2160p, the turn being at 778 kbps. Steps are ~1.5x except 1154->4213, a REAL 3.65x gap: swept at 5% throughout and the player never chose anything inside it. It sits exactly where VMAF saturates (94->100). 4213/8366/11846 score 100-102 = SATURATED, so they exist for bandwidth-rich clients, grain and dark scenes rather than for metric gain. The bottom rung's VMAF 30 is genuinely poor. A 1440p rung exists here (528 kbps) where the AV1 ladder has none. RESOLUTIONS NUDGED: the five 4K rungs are all 3840x2160 as measured and HLS variant dirs are named <height>p, so they would collide. The four lower ones step down by the smallest exact-16:9 increment (18px each: 2088p/2106p/2124p/2142p) and the measured 2160 stays on the top rung. Same device apple-uniq uses. DO NOT COPY THE BITRATES: they are the OUTPUT of per-shot optimisation on one title, and ours is an SDR encode, not Dolby Vision - these numbers describe an encode we do not produce. Their VMAF column is NOT comparable with the SDR ladder's (VMAF's standard model is built for SDR). Use this for the rung SHAPE. DELIVERY PROFILE IS OURS - none of it is observable from a player: 6s segments, 0.2s LL-HLS parts, 6s GOP, VOD VBV (200% / 2.0x). With gop 6 == segment 6, parts are INDEPENDENT only at segment boundaries.",
			Seed:              true,
			MaxratePercent:    200,
			BufsizeMultiplier: 2.0,
			SegmentDuration:   "6",
			PartialDuration:   "0.2",
			GopDuration:       "6",
			OutputTag:         "nf6s",
			Codecs: map[string][][]int{
				"hevc": netflixDVHEVC,
			},
		},
	}
}

// ladderDefsEqual compares two ladder definitions by their JSON encoding — a
// cheap structural equality that ignores map/field ordering. Used to detect a
// seed whose code definition drifted from the persisted copy.
func ladderDefsEqual(a, b LadderDef) bool {
	ab, err1 := json.Marshal(a)
	bb, err2 := json.Marshal(b)
	return err1 == nil && err2 == nil && string(ab) == string(bb)
}

// LoadLadderStore loads the store from `path`, seeding it from the built-ins
// (and writing the file) if it doesn't exist yet. Any user-added ladders in an
// existing file are preserved; missing seed ladders are re-added so a built-in
// can never be permanently lost. Never returns nil — on error it falls back to
// an in-memory seed set so encoding still works.
func LoadLadderStore(path string) *LadderStore {
	s := &LadderStore{path: path, ladders: map[string]LadderDef{}}
	seeds := defaultSeedLadders()

	if data, err := os.ReadFile(path); err == nil {
		var f ladderFile
		if json.Unmarshal(data, &f) == nil && f.Ladders != nil {
			s.ladders = f.Ladders
		}
	}
	changed := false
	// Prune stale seeds: a stored ladder marked seed:true that is no longer a
	// current built-in (renamed/removed in code) — e.g. an old "live" after it
	// became "apple-uniq-live". User ladders (seed:false) are never pruned.
	for name, def := range s.ladders {
		if def.Seed {
			if _, ok := seeds[name]; !ok {
				delete(s.ladders, name)
				changed = true
			}
		}
	}
	// Ensure every current seed exists + is up to date (re-add if removed, and
	// refresh a seed whose code definition changed).
	for name, def := range seeds {
		if cur, ok := s.ladders[name]; !ok || !ladderDefsEqual(cur, def) {
			s.ladders[name] = def
			changed = true
		}
	}
	if len(s.ladders) == 0 {
		s.ladders = seeds
		changed = true
	}
	if changed {
		s.persist() // best-effort
	}
	return s
}

// persist writes the store to disk atomically. Caller holds no lock or the
// write lock; persist itself takes a read snapshot under RLock is unsafe when
// called with the write lock held, so callers pass an already-built map — see
// usage. Best-effort: logs nothing, returns the error for the caller to ignore.
func (s *LadderStore) persist() error {
	if s.path == "" {
		return nil
	}
	f := ladderFile{Version: 1, Ladders: s.ladders}
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0755); err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// List returns all ladder definitions (a copy of the name->def map).
func (s *LadderStore) List() map[string]LadderDef {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]LadderDef, len(s.ladders))
	for k, v := range s.ladders {
		out[k] = v
	}
	return out
}

// Get returns a ladder by name (ok=false if unknown).
func (s *LadderStore) Get(name string) (LadderDef, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	d, ok := s.ladders[name]
	return d, ok
}

// Has reports whether a ladder name is known.
func (s *LadderStore) Has(name string) bool {
	_, ok := s.Get(name)
	return ok
}

// validateName rejects a ladder the store does not know, naming it and listing
// the alternatives — mirroring ladder.py's `unknown ladder 'x' (have: a, b, c)`,
// which got this right from the start.
//
// This exists because resolveRungs returns nil for BOTH "no such ladder" and
// "this ladder has no rungs for this codec" (#289). Phrased as the latter, the
// message sends someone hunting for a missing codec column when the ladder is
// simply not there. Everything that reports one of those two conditions must
// therefore rule this one out FIRST — the callers are ValidateResBand and
// buildSFNInput, which is why the error is defined once here rather than at
// each of them.
//
// It is reachable rather than theoretical: JobConfig.Ladder is persisted per
// job and replayed by Manager.Reconcile, so #286's retirement of the
// apple-uniq-live / apple-uniq-live-full seeds dangled every stored reference
// to them. Deleting a custom ladder while a job is queued does the same.
func (s *LadderStore) validateName(name string) error {
	if s.Has(name) {
		return nil
	}
	all := s.List()
	known := make([]string, 0, len(all))
	for n := range all {
		known = append(known, n)
	}
	sort.Strings(known)
	return fmt.Errorf("unknown ladder %q (have: %s)", name, strings.Join(known, ", "))
}

// isSeedName reports whether a name is a built-in ladder (read-only: may not be
// overwritten or deleted via the API). Derived from defaultSeedLadders so it
// never drifts as seeds are added/renamed.
func isSeedName(name string) bool {
	_, ok := defaultSeedLadders()[name]
	return ok
}

// Put adds or replaces a user-defined ladder. Seed ladders are read-only and
// cannot be overwritten. The def is validated and force-marked non-seed.
func (s *LadderStore) Put(name string, def LadderDef) error {
	if name == "" {
		return fmt.Errorf("ladder name is required")
	}
	if isSeedName(name) {
		return fmt.Errorf("%q is a built-in ladder and cannot be modified", name)
	}
	if err := validateLadderDef(def); err != nil {
		return err
	}
	def.Seed = false
	s.mu.Lock()
	s.ladders[name] = def
	err := s.persist()
	s.mu.Unlock()
	return err
}

// Delete removes a user-defined ladder. Seed ladders cannot be deleted.
func (s *LadderStore) Delete(name string) error {
	if isSeedName(name) {
		return fmt.Errorf("%q is a built-in ladder and cannot be deleted", name)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.ladders[name]; !ok {
		return fmt.Errorf("ladder %q not found", name)
	}
	delete(s.ladders, name)
	return s.persist()
}

// validateLadderDef checks a user-supplied ladder is well-formed: at least one
// codec with at least one rung, every rung a [width, height, bitrate] triple of
// positive ints. Keeps a malformed ladder from ever reaching an encode.
func validateLadderDef(def LadderDef) error {
	if len(def.Codecs) == 0 {
		return fmt.Errorf("ladder needs at least one codec")
	}
	// A ladder's OutputTag is copied onto every job that selects it, and from
	// there into the output directory name. Validated HERE rather than only in
	// the handler because this one is PERSISTED: a traversal stored in a ladder
	// would be applied to every later job that picks that profile, long after
	// the request that planted it.
	if def.OutputTag != "" {
		if err := ValidPathSegment("output_tag", def.OutputTag); err != nil {
			return err
		}
	}
	total := 0
	for codec, rows := range def.Codecs {
		if codec != "h264" && codec != "hevc" && codec != "av1" {
			return fmt.Errorf("unknown codec %q (want h264/hevc/av1)", codec)
		}
		for i, r := range rows {
			if len(r) < 3 {
				return fmt.Errorf("%s rung %d: need [width, height, bitrate]", codec, i)
			}
			if r[0] <= 0 || r[1] <= 0 || r[2] <= 0 {
				return fmt.Errorf("%s rung %d: width/height/bitrate must be positive", codec, i)
			}
		}
		total += len(rows)
	}
	if total == 0 {
		return fmt.Errorf("ladder needs at least one rung")
	}
	if def.MaxratePercent < 0 || def.BufsizeMultiplier < 0 {
		return fmt.Errorf("maxrate_percent / bufsize_multiplier must be >= 0")
	}
	// extra_args: known codec, and no shell metacharacters. The Python side
	// shlex-splits to argv (never shell-eval'd), so this is a footgun guard, not
	// an injection defense — but rejecting metacharacters keeps a bad flag from
	// looking like it might shell-escape.
	for codec, raw := range def.ExtraArgs {
		if codec != "h264" && codec != "hevc" && codec != "av1" {
			return fmt.Errorf("extra_args: unknown codec %q (want h264/hevc/av1)", codec)
		}
		if i := strings.IndexAny(raw, ";|&<>`\n\r"); i >= 0 {
			return fmt.Errorf("extra_args[%s]: shell metacharacter %q is not allowed", codec, raw[i:i+1])
		}
		if strings.Contains(raw, "$(") || strings.Contains(raw, "${") {
			return fmt.Errorf("extra_args[%s]: command/variable substitution is not allowed", codec)
		}
	}
	// passes: 1 or 2 for any codec — every profile carries a per-codec default
	// (h264:1, hevc:2, av1:2) and each is user-settable.
	for codec, n := range def.Passes {
		if codec != "h264" && codec != "hevc" && codec != "av1" {
			return fmt.Errorf("passes: unknown codec %q (want h264/hevc/av1)", codec)
		}
		if n != 1 && n != 2 {
			return fmt.Errorf("passes[%s]: must be 1 or 2", codec)
		}
	}
	return nil
}

// extraArgsFor returns the raw ffmpeg extra-args string for a codec on this
// ladder ("" when unset — the default). av1 uses its own key (its param syntax
// differs from hevc), unlike rung resolution where av1 reuses the hevc column.
func (d LadderDef) extraArgsFor(codec string) string {
	if d.ExtraArgs == nil {
		return ""
	}
	return d.ExtraArgs[codec]
}

// passesFor returns the encode pass count for a codec on this ladder, falling
// back to TWO for every codec when the ladder doesn't pin it. Single source of
// truth for the two-pass decision (was JobConfig.HevcSinglePass).
//
// h264 defaulted to 1 on the stated grounds that "x264's single-pass VBV
// already lands the target average, so two-passing H264 just doubles encode
// time for no measurable gain". That holds at a loose VBV and fails badly at a
// tight one. Measured on one source, two encodes differing ONLY in pass count,
// delivered bitrate as a fraction of the rung target:
//
//	rung    1-pass  2-pass
//	1080p      68%     85%
//	 540p      66%     80%
//	 234p      64%     76%
//
// +12 to +18 points at every rung on a 0.10x buffer, and the peak/avg ratio
// steadies with it — 1.16-1.50 wandering under single-pass, 1.19-1.29 under
// two-pass, back under Apple's 1.25x live cap at the top rungs.
//
// The cost is real: h264 encode time roughly doubles. A ladder with a loose
// buffer gains little (0.90x already delivers 97-100% single-pass) and can pin
// `passes: {"h264": 1}` to buy the time back. Defaulting the other way was the
// error — it made the ladders that need it most the ones that did not get it.
func (d LadderDef) passesFor(codec string) int {
	if d.Passes != nil {
		if n, ok := d.Passes[codec]; ok && n > 0 {
			return n
		}
	}
	return 2
}

// twoPassFor reports whether a codec ACTUALLY encodes in two passes for a given
// run: the ladder profile's count, with the per-encode HevcSinglePass override
// forcing HEVC to one. The Go twin of encode_variants.two_pass_for, and the one
// definition every caller must use.
//
// It exists because four callers each carried their own copy of an older rule,
// `codec == "hevc" && !hevcSinglePass`, which was right while h264 was
// single-pass and silently wrong the day passesFor started returning 2 for
// every codec (#314). That literal is the load-bearing half of the same bug on
// the Python side, and it is the same shape here: nothing fails, the run
// completes, and the only symptom is a learned-speed key nobody writes to being
// read by the cost estimate, the progress weighting and the fan-out priority.
//
// A grep for `== "hevc" &&` is how you find the next copy.
func (d LadderDef) twoPassFor(codec string, hevcSinglePass bool) bool {
	if hevcSinglePass && codec == "hevc" {
		return false
	}
	return d.passesFor(codec) == 2
}

// resolveRungs returns the rungs to encode for a (ladder, codec), filtered to
// those that fit the source (no upscale) and the --min-res/--max-res band
// (both inclusive), with ordinal labels for repeated resolutions. Mirrors
// ladder.select_rungs. av1 reuses the hevc column. Returns nil for an unknown
// ladder/codec.
func (s *LadderStore) resolveRungs(ladderName, codec, maxRes, minRes string, sourceWidth int) []ladderRung {
	def, ok := s.Get(ladderName)
	if !ok {
		return nil
	}
	col := codec
	if codec == "av1" {
		col = "hevc"
	}
	rows := def.Codecs[col]
	if len(rows) == 0 {
		return nil
	}

	// Count resolution occurrences (by height) for label disambiguation over
	// the FULL ladder, so filtering never changes a rung's label.
	counts := map[string]int{}
	for _, r := range rows {
		if len(r) < 3 {
			continue
		}
		counts[fmt.Sprintf("%dp", r[1])]++
	}

	maxH, capByRes := resHeight(maxRes)
	minH, floorByRes := resHeight(minRes)
	idx := map[string]int{}
	var out []ladderRung
	for _, r := range rows {
		if len(r) < 3 {
			continue
		}
		w, h, b := r[0], r[1], r[2]
		rn := fmt.Sprintf("%dp", h)
		label := rn
		if counts[rn] > 1 {
			idx[rn]++
			label = fmt.Sprintf("%s_%d", rn, idx[rn])
		}
		if sourceWidth > 0 && w > sourceWidth {
			continue
		}
		if capByRes && h > maxH {
			continue
		}
		if floorByRes && h < minH {
			continue
		}
		out = append(out, ladderRung{
			Label: label, ResName: rn, Width: w, Height: h,
			Bitrate: b, Preset: "medium",
		})
	}
	return out
}

// codecHeightRange returns the lowest and highest rung heights a codec has on a
// ladder, ignoring any band or source-width filter (ok=false when the ladder or
// codec has no rungs). Used to phrase an empty-band rejection precisely — e.g.
// "this ladder's h264 tops at 1080p".
func (s *LadderStore) codecHeightRange(ladderName, codec string) (lo, hi int, ok bool) {
	rungs := s.resolveRungs(ladderName, codec, "", "", 0)
	if len(rungs) == 0 {
		return 0, 0, false
	}
	lo, hi = rungs[0].Height, rungs[0].Height
	for _, r := range rungs {
		if r.Height < lo {
			lo = r.Height
		}
		if r.Height > hi {
			hi = r.Height
		}
	}
	return lo, hi, true
}

// ValidateResBand rejects a --min-res/--max-res band that would select zero
// rungs for any of the config's chosen codecs — the "no ladder rungs fit this
// source" failure, caught at submit time instead of one second into a launched
// worker (issue #115). Codec-specific because the ladder's columns differ in
// reach: apple-uniq / apple-uniq-vod stop h264 at 1080p while hevc/av1 reach
// 2160p, so a [1800p,2160p] band is fine for hevc but empty for h264. (The
// apple-uniq-live-* ladders carry h264 to 2160p, so they do not hit this.)
//
// Source-width (no-upscale) filtering is deliberately NOT applied here: the
// probe isn't available at submit time, and a too-small source dropping a rung
// is a legitimate per-file skip, not a config error. Passing sourceWidth=0 to
// resolveRungs disables that filter, so this checks the ladder+codec+band only.
// Returns nil when the band is unset or every selected codec keeps ≥1 rung.
//
// It also rejects an unknown ladder, and does that BEFORE the unset-band early
// return (#289). This is the only ladder validation on the submit path, so
// while the existence check sat below that return, a job naming a ladder that
// does not exist — the common case, since most submissions set no band — passed
// validation and went on to build an execution with no variants at all.
func (m *Manager) ValidateResBand(cfg JobConfig) error {
	ladderName := cfg.Ladder
	if ladderName == "" {
		ladderName = DefaultLadderName
	}
	if err := m.Ladders.validateName(ladderName); err != nil {
		return err
	}
	if cfg.MinRes == "" && cfg.MaxRes == "" {
		return nil
	}
	minH, minSet := resHeight(cfg.MinRes)
	maxH, maxSet := resHeight(cfg.MaxRes)
	for _, c := range parseCodecSel(cfg.Codec) {
		if len(m.Ladders.resolveRungs(ladderName, c, cfg.MaxRes, cfg.MinRes, 0)) > 0 {
			continue
		}
		lo, hi, ok := m.Ladders.codecHeightRange(ladderName, c)
		if !ok {
			// Now accurate: the ladder is known to EXIST by this point (the
			// validateName check above), so an empty height range really does
			// mean this ladder has no column for this codec. It used to fire
			// for a missing ladder too, describing it as a missing codec (#289).
			return fmt.Errorf("ladder %q defines no %s rungs", ladderName, c)
		}
		switch {
		// Lead lowercase (ST1005) while keeping "Min Res" / "Max Res" — those are
		// the form's own field labels, and naming the control the user actually
		// touched is what makes the message actionable. static/index.html carries
		// an independent client-side copy of the same rule; nothing parses these
		// strings, so the two need to agree in meaning, not in wording.
		case minSet && minH > hi:
			return fmt.Errorf("no rung qualifies: Min Res %s, but this ladder's %s tops at %dp", cfg.MinRes, c, hi)
		case maxSet && maxH < lo:
			return fmt.Errorf("no rung qualifies: Max Res %s, but this ladder's %s starts at %dp", cfg.MaxRes, c, lo)
		default:
			return fmt.Errorf("no %s rung falls in the selected resolution band — this ladder's %s spans %dp–%dp", c, c, lo, hi)
		}
	}
	return nil
}
