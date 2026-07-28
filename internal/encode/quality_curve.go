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
//   334s of EXTREME-MOTION FPV footage. Ladder apple-uniq-live-full, 12 rungs.
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
type CurvePoint struct {
	Codec     string  `json:"codec"`
	Reference int     `json:"reference"` // grading reference height (2160 | 1080)
	Height    int     `json:"height"`    // the rung's own encoded height
	Kbps      int     `json:"kbps"`      // DELIVERED bitrate, not the target
	Vmaf      float64 `json:"vmaf"`      // pooled mean
	Harmonic  float64 `json:"harmonic"`  // harmonic mean — punishes bad frames
}

// SeedClip names the content the built-in curves were measured on, so the UI can
// say whose quality it is estimating.
const SeedClip = "insane_fpv_shots_hydrofoil_windsurfing.mkv (4K, extreme motion)"

// DefaultCurveReference is the grading reference used when none is requested.
// 2160 matches the top of the shipped ladders.
const DefaultCurveReference = 2160

func defaultSeedCurves() []CurvePoint {
	return []CurvePoint{
		{Codec: "h264", Reference: 2160, Height: 234, Kbps: 138, Vmaf: 24.74, Harmonic: 21.27},
		{Codec: "h264", Reference: 2160, Height: 360, Kbps: 339, Vmaf: 38.26, Harmonic: 35.63},
		{Codec: "h264", Reference: 2160, Height: 396, Kbps: 673, Vmaf: 47.87, Harmonic: 44.89},
		{Codec: "h264", Reference: 2160, Height: 432, Kbps: 1013, Vmaf: 54.41, Harmonic: 51.3},
		{Codec: "h264", Reference: 2160, Height: 540, Kbps: 1856, Vmaf: 64.93, Harmonic: 61.95},
		{Codec: "h264", Reference: 2160, Height: 684, Kbps: 2798, Vmaf: 72.68, Harmonic: 69.89},
		{Codec: "h264", Reference: 2160, Height: 720, Kbps: 4220, Vmaf: 78.49, Harmonic: 75.79},
		{Codec: "h264", Reference: 2160, Height: 1044, Kbps: 5669, Vmaf: 83.12, Harmonic: 80.52},
		{Codec: "h264", Reference: 2160, Height: 1080, Kbps: 7423, Vmaf: 86.6, Harmonic: 84.01},
		{Codec: "h264", Reference: 2160, Height: 1440, Kbps: 12975, Vmaf: 92.12, Harmonic: 89.46},
		{Codec: "h264", Reference: 2160, Height: 2124, Kbps: 18376, Vmaf: 93.67, Harmonic: 90.98},
		{Codec: "h264", Reference: 2160, Height: 2160, Kbps: 26190, Vmaf: 96.51, Harmonic: 93.72},
		{Codec: "hevc", Reference: 2160, Height: 360, Kbps: 151, Vmaf: 32.34, Harmonic: 29.41},
		{Codec: "hevc", Reference: 2160, Height: 432, Kbps: 301, Vmaf: 42.28, Harmonic: 39.06},
		{Codec: "hevc", Reference: 2160, Height: 468, Kbps: 593, Vmaf: 51.23, Harmonic: 47.76},
		{Codec: "hevc", Reference: 2160, Height: 504, Kbps: 886, Vmaf: 57.31, Harmonic: 53.87},
		{Codec: "hevc", Reference: 2160, Height: 540, Kbps: 1566, Vmaf: 65.09, Harmonic: 61.86},
		{Codec: "hevc", Reference: 2160, Height: 684, Kbps: 2345, Vmaf: 73.21, Harmonic: 70.25},
		{Codec: "hevc", Reference: 2160, Height: 720, Kbps: 3330, Vmaf: 77.97, Harmonic: 75.14},
		{Codec: "hevc", Reference: 2160, Height: 1044, Kbps: 4399, Vmaf: 83.72, Harmonic: 80.98},
		{Codec: "hevc", Reference: 2160, Height: 1080, Kbps: 5670, Vmaf: 86.7, Harmonic: 84.02},
		{Codec: "hevc", Reference: 2160, Height: 1440, Kbps: 7922, Vmaf: 90.69, Harmonic: 88.02},
		{Codec: "hevc", Reference: 2160, Height: 2124, Kbps: 11352, Vmaf: 93.55, Harmonic: 90.82},
		{Codec: "hevc", Reference: 2160, Height: 2160, Kbps: 16442, Vmaf: 96.23, Harmonic: 93.44},
		{Codec: "av1", Reference: 2160, Height: 360, Kbps: 146, Vmaf: 35.42, Harmonic: 32.56},
		{Codec: "av1", Reference: 2160, Height: 432, Kbps: 301, Vmaf: 46.02, Harmonic: 42.99},
		{Codec: "av1", Reference: 2160, Height: 468, Kbps: 598, Vmaf: 54.46, Harmonic: 51.48},
		{Codec: "av1", Reference: 2160, Height: 504, Kbps: 895, Vmaf: 60.46, Harmonic: 57.55},
		{Codec: "av1", Reference: 2160, Height: 540, Kbps: 1583, Vmaf: 67.4, Harmonic: 64.6},
		{Codec: "av1", Reference: 2160, Height: 684, Kbps: 2398, Vmaf: 75.45, Harmonic: 72.79},
		{Codec: "av1", Reference: 2160, Height: 720, Kbps: 3388, Vmaf: 79.37, Harmonic: 76.76},
		{Codec: "av1", Reference: 2160, Height: 1044, Kbps: 4492, Vmaf: 86.06, Harmonic: 83.48},
		{Codec: "av1", Reference: 2160, Height: 1080, Kbps: 5801, Vmaf: 88.66, Harmonic: 86.04},
		{Codec: "av1", Reference: 2160, Height: 1440, Kbps: 8095, Vmaf: 92.5, Harmonic: 89.83},
		{Codec: "av1", Reference: 2160, Height: 2124, Kbps: 11557, Vmaf: 95.09, Harmonic: 92.35},
		{Codec: "av1", Reference: 2160, Height: 2160, Kbps: 16639, Vmaf: 96.86, Harmonic: 94.06},

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
	s := &CurveStore{path: path, points: defaultSeedCurves(), Clip: SeedClip}
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
		return fmt.Sprintf("%s/%d/%d", p.Codec, p.Reference, p.Height)
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

// Points returns every curve point for one codec at one grading reference,
// sorted by bitrate.
func (s *CurveStore) Points(codec string, reference int) []CurvePoint {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var out []CurvePoint
	for _, p := range s.points {
		if p.Codec == codec && p.Reference == reference {
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
func (s *CurveStore) EstimateVmaf(codec string, kbps int, reference int) (float64, bool, bool) {
	pts := s.Points(codec, reference)
	if len(pts) == 0 || kbps <= 0 {
		return 0, false, false
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
func (m *Manager) LadderEstimates(ladderName, codec string, reference int) []RungEstimate {
	if m.Ladders == nil || m.Curves == nil {
		return nil
	}
	if reference == 0 {
		reference = DefaultCurveReference
	}
	rungs := m.Ladders.resolveRungs(ladderName, codec, "", "", 0)
	out := make([]RungEstimate, 0, len(rungs))
	var prev *RungEstimate
	for _, r := range rungs {
		e := RungEstimate{Label: r.Label, Height: r.Height, Width: r.Width, Kbps: r.Bitrate}
		if v, clamped, ok := m.Curves.EstimateVmaf(codec, r.Bitrate, reference); ok {
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
