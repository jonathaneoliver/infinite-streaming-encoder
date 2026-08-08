package encode

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// The host-built mezzanine (#266) is written to the SAME mezz/<key>/ prefix a
// Batch-built one would be, and the run is then treated as a cache hit so
// MezzCheck skips the mezzanine job. Two things have to hold for that to be
// safe, and neither fails loudly if it stops holding.

// If the key were computed differently on the host, every run would miss its own
// cache and rebuild an identical file — silently, and only visible as a
// mezzanine phase that never stops appearing.
func TestHostAndBatchAgreeOnTheMezzKey(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "clip.mp4")
	if err := os.WriteFile(src, []byte("not really a video"), 0644); err != nil {
		t.Fatal(err)
	}

	a, ok := sourceMezzKey(src, 0)
	if !ok {
		t.Fatal("key not derivable for a readable file")
	}
	b, ok := sourceMezzKey(src, 0)
	if !ok || a != b {
		t.Fatalf("key is not stable across calls: %q vs %q", a, b)
	}

	// Same content, different mtime => different key. This is what makes an
	// edited source re-mezzanine rather than serve the old one.
	later := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(src, later, later); err != nil {
		t.Fatal(err)
	}
	c, _ := sourceMezzKey(src, 0)
	if c == a {
		t.Error("mtime change did not move the key — an edited source would reuse a stale mezzanine")
	}
}

// A limited run truncates the mezzanine, so it is NOT a pure function of the
// source any more. The host path writes into the cache exactly like the Batch
// path did, so if the limit fell out of the key a `--time 10` run would poison
// the cache for every later full encode of that file: right name, right
// manifests, silently short video.
func TestTimeLimitKeepsLimitedMezzanineOutOfTheFullCache(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "clip.mp4")
	if err := os.WriteFile(src, []byte("not really a video"), 0644); err != nil {
		t.Fatal(err)
	}

	full, _ := sourceMezzKey(src, 0)
	ten, _ := sourceMezzKey(src, 10)
	thirty, _ := sourceMezzKey(src, 30)

	if full == ten {
		t.Error("a 10s-limited mezzanine would be cached under the full key")
	}
	if ten == thirty {
		t.Error("two different limits share a key")
	}
	// 0 must reproduce the pre-#184 key exactly, or every existing cached
	// mezzanine is orphaned the day this ships.
	if again, _ := sourceMezzKey(src, 0); again != full {
		t.Error("the unlimited key is not stable")
	}
}

// An unreadable source yields no key, and the caller falls back to the job
// prefix rather than writing to mezz/<empty>/ — which would collide across
// every job whose source could not be stat'd.
func TestNoKeyForAnUnreadableSource(t *testing.T) {
	if _, ok := sourceMezzKey(filepath.Join(t.TempDir(), "absent.mp4"), 0); ok {
		t.Error("a missing file produced a cache key")
	}
}
