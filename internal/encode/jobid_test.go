package encode

import (
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
)

// Job IDs must be unique, and must stay all-digits.
//
// They were `time.Now().UnixMilli()` with no uniqueness check. That held while
// submissions were spaced out and broke the day #324 made the form fan out over
// ladders x codecs: three codecs means three POSTs in a tight loop, the server
// answers two inside one millisecond, and two jobs get the same ID.
//
// The ID keys $TMP_DIR/<id>/, the worker container name, the restart state file
// and the MinIO staging prefix. A duplicate makes two jobs share all four, which
// presents as a move failure ("lstat .../init.mp4: no such file") or a container
// name conflict — neither of which points at the ID.
//
// These exercise nextJobIDLocked rather than Submit. Submit also spawns
// `go m.run(job, 0)`, so a 200-iteration test would start 200 real encodes and
// leave goroutines writing into the temp dir after the test returned — which is
// exactly how the first version of this file failed in CI ("TempDir RemoveAll
// cleanup: directory not empty"). claimJobID below mirrors precisely what Submit
// does under the lock, and nothing else.

var allDigits = regexp.MustCompile(`^[0-9]+$`)

// claimJobID is Submit's ID step, without the encode. Submit holds m.mu across
// exactly this pair, and the append is what makes the ID taken — leave it out
// and every caller races to the same value.
func claimJobID(m *Manager) string {
	m.mu.Lock()
	defer m.mu.Unlock()
	id := m.nextJobIDLocked()
	m.jobs = append(m.jobs, &Job{ID: id})
	return id
}

// The regression, reproduced: many claims with no delay between them.
func TestJobIDsAreUniqueUnderRapidFire(t *testing.T) {
	m := &Manager{}
	const n = 200
	seen := map[string]int{}
	for i := 0; i < n; i++ {
		seen[claimJobID(m)]++
	}
	if len(seen) != n {
		for id, c := range seen {
			if c > 1 {
				t.Errorf("id %s used by %d jobs", id, c)
			}
		}
		t.Fatalf("%d claims produced %d distinct ids", n, len(seen))
	}
}

// Two HTTP handlers can run at once, so uniqueness has to come from inside the
// lock rather than from being fast. Run with -race.
func TestJobIDsAreUniqueUnderConcurrency(t *testing.T) {
	m := &Manager{}
	const n = 100
	ids := make([]string, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			ids[i] = claimJobID(m)
		}(i)
	}
	wg.Wait()
	seen := map[string]bool{}
	for _, id := range ids {
		if seen[id] {
			t.Errorf("duplicate id %s across concurrent claims", id)
		}
		seen[id] = true
	}
}

// The shape is a contract with internal/tmpstage, which tells a reclaimable job
// directory from the caches and learned state sharing $TMP_DIR by matching
// ^[0-9]+$. Give IDs a suffix and the sweeper stops seeing them — a silent leak.
func TestJobIDsStayAllDigits(t *testing.T) {
	m := &Manager{}
	for i := 0; i < 50; i++ {
		id := claimJobID(m)
		if !allDigits.MatchString(id) {
			t.Fatalf("id %q is not all-digits — internal/tmpstage will not sweep it", id)
		}
		if _, err := strconv.ParseInt(id, 10, 64); err != nil {
			t.Fatalf("id %q does not parse as an integer: %v", id, err)
		}
	}
}

// Later jobs still sort after earlier ones. jobPriorityBase bands the cloud
// SchedulingPriority by job age, so a non-monotonic ID would reorder the queue
// rather than just look odd.
func TestJobIDsAreMonotonic(t *testing.T) {
	m := &Manager{}
	var prev int64
	for i := 0; i < 100; i++ {
		v, err := strconv.ParseInt(claimJobID(m), 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		if v <= prev {
			t.Fatalf("id %d did not advance past %d", v, prev)
		}
		prev = v
	}
}

// A reconciled job holds its ID in m.jobs after a restart. A new claim must not
// reuse it — that $TMP_DIR/<id>/ still exists with another job's chunks in it,
// and the clock going backwards is exactly when this would bite.
func TestJobIDDoesNotReuseAReconciledID(t *testing.T) {
	m := &Manager{}
	base := claimJobID(m)
	n, err := strconv.ParseInt(base, 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	// Occupy the next few milliseconds, as reconciled jobs would.
	taken := map[string]bool{base: true}
	for i := int64(1); i <= 5; i++ {
		id := strconv.FormatInt(n+i, 10)
		m.jobs = append(m.jobs, &Job{ID: id})
		taken[id] = true
	}
	if got := claimJobID(m); taken[got] {
		t.Fatalf("new job reused occupied id %s", got)
	}
}

// Submit must actually USE the unique path. The tests above exercise
// nextJobIDLocked directly, so without this one the wiring could be reverted and
// every one of them would still pass.
func TestSubmitUsesTheUniqueIDPath(t *testing.T) {
	b, err := os.ReadFile("job.go")
	if err != nil {
		t.Skip(err)
	}
	src := string(b)
	i := strings.Index(src, "func (m *Manager) Submit(")
	if i < 0 {
		t.Fatal("Submit not found")
	}
	body := src[i : i+1200]
	if !strings.Contains(body, "m.nextJobIDLocked()") {
		t.Error("Submit no longer takes its ID from nextJobIDLocked — duplicates are back")
	}
	if strings.Contains(body, `ID:        fmt.Sprintf("%d", time.Now().UnixMilli())`) {
		t.Error("Submit is minting the ID from the clock again, unchecked")
	}
}
