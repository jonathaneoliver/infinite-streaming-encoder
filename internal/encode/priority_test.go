package encode

import (
	"encoding/json"
	"testing"
)

// Audio shares the FanOut Parallel with the variants map, so packaging cannot
// start until BOTH finish. At base+55 audio sat below all 336 chunk jobs and was
// placed near the end of the run: on 1785781612611 the last chunk container
// stopped at 18:34:26.969 and audio — an ~11s job that depends on nothing —
// stopped at 18:34:33.224. Six seconds of a 42.9s fan-in gap, every run, spent
// waiting for a job that could have run first.
//
// This is a scheduling property, invisible in any output: the encode is correct
// either way, just slower. So it is pinned rather than left to review.

func sfnDoc(t *testing.T) struct {
	PrioAudio int `json:"prio_audio"`
	PrioMezz  int `json:"prio_mezz"`
	PrioPkg   int `json:"prio_pkg"`
	Variants  []struct {
		Label    string `json:"label"`
		Priority int    `json:"priority"`
	} `json:"variants"`
} {
	t.Helper()
	var doc struct {
		PrioAudio int `json:"prio_audio"`
		PrioMezz  int `json:"prio_mezz"`
		PrioPkg   int `json:"prio_pkg"`
		Variants  []struct {
			Label    string `json:"label"`
			Priority int    `json:"priority"`
		} `json:"variants"`
	}
	in, _, err := buildSFNInput(LoadLadderStore(""), LoadEncodeSpeedStore(""),
		"s3://in/x.mp4", "s3://p", "s3://m", "apple-uniq-live-xs", "h264",
		"", "", false, false, true, false, false, 3840, 30, 334.4, 0,
		"12", "6", "0.2", "1.0", 9000, nil, nil)
	if err != nil {
		t.Fatalf("buildSFNInput: %v", err)
	}
	if err := json.Unmarshal([]byte(in), &doc); err != nil {
		t.Fatalf("unmarshal SFN input: %v", err)
	}
	if len(doc.Variants) < 2 {
		t.Fatalf("expected a full ladder, got %d variants", len(doc.Variants))
	}
	return doc
}

func TestAudioOutranksEveryVariant(t *testing.T) {
	doc := sfnDoc(t)
	for _, v := range doc.Variants {
		if doc.PrioAudio <= v.Priority {
			t.Errorf("audio priority %d does not outrank variant %s (%d) — it will "+
				"queue behind every chunk and gate the fan-in",
				doc.PrioAudio, v.Label, v.Priority)
		}
	}
}

func TestPrioritiesStayInsideTheBatchCeiling(t *testing.T) {
	// The oldest active job's band is 9000, and Batch caps schedulingPriority at
	// 9999. Audio at base+999 sits exactly on that ceiling, so anything added
	// above it would be silently clamped and tie with the variants.
	doc := sfnDoc(t)
	for name, p := range map[string]int{
		"audio": doc.PrioAudio, "mezz": doc.PrioMezz, "pkg": doc.PrioPkg,
	} {
		if p > 9999 || p < 1 {
			t.Errorf("%s priority %d is outside 1..9999", name, p)
		}
	}
	for _, v := range doc.Variants {
		if v.Priority > 9999 || v.Priority < 1 {
			t.Errorf("variant %s priority %d is outside 1..9999", v.Label, v.Priority)
		}
	}
}

func TestVariantPrioritiesRemainDistinct(t *testing.T) {
	// Ranking (rather than clamping a raw score) exists so two heavy variants
	// cannot tie and let Batch start the cheaper one first. Shifting the band
	// down by one to make room for audio must not reintroduce a collision.
	doc := sfnDoc(t)
	seen := map[int]string{}
	for _, v := range doc.Variants {
		if other, dup := seen[v.Priority]; dup {
			t.Errorf("variants %s and %s tie at priority %d", other, v.Label, v.Priority)
		}
		seen[v.Priority] = v.Label
	}
}

func TestPackagingStaysBelowEverything(t *testing.T) {
	// Packaging cannot run until every chunk is done, so ranking it above them
	// would only let it occupy a slot it cannot use.
	doc := sfnDoc(t)
	if doc.PrioPkg >= doc.PrioAudio {
		t.Errorf("pkg %d should rank below audio %d", doc.PrioPkg, doc.PrioAudio)
	}
	for _, v := range doc.Variants {
		if doc.PrioPkg >= v.Priority {
			t.Errorf("pkg %d should rank below variant %s (%d)",
				doc.PrioPkg, v.Label, v.Priority)
		}
	}
}
