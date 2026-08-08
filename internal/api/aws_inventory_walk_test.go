package api

import (
	"encoding/json"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// The S3 staging walk is the most expensive read on the AWS tab and the one
// with nothing live to say. These pin the rule that keeps it off the 10s poll:
// it happens only when a request opts in with ?s3=1.

// awsInventory reaches into the Manager for fleet CPU on its way out, so these
// need a real one — scratch dirs only, nothing is launched.
func newInventoryTestServer(t *testing.T) *Server {
	t.Helper()
	return &Server{Manager: encode.NewManager(encode.ManagerConfig{TmpDir: t.TempDir()})}
}

// stubPythonCloud swaps the Python shim for one that records the args it was
// called with and returns `body`.
func stubPythonCloud(t *testing.T, body string) *[][]string {
	t.Helper()
	var calls [][]string
	prev := runPythonCloud
	runPythonCloud = func(module string, args ...any) ([]byte, error) {
		got := []string{module}
		for _, a := range args {
			got = append(got, a.(string))
		}
		calls = append(calls, got)
		return []byte(body), nil
	}
	t.Cleanup(func() { runPythonCloud = prev })
	return &calls
}

func walked(calls [][]string) bool {
	for _, c := range calls {
		if len(c) > 0 && c[0] == "inventory" {
			// The walk is the ABSENCE of --no-s3-prefixes.
			for _, a := range c[1:] {
				if a == "--no-s3-prefixes" {
					return false
				}
			}
			return true
		}
	}
	return false
}

func TestPollDoesNotWalkS3(t *testing.T) {
	s := newInventoryTestServer(t)
	calls := stubPythonCloud(t, noWalkInv)

	w := httptest.NewRecorder()
	s.awsInventory(w, httptest.NewRequest("GET", "/api/aws/inventory", nil))

	if walked(*calls) {
		t.Fatalf("a plain poll enumerated the bucket: %v", *calls)
	}
	// And it must report "not measured", never an empty bucket.
	var doc struct {
		S3Prefixes json.RawMessage `json:"s3_prefixes"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &doc); err != nil {
		t.Fatalf("payload does not parse: %v", err)
	}
	if string(doc.S3Prefixes) != "null" {
		t.Errorf("s3_prefixes = %s, want null (not measured)", doc.S3Prefixes)
	}
}

func TestExplicitRequestWalksS3(t *testing.T) {
	s := newInventoryTestServer(t)
	calls := stubPythonCloud(t, fullInv)

	w := httptest.NewRecorder()
	s.awsInventory(w, httptest.NewRequest("GET", "/api/aws/inventory?s3=1", nil))

	if !walked(*calls) {
		t.Fatalf("?s3=1 did not enumerate the bucket: %v", *calls)
	}
	if _, _, _, ok := s.cachedS3Prefixes(); !ok {
		t.Error("the measurement was not cached, so the next poll has nothing to splice")
	}
}

// A poll after a measurement serves the cached sizes with their timestamp —
// the number stays on screen, it just stops being re-bought every 10 seconds.
func TestPollSplicesLastMeasurement(t *testing.T) {
	s := newInventoryTestServer(t)
	s.storeS3Prefixes([]byte(fullInv), s.s3Generation())
	calls := stubPythonCloud(t, noWalkInv)

	w := httptest.NewRecorder()
	s.awsInventory(w, httptest.NewRequest("GET", "/api/aws/inventory", nil))

	if walked(*calls) {
		t.Fatalf("poll walked despite a cached measurement: %v", *calls)
	}
	var doc struct {
		PrefixesAt string `json:"s3_prefixes_at"`
		Summary    struct {
			TotalS3Bytes int64 `json:"total_s3_bytes"`
		} `json:"summary"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &doc); err != nil {
		t.Fatalf("payload does not parse: %v", err)
	}
	if doc.Summary.TotalS3Bytes != 2600000000 {
		t.Errorf("total_s3_bytes = %d, want the cached 2600000000", doc.Summary.TotalS3Bytes)
	}
	if doc.PrefixesAt == "" {
		t.Error("no s3_prefixes_at stamp — the tab cannot say how old the sizes are")
	}
}

// Holding down Refresh must not re-enumerate the bucket on every press.
func TestRepeatedAsksAreFloored(t *testing.T) {
	s := newInventoryTestServer(t)
	s.storeS3Prefixes([]byte(fullInv), s.s3Generation())
	calls := stubPythonCloud(t, noWalkInv)

	w := httptest.NewRecorder()
	s.awsInventory(w, httptest.NewRequest("GET", "/api/aws/inventory?s3=1", nil))
	if walked(*calls) {
		t.Fatalf("re-measured within s3PrefixMinInterval: %v", *calls)
	}

	// ...but a measurement older than the floor is re-taken.
	s.s3Mu.Lock()
	s.s3At = time.Now().Add(-s3PrefixMinInterval - time.Second)
	s.s3Mu.Unlock()
	*calls = nil
	s.awsInventory(httptest.NewRecorder(),
		httptest.NewRequest("GET", "/api/aws/inventory?s3=1", nil))
	if !walked(*calls) {
		t.Errorf("a measurement past the floor was not re-taken: %v", *calls)
	}
}

// A delete zeroes the cache, so the refresh that follows re-measures at once
// rather than showing the deleted row until the floor expires.
func TestDeleteForcesImmediateRemeasure(t *testing.T) {
	s := newInventoryTestServer(t)
	s.storeS3Prefixes([]byte(fullInv), s.s3Generation())
	s.invalidateS3Prefixes()
	calls := stubPythonCloud(t, fullInv)

	s.awsInventory(httptest.NewRecorder(),
		httptest.NewRequest("GET", "/api/aws/inventory?s3=1", nil))
	if !walked(*calls) {
		t.Errorf("no re-measure after an invalidate: %v", *calls)
	}
}
