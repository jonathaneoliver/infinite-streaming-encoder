package encode

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// The mezzanine cache key is deliberately ladder-independent, so every ladder of
// one source maps to ONE key — and #286 made "select several ladders, get a job
// each" a single click. Without serialisation two jobs both miss the cache, both
// run ffmpeg on the host, and both upload a source-sized file to the same
// prefix. Nothing is corrupted (the copy is deterministic, PutObject is atomic);
// the cost is one wasted local encode and ~2.3 GB of redundant upload per
// submission, which is the round trip #266 removed.

// The property that matters: two holders of one key never overlap.
func TestMezzLockSerialisesOneKey(t *testing.T) {
	m := &Manager{}
	var inside, maxInside int32
	var wg sync.WaitGroup

	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			unlock := m.lockMezz("same-key")
			defer unlock()
			n := atomic.AddInt32(&inside, 1)
			for {
				old := atomic.LoadInt32(&maxInside)
				if n <= old || atomic.CompareAndSwapInt32(&maxInside, old, n) {
					break
				}
			}
			// Long enough that an unserialised implementation overlaps.
			time.Sleep(2 * time.Millisecond)
			atomic.AddInt32(&inside, -1)
		}()
	}
	wg.Wait()

	if got := atomic.LoadInt32(&maxInside); got != 1 {
		t.Errorf("%d holders of one key at once — the second job would build and "+
			"upload a mezzanine the first is already building", got)
	}
}

// Different sources must NOT wait on each other. Serialising everything would
// turn a correctness fix into a throughput bug, and with MAX_CONCURRENT>1 that
// is the common case: two jobs on two different files.
func TestMezzLockDoesNotSerialiseDifferentKeys(t *testing.T) {
	m := &Manager{}
	const n = 4
	started := make(chan struct{}, n)
	release := make(chan struct{})
	var wg sync.WaitGroup

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			unlock := m.lockMezz(string(rune('a' + i)))
			defer unlock()
			started <- struct{}{}
			<-release
		}(i)
	}

	// All four must get in while none has released. A per-key lock allows it; a
	// global one deadlocks here until the timeout.
	for i := 0; i < n; i++ {
		select {
		case <-started:
		case <-time.After(2 * time.Second):
			close(release)
			wg.Wait()
			t.Fatalf("only %d of %d distinct keys could proceed — the lock is "+
				"global, not per-key", i, n)
		}
	}
	close(release)
	wg.Wait()
}

// The map must not accumulate one entry per source for the life of the server.
func TestMezzLockReleasesItsMapEntry(t *testing.T) {
	m := &Manager{}
	for i := 0; i < 50; i++ {
		unlock := m.lockMezz(string(rune('a' + i%26)))
		unlock()
	}
	m.mezzMu.Lock()
	n := len(m.mezzBuild)
	m.mezzMu.Unlock()
	if n != 0 {
		t.Errorf("%d gate(s) left behind; the map grows without bound", n)
	}
}

// A waiter must keep the gate alive while it is queued, or the holder's release
// deletes the entry out from under it and the next caller gets a DIFFERENT
// mutex — which silently restores the race this exists to prevent.
func TestMezzLockGateSurvivesAHandover(t *testing.T) {
	m := &Manager{}
	first := m.lockMezz("k")

	queued := make(chan func(), 1)
	go func() { queued <- m.lockMezz("k") }()

	// Give the waiter time to register its ref before the holder releases.
	time.Sleep(20 * time.Millisecond)
	m.mezzMu.Lock()
	refs := 0
	if g := m.mezzBuild["k"]; g != nil {
		refs = g.refs
	}
	m.mezzMu.Unlock()
	if refs != 2 {
		t.Errorf("refs = %d during handover, want 2 (holder + waiter)", refs)
	}

	first()
	select {
	case unlock := <-queued:
		unlock()
	case <-time.After(2 * time.Second):
		t.Fatal("the waiter never acquired the gate after the holder released")
	}

	m.mezzMu.Lock()
	left := len(m.mezzBuild)
	m.mezzMu.Unlock()
	if left != 0 {
		t.Errorf("%d gate(s) left after both released", left)
	}
}
