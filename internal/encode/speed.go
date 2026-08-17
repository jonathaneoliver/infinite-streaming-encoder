package encode

import (
	"encoding/json"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"
)

// Dynamic-chunking knobs (per the design):
//   - aim for ~4 min of encode WALL time per chunk, so all variants' chunks
//     finish around the same time;
//   - never smaller than a 12s content chunk (bounds join count / cold-starts).
const (
	dynamicTargetWallSeconds = 240.0
	// dynamicMinChunkSeconds is both the floor and the quantum: dynamic chunk
	// lengths are whole multiples of it (12s, 24s, 36s, …). 12 is itself a
	// multiple of the 6s segment duration, so every resulting size satisfies
	// the worker's plan_chunks._validate alignment contract for free. A smaller
	// floor than the old 30s lets the heaviest rungs (4K HEVC 2-pass) subdivide
	// into a shorter atomic long pole and a finer tail the fleet can pack.
	dynamicMinChunkSeconds = 12.0
)

// EncodeSpeedStore learns each variant's encode SPEED — content-seconds encoded
// per wall-second — keyed by {machine}:{codec}:{height}:{pass}:{preset}:{fps}
// (see speedKey), persisted to $TmpDir/encode_speeds.json. Three consumers: the
// dynamic chunk selector sizes each variant's chunks (slow 4K HEVC → many 12s
// chunks for parallelism; cheap H264 → one whole chunk, no join cost); cloud
// cost projects graviton wall-time; ETA extrapolates. Machine-keyed because a
// Mac, a Ryzen box, and a Graviton vCPU differ widely — RelativeSpeed averages
// across machines when only relative variant weight matters. Learned values
// (from completed encodes) override the seed model.
type EncodeSpeedStore struct {
	mu      sync.Mutex
	path    string
	speeds  map[string]float64 // learned content_s/wall_s per key
	samples map[string]int
}

// speedKey identifies a learned encode speed by every dimension that materially
// changes encode time: MACHINE (mac/ubuntu/macmini/graviton — hardware varies
// widely, and this also encodes local-vs-cloud), codec, output height, 1-vs-2
// pass, encoder preset, and source fps (encode time ∝ frames). Empty machine or
// preset default to "any"/"medium"; fps<=0 → 30.
func speedKey(machine, codec string, height int, twoPass bool, preset string, fps int) string {
	p := 1
	if twoPass {
		p = 2
	}
	if machine == "" {
		machine = "any"
	}
	if preset == "" {
		preset = "medium"
	}
	if fps <= 0 {
		fps = 30
	}
	return machine + ":" + codec + ":" + strconv.Itoa(height) + ":" +
		strconv.Itoa(p) + ":" + preset + ":" + strconv.Itoa(fps)
}

// keyMatch reports whether a stored key matches (codec,height,pass,preset,fps)
// ignoring the machine — used by machine-agnostic lookups.
func keyMatchNoMachine(key, codec string, height int, twoPass bool, preset string, fps int) bool {
	suffix := speedKey("", codec, height, twoPass, preset, fps)
	suffix = suffix[len("any"):] // ":codec:height:pass:preset:fps"
	return len(key) > len(suffix) && key[len(key)-len(suffix):] == suffix
}

// _presetSpeedMult scales the seed speed by encoder preset (relative to medium):
// faster presets encode faster, slower ones slower.
func _presetSpeedMult(preset string) float64 {
	switch preset {
	case "ultrafast":
		return 8
	case "superfast":
		return 5
	case "veryfast":
		return 3
	case "faster":
		return 1.8
	case "fast":
		return 1.3
	case "slow":
		return 0.5
	case "slower":
		return 0.3
	case "veryslow":
		return 0.18
	default: // medium
		return 1
	}
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

// seedSpeed is the model estimate for a variant on a given machine, used when no
// learned value exists. base speed at 1080p/medium/30fps single-pass, scaled
// ~1/pixels (area), by preset, by 30/fps (encode time ∝ frame count), halved for
// two-pass, and by the machine's relative throughput. Never <= 0.
func seedSpeed(machine, codec string, height int, twoPass bool, preset string, fps int) float64 {
	// Measured baselines at 1080p, medium, 30fps, single-pass (content/wall):
	//   h264 ~1.5×, hevc ~0.14× (=> 4K ~0.035×, matching the observed ~0.03×),
	//   av1 (SVT preset 6) ~0.1×.
	base := map[string]float64{"h264": 1.5, "hevc": 0.14, "av1": 0.1}[codec]
	if base <= 0 {
		base = 0.14
	}
	if height <= 0 {
		height = 1080
	}
	if fps <= 0 {
		fps = 30
	}
	sp := base * (1080.0 * 1080.0) / (float64(height) * float64(height))
	sp *= _presetSpeedMult(preset)
	sp *= 30.0 / float64(fps)
	if twoPass {
		sp /= 2
	}
	sp *= _machineSpeedMult(machine)
	return math.Max(sp, 0.001)
}

// _machineSpeedMult scales the seed by hardware. The baselines were measured on
// the ubuntu box (Ryzen); other machines are relative to it. graviton (cloud
// c7g) is the cost/ETA reference for cloud-batch.
func _machineSpeedMult(machine string) float64 {
	switch machine {
	case "mac", "macmini": // Apple Silicon perf cores, fewer of them
		return 0.8
	case "graviton": // c7g, per-vCPU comparable but arm x265 a touch slower
		return 0.7
	default: // ubuntu (baseline) / any / unknown
		return 1
	}
}

// Speed returns content-seconds-per-wall-second for the exact key. A learned
// value wins; otherwise the seed model. Never <= 0.
func (s *EncodeSpeedStore) Speed(machine, codec string, height int, twoPass bool, preset string, fps int) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	if v, ok := s.speeds[speedKey(machine, codec, height, twoPass, preset, fps)]; ok && v > 0 {
		return v
	}
	return seedSpeed(machine, codec, height, twoPass, preset, fps)
}

// RelativeSpeed is a machine-AGNOSTIC speed for a variant, used to weight
// progress/work across variants (the machine cancels out of relative weights).
// It averages every learned key matching (codec,height,pass,preset,fps) across
// machines; absent any, falls back to the machine-neutral seed ("any").
func (s *EncodeSpeedStore) RelativeSpeed(codec string, height int, twoPass bool, preset string, fps int) float64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	var sum float64
	var n int
	for k, v := range s.speeds {
		if v > 0 && keyMatchNoMachine(k, codec, height, twoPass, preset, fps) {
			sum += v
			n++
		}
	}
	if n > 0 {
		return sum / float64(n)
	}
	return seedSpeed("any", codec, height, twoPass, preset, fps)
}

// LocalSpeed is like RelativeSpeed but restricted to the LOCAL fleet (excludes
// graviton keys), for predicting local-hardware wall time. Averages learned
// non-graviton keys matching the variant; absent any, seeds from a representative
// local box (ubuntu, the baseline). Machine-agnostic across the local boxes so
// the makespan model can treat the fleet's cores as a single pool.
func (s *EncodeSpeedStore) LocalSpeed(codec string, height int, twoPass bool, preset string, fps int) float64 {
	sp, _ := s.LocalSpeedN(codec, height, twoPass, preset, fps)
	return sp
}

// LocalSpeedN is LocalSpeed plus the number of learned keys it averaged. n == 0
// means the speed is the SEED model, not an observation — a caller predicting
// wall time has to be able to say which, because a cold key and a key with 40k
// samples otherwise render with identical confidence. Same silent-divergence
// class as the pass-count contract (#314): the number is fine, the claim about
// where it came from is what misleads.
func (s *EncodeSpeedStore) LocalSpeedN(codec string, height int, twoPass bool, preset string, fps int) (float64, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var sum float64
	var n int
	for k, v := range s.speeds {
		if v > 0 && !strings.HasPrefix(k, "graviton:") && keyMatchNoMachine(k, codec, height, twoPass, preset, fps) {
			sum += v
			n++
		}
	}
	if n > 0 {
		return sum / float64(n), n
	}
	return seedSpeed("ubuntu", codec, height, twoPass, preset, fps), 0
}

// LocalSpeedSlowest is the local fleet's SLOWEST learned speed for a variant,
// not its average — the two answer different questions and #362 needs this one.
//
// LocalSpeedN predicts wall time, and an average is right for that: the run
// spreads over every box, so the fleet's mean throughput is what the clock
// follows. Chunk SIZING is a bound. The target is "no chunk is much more than
// ~4 minutes of work", and chunks go onto one queue that every worker polls, so
// the box that takes a given chunk is unknown when it is planned. Size by the
// mean and the promise is false on the slow box by exactly its ratio; size by
// the slowest and it holds for whoever picks it up.
//
// Smaller atoms are also the forgiving direction on a heterogeneous fleet: a
// fast box simply takes more of them, where one oversized chunk on the slowest
// box is a tail nothing can rebalance.
//
// Same graviton exclusion and same cold-start seed as LocalSpeedN, so a store
// with nothing learned sizes exactly as it predicts.
func (s *EncodeSpeedStore) LocalSpeedSlowest(codec string, height int, twoPass bool, preset string, fps int) (float64, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	slowest, n := 0.0, 0
	for k, v := range s.speeds {
		if v > 0 && !strings.HasPrefix(k, "graviton:") && keyMatchNoMachine(k, codec, height, twoPass, preset, fps) {
			if n == 0 || v < slowest {
				slowest = v
			}
			n++
		}
	}
	if n > 0 {
		return slowest, n
	}
	return seedSpeed("ubuntu", codec, height, twoPass, preset, fps), 0
}

// SpeedDetail returns the speed plus how many learned samples back it (0 =
// seeded from the model, not yet observed). For chunk-plan logging.
func (s *EncodeSpeedStore) SpeedDetail(machine, codec string, height int, twoPass bool, preset string, fps int) (float64, int) {
	s.mu.Lock()
	n := s.samples[speedKey(machine, codec, height, twoPass, preset, fps)]
	s.mu.Unlock()
	return s.Speed(machine, codec, height, twoPass, preset, fps), n
}

// Update folds a completed encode's (content_s / wall_s) into a rolling average
// and persists. Ignored for non-positive inputs.
func (s *EncodeSpeedStore) Update(machine, codec string, height int, twoPass bool, preset string, fps int, contentS, wallS float64) {
	if wallS <= 0 || contentS <= 0 {
		return
	}
	sample := contentS / wallS
	s.mu.Lock()
	k := speedKey(machine, codec, height, twoPass, preset, fps)
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
// learned speed, clamped to [dynamicMinChunkSeconds, clip]. Slow variants clamp
// to the floor (most parallel); fast variants reach the whole clip (one chunk,
// no joins).
func dynamicChunkSeconds(speeds *EncodeSpeedStore, codec string, height int, twoPass bool, preset string, fps int, clipDurationS float64) float64 {
	return dynamicChunkSecondsAt(dynamicTargetWallSeconds, speeds, codec, height, twoPass, preset, fps, clipDurationS)
}

// dynamicChunkSecondsAt is dynamicChunkSeconds with the wall-time target passed
// in rather than taken from the constant. The cloud planner raises it when the
// whole job's chunk count would not fit one Step Functions history (#316); every
// other caller wants the default and should use dynamicChunkSeconds.
//
// Note what does NOT move with the target: dynamicMinChunkSeconds stays both the
// floor and the quantum. Raising the floor alongside was tried and is worse at
// the same fit — the floor binds on the SLOWEST rungs, which are already the
// longest chunks in the run, so lifting it grows the atomic long pole to save
// chunks that are not where the count is. Raising only the target lengthens the
// rungs with room to spare first, and reaches the same chunk count with a
// shorter worst chunk (measured in TestScalingTheFloorTooIsWorseAtTheSameFit:
// 1361s against 1701s on a 4h HEVC ladder).
//
// It is not free either way: a target big enough to fit 4h of HEVC does lift the
// slowest rungs off the floor eventually, so the makespan floor rises. That is
// the trade #316 makes knowingly, and chunkBudgetLine states it in the job log.
func dynamicChunkSecondsAt(targetWallS float64, speeds *EncodeSpeedStore, codec string, height int, twoPass bool, preset string, fps int, clipDurationS float64) float64 {
	if targetWallS <= 0 {
		targetWallS = dynamicTargetWallSeconds
	}
	// Cloud-batch fans onto Graviton, so size chunks by graviton throughput.
	return quantizeChunkSeconds(targetWallS*speeds.Speed("graviton", codec, height, twoPass, preset, fps), clipDurationS)
}

// dynamicLocalChunkSeconds is dynamicChunkSecondsAt for the LOCAL fleet (#362).
//
// Everything about the dynamic selector except its throughput term is
// target-neutral — the 240s wall-time target, the 12s quantum and floor, the
// clamp to clip are properties of "how big should a chunk be", not of where it
// runs. The cloud/local split was the ONE hardcoded argument
// (`speeds.Speed("graviton", …)`), which is why local collapsed to a fixed
// 2×segment: not because the model was missing, but because nothing asked it.
// speedKey has always carried the machine, so the farm's boxes were being
// learned all along.
//
// No whole-job budget pass here, unlike cloud: #316 rations against Step
// Functions' 25,000-event history, and Temporal's limits are ~3x larger. The
// target is always the default.
func dynamicLocalChunkSeconds(speeds *EncodeSpeedStore, codec string, height int, twoPass bool, preset string, fps int, clipDurationS float64) float64 {
	sp, _ := speeds.LocalSpeedSlowest(codec, height, twoPass, preset, fps)
	return quantizeChunkSeconds(dynamicTargetWallSeconds*sp, clipDurationS)
}

// quantizeChunkSeconds turns "content seconds that fit the wall-time target"
// into a plannable chunk length. Shared by both targets so they cannot drift:
// the sizing differs only in whose throughput went in.
func quantizeChunkSeconds(c, clipDurationS float64) float64 {
	// Quantize to a whole multiple of the minimum (12/24/36/…), floored at the
	// minimum. Keeps sizes clean and segment-aligned (12 | 6).
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
