package encode

import "testing"

// The tag is what keeps one source's several delivery profiles apart on disk.
// Before #286 every PINNED-segment ladder derived the same empty tag, so a 6s,
// a 2s and a 1s ladder all produced <stem>_<codec> and silently overwrote each
// other. That is not a naming preference — it is data loss, and it is why a _6s
// output had to be tagged by hand.
func TestPinnedSegmentLaddersGetDistinctTags(t *testing.T) {
	seen := map[string]string{}
	for _, seg := range []string{"6", "2", "1"} {
		tag := deriveOutputTag("", seg)
		if tag == "" {
			t.Fatalf("segment %qs derived an empty tag — every pinned ladder "+
				"would share one output directory", seg)
		}
		if prev, dup := seen[tag]; dup {
			t.Fatalf("segment %qs and %qs both derived %q", seg, prev, tag)
		}
		seen[tag] = seg
	}
	if seen["6s"] != "6" || seen["2s"] != "2" || seen["1s"] != "1" {
		t.Errorf("tags are not the segment lengths: %v", seen)
	}
}

// The flexible base keeps "xs". go-live keys off it to decide that this output
// is the one it repackages into 1s/2s/6s, so changing it would silently change
// which outputs a downstream consumer treats as re-choppable.
func TestFlexibleBaseStillDerivesXs(t *testing.T) {
	if got := deriveOutputTag("", ""); got != "xs" {
		t.Errorf("flexible base derived %q, want \"xs\"", got)
	}
}

// An explicit tag — from the ladder or the encode form — always wins. That is
// the escape hatch for anything the derivation cannot express.
func TestExplicitTagWins(t *testing.T) {
	for _, seg := range []string{"", "6", "0", "nonsense"} {
		if got := deriveOutputTag("mine", seg); got != "mine" {
			t.Errorf("segment %q overrode an explicit tag: got %q", seg, got)
		}
	}
}

// "6" and "6.0" are the same profile. Two spellings must not become two
// directories, or a re-encode lands beside its predecessor instead of replacing
// it and resolveCodec's skip check misses.
func TestEquivalentSpellingsCollapse(t *testing.T) {
	a, b := deriveOutputTag("", "6"), deriveOutputTag("", "6.0")
	if a != b {
		t.Errorf("%q vs %q — one profile, two output directories", a, b)
	}
	if got := deriveOutputTag("", " 2 "); got != "2s" {
		t.Errorf("whitespace changed the tag: %q", got)
	}
	if got := deriveOutputTag("", "1.5"); got != "1.5s" {
		t.Errorf("fractional segment: got %q, want \"1.5s\"", got)
	}
}

// A segment that is zero, negative or unparseable keeps the OLD empty tag
// rather than inventing one. "0s" is not a segmentation anyone can be served,
// so naming a directory after it would assert something false — and an
// unparseable value must not reach a path at all.
func TestUnusableSegmentYieldsNoTag(t *testing.T) {
	for _, seg := range []string{"0", "0.0", "-6", "abc", "6s", "../x"} {
		if got := deriveOutputTag("", seg); got != "" {
			t.Errorf("segment %q derived tag %q, want empty", seg, got)
		}
	}
}

// Whatever the derivation produces is appended to a directory name, so it must
// survive the same check every other path segment does.
func TestDerivedTagIsPathSafe(t *testing.T) {
	for _, seg := range []string{"6", "2", "1", "1.5", "0.5"} {
		tag := deriveOutputTag("", seg)
		if tag == "" {
			continue
		}
		if err := ValidPathSegment("output tag", tag); err != nil {
			t.Errorf("segment %q derived unsafe tag %q: %v", seg, tag, err)
		}
	}
}
