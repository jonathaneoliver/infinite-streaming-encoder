package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// /api/outputs/{name}/files is what makes `encoder_cli --download` possible: a
// client needs the whole tree, and the alternative was reconstructing it by
// parsing playlists and then guessing at the files no playlist mentions
// (.byteranges sidecars, encode.json, the master manifest). Each of those
// omissions would be silent — the download would "succeed" and the output
// would not play.

func serveFiles(t *testing.T, outDir, name string) (*httptest.ResponseRecorder, []map[string]any) {
	t.Helper()
	s := &Server{Manager: &encode.Manager{OutputDir: outDir}}
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/outputs/"+name+"/files", nil)
	req.SetPathValue("name", name)
	s.listOutputFiles(rec, req)
	if rec.Code != http.StatusOK {
		return rec, nil
	}
	var got struct {
		Files []map[string]any `json:"files"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal: %v (%s)", err, rec.Body.String())
	}
	return rec, got.Files
}

func TestListOutputFilesWalksTheWholeTree(t *testing.T) {
	out := t.TempDir()
	dir := filepath.Join(out, "clip_p200_h264")
	for path, body := range map[string]string{
		"master.m3u8":                "#EXTM3U",
		"manifest.mpd":               "<MPD/>",
		"encode.json":                "{}",
		"1080p/playlist.m3u8":        "#EXTM3U",
		"1080p/init.mp4":             "init",
		"1080p/seg_00000.m4s":        "aaaaa",
		"1080p/seg_00000.byteranges": "0-1",
		"720p/seg_00000.m4s":         "bb",
	} {
		full := filepath.Join(dir, path)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	_, files := serveFiles(t, out, "clip_p200_h264")
	byPath := map[string]float64{}
	for _, f := range files {
		byPath[f["path"].(string)] = f["size"].(float64)
	}
	if len(byPath) != 8 {
		t.Fatalf("listed %d files, want 8: %v", len(byPath), byPath)
	}
	// Nested paths, relative to the output dir, forward-slashed — they are
	// pasted into /content/ URLs and joined into a local tree on the client.
	for _, want := range []string{"master.m3u8", "1080p/seg_00000.m4s",
		"1080p/seg_00000.byteranges", "720p/seg_00000.m4s"} {
		if _, ok := byPath[want]; !ok {
			t.Errorf("%s missing — a download would silently omit it", want)
		}
	}
	// Sizes are what let a download skip what it already has WITHOUT a request
	// per file. A zero here would make every skip check fail and re-fetch.
	if byPath["1080p/seg_00000.m4s"] != 5 || byPath["720p/seg_00000.m4s"] != 2 {
		t.Fatalf("sizes wrong: %v", byPath)
	}
	// Directories themselves are not entries: the client makes parents from the
	// paths, and a dir with a size would be double-counted in the total.
	for p := range byPath {
		if p == "1080p" || p == "720p" {
			t.Fatalf("directory %q listed as a file", p)
		}
	}
}

func TestListOutputFilesRejectsTraversalAndMissingDirs(t *testing.T) {
	out := t.TempDir()
	// The name arrives from a request path and is joined onto OutputDir.
	for _, bad := range []string{"..", ".", "", "../etc", "a/b"} {
		rec, _ := serveFiles(t, out, bad)
		if rec.Code == http.StatusOK {
			t.Errorf("name %q was accepted", bad)
		}
	}
	if rec, _ := serveFiles(t, out, "no-such-output"); rec.Code != http.StatusNotFound {
		t.Errorf("missing output returned %d, want 404", rec.Code)
	}
	// A FILE where a directory is expected is not an output dir either.
	if err := os.WriteFile(filepath.Join(out, "afile"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if rec, _ := serveFiles(t, out, "afile"); rec.Code != http.StatusNotFound {
		t.Errorf("a plain file returned %d, want 404", rec.Code)
	}
}

func TestListOutputFilesReturnsAnEmptyListNotNull(t *testing.T) {
	// `null` would make a client's `for f in files` throw rather than no-op.
	out := t.TempDir()
	if err := os.MkdirAll(filepath.Join(out, "empty_h264"), 0o755); err != nil {
		t.Fatal(err)
	}
	rec, files := serveFiles(t, out, "empty_h264")
	if rec.Code != http.StatusOK {
		t.Fatalf("code %d", rec.Code)
	}
	if files == nil {
		t.Fatal("files was null; want []")
	}
	if len(files) != 0 {
		t.Fatalf("want no files, got %v", files)
	}
}
