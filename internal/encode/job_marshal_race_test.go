package encode

import (
	"encoding/json"
	"sync"
	"testing"
)

// TestJobMarshalIsRaceFree exercises the exact pairing the SSE stream creates:
// one goroutine marshalling a *Job while another mutates its Stages.
//
// The SSE handler sends *Job POINTERS to subscribers and marshals them at send
// time, so json.Marshal walks j.Stages concurrently with upsertStage. The
// dangerous case is not a stale field — upsertStage APPENDS a StageProgress for
// a key it has not seen, and an append can reallocate the backing array while
// the marshaller is walking it.
//
// Run with -race. Without Job.MarshalJSON taking j.mu this reports a data race
// on j.Stages; with it, it is clean. That is the whole point of the test: it
// fails for the reason the fix exists, not merely because the code changed.
func TestJobMarshalIsRaceFree(t *testing.T) {
	j := &Job{ID: "race-test", Status: StatusRunning}

	var wg sync.WaitGroup
	stop := make(chan struct{})

	// Writer: grow Stages the way a run does — new keys appear as chunks are
	// announced, which is when the grid is filling in.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			key := "encode:h264:1080p:chunk" + string(rune('a'+i%26))
			j.upsertStage(key, key, "running", float64(i%100))
		}
	}()

	// Reader: what the SSE handler does on every frame.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			if _, err := json.Marshal(j); err != nil {
				t.Errorf("marshal failed: %v", err)
				return
			}
		}
	}()

	// Enough iterations to hit an append that reallocates, which is the case
	// a single pass would usually miss.
	for i := 0; i < 2000; i++ {
		if _, err := json.Marshal(j); err != nil {
			t.Fatalf("marshal failed: %v", err)
		}
	}
	close(stop)
	wg.Wait()

	// Sanity: the job really did accumulate stages, so the test was not racing
	// an empty slice and proving nothing.
	if len(j.Stages) == 0 {
		t.Fatal("no stages were added — the test did not exercise append")
	}
}
