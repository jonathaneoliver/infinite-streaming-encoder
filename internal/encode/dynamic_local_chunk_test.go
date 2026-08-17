package encode

import (
	"math"
	"strings"
	"testing"
)

// #362. Local dynamic chunking sizes each variant from learned encode speed,
// the same way cloud does. Before, "dynamic" on a local target collapsed to a
// fixed 2×segment — not because the model was missing (speedKey has always
// carried the machine) but because nothing asked it.

func storeWith(t *testing.T, speeds map[string]float64) *EncodeSpeedStore {
	t.Helper()
	s := &EncodeSpeedStore{speeds: map[string]float64{}, samples: map[string]int{}}
	for k, v := range speeds {
		s.speeds[k] = v
		s.samples[k] = 5
	}
	return s
}

// The whole point of choosing slowest over average: the target is a BOUND, and
// a chunk lands on whichever box takes it off the queue.
func TestLocalSpeedSlowestTakesTheMinNotTheMean(t *testing.T) {
	s := storeWith(t, map[string]float64{
		speedKey("mac", "h264", 1080, true, "medium", 30):     4.0,
		speedKey("ubuntu", "h264", 1080, true, "medium", 30):  2.0,
		speedKey("macmini", "h264", 1080, true, "medium", 30): 1.0,
	})
	got, n := s.LocalSpeedSlowest("h264", 1080, true, "medium", 30)
	if got != 1.0 || n != 3 {
		t.Fatalf("LocalSpeedSlowest = %v (n=%d), want 1.0 (n=3) — the macmini's", got, n)
	}
	// The average is what LocalSpeedN is for, and it is a different number. If
	// these ever agree the test has stopped proving anything.
	if avg, _ := s.LocalSpeedN("h264", 1080, true, "medium", 30); avg <= got {
		t.Fatalf("average %v should exceed slowest %v", avg, got)
	}
}

// A chunk never runs on Graviton locally, so a cloud sample must not size one.
// This is the bug the whole change is about, in miniature: one wrong machine in
// the throughput term and the answer is for the wrong hardware.
func TestLocalSpeedSlowestIgnoresGraviton(t *testing.T) {
	s := storeWith(t, map[string]float64{
		speedKey("graviton", "hevc", 2160, true, "medium", 30): 0.1, // slowest overall
		speedKey("mac", "hevc", 2160, true, "medium", 30):      0.5,
	})
	got, n := s.LocalSpeedSlowest("hevc", 2160, true, "medium", 30)
	if got != 0.5 || n != 1 {
		t.Fatalf("LocalSpeedSlowest = %v (n=%d), want 0.5 (n=1) — graviton excluded", got, n)
	}
}

// n == 0 says "seeded, not observed". A caller cannot tell a cold key from a
// well-learned one otherwise, which is the #314 class of mistake.
func TestLocalSpeedSlowestSeedsWhenNothingLearned(t *testing.T) {
	s := storeWith(t, nil)
	got, n := s.LocalSpeedSlowest("h264", 720, false, "medium", 30)
	if n != 0 {
		t.Fatalf("n = %d, want 0 for a cold store", n)
	}
	if want := seedSpeed("ubuntu", "h264", 720, false, "medium", 30); got != want {
		t.Fatalf("seed = %v, want %v — same baseline LocalSpeedN uses, so a cold "+
			"store sizes exactly as it predicts", got, want)
	}
}

// A slow variant and a fast one must not come out the same length — that IS the
// feature. Sized against the 240s target: 0.5x → 120s → quantised to 120;
// 8x → 1920s → clamped to the clip.
func TestDynamicLocalSizesPerVariant(t *testing.T) {
	s := storeWith(t, map[string]float64{
		speedKey("macmini", "hevc", 2160, true, "medium", 30): 0.5,
		speedKey("macmini", "h264", 360, false, "medium", 30): 8.0,
	})
	slow := dynamicLocalChunkSeconds(s, "hevc", 2160, true, "medium", 30, 600)
	fast := dynamicLocalChunkSeconds(s, "h264", 360, false, "medium", 30, 600)
	if slow != 120 {
		t.Fatalf("hevc/2160p chunk = %v, want 120 (240s x 0.5, on the 12s quantum)", slow)
	}
	if fast != 600 {
		t.Fatalf("h264/360p chunk = %v, want 600 — a cheap rung is ONE whole chunk", fast)
	}
}

// The floor and the quantum are shared with the cloud selector, because they
// are properties of "how big should a chunk be", not of where it runs.
func TestDynamicLocalHonoursFloorAndQuantum(t *testing.T) {
	s := storeWith(t, map[string]float64{
		speedKey("ubuntu", "av1", 2160, true, "medium", 30): 0.001, // glacial
	})
	got := dynamicLocalChunkSeconds(s, "av1", 2160, true, "medium", 30, 3600)
	if got != dynamicMinChunkSeconds {
		t.Fatalf("chunk = %v, want the %v floor", got, dynamicMinChunkSeconds)
	}
	s2 := storeWith(t, map[string]float64{
		speedKey("ubuntu", "h264", 540, false, "medium", 30): 0.13, // 31.2s raw
	})
	got2 := dynamicLocalChunkSeconds(s2, "h264", 540, false, "medium", 30, 3600)
	if math.Mod(got2, dynamicMinChunkSeconds) != 0 {
		t.Fatalf("chunk = %v, not a multiple of the %v quantum — chunk boundaries "+
			"must stay segment-aligned", got2, dynamicMinChunkSeconds)
	}
}

// Cloud must plan byte-identically: this change adds a local caller, it does not
// re-tune the cloud selector.
func TestCloudSelectorUnchangedByTheRefactor(t *testing.T) {
	s := storeWith(t, map[string]float64{
		speedKey("graviton", "hevc", 1080, true, "medium", 30): 0.75,
	})
	got := dynamicChunkSecondsAt(dynamicTargetWallSeconds, s, "hevc", 1080, true, "medium", 30, 3600)
	if got != 180 {
		t.Fatalf("graviton chunk = %v, want 180 (240 x 0.75)", got)
	}
}

// An explicit size is what the user asked for. Growing it silently is the thing
// #312 refuses to do on the cloud side, and this must not do it either.
func TestVariantChunkArgsOnlyForDynamic(t *testing.T) {
	m := &Manager{Speeds: storeWith(t, nil), Ladders: testStore(t)}
	for _, cfg := range []string{"12", "30", "whole", "300"} {
		if got := m.variantChunkArgs(JobConfig{ChunkDuration: cfg}, "/nonexistent.mp4"); got != nil {
			t.Fatalf("ChunkDuration %q produced %v, want nil — a fixed request is "+
				"passed through untouched", cfg, got)
		}
	}
}

// The arg is the contract with cli_local_dist's _parse_variant_chunks, which
// splits on the LAST colon and then the first slash. A label containing either
// would break it silently — this pins the shape Go emits.
func TestVariantChunkArgShape(t *testing.T) {
	m := &Manager{Speeds: storeWith(t, nil), Ladders: testStore(t)}
	args := m.variantChunkArgs(JobConfig{ChunkDuration: "dynamic", Codec: "h264"}, "/nonexistent.mp4")
	// Guard against the assertions below being vacuous: an unprobeable source
	// must still size every rung the ladder offers, because width 0 means "no
	// source cap", not "no rungs".
	if len(args) == 0 {
		t.Fatal("no --variant-chunk args produced; the loop below would prove nothing")
	}
	for i := 0; i < len(args); i += 2 {
		if args[i] != "--variant-chunk" {
			t.Fatalf("args[%d] = %q, want --variant-chunk", i, args[i])
		}
		v := args[i+1]
		key, secs, ok := strings.Cut(v, ":")
		if !ok || !strings.Contains(key, "/") {
			t.Fatalf("%q is not CODEC/LABEL:SECONDS", v)
		}
		if strings.Contains(key, ":") || secs == "" {
			t.Fatalf("%q: the key must hold no colon and the value must be present", v)
		}
	}
}
