package api

import (
	"os"
	"path/filepath"
	"testing"
)

// A metadata-only output (#214) has no .m4s or .ts on disk, so the file-extension
// scan finds nothing and the format reads as unknown — for an output whose format
// is perfectly knowable from the playlists, which stay local. This pins the
// fallback, because the symptom is a missing badge: easy to miss, easy to
// "simplify" away later.

func writePlaylist(t *testing.T, dir, sub, name, body string) {
	t.Helper()
	d := filepath.Join(dir, sub)
	if err := os.MkdirAll(d, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(d, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestHlsFormatFromPlaylistsDetectsFmp4WithNoSegments(t *testing.T) {
	dir := t.TempDir()
	// Exactly what a real metadata-only output contains.
	writePlaylist(t, dir, "1080p", "playlist.m3u8",
		"#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:6.0,\nsegment_00000.m4s\n")
	m4s, ts := hlsFormatFromPlaylists(dir)
	if !m4s || ts {
		t.Fatalf("want fmp4, got m4s=%v ts=%v", m4s, ts)
	}
	if got := parseOutputMeta("clip_p200_h264", dir).hlsFormat; got != "fmp4" {
		t.Fatalf("parseOutputMeta hlsFormat = %q, want fmp4", got)
	}
}

func TestHlsFormatFromPlaylistsDetectsTs(t *testing.T) {
	dir := t.TempDir()
	writePlaylist(t, dir, "1080p", "playlist.m3u8",
		"#EXTM3U\n#EXTINF:6.0,\nsegment_00000.ts\n")
	m4s, ts := hlsFormatFromPlaylists(dir)
	if m4s || !ts {
		t.Fatalf("want ts, got m4s=%v ts=%v", m4s, ts)
	}
}

func TestHlsFormatFromPlaylistsDetectsBoth(t *testing.T) {
	dir := t.TempDir()
	writePlaylist(t, dir, "1080p", "playlist.m3u8",
		"#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\nsegment_00000.m4s\n")
	writePlaylist(t, dir, "1080p_ts", "playlist.m3u8",
		"#EXTM3U\n#EXTINF:6.0,\nsegment_00000.ts\n")
	if got := parseOutputMeta("clip_p200_h264", dir).hlsFormat; got != "both" {
		t.Fatalf("hlsFormat = %q, want both", got)
	}
}

func TestRealSegmentsStillWinOverPlaylists(t *testing.T) {
	// The fallback must only run when the scan found nothing. A normal output
	// keeps its existing behaviour — this path is additive, not a replacement.
	dir := t.TempDir()
	writePlaylist(t, dir, "1080p", "playlist.m3u8", "#EXTM3U\nsegment_00000.ts\n")
	if err := os.WriteFile(filepath.Join(dir, "1080p", "segment_00000.ts"),
		[]byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := parseOutputMeta("clip_p200_h264", dir).hlsFormat; got != "ts" {
		t.Fatalf("hlsFormat = %q, want ts", got)
	}
}

func TestHlsFormatUnknownWhenThereIsNothingToGoOn(t *testing.T) {
	// No segments AND no playlists: stay empty rather than guessing a format.
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "1080p"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := parseOutputMeta("clip_p200_h264", dir).hlsFormat; got != "" {
		t.Fatalf("hlsFormat = %q, want empty", got)
	}
}
