package encode

import (
	"regexp"
	"strconv"
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

var allDigits = regexp.MustCompile(`^[0-9]+$`)

// newTestManager gives Submit a scratch TmpDir. Without one it persists each
// job's state file to a RELATIVE ./jobs/<id>.json — i.e. into the package
// source directory. A 200-submission test then leaves 200 files in the repo.
func newTestManager(t *testing.T) *Manager {
	t.Helper()
	return &Manager{TmpDir: t.TempDir()}
}

// The regression, reproduced: many submissions with no delay between them.
func TestSubmitIDsAreUniqueUnderRapidFire(t *testing.T) {
	m := newTestManager(t)
	const n = 200
	seen := map[string]int{}
	for i := 0; i < n; i++ {
		job := m.Submit(JobConfig{Codec: "h264"})
		seen[job.ID]++
	}
	if len(seen) != n {
		dupes := 0
		for id, c := range seen {
			if c > 1 {
				dupes++
				t.Errorf("id %s used by %d jobs", id, c)
			}
		}
		t.Fatalf("%d submissions produced %d distinct ids (%d duplicated)", n, len(seen), dupes)
	}
}

// Concurrent submits: two HTTP handlers can run at once, so uniqueness has to
// come from inside the lock, not from being fast.
func TestSubmitIDsAreUniqueUnderConcurrency(t *testing.T) {
	m := newTestManager(t)
	const n = 100
	ids := make([]string, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			ids[i] = m.Submit(JobConfig{Codec: "hevc"}).ID
		}(i)
	}
	wg.Wait()
	seen := map[string]bool{}
	for _, id := range ids {
		if seen[id] {
			t.Errorf("duplicate id %s across concurrent submits", id)
		}
		seen[id] = true
	}
}

// The shape is a contract with internal/tmpstage, which tells a reclaimable job
// directory from the caches and learned state sharing $TMP_DIR by matching
// ^[0-9]+$. Give IDs a suffix and the sweeper stops seeing them — a silent leak.
func TestSubmitIDsStayAllDigits(t *testing.T) {
	m := newTestManager(t)
	for i := 0; i < 50; i++ {
		id := m.Submit(JobConfig{}).ID
		if !allDigits.MatchString(id) {
			t.Fatalf("id %q is not all-digits — internal/tmpstage will not sweep it", id)
		}
		if _, err := strconv.ParseInt(id, 10, 64); err != nil {
			t.Fatalf("id %q does not parse as an integer: %v", id, err)
		}
	}
}

// Later submissions still sort after earlier ones. jobPriorityBase bands the
// cloud SchedulingPriority by job age, and the UI orders by ID, so a
// non-monotonic ID would reorder the queue rather than just look odd.
func TestSubmitIDsAreMonotonic(t *testing.T) {
	m := newTestManager(t)
	var prev int64
	for i := 0; i < 100; i++ {
		v, err := strconv.ParseInt(m.Submit(JobConfig{}).ID, 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		if v <= prev {
			t.Fatalf("id %d did not advance past %d", v, prev)
		}
		prev = v
	}
}

// A reconciled job holds its ID in m.jobs after a restart. A new submission must
// not reuse it — that $TMP_DIR/<id>/ still exists on disk with another job's
// chunks in it, and the clock going backwards is exactly when this would bite.
func TestSubmitDoesNotReuseAReconciledID(t *testing.T) {
	m := newTestManager(t)
	first := m.Submit(JobConfig{}).ID

	// Simulate the clock rewinding: a job already exists at an id the next
	// UnixMilli() could plausibly hand out again.
	n, _ := strconv.ParseInt(first, 10, 64)
	for i := int64(0); i < 5; i++ {
		m.jobs = append(m.jobs, &Job{ID: strconv.FormatInt(n+i, 10)})
	}
	next := m.Submit(JobConfig{}).ID
	for _, j := range m.jobs {
		if j != m.jobs[len(m.jobs)-1] && j.ID == next {
			t.Fatalf("new job reused existing id %s", next)
		}
	}
}
