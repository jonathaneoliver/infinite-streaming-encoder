package encode

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
)

// Quality curves: measured VMAF-vs-bitrate points used to ESTIMATE a ladder
// rung's quality without encoding anything.
//
// This exists to answer "why are these the right variants?" at ladder-design
// time. The Ladders tab already colours each rung's BITRATE step against its
// neighbour (_stepColor in static/index.html) — a proxy for quality. These
// curves replace the proxy with a measured one: a rung whose estimated VMAF
// delta from its neighbour is ~0 is redundant however the bitrates are spaced.
//
// SEED DATA PROVENANCE — read this before trusting a number:
//
//   Source: insane_fpv_shots_hydrofoil_windsurfing.mkv — 3840x2160, 29.97fps,
//   334s of EXTREME-MOTION FPV footage. Ladder apple-uniq-live-xs, 12 rungs.
//   Measured with libvmaf, n_subsample=5 (~2005 frames/rung), fps-paired,
//   run natively. Extracted from docs/vmaf-audit/*.html (see that README).
//
// It is ONE clip, and a deliberately hard one. Absolute values will be
// pessimistic against calmer content; the SHAPE of the curve (where quality
// saturates, which rungs are redundant) is what transfers. Estimates are
// labelled as such in the UI and must never be presented as measurements of a
// particular encode.
//
// Reference height matters and is not interchangeable: a rung graded against a
// 4K master ("how does this look on a 4K display") scores differently from the
// same rung graded at 1080p ("how clean is its compression"). Points carry the
// reference they were measured at; estimates never mix the two.
//
// Seeds live in code and a JSON store overlays them, mirroring LadderStore —
// so a `ladder_audit` run on your own content replaces these numbers without a
// rebuild.

// CurvePoint is one measured (bitrate -> quality) sample for a codec.
//
// IDENTITY is (Clip, Codec, Reference, Height, Kbps) — all five. Height and
// Kbps are both needed: two rungs can share a bitrate at different heights
// (720p@3000 and 1080p@3000 are different quality), and two can share a height
// at different bitrates (the Apple ladders carry two 1080p rungs). Dropping
// either collapses distinct measurements onto one key and silently discards
// samples. Clip is in the key because quality-vs-bitrate is CONTENT-dependent:
// an extreme-motion clip and a talking head give genuinely different curves,
// and pooling them describes neither.
type CurvePoint struct {
	// Clip is the content this was measured on. Empty means the built-in seed.
	Clip      string  `json:"clip,omitempty"`
	Codec     string  `json:"codec"`
	Reference int     `json:"reference"` // grading reference height (2160 | 1080)
	Height    int     `json:"height"`    // the rung's own encoded height
	Kbps      int     `json:"kbps"`      // DELIVERED bitrate, not the target
	Vmaf      float64 `json:"vmaf"`      // pooled mean
	Harmonic  float64 `json:"harmonic"`  // harmonic mean — punishes bad frames

	// PROVENANCE — not part of the identity above, but what makes a number
	// interpretable. A VMAF score is only comparable against another measured
	// the same way, and the two things that move it most are the model and the
	// resolution both streams were scaled to. Model follows CommonH
	// (vmaf_4k_v0.6.1 at >=1440p, else vmaf_v0.6.1), which is itself the source
	// height capped at Reference — so all rungs in one audit share both, which
	// is what makes a ladder internally comparable.
	//
	// Recorded because a set of measured points once sat up to +15 VMAF above
	// the seed at mid rungs with nothing in the file able to explain why.
	// Empty on seed points and on files written before this was added.
	Model   string `json:"model,omitempty"`
	CommonW int    `json:"common_w,omitempty"`
	CommonH int    `json:"common_h,omitempty"`
	// Profile is the ladder this rung came from (encode.json "profile"), so
	// points measured from different ladders stay distinguishable.
	Profile string `json:"profile,omitempty"`
}

// SeedClip names the content the built-in curves were measured on, so the UI can
// say whose quality it is estimating.
// The reference=2160 points were re-measured 2026-07-29 by `ladder_audit` on
// ffmpeg 8.0.1 with vmaf_4k_v0.6.1 at common=3840x2160, and follow the CURRENT
// apple-uniq-live-* rungs (594/954/1800 rather than the retired 684/1044/2124).
// The reference=1080 points are older and their provenance was not recorded —
// see #163, which is why curve points now carry model/common_w/common_h.
const SeedClip = "insane_fpv_shots_hydrofoil_windsurfing.mkv (4K, extreme motion)"

// DefaultCurveReference is the grading reference used when none is requested.
// 2160 matches the top of the shipped ladders.
const DefaultCurveReference = 2160

func defaultSeedCurves() []CurvePoint {
	return []CurvePoint{
		{Codec: "h264", Reference: 2160, Height: 234, Kbps: 138, Vmaf: 24.98, Harmonic: 21.55},
		{Codec: "h264", Reference: 2160, Height: 360, Kbps: 339, Vmaf: 38.94, Harmonic: 36.4},
		{Codec: "h264", Reference: 2160, Height: 396, Kbps: 672, Vmaf: 48.85, Harmonic: 46.02},
		{Codec: "h264", Reference: 2160, Height: 432, Kbps: 1013, Vmaf: 55.56, Harmonic: 52.81},
		{Codec: "h264", Reference: 2160, Height: 540, Kbps: 1855, Vmaf: 66.33, Harmonic: 64.09},
		{Codec: "h264", Reference: 2160, Height: 594, Kbps: 2793, Vmaf: 72.8, Harmonic: 71.0},
		{Codec: "h264", Reference: 2160, Height: 720, Kbps: 4221, Vmaf: 80.13, Harmonic: 78.9},
		{Codec: "h264", Reference: 2160, Height: 954, Kbps: 5668, Vmaf: 84.95, Harmonic: 84.06},
		{Codec: "h264", Reference: 2160, Height: 1080, Kbps: 7422, Vmaf: 88.41, Harmonic: 87.76},

		{Codec: "hevc", Reference: 2160, Height: 360, Kbps: 151, Vmaf: 32.85, Harmonic: 29.93},
		{Codec: "hevc", Reference: 2160, Height: 432, Kbps: 300, Vmaf: 43.17, Harmonic: 39.9},
		{Codec: "hevc", Reference: 2160, Height: 468, Kbps: 592, Vmaf: 52.31, Harmonic: 49.02},
		{Codec: "hevc", Reference: 2160, Height: 504, Kbps: 885, Vmaf: 58.56, Harmonic: 55.49},
		{Codec: "hevc", Reference: 2160, Height: 540, Kbps: 1565, Vmaf: 66.48, Harmonic: 63.94},
		{Codec: "hevc", Reference: 2160, Height: 594, Kbps: 2343, Vmaf: 72.74, Harmonic: 70.74},
		{Codec: "hevc", Reference: 2160, Height: 720, Kbps: 3331, Vmaf: 79.56, Harmonic: 78.12},
		{Codec: "hevc", Reference: 2160, Height: 954, Kbps: 4404, Vmaf: 85.07, Harmonic: 84.02},
		{Codec: "hevc", Reference: 2160, Height: 1080, Kbps: 5669, Vmaf: 88.44, Harmonic: 87.69},
		{Codec: "hevc", Reference: 2160, Height: 1440, Kbps: 7920, Vmaf: 92.54, Harmonic: 92.08},
		{Codec: "hevc", Reference: 2160, Height: 1800, Kbps: 11351, Vmaf: 95.56, Harmonic: 95.32},
		{Codec: "hevc", Reference: 2160, Height: 2160, Kbps: 16444, Vmaf: 98.16, Harmonic: 98.07},

		{Codec: "av1", Reference: 2160, Height: 360, Kbps: 145, Vmaf: 36.12, Harmonic: 33.15},
		{Codec: "av1", Reference: 2160, Height: 432, Kbps: 298, Vmaf: 46.95, Harmonic: 43.86},
		{Codec: "av1", Reference: 2160, Height: 468, Kbps: 597, Vmaf: 55.63, Harmonic: 52.94},
		{Codec: "av1", Reference: 2160, Height: 504, Kbps: 894, Vmaf: 61.73, Harmonic: 59.35},
		{Codec: "av1", Reference: 2160, Height: 540, Kbps: 1582, Vmaf: 68.79, Harmonic: 66.89},
		{Codec: "av1", Reference: 2160, Height: 594, Kbps: 2380, Vmaf: 74.88, Harmonic: 73.42},
		{Codec: "av1", Reference: 2160, Height: 720, Kbps: 3389, Vmaf: 81.01, Harmonic: 79.94},
		{Codec: "av1", Reference: 2160, Height: 954, Kbps: 4498, Vmaf: 87.15, Harmonic: 86.46},
		{Codec: "av1", Reference: 2160, Height: 1080, Kbps: 5797, Vmaf: 90.42, Harmonic: 89.93},
		{Codec: "av1", Reference: 2160, Height: 1440, Kbps: 8091, Vmaf: 94.34, Harmonic: 94.08},
		{Codec: "av1", Reference: 2160, Height: 1800, Kbps: 11568, Vmaf: 96.89, Harmonic: 96.76},
		{Codec: "av1", Reference: 2160, Height: 2160, Kbps: 16610, Vmaf: 98.77, Harmonic: 98.73},

		{Codec: "h264", Reference: 1080, Height: 234, Kbps: 138, Vmaf: 13.28, Harmonic: 6.84},
		{Codec: "h264", Reference: 1080, Height: 360, Kbps: 339, Vmaf: 32.5, Harmonic: 24.84},
		{Codec: "h264", Reference: 1080, Height: 396, Kbps: 673, Vmaf: 48.65, Harmonic: 41.01},
		{Codec: "h264", Reference: 1080, Height: 432, Kbps: 1013, Vmaf: 58.33, Harmonic: 50.72},
		{Codec: "h264", Reference: 1080, Height: 540, Kbps: 1856, Vmaf: 71.07, Harmonic: 63.46},
		{Codec: "h264", Reference: 1080, Height: 684, Kbps: 2798, Vmaf: 78.92, Harmonic: 71.39},
		{Codec: "h264", Reference: 1080, Height: 720, Kbps: 4220, Vmaf: 85.69, Harmonic: 77.95},
		{Codec: "h264", Reference: 1080, Height: 1044, Kbps: 5669, Vmaf: 88.58, Harmonic: 80.59},
		{Codec: "h264", Reference: 1080, Height: 1080, Kbps: 7423, Vmaf: 92.06, Harmonic: 83.79},
		{Codec: "h264", Reference: 1080, Height: 1440, Kbps: 12975, Vmaf: 95.62, Harmonic: 87.11},
		{Codec: "h264", Reference: 1080, Height: 2124, Kbps: 18376, Vmaf: 96.19, Harmonic: 87.49},
		{Codec: "h264", Reference: 1080, Height: 2160, Kbps: 26190, Vmaf: 97.18, Harmonic: 88.36},
		{Codec: "hevc", Reference: 1080, Height: 360, Kbps: 151, Vmaf: 22.08, Harmonic: 12.21},
		{Codec: "hevc", Reference: 1080, Height: 432, Kbps: 301, Vmaf: 36.9, Harmonic: 26.6},
		{Codec: "hevc", Reference: 1080, Height: 468, Kbps: 593, Vmaf: 50.94, Harmonic: 42.03},
		{Codec: "hevc", Reference: 1080, Height: 504, Kbps: 886, Vmaf: 59.75, Harmonic: 51.23},
		{Codec: "hevc", Reference: 1080, Height: 540, Kbps: 1566, Vmaf: 70.85, Harmonic: 63.27},
		{Codec: "hevc", Reference: 1080, Height: 684, Kbps: 2345, Vmaf: 79.06, Harmonic: 71.32},
		{Codec: "hevc", Reference: 1080, Height: 720, Kbps: 3330, Vmaf: 84.6, Harmonic: 76.66},
		{Codec: "hevc", Reference: 1080, Height: 1044, Kbps: 4399, Vmaf: 88.75, Harmonic: 80.68},
		{Codec: "hevc", Reference: 1080, Height: 1080, Kbps: 5670, Vmaf: 91.83, Harmonic: 83.53},
		{Codec: "hevc", Reference: 1080, Height: 1440, Kbps: 7922, Vmaf: 94.3, Harmonic: 85.72},
		{Codec: "hevc", Reference: 1080, Height: 2124, Kbps: 11352, Vmaf: 95.82, Harmonic: 87.29},
		{Codec: "hevc", Reference: 1080, Height: 2160, Kbps: 16442, Vmaf: 97.0, Harmonic: 88.25},
		{Codec: "av1", Reference: 1080, Height: 360, Kbps: 146, Vmaf: 29.46, Harmonic: 18.12},
		{Codec: "av1", Reference: 1080, Height: 432, Kbps: 301, Vmaf: 45.08, Harmonic: 35.85},
		{Codec: "av1", Reference: 1080, Height: 468, Kbps: 598, Vmaf: 58.23, Harmonic: 50.33},
		{Codec: "av1", Reference: 1080, Height: 504, Kbps: 895, Vmaf: 66.39, Harmonic: 58.9},
		{Codec: "av1", Reference: 1080, Height: 540, Kbps: 1583, Vmaf: 75.73, Harmonic: 68.34},
		{Codec: "av1", Reference: 1080, Height: 684, Kbps: 2398, Vmaf: 83.19, Harmonic: 75.52},
		{Codec: "av1", Reference: 1080, Height: 720, Kbps: 3388, Vmaf: 87.34, Harmonic: 79.48},
		{Codec: "av1", Reference: 1080, Height: 1044, Kbps: 4492, Vmaf: 91.75, Harmonic: 83.3},
		{Codec: "av1", Reference: 1080, Height: 1080, Kbps: 5801, Vmaf: 93.96, Harmonic: 85.27},
		{Codec: "av1", Reference: 1080, Height: 1440, Kbps: 8095, Vmaf: 95.61, Harmonic: 86.95},
		{Codec: "av1", Reference: 1080, Height: 2124, Kbps: 11557, Vmaf: 96.54, Harmonic: 87.78},
		{Codec: "av1", Reference: 1080, Height: 2160, Kbps: 16639, Vmaf: 97.13, Harmonic: 88.35},
	}
}

// CurveStore holds the seed curves plus any measured by a `ladder_audit` run.
// Overlay semantics mirror LadderStore: a stored point for the same
// (codec, reference, height) replaces the seed, everything else is preserved.
type CurveStore struct {
	mu     sync.RWMutex
	path   string
	points []CurvePoint
	// Clip names the content the CURRENT points came from — the seed clip until
	// an audit overwrites it. Surfaced so an estimate is never read without
	// knowing whose content produced it.
	Clip string
}

type curveFile struct {
	Clip   string       `json:"clip"`
	Points []CurvePoint `json:"points"`
}

// LoadCurveStore reads measured curves from `path`, falling back to the built-in
// seed set. Never returns nil — a missing or corrupt file just means seeds only,
// since an estimate is a nicety and must never break ladder rendering.
func LoadCurveStore(path string) *CurveStore {
	seed := defaultSeedCurves()
	for i := range seed {
		seed[i].Clip = SeedClip
	}
	s := &CurveStore{path: path, points: seed, Clip: SeedClip}
	data, err := os.ReadFile(path)
	if err != nil {
		return s
	}
	var f curveFile
	if json.Unmarshal(data, &f) != nil || len(f.Points) == 0 {
		return s
	}
	// Index the seed by (codec, reference, height) so measured points replace
	// their seed counterpart rather than appending a duplicate the interpolator
	// would then average over.
	key := func(p CurvePoint) string {
		return fmt.Sprintf("%s/%s/%d/%d/%d", p.Clip, p.Codec, p.Reference, p.Height, p.Kbps)
	}
	merged := map[string]CurvePoint{}
	for _, p := range s.points {
		merged[key(p)] = p
	}
	for _, p := range f.Points {
		merged[key(p)] = p
	}
	out := make([]CurvePoint, 0, len(merged))
	for _, p := range merged {
		out = append(out, p)
	}
	s.points = out
	if f.Clip != "" {
		s.Clip = f.Clip
	}
	return s
}

// Clips lists the content the store holds curves for, seed first.
func (s *CurveStore) Clips() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	seen := map[string]bool{}
	var out []string
	for _, p := range s.points {
		c := p.Clip
		if c == "" {
			c = SeedClip
		}
		if !seen[c] {
			seen[c] = true
			out = append(out, c)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if (out[i] == SeedClip) != (out[j] == SeedClip) {
			return out[i] == SeedClip
		}
		return out[i] < out[j]
	})
	return out
}

// Points returns curve points for one codec at one grading reference, measured
// on one clip, sorted by bitrate. Curves are never pooled across clips — see
// CurvePoint's identity note.
func (s *CurveStore) Points(codec string, reference int, clip string) []CurvePoint {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var out []CurvePoint
	for _, p := range s.points {
		pc := p.Clip
		if pc == "" {
			pc = SeedClip
		}
		if p.Codec == codec && p.Reference == reference && pc == clip {
			out = append(out, p)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Kbps < out[j].Kbps })
	return out
}

// EstimateVmaf interpolates a rung's expected VMAF from the measured curve for
// its codec. Returns false when there's nothing to interpolate from.
//
// Interpolation is on BITRATE alone, not (bitrate, height). On a sane ladder a
// rung's height is chosen for its bitrate, so the measured points describe a
// rate-quality curve — which is exactly what the audit reports plot. Treating
// height as a second axis would demand a 2-D fit that 12 points can't support.
//
// Outside the measured range the value is CLAMPED to the nearest endpoint
// rather than extrapolated: VMAF saturates near 100 and collapses at the
// bottom, so a linear extrapolation would confidently invent quality that
// doesn't exist. Callers get `false` for "no data", never a fabricated number.
// Returns (vmaf, clamped, ok). `clamped` means the bitrate fell OUTSIDE the
// measured range and the value is the nearest endpoint, not an interpolation —
// callers must present it differently, because the delta against a neighbouring
// rung is then meaningless and would otherwise read as a real verdict. Every
// shipped ladder's top rung is currently above the measured maximum, so this is
// the common case at the top of the table, not an edge case.
func (s *CurveStore) EstimateVmaf(codec string, height, kbps, reference int, clip string) (float64, bool, bool) {
	all := s.Points(codec, reference, clip)
	if len(all) == 0 || kbps <= 0 {
		return 0, false, false
	}
	// Prefer points measured at THIS rung's height. With height in a point's
	// identity the curve can hold two samples at one bitrate (720p@3000 and
	// 1080p@3000 are different quality), so a bitrate-only walk would pick
	// whichever the sort happened to order first. Same-height points remove the
	// ambiguity; the codec-wide curve is the fallback when a height has never
	// been measured, which is the normal case for a freshly authored ladder.
	pts := all
	var sameHeight []CurvePoint
	for _, p := range all {
		if p.Height == height {
			sameHeight = append(sameHeight, p)
		}
	}
	// One sample can't span a range, so it only helps as an exact hit; two or
	// more can be interpolated between.
	if len(sameHeight) >= 2 {
		pts = sameHeight
	} else if len(sameHeight) == 1 && sameHeight[0].Kbps == kbps {
		return sameHeight[0].Vmaf, false, true
	}

	if kbps <= pts[0].Kbps {
		return pts[0].Vmaf, kbps < pts[0].Kbps, true
	}
	if kbps >= pts[len(pts)-1].Kbps {
		return pts[len(pts)-1].Vmaf, kbps > pts[len(pts)-1].Kbps, true
	}
	for i := 1; i < len(pts); i++ {
		if kbps <= pts[i].Kbps {
			lo, hi := pts[i-1], pts[i]
			span := float64(hi.Kbps - lo.Kbps)
			if span <= 0 {
				return hi.Vmaf, false, true
			}
			t := float64(kbps-lo.Kbps) / span
			return lo.Vmaf + t*(hi.Vmaf-lo.Vmaf), false, true
		}
	}
	return pts[len(pts)-1].Vmaf, false, true
}

// RungEstimate is one rung's design-time quality projection plus the verdict
// that answers "is this rung earning its place?".
type RungEstimate struct {
	Label  string  `json:"label"`
	Height int     `json:"height"`
	Width  int     `json:"width"`
	Kbps   int     `json:"kbps"`
	Vmaf   float64 `json:"vmaf,omitempty"`
	// DVmaf is the gain over the rung below (nil on the lowest rung, which has
	// nothing to improve on). This is the number that matters: a rung that adds
	// bitrate without adding quality is redundant regardless of how the ladder
	// looks on paper.
	DVmaf *float64 `json:"d_vmaf,omitempty"`
	DKbps *int     `json:"d_kbps,omitempty"`
	// Clamped marks an estimate whose bitrate fell outside the measured range,
	// so the value is the nearest endpoint rather than an interpolation. No
	// verdict is issued for a clamped rung — the delta isn't real.
	Clamped bool `json:"clamped,omitempty"`
	// Verdict is a plain-language call, or "" when there's no curve data.
	//   redundant  — costs bitrate, returns < RedundantVmaf VMAF over the rung below
	//   saturated  — already above SaturatedVmaf; more bitrate buys nothing visible
	//   wide-gap   — > WideGapVmaf jump from the rung below; a switch here is visible
	//   ok         — earns its slot
	Verdict string `json:"verdict,omitempty"`
}

// Verdict thresholds. ~6 VMAF is the commonly cited just-noticeable difference,
// so a rung under a third of that is doing nothing a viewer can see, and a jump
// over one-and-a-half JNDs is a visible step when the player switches.
const (
	RedundantVmaf = 2.0
	WideGapVmaf   = 9.0
	SaturatedVmaf = 96.0
)

// LadderEstimates projects every rung of a ladder for one codec against the
// measured curve. sourceWidth 0 means "design time" — no upscale filtering, so
// the whole ladder is shown as authored rather than as it would apply to some
// particular file.
func (m *Manager) LadderEstimates(ladderName, codec string, reference int, clip string) []RungEstimate {
	if m.Ladders == nil || m.Curves == nil {
		return nil
	}
	if reference == 0 {
		reference = DefaultCurveReference
	}
	if clip == "" {
		clip = m.Curves.Clip
	}
	rungs := m.Ladders.resolveRungs(ladderName, codec, "", "", 0)
	out := make([]RungEstimate, 0, len(rungs))
	var prev *RungEstimate
	for _, r := range rungs {
		e := RungEstimate{Label: r.Label, Height: r.Height, Width: r.Width, Kbps: r.Bitrate}
		if v, clamped, ok := m.Curves.EstimateVmaf(codec, r.Height, r.Bitrate, reference, clip); ok {
			e.Vmaf, e.Clamped = v, clamped
			if clamped {
				// Outside the measured curve: report the endpoint, judge nothing.
			} else if prev != nil && prev.Vmaf > 0 && !prev.Clamped {
				d := v - prev.Vmaf
				dk := r.Bitrate - prev.Kbps
				e.DVmaf, e.DKbps = &d, &dk
				switch {
				case v >= SaturatedVmaf:
					e.Verdict = "saturated"
				case d < RedundantVmaf:
					e.Verdict = "redundant"
				case d > WideGapVmaf:
					e.Verdict = "wide-gap"
				default:
					e.Verdict = "ok"
				}
			} else if v >= SaturatedVmaf {
				e.Verdict = "saturated"
			} else {
				e.Verdict = "ok"
			}
		}
		out = append(out, e)
		last := out[len(out)-1]
		prev = &last
	}
	return out
}
