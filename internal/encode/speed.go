package encode

import (
	"encoding/json"
	"math"
	"os"
	"strconv"
	"sync"
)

// Dynamic-chunking knobs (per the design):
//   - aim for ~4 min of encode WALL time per chunk, so all variants' chunks
//     finish around the same time;
//   - never smaller than a 30s content chunk (bounds join count / cold-starts).
const (
	dynamicTargetWallSeconds = 240.0
	// dynamicMinChunkSeconds is both the floor and the quantum: dynamic chunk
	// lengths are whole multiples of it (30s, 60s, 90s, …). 30 is itself a
	// multiple of the 6s segment duration, so every resulting size satisfies
	// the worker's plan_chunks._validate alignment contract for free.
	dynamicMinChunkSeconds = 30.0
)

// EncodeSpeedStore learns each variant's encode SPEED — content-seconds encoded
// per wall-second — keyed by {codec}:{height}:{pass}, persisted to
// $TmpDir/encode_speeds.json. The dynamic chunk selector uses it to size each
// variant's chunks: slow variants (4K HEVC ~0.03×) get many 30s chunks for max
// parallelism; cheap ones (small H264, many× real-time) get one whole chunk and
// pay no join cost. Learned values (from completed encodes) override the model.
type EncodeSpeedStore struct {
	mu      sync.Mutex
	path    string
	speeds  map[string]float64 // learned content_s/wall_s per key
	samples map[string]int
}

func speedKey(codec string, height int, twoPass bool) string {
	p := 1
	if twoPass {
		p = 2
	}
	return codec + ":" + strconv.Itoa(height) + ":" + strconv.Itoa(p)
}

// LoadEncodeSpeedStore reads the persisted model (empty if absent/corrupt).
func LoadEncodeSpeedStore(path string) *EncodeSpeedStore {
	s := &EncodeSpeedStore{path: path, speeds: map[string]float64{}, samples: map[string]int{}}
	if data, err := os.ReadFile(path); err == nil {
		var d struct {
			Speeds  map[string]float64 `json:"speeds"`
			Samples map[string]int     `json:"samples"`
		}
		if json.Unmarshal(data, &d) == nil {
			if d.Speeds != nil {
				s.speeds = d.Speeds
			}
			if d.Samples != nil {
				s.samples = d.Samples
			}
		}
	}
	return s
}

// Speed returns content-seconds-per-wall-second for (codec, height, pass). A
// learned value for the exact key wins; otherwise a model seeded from measured
// baselines: base speed at 1080p single-pass, scaled ~1/pixels (area), halved
// for two-pass. Never <= 0.
func (s *EncodeSpeedStore) Speed(codec string, height int, twoPass bool) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	if v, ok := s.speeds[speedKey(codec, height, twoPass)]; ok && v > 0 {
		return v
	}
	// Measured baselines at 1080p, single-pass (content/wall):
	//   h264 ~1.5×, hevc ~0.14× (=> 4K ~0.035×, matching the observed ~0.03×),
	//   av1 (SVT preset 6) ~0.1×.
	base := map[string]float64{"h264": 1.5, "hevc": 0.14, "av1": 0.1}[codec]
	if base <= 0 {
		base = 0.14
	}
	if height <= 0 {
		height = 1080
	}
	sp := base * (1080.0 * 1080.0) / (float64(height) * float64(height))
	if twoPass {
		sp /= 2
	}
	return math.Max(sp, 0.001)
}

// Update folds a completed encode's (content_s / wall_s) into a rolling average
// and persists. Ignored for non-positive inputs.
func (s *EncodeSpeedStore) Update(codec string, height int, twoPass bool, contentS, wallS float64) {
	if wallS <= 0 || contentS <= 0 {
		return
	}
	sample := contentS / wallS
	s.mu.Lock()
	k := speedKey(codec, height, twoPass)
	if s.samples[k] == 0 || s.speeds[k] <= 0 {
		s.speeds[k] = sample
	} else {
		const w = 0.3 // weight recent samples, but stabilize
		s.speeds[k] = s.speeds[k]*(1-w) + sample*w
	}
	s.samples[k]++
	snap := struct {
		Speeds  map[string]float64 `json:"speeds"`
		Samples map[string]int     `json:"samples"`
	}{s.speeds, s.samples}
	s.mu.Unlock()
	if data, err := json.MarshalIndent(snap, "", "  "); err == nil {
		tmp := s.path + ".tmp"
		if os.WriteFile(tmp, data, 0644) == nil {
			_ = os.Rename(tmp, s.path)
		}
	}
}

// dynamicChunkSeconds sizes one variant's chunk length: target wall time ×
// learned speed, clamped to [30s, clip]. Slow variants clamp to 30s (most
// parallel); fast variants reach the whole clip (one chunk, no joins).
func dynamicChunkSeconds(speeds *EncodeSpeedStore, codec string, height int, twoPass bool, clipDurationS float64) float64 {
	c := dynamicTargetWallSeconds * speeds.Speed(codec, height, twoPass)
	// Quantize to a whole multiple of the 30s minimum (30/60/90/…), floored at
	// the minimum. Keeps sizes clean and segment-aligned (30 | 6).
	c = math.Round(c/dynamicMinChunkSeconds) * dynamicMinChunkSeconds
	if c < dynamicMinChunkSeconds {
		c = dynamicMinChunkSeconds
	}
	// Clamp to the clip: a fast variant whose chunk >= clip becomes one whole
	// chunk (chunkCountForDuration returns 1 → EncodeWhole, which never runs
	// plan_chunks), so the clip value here needn't be segment-aligned.
	if clipDurationS > 0 && c > clipDurationS {
		c = clipDurationS
	}
	return c
}
