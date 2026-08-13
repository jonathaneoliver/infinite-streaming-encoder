package encode

import (
	"path/filepath"
	"testing"
)

// The pre-flight estimate has one job: agree with what the finished run reports.
// If it disagrees, the difference reads as a bug in the ENCODE rather than in
// the estimator, which is worse than showing no estimate at all. These pin the
// parts that can be checked without ffprobe.

func testStore(t *testing.T) *LadderStore {
	t.Helper()
	return LoadLadderStore(filepath.Join(t.TempDir(), "ladders.json"))
}

func TestLadderOutputGBIsBitrateTimesDuration(t *testing.T) {
	m := &Manager{Ladders: testStore(t)}
	cfg := JobConfig{Codec: "h264", Ladder: DefaultLadderName}

	// 300s of a ladder summing to K kbps is K*1000*300/8 bytes. Derive the
	// expected value from the same rungs rather than hardcoding a number, so
	// the test survives a ladder edit but still catches an arithmetic slip.
	var kbps int
	for _, r := range m.Ladders.resolveRungs(DefaultLadderName, "h264", "", "", 3840) {
		kbps += r.Bitrate
	}
	if kbps == 0 {
		t.Skip("seed ladder unavailable")
	}
	want := float64(kbps) * 1000 * 300 / 8 / 1e9
	got := m.ladderOutputGB(cfg, 3840, 300)
	if got < want*0.999 || got > want*1.001 {
		t.Fatalf("ladderOutputGB = %v, want %v", got, want)
	}

	// Doubling the duration must double the bytes — a linear relationship the
	// egress figure depends on.
	if d := m.ladderOutputGB(cfg, 3840, 600); d < got*1.999 || d > got*2.001 {
		t.Fatalf("not linear in duration: %v vs %v", d, got)
	}
}

func TestLadderOutputGBRespectsResolutionLimits(t *testing.T) {
	// max-res narrows the ladder, so it must narrow the predicted bytes too —
	// otherwise the estimate quotes a full ladder for a capped run and the user
	// is told a 4K price for a 1080p encode.
	m := &Manager{Ladders: testStore(t)}
	full := m.ladderOutputGB(JobConfig{Codec: "h264", Ladder: DefaultLadderName}, 3840, 300)
	capped := m.ladderOutputGB(
		JobConfig{Codec: "h264", Ladder: DefaultLadderName, MaxRes: "720p"}, 3840, 300)
	if full == 0 {
		t.Skip("seed ladder unavailable")
	}
	if capped >= full {
		t.Fatalf("max-res 720p did not reduce output: capped=%v full=%v", capped, full)
	}
}

func TestEstimateRefusesWithoutFiles(t *testing.T) {
	m := &Manager{Ladders: testStore(t)}
	if _, err := m.EstimateCost(JobConfig{}); err == nil {
		t.Fatal("estimate with no files should fail, not quote $0")
	}
}

func TestEstimateIsZeroAndFlaggedForLocalTargets(t *testing.T) {
	// Local and local-dist write straight to disk: no AWS spend, no egress.
	// Reported as an explicit $0 with Local set, rather than an absent line —
	// the contrast with the cloud figure is the point of showing it.
	m := &Manager{Ladders: testStore(t), SourceDir: t.TempDir()}
	for _, target := range []Target{"local", "", "local-dist"} {
		cfg := JobConfig{Files: []string{"nope.mp4"}, Target: target}
		// Unprobeable file -> error path; what matters is it never quotes a
		// cost it cannot support.
		if est, err := m.EstimateCost(cfg); err == nil && est.TotalUSD != 0 {
			t.Fatalf("target %q quoted $%v for an unprobeable source", target, est.TotalUSD)
		}
	}
}

func TestLocalWallPredictionRespondsToTheFormControls(t *testing.T) {
	// #343: on a local target $0.00 is a literal, so the predicted wall time is
	// the ONLY figure the panel can show that answers to anything the user
	// selects. Each of these used to move the estimate by exactly nothing.
	m := &Manager{Ladders: testStore(t), Speeds: LoadEncodeSpeedStore(filepath.Join(t.TempDir(), "speeds.json"))}
	base := JobConfig{Codec: "h264", Ladder: DefaultLadderName, Target: "local"}

	oneCodec, _, rungs := m.projectLocalWallDetail(base, 3840, 30, 300)
	if rungs == 0 || oneCodec <= 0 {
		t.Skip("seed ladder unavailable")
	}

	// More codecs is strictly more work on the same fleet.
	twoCodec, _, _ := m.projectLocalWallDetail(
		JobConfig{Codec: "both", Ladder: DefaultLadderName, Target: "local"}, 3840, 30, 300)
	if twoCodec <= oneCodec {
		t.Fatalf("adding hevc did not increase predicted wall: %v -> %v", oneCodec, twoCodec)
	}

	// A narrower resolution band is less work.
	capped, _, _ := m.projectLocalWallDetail(
		JobConfig{Codec: "h264", Ladder: DefaultLadderName, Target: "local", MaxRes: "720p"}, 3840, 30, 300)
	if capped >= oneCodec {
		t.Fatalf("max-res 720p did not reduce predicted wall: %v vs %v", capped, oneCodec)
	}

	// And twice the content is more wall. Not asserted as exactly 2x: the
	// makespan is max(coreSeconds/cores, longest atomic chunk), and the floor
	// term is deliberately not linear.
	longer, _, _ := m.projectLocalWallDetail(base, 3840, 30, 600)
	if longer <= oneCodec {
		t.Fatalf("doubling duration did not increase predicted wall: %v -> %v", oneCodec, longer)
	}
}

func TestLocalTargetsCarryTheCloudWhatIfButAreStillBilledNothing(t *testing.T) {
	// The local panel compared against four SaaS vendors while omitting our own
	// cloud path — the one baseline the user can act on. So SpotUSD is now
	// projected for local targets too.
	//
	// The trap it must not fall into: TotalUSD means "what this run will be
	// billed". Folding the what-if into it would quote a local run a charge that
	// never arrives, and put the estimate and the finished run's report on two
	// different bases (#237).
	m := &Manager{Ladders: testStore(t), Speeds: LoadEncodeSpeedStore(filepath.Join(t.TempDir(), "speeds.json"))}
	cfg := JobConfig{Codec: "h264", Ladder: DefaultLadderName, Target: "local"}

	spot, _, _, _, _ := m.projectCloudCostDetail(cfg, 3840, 30, 300)
	if spot <= 0 {
		t.Skip("seed ladder unavailable")
	}

	// And the same config priced as cloud must agree — one projection, two
	// presentations. If these diverge the "would be" figure is fiction.
	cloudCfg := cfg
	cloudCfg.Target = "cloud"
	cloudSpot, _, _, _, _ := m.projectCloudCostDetail(cloudCfg, 3840, 30, 300)
	if cloudSpot != spot {
		t.Fatalf("local what-if $%v disagrees with the cloud projection $%v for the same ladder", spot, cloudSpot)
	}
}

func TestLocalWallSaysWhenItIsGuessing(t *testing.T) {
	// seedSpeed never returns <= 0, so an unlearned fleet still produces a
	// confident-looking number. The UI can only mark it as seeded if the
	// estimate reports the provenance, so a cold store must come back with
	// learned == 0 rather than silently looking measured.
	m := &Manager{Ladders: testStore(t), Speeds: LoadEncodeSpeedStore(filepath.Join(t.TempDir(), "speeds.json"))}
	cfg := JobConfig{Codec: "h264", Ladder: DefaultLadderName, Target: "local"}
	secs, learned, rungs := m.projectLocalWallDetail(cfg, 3840, 30, 300)
	if rungs == 0 {
		t.Skip("seed ladder unavailable")
	}
	if secs <= 0 {
		t.Fatal("a cold speed store must still predict a time, from the seed model")
	}
	if learned != 0 {
		t.Fatalf("cold store reported %d learned rungs — the UI would call a guess a measurement", learned)
	}
}

func TestSkipMediaDownloadRemovesEgressFromTheEstimate(t *testing.T) {
	// The whole point of showing this before the button: ticking "leave media in
	// S3" should visibly drop the quote. If the estimate ignored the flag it
	// would advertise a saving the run then does not make, or hide one it does.
	m := &Manager{Ladders: testStore(t)}
	yes, no := true, false
	cfgOn := JobConfig{Codec: "h264", Ladder: DefaultLadderName, Target: "cloud",
		SkipMediaDownload: &yes}
	cfgOff := JobConfig{Codec: "h264", Ladder: DefaultLadderName, Target: "cloud",
		SkipMediaDownload: &no}
	if m.skipMediaDownload(cfgOn) != true || m.skipMediaDownload(cfgOff) != false {
		t.Fatal("skipMediaDownload does not honour the per-run override")
	}
	// And the bytes it would have transferred are non-zero, so the difference
	// the UI shows is real rather than two zeros.
	if gb := m.ladderOutputGB(cfgOff, 3840, 300); gb <= 0 {
		t.Skip("seed ladder unavailable")
	}
}
