package encode

import (
	"os"
	"path/filepath"
	"testing"
)

// The point of ResolveSourceFiles is that nothing a client typed reaches
// Config.Files — so the assertions are about what comes BACK, not just about
// which names are refused.
func TestResolveSourceFiles(t *testing.T) {
	dir := t.TempDir()
	for _, n := range []string{"clip.mp4", "with space.mov", "-dashed.mp4"} {
		if err := os.WriteFile(filepath.Join(dir, n), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(dir, "subdir"), 0o755); err != nil {
		t.Fatal(err)
	}
	m := &Manager{SourceDir: dir}

	t.Run("resolves to the listing's own string", func(t *testing.T) {
		got, err := m.ResolveSourceFiles([]string{"clip.mp4", "with space.mov"})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		want := []string{"clip.mp4", "with space.mov"}
		if len(got) != len(want) {
			t.Fatalf("got %v, want %v", got, want)
		}
		for i := range want {
			if got[i] != want[i] {
				t.Errorf("[%d] got %q, want %q", i, got[i], want[i])
			}
		}
	})

	// A name that exists on disk is returned even if it looks hostile: the
	// resolver's job is provenance, not taste. Refusing it here would mean a
	// file the user can see in the UI is one they can't encode.
	t.Run("a real file that starts with a dash still resolves", func(t *testing.T) {
		got, err := m.ResolveSourceFiles([]string{"-dashed.mp4"})
		if err != nil || len(got) != 1 || got[0] != "-dashed.mp4" {
			t.Fatalf("got %v, %v", got, err)
		}
	})

	for _, tc := range []struct {
		name string
		in   string
	}{
		{"traversal", "../../etc/passwd"},
		{"traversal within", "subdir/../clip.mp4"},
		{"absolute", "/etc/passwd"},
		{"directory", "subdir"},
		{"unknown", "nope.mp4"},
		{"flag-shaped and absent", "--sweep-all"},
		{"empty", ""},
	} {
		t.Run("rejects "+tc.name, func(t *testing.T) {
			if got, err := m.ResolveSourceFiles([]string{tc.in}); err == nil {
				t.Fatalf("expected an error for %q, got %v", tc.in, got)
			}
		})
	}

	// One bad name fails the whole request — the API creates no jobs at all,
	// rather than encoding the good half of a selection.
	t.Run("one bad name fails the batch", func(t *testing.T) {
		if _, err := m.ResolveSourceFiles([]string{"clip.mp4", "nope.mp4"}); err == nil {
			t.Fatal("expected an error")
		}
	})
}
