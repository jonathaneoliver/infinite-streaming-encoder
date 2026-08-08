package encode

import (
	"testing"
	"time"
)

// The version field is OPTIONAL on both markers: the cloud path never emits it,
// and a farm worker older than the heartbeat field cannot. So the pre-version
// forms must keep parsing exactly as before — a regex that silently stopped
// matching them would take the chunk plot's per-machine colouring with it.

func TestFleetMarkerParsesWithAndWithoutVersion(t *testing.T) {
	cases := []struct {
		name        string
		line        string
		wantMachine string
		wantVersion string
		wantChunks  []string
	}{{
		name:        "pre-version form (cloud, and old farm workers)",
		line:        `[[ENCODER-FLEET machine=ubuntu busy=7.5 perf=8 chunks=enc-1|enc-2]]`,
		wantMachine: "ubuntu", wantVersion: "", wantChunks: []string{"enc-1", "enc-2"},
	}, {
		name:        "with version",
		line:        `[[ENCODER-FLEET machine=ubuntu busy=7.5 perf=8 version=84df69e chunks=enc-1|enc-2]]`,
		wantMachine: "ubuntu", wantVersion: "84df69e", wantChunks: []string{"enc-1", "enc-2"},
	}, {
		name:        "version with no chunks (box idle)",
		line:        `[[ENCODER-FLEET machine=mac busy=0 perf=8 version=84df69e chunks=]]`,
		wantMachine: "mac", wantVersion: "84df69e", wantChunks: nil,
	}, {
		name:        "neither optional field",
		line:        `[[ENCODER-FLEET machine=mac busy=0 perf=8]]`,
		wantMachine: "mac", wantVersion: "", wantChunks: nil,
	}}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := &Manager{}
			if !m.recordFleetCPU(c.line) {
				t.Fatalf("marker not recognised: %s", c.line)
			}
			fleet := m.FleetCPU()
			if len(fleet) != 1 {
				t.Fatalf("got %d fleet entries, want 1", len(fleet))
			}
			e := fleet[0]
			if e.Machine != c.wantMachine {
				t.Errorf("machine = %q, want %q", e.Machine, c.wantMachine)
			}
			if e.Version != c.wantVersion {
				t.Errorf("version = %q, want %q", e.Version, c.wantVersion)
			}
			if len(e.Chunks) != len(c.wantChunks) {
				t.Fatalf("chunks = %v, want %v", e.Chunks, c.wantChunks)
			}
			for i := range c.wantChunks {
				if e.Chunks[i] != c.wantChunks[i] {
					t.Errorf("chunks = %v, want %v", e.Chunks, c.wantChunks)
				}
			}
		})
	}
}

// The ordering trap: chunks is `[^\]]*` so it swallows anything after it. If
// version were emitted last it would land inside the chunk list instead.
func TestVersionIsNotSwallowedIntoChunks(t *testing.T) {
	m := &Manager{}
	m.recordFleetCPU(`[[ENCODER-FLEET machine=mac busy=1 perf=8 version=abc123 chunks=enc-1]]`)
	e := m.FleetCPU()[0]
	for _, c := range e.Chunks {
		if c == "enc-1 version=abc123" || len(c) > len("enc-1") {
			t.Fatalf("version leaked into the chunk list: %q", e.Chunks)
		}
	}
	if e.Version != "abc123" {
		t.Errorf("version = %q, want abc123", e.Version)
	}
}

// A worker that stops reporting a version must not erase what we already knew:
// unknown is a weaker claim than the last known value, not a correction of it.
func TestSilentMarkerDoesNotEraseKnownVersion(t *testing.T) {
	m := &Manager{}
	m.recordFleetCPU(`[[ENCODER-FLEET machine=mac busy=1 perf=8 version=abc123 chunks=]]`)
	m.recordFleetCPU(`[[ENCODER-FLEET machine=mac busy=2 perf=8 chunks=]]`)
	if got := m.FleetCPU()[0].Version; got != "abc123" {
		t.Errorf("version = %q after a silent marker, want abc123 retained", got)
	}
}

func TestFleetVersionSkew(t *testing.T) {
	t.Run("uniform fleet is not mixed", func(t *testing.T) {
		m := &Manager{}
		m.recordFleetCPU(`[[ENCODER-FLEET machine=mac busy=1 perf=8 version=abc123 chunks=]]`)
		m.recordFleetCPU(`[[ENCODER-FLEET machine=ubuntu busy=1 perf=8 version=abc123 chunks=]]`)
		mixed, by, unknown := m.FleetVersionSkew()
		if mixed {
			t.Error("same version on both boxes reported as mixed")
		}
		if len(by) != 2 || len(unknown) != 0 {
			t.Errorf("by=%v unknown=%v", by, unknown)
		}
	})

	t.Run("two versions is mixed", func(t *testing.T) {
		m := &Manager{}
		m.recordFleetCPU(`[[ENCODER-FLEET machine=mac busy=1 perf=8 version=new chunks=]]`)
		m.recordFleetCPU(`[[ENCODER-FLEET machine=ubuntu busy=1 perf=8 version=old chunks=]]`)
		mixed, by, _ := m.FleetVersionSkew()
		if !mixed {
			t.Fatal("mac=new ubuntu=old not reported as mixed — this is the whole bug")
		}
		if by["mac"] != "new" || by["ubuntu"] != "old" {
			t.Errorf("by = %v", by)
		}
	})

	// The important negative: a box that never reported is NOT agreement. Calling
	// the fleet uniform on the strength of silence is exactly how #248 hid.
	t.Run("silent box is unknown, not agreement", func(t *testing.T) {
		m := &Manager{}
		m.recordFleetCPU(`[[ENCODER-FLEET machine=mac busy=1 perf=8 version=abc123 chunks=]]`)
		m.recordFleetCPU(`[[ENCODER-FLEET machine=ubuntu busy=1 perf=8 chunks=]]`)
		mixed, by, unknown := m.FleetVersionSkew()
		if mixed {
			t.Error("one known + one silent should not be MIXED (nothing to compare)")
		}
		if len(unknown) != 1 || unknown[0] != "ubuntu" {
			t.Fatalf("unknown = %v, want [ubuntu]", unknown)
		}
		if _, ok := by["ubuntu"]; ok {
			t.Error("a silent box must not appear in the version map")
		}
	})

	t.Run("a departed box's version is not a live skew", func(t *testing.T) {
		m := &Manager{}
		m.recordFleetCPU(`[[ENCODER-FLEET machine=mac busy=1 perf=8 version=new chunks=]]`)
		m.recordFleetCPU(`[[ENCODER-FLEET machine=gone busy=1 perf=8 version=old chunks=]]`)
		// Age `gone` past the liveness TTL.
		m.fleetMu.Lock()
		s := m.fleetCPU["gone"]
		s[len(s)-1].T = time.Now().Add(-fleetEntryTTL - time.Minute)
		m.fleetMu.Unlock()
		if mixed, _, _ := m.FleetVersionSkew(); mixed {
			t.Error("a box silent past the TTL still counted toward skew")
		}
	})
}

// ENCODER-HOST carries the version onto the stage record — the durable half,
// since RunRecord.Stages persists it into every output's run.json.
func TestHostMarkerCarriesVersionOntoStage(t *testing.T) {
	newJob := func() *Job {
		return &Job{Stages: []StageProgress{{Key: "encode:h264:396p:chunk1"}}}
	}

	t.Run("pre-version form still sets the instance", func(t *testing.T) {
		j := newJob()
		if !j.parseMarker(`[[ENCODER-HOST key=encode:h264:396p:chunk1 instance=ubuntu]]`) {
			t.Fatal("marker not recognised")
		}
		if j.Stages[0].Instance != "ubuntu" {
			t.Errorf("instance = %q", j.Stages[0].Instance)
		}
		if j.Stages[0].Version != "" {
			t.Errorf("version = %q, want empty", j.Stages[0].Version)
		}
	})

	t.Run("version lands on the stage", func(t *testing.T) {
		j := newJob()
		if !j.parseMarker(`[[ENCODER-HOST key=encode:h264:396p:chunk1 instance=ubuntu version=84df69e]]`) {
			t.Fatal("marker not recognised")
		}
		if j.Stages[0].Instance != "ubuntu" || j.Stages[0].Version != "84df69e" {
			t.Errorf("instance=%q version=%q", j.Stages[0].Instance, j.Stages[0].Version)
		}
	})

	// Failover re-tags the chunk. If the new worker is silent, the known version
	// must survive rather than being blanked.
	t.Run("re-tag without a version keeps the old one", func(t *testing.T) {
		j := newJob()
		j.parseMarker(`[[ENCODER-HOST key=encode:h264:396p:chunk1 instance=ubuntu version=84df69e]]`)
		j.parseMarker(`[[ENCODER-HOST key=encode:h264:396p:chunk1 instance=mac]]`)
		if j.Stages[0].Instance != "mac" {
			t.Errorf("instance = %q, want the re-tag to win", j.Stages[0].Instance)
		}
		if j.Stages[0].Version != "84df69e" {
			t.Errorf("version = %q, want the known value retained", j.Stages[0].Version)
		}
	})
}
