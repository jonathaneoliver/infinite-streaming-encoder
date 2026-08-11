package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// #294: `on` was one boolean covering two unrelated facts — "the user turned it
// off" and "it is not polling" — and the second silently absorbed asleep,
// crashed, unreachable and never-started. A macmini that slept through most of a
// run rendered as a switched-off box, and that is the conclusion that got drawn
// from the pill.
//
// The states are computed in the handler, so these tests go through it.

// fakeTemporal serves the task-queue poller listing the handler reads.
func fakeTemporal(t *testing.T, pollers map[string]time.Time) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.Path, "/task-queues/") {
			http.NotFound(w, r)
			return
		}
		var rows []map[string]any
		for id, at := range pollers {
			rows = append(rows, map[string]any{
				"identity": id, "lastAccessTime": at.UTC().Format(time.RFC3339Nano),
			})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"pollers": rows})
	}))
	t.Cleanup(srv.Close)
	t.Setenv("TEMPORAL_UI_ADDR", srv.URL)
	return srv
}

type workerRow struct {
	Name         string `json:"name"`
	On           bool   `json:"on"`
	Local        bool   `json:"local"`
	State        string `json:"state"`
	LastSeenAgoS *int64 `json:"last_seen_ago_s"`
}

func fetchWorkers(t *testing.T, s *Server) map[string]workerRow {
	t.Helper()
	rec := httptest.NewRecorder()
	s.distWorkers(rec, httptest.NewRequest("GET", "/api/dist/workers", nil))
	var body struct {
		Machines []workerRow `json:"machines"`
		Count    int         `json:"count"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	out := map[string]workerRow{}
	for _, m := range body.Machines {
		out[m.Name] = m
	}
	return out
}

func newDistServer(t *testing.T) *Server {
	t.Helper()
	t.Setenv("LOCAL_WORKER_LABEL", "mac")
	t.Setenv("DIST_WORKERS", "macmini=user@macmini.local ubuntu=user@ubuntu.local")
	return NewServer(encode.NewManager(encode.ManagerConfig{TmpDir: t.TempDir()}))
}

// The four states, told apart. This is the whole issue in one test.
func TestWorkerStatesAreDistinguished(t *testing.T) {
	now := time.Now()
	fakeTemporal(t, map[string]time.Time{
		"mac":     now.Add(-2 * time.Second),
		"macmini": now.Add(-10 * time.Minute), // listed, but long silent: ASLEEP
		// "ubuntu" absent entirely and never seen.
	})
	s := newDistServer(t)

	got := fetchWorkers(t, s)
	if got["mac"].State != WorkerPolling {
		t.Errorf("mac: state = %q, want %q", got["mac"].State, WorkerPolling)
	}
	// The one that matters. Temporal keeps a poller record for ~5 minutes after
	// its last poll, so a sleeping box stays LISTED — reading presence as
	// health calls it healthy for exactly the window someone would notice.
	if got["macmini"].State != WorkerStale {
		t.Errorf("macmini: state = %q, want %q — a listed-but-silent poller was "+
			"read as healthy, which is the #294 misreport", got["macmini"].State, WorkerStale)
	}
	if got["ubuntu"].State != WorkerNeverSeen {
		t.Errorf("ubuntu: state = %q, want %q", got["ubuntu"].State, WorkerNeverSeen)
	}

	// And a disabled box is not any of those.
	s.distMu.Lock()
	s.distDisabled["ubuntu"] = true
	s.distMu.Unlock()
	if st := fetchWorkers(t, s)["ubuntu"].State; st != WorkerDisabled {
		t.Errorf("disabled ubuntu: state = %q, want %q", st, WorkerDisabled)
	}
}

// Disabled outranks polling. During the seconds between the toggle and the
// container actually stopping — or forever, if the stop failed — the user's
// intent is the more useful answer than the stale fact that it is still there.
func TestDisabledWinsOverALivePoller(t *testing.T) {
	fakeTemporal(t, map[string]time.Time{"macmini": time.Now()})
	s := newDistServer(t)
	s.distMu.Lock()
	s.distDisabled["macmini"] = true
	s.distMu.Unlock()

	got := fetchWorkers(t, s)["macmini"]
	if got.State != WorkerDisabled {
		t.Errorf("state = %q, want %q", got.State, WorkerDisabled)
	}
	if got.On {
		t.Error("on = true for a machine the user disabled")
	}
}

// `on` is kept for compatibility and must mean exactly one thing: polling.
// Anything looser and the old callers start disagreeing with the new field.
func TestOnMeansExactlyPolling(t *testing.T) {
	now := time.Now()
	fakeTemporal(t, map[string]time.Time{
		"mac":     now,
		"macmini": now.Add(-10 * time.Minute),
	})
	s := newDistServer(t)
	for name, m := range fetchWorkers(t, s) {
		if m.On != (m.State == WorkerPolling) {
			t.Errorf("%s: on=%v but state=%q", name, m.On, m.State)
		}
	}
}

// The number the tooltip shows. "never seen" and "seen 10 minutes ago" are
// different sentences, so the absent case must stay absent rather than becoming
// a plausible-looking zero.
func TestLastSeenIsReportedAndAbsentWhenNeverSeen(t *testing.T) {
	fakeTemporal(t, map[string]time.Time{"macmini": time.Now().Add(-8 * time.Minute)})
	s := newDistServer(t)

	got := fetchWorkers(t, s)
	if got["macmini"].LastSeenAgoS == nil {
		t.Fatal("no last-seen for a box that was polling 8 minutes ago")
	}
	if ago := *got["macmini"].LastSeenAgoS; ago < 470 || ago > 490 {
		t.Errorf("last seen %ds ago, want ~480", ago)
	}
	if got["ubuntu"].LastSeenAgoS != nil {
		t.Errorf("never-seen box reported a last-seen of %d", *got["ubuntu"].LastSeenAgoS)
	}
}

// Temporal DROPS a vanished poller from the listing outright, so once a box has
// been gone a few minutes the listing says nothing about it at all. Without a
// server-side memory of when it was last there, "asleep since 13:04" and "never
// configured correctly" are again the same pill — which is the bug.
func TestLastSeenSurvivesThePollerVanishingFromTheListing(t *testing.T) {
	// First poll: macmini is there.
	fakeTemporal(t, map[string]time.Time{"macmini": time.Now()})
	s := newDistServer(t)
	if st := fetchWorkers(t, s)["macmini"].State; st != WorkerPolling {
		t.Fatalf("setup: state = %q, want %q", st, WorkerPolling)
	}

	// Second poll: gone from the listing entirely, as if it slept.
	fakeTemporal(t, map[string]time.Time{})
	got := fetchWorkers(t, s)["macmini"]
	if got.State != WorkerStale {
		t.Errorf("state = %q, want %q — a box that vanished from the listing is "+
			"reported as never-seen, losing the fact that it WAS here", got.State, WorkerStale)
	}
	if got.LastSeenAgoS == nil {
		t.Error("last-seen was forgotten when the poller left the listing")
	}
}

// A healthy but IDLE worker long-polls with a 60s timeout, so its lastAccessTime
// is routinely up to a minute old. Calling that stale would paint an amber
// warning on every box between chunks — the false positive that makes the whole
// signal ignorable.
func TestAnIdleLongPollingWorkerIsNotStale(t *testing.T) {
	fakeTemporal(t, map[string]time.Time{"macmini": time.Now().Add(-70 * time.Second)})
	s := newDistServer(t)
	if st := fetchWorkers(t, s)["macmini"].State; st != WorkerPolling {
		t.Errorf("state = %q, want %q — a 70s-old lastAccessTime is one normal "+
			"long-poll cycle, not a fault", st, WorkerPolling)
	}
}

// Losing the cluster must not relabel every box as never-seen: the machines are
// still configured, and the ones that were here were here.
func TestTemporalUnreachableDoesNotEraseHistory(t *testing.T) {
	fakeTemporal(t, map[string]time.Time{"macmini": time.Now()})
	s := newDistServer(t)
	fetchWorkers(t, s)

	// Point at a closed port.
	t.Setenv("TEMPORAL_UI_ADDR", "http://127.0.0.1:1")
	got := fetchWorkers(t, s)
	if got["macmini"].State != WorkerStale {
		t.Errorf("macmini: state = %q, want %q", got["macmini"].State, WorkerStale)
	}
	if got["ubuntu"].State != WorkerNeverSeen {
		t.Errorf("ubuntu: state = %q, want %q", got["ubuntu"].State, WorkerNeverSeen)
	}
	if len(got) != 3 {
		t.Errorf("%d machines listed with the cluster down, want 3 — configured "+
			"machines must not disappear from the panel", len(got))
	}
}

// A poller nobody configured still shows up (it is contributing chunks), and it
// gets the same state treatment rather than a hardcoded "on".
func TestUnexpectedPollerIsListedWithAState(t *testing.T) {
	fakeTemporal(t, map[string]time.Time{"stranger": time.Now()})
	s := newDistServer(t)
	got := fetchWorkers(t, s)["stranger"]
	if got.State != WorkerPolling || !got.On {
		t.Errorf("stranger: state=%q on=%v, want polling/true", got.State, got.On)
	}
	if got.LastSeenAgoS == nil {
		t.Error("no last-seen for an unexpected poller")
	}
}

// A clock skew between this box and the Temporal server must not produce a
// negative age that formats as nonsense in the tooltip.
func TestFutureLastAccessTimeClampsToZero(t *testing.T) {
	fakeTemporal(t, map[string]time.Time{"macmini": time.Now().Add(30 * time.Second)})
	s := newDistServer(t)
	got := fetchWorkers(t, s)["macmini"]
	if got.LastSeenAgoS == nil || *got.LastSeenAgoS != 0 {
		t.Errorf("last seen = %v, want 0 for a future stamp", got.LastSeenAgoS)
	}
	if got.State != WorkerPolling {
		t.Errorf("state = %q, want %q", got.State, WorkerPolling)
	}
}

// count is what the UI prints as the fleet size, and it must count contributors
// — not configured boxes, and not sleeping ones.
func TestCountIsPollingMachinesOnly(t *testing.T) {
	now := time.Now()
	fakeTemporal(t, map[string]time.Time{
		"mac":     now,
		"macmini": now.Add(-10 * time.Minute),
	})
	s := newDistServer(t)

	rec := httptest.NewRecorder()
	s.distWorkers(rec, httptest.NewRequest("GET", "/api/dist/workers", nil))
	var body struct {
		Count int `json:"count"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Count != 1 {
		t.Errorf("count = %d, want 1 (mac polling; macmini asleep, ubuntu never seen)", body.Count)
	}
}
