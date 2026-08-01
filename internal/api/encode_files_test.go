package api

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// A rejected filename must be rejected at the HTTP boundary — before Submit, so
// no job row, no worker container, no tmp dir. The accept path isn't covered
// here: Submit spawns a real encode.
func TestStartEncodeRejectsUnknownFile(t *testing.T) {
	src := t.TempDir()
	if err := os.WriteFile(filepath.Join(src, "clip.mp4"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	s := NewServer(encode.NewManager(encode.ManagerConfig{
		SourceDir: src,
		OutputDir: t.TempDir(),
		TmpDir:    t.TempDir(),
	}))

	for _, name := range []string{"../../etc/passwd", "-rf", "nope.mp4", "subdir/clip.mp4"} {
		body := `{"files":["` + name + `"],"target":"local","codec":"h264","max_res":"720p"}`
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/api/encode", strings.NewReader(body))
		s.Mux.ServeHTTP(rec, req)

		if rec.Code != http.StatusBadRequest {
			t.Errorf("POST files=[%q]: got %d, want 400 (body: %s)", name, rec.Code, rec.Body.String())
		}
		if n := len(s.Manager.Jobs()); n != 0 {
			t.Fatalf("POST files=[%q] created %d job(s); nothing should have been submitted", name, n)
		}
	}
}
