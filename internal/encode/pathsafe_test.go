package encode

import (
	"path/filepath"
	"strings"
	"testing"
)

// ValidPathSegment is the one rule for "this string will be joined to a
// directory and must not escape it". It replaced two subtly different copies
// (Promote's and outputDirFor's) and a third site that had no check at all.
func TestValidPathSegmentRejectsEscapes(t *testing.T) {
	bad := []struct{ name, why string }{
		{"..", "the classic parent reference"},
		{"../../../../tmp/pwned", "the actual OutputTag traversal payload"},
		{"a/b", "a separator anywhere splits the segment"},
		{`a\b`, "backslash too — this value also reaches Python and S3 keys"},
		{".", "current directory"},
		{".hidden", "a leading dot is refused outright, which subsumes .."},
		{"", "empty would collapse the name it is appended to"},
		{"ok\x00then", "a NUL truncates the name for every C-level consumer"},
	}
	for _, c := range bad {
		if err := ValidPathSegment("output_tag", c.name); err == nil {
			t.Errorf("accepted %q — %s", c.name, c.why)
		}
	}

	good := []string{"6s", "xs", "go-live", "v2_final", "tag.with.dots", "UPPER"}
	for _, n := range good {
		if err := ValidPathSegment("output_tag", n); err != nil {
			t.Errorf("rejected a legitimate tag %q: %v", n, err)
		}
	}
}

// The error names the field, because these surface to a user filling in a form
// and "invalid output name" for a bad output_tag sends them to the wrong box.
func TestValidPathSegmentErrorNamesTheField(t *testing.T) {
	err := ValidPathSegment("output_tag", "../x")
	if err == nil || !strings.Contains(err.Error(), "output_tag") {
		t.Errorf("error does not name the field: %v", err)
	}
}

// The vulnerability this closes, stated as the arithmetic rather than as a
// claim: an unvalidated tag reaches filepath.Join through outputCodecDir, and
// Join CLEANS the result — so "../../.." does not stay inside OutputDir, it
// resolves out of it. (The Python side is worse: pathlib does not clean at all,
// so the OS resolves it at mkdir.)
func TestOutputTagTraversalWouldEscapeOutputDir(t *testing.T) {
	const outputDir = "/media/dynamic_content"
	cfg := JobConfig{OutputTag: "../../../../tmp/pwned"}

	escaped := filepath.Join(outputDir, cfg.outputCodecDir("clip.mp4", "h264"))
	if strings.HasPrefix(escaped, outputDir+"/") {
		t.Fatalf("expected this fixture to escape OutputDir, got %q — "+
			"if outputCodecDir changed shape, this test is no longer proving anything",
			escaped)
	}

	// ...and the guard is what stops it reaching that point.
	if err := ValidPathSegment("output_tag", cfg.OutputTag); err == nil {
		t.Error("ValidPathSegment accepted the payload that escapes")
	}
}

// A ladder's tag is PERSISTED and copied onto every job that selects the
// profile, so a traversal stored there outlives the request that planted it.
func TestLadderPutRejectsATraversalTag(t *testing.T) {
	s := &LadderStore{ladders: map[string]LadderDef{}}
	def := LadderDef{
		Codecs:    map[string][][]int{"h264": {{1280, 720, 3000}}},
		OutputTag: "../../../../tmp/pwned",
	}
	if err := s.Put("evil", def); err == nil {
		t.Error("stored a ladder whose output_tag escapes OUTPUT_DIR")
	}

	def.OutputTag = "6s"
	if err := s.Put("fine", def); err != nil {
		t.Errorf("rejected a legitimate ladder: %v", err)
	}
}
