package api

import (
	"encoding/json"
	"testing"
	"time"
)

// A full inventory, trimmed to the fields the staging cache reads plus a couple
// it must not disturb.
const fullInv = `{
  "fetched_at": "2026-08-07T10:00:00Z",
  "instances": [{"id": "i-1", "age_seconds": 12.5}],
  "s3_prefixes": [{"prefix": "s3://b/jobs/x/", "object_count": 7673, "size_bytes": 2600000000, "kind": "jobs"}],
  "summary": {"running_instances": 1, "total_s3_bytes": 2600000000, "estimated_hourly_usd": 0.4321}
}`

// What --no-s3-prefixes produces: the same document with the walk skipped.
const noWalkInv = `{
  "fetched_at": "2026-08-07T10:05:00Z",
  "instances": [{"id": "i-1", "age_seconds": 312.5}],
  "s3_prefixes": null,
  "summary": {"running_instances": 1, "total_s3_bytes": null, "estimated_hourly_usd": 0.4321}
}`

func TestStoreAndReuseS3Prefixes(t *testing.T) {
	s := &Server{}
	if _, _, _, ok := s.cachedS3Prefixes(); ok {
		t.Fatal("empty cache reported a hit; the first inventory must walk")
	}
	s.storeS3Prefixes([]byte(fullInv), s.s3Generation())
	prefixes, sizeBytes, at, ok := s.cachedS3Prefixes()
	if !ok {
		t.Fatal("stored enumeration did not come back")
	}
	if sizeBytes != 2600000000 {
		t.Errorf("total_s3_bytes = %d, want 2600000000", sizeBytes)
	}
	if time.Since(at) > time.Minute {
		t.Errorf("cache timestamp %s is not from this test", at)
	}
	if len(prefixes) == 0 {
		t.Error("prefixes came back empty")
	}
}

// The skipped-walk shape says "not measured". Caching it would turn one cheap
// call into ten minutes of a UI claiming the bucket is empty.
func TestNullPrefixesNotCached(t *testing.T) {
	s := &Server{}
	s.storeS3Prefixes([]byte(noWalkInv), s.s3Generation())
	if _, _, _, ok := s.cachedS3Prefixes(); ok {
		t.Error("a null s3_prefixes was cached as if it were a measurement")
	}
}

func TestStaleCacheMisses(t *testing.T) {
	s := &Server{}
	s.storeS3Prefixes([]byte(fullInv), s.s3Generation())
	s.s3Mu.Lock()
	s.s3At = time.Now().Add(-s3PrefixTTL - time.Second)
	s.s3Mu.Unlock()
	if _, _, _, ok := s.cachedS3Prefixes(); ok {
		t.Error("cache older than the TTL was served")
	}
}

func TestInvalidateDropsCacheAndInflightWalk(t *testing.T) {
	s := &Server{}
	s.storeS3Prefixes([]byte(fullInv), s.s3Generation())

	// A walk that started before the delete...
	gen := s.s3Generation()
	s.invalidateS3Prefixes()
	if _, _, _, ok := s.cachedS3Prefixes(); ok {
		t.Fatal("invalidate left the cache populated")
	}
	// ...must not land its pre-delete result afterwards.
	s.storeS3Prefixes([]byte(fullInv), gen)
	if _, _, _, ok := s.cachedS3Prefixes(); ok {
		t.Error("an in-flight walk repopulated the cache after invalidation")
	}
}

func TestMergeS3PrefixesRestoresShape(t *testing.T) {
	s := &Server{}
	s.storeS3Prefixes([]byte(fullInv), s.s3Generation())
	prefixes, sizeBytes, at, ok := s.cachedS3Prefixes()
	if !ok {
		t.Fatal("cache miss right after store")
	}

	merged := mergeS3Prefixes([]byte(noWalkInv), prefixes, sizeBytes, at)
	var doc struct {
		FetchedAt  string `json:"fetched_at"`
		PrefixesAt string `json:"s3_prefixes_at"`
		S3Prefixes []struct {
			Prefix      string `json:"prefix"`
			ObjectCount int    `json:"object_count"`
		} `json:"s3_prefixes"`
		Instances []struct {
			AgeSeconds float64 `json:"age_seconds"`
		} `json:"instances"`
		Summary struct {
			TotalS3Bytes     int64   `json:"total_s3_bytes"`
			RunningInstances int     `json:"running_instances"`
			HourlyUSD        float64 `json:"estimated_hourly_usd"`
		} `json:"summary"`
	}
	if err := json.Unmarshal(merged, &doc); err != nil {
		t.Fatalf("merged payload does not parse: %v", err)
	}
	if len(doc.S3Prefixes) != 1 || doc.S3Prefixes[0].ObjectCount != 7673 {
		t.Errorf("prefixes not spliced back: %+v", doc.S3Prefixes)
	}
	if doc.Summary.TotalS3Bytes != 2600000000 {
		t.Errorf("total_s3_bytes = %d, want the cached 2600000000", doc.Summary.TotalS3Bytes)
	}
	if doc.PrefixesAt == "" {
		t.Error("no s3_prefixes_at stamp — the tab cannot say how old the sizes are")
	}
	// Everything else comes from THIS call, not from the cache.
	if doc.FetchedAt != "2026-08-07T10:05:00Z" {
		t.Errorf("fetched_at = %q, want the live call's", doc.FetchedAt)
	}
	if doc.Summary.RunningInstances != 1 || doc.Summary.HourlyUSD != 0.4321 {
		t.Errorf("live summary fields disturbed: %+v", doc.Summary)
	}
	if len(doc.Instances) != 1 || doc.Instances[0].AgeSeconds != 312.5 {
		t.Errorf("live instances disturbed: %+v", doc.Instances)
	}
}

// A payload that is not an inventory at all must pass through rather than
// blanking the panel.
func TestMergeS3PrefixesPassesThroughGarbage(t *testing.T) {
	junk := []byte(`not json`)
	if got := mergeS3Prefixes(junk, json.RawMessage(`[]`), 0, time.Now()); string(got) != string(junk) {
		t.Errorf("garbage was rewritten to %q", got)
	}
}
