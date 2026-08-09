package api

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

func newPathSafeServer(t *testing.T) *Server {
	t.Helper()
	return &Server{Manager: encode.NewManager(encode.ManagerConfig{
		TmpDir:    t.TempDir(),
		SourceDir: t.TempDir(),
		OutputDir: t.TempDir(),
	})}
}

// output_tag reaches the output directory NAME, and from there filepath.Join on
// the Go side and pathlib on the Python side — where "..' is not normalised at
// all. It must be rejected at the boundary, with a 400 rather than a 500: this
// is a bad request, not a server fault.
func TestStartEncodeRejectsATraversalOutputTag(t *testing.T) {
	s := newPathSafeServer(t)
	body := `{"files":["clip.mp4"],"output_tag":"../../../../tmp/pwned"}`
	w := httptest.NewRecorder()
	s.startEncode(w, httptest.NewRequest("POST", "/api/encode", strings.NewReader(body)))

	if w.Code != 400 {
		t.Fatalf("status = %d, want 400 — a traversal tag was not rejected", w.Code)
	}
	if !strings.Contains(w.Body.String(), "output_tag") {
		t.Errorf("the error does not name the offending field: %q", w.Body.String())
	}
}

// The estimate endpoint takes the SAME JobConfig as the real submit and used to
// skip the one step that makes filenames trustworthy, handing them straight to
// filepath.Join(SourceDir, f) and ffprobe. Its contract is a 200 with ok:false
// for anything unusable, so that is what a rejected name must produce — the
// form should quietly show nothing, not surface an error.
func TestEstimateRejectsATraversalFilename(t *testing.T) {
	s := newPathSafeServer(t)
	body := `{"files":["../../../../etc/passwd"],"target":"cloud"}`
	w := httptest.NewRecorder()
	s.estimateEncode(w, httptest.NewRequest("POST", "/api/encode/estimate", strings.NewReader(body)))

	got := w.Body.String()
	if strings.Contains(got, `"ok":true`) {
		t.Fatalf("a traversal filename was probed: %q", got)
	}
	if !strings.Contains(got, `"ok":false`) {
		t.Errorf("expected the ok:false shape the form relies on, got %q", got)
	}
}

// A legitimate tag must still work, or the fix has broken the feature it was
// protecting. Submission fails here for want of a real source file, which is
// the point: it must get PAST the tag check to fail on the file.
func TestStartEncodeAcceptsALegitimateOutputTag(t *testing.T) {
	s := newPathSafeServer(t)
	body := `{"files":["clip.mp4"],"output_tag":"6s"}`
	w := httptest.NewRecorder()
	s.startEncode(w, httptest.NewRequest("POST", "/api/encode", strings.NewReader(body)))

	if strings.Contains(w.Body.String(), "output_tag") {
		t.Errorf("a valid tag %q was rejected: %q", "6s", w.Body.String())
	}
}
