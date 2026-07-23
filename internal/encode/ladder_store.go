package encode

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// LadderStore is the control plane's source of truth for encoding ladders.
// It owns a persisted ladders.json (seeded from the built-ins on first run),
// serves the API, and resolves the concrete rungs the SFN hands to workers —
// so a user-defined ladder flows through to both local and cloud encodes.
//
// The on-disk format is shared with the Python encoder (scripts/encoder/
// ladder.py reads the same file), so a rung is a [width, height, bitrate_kbps]
// triple and burn-in geometry is derived from height on both sides.

// LadderDef is one ladder: per-codec rung lists plus optional VBV shaping.
// JSON-shaped for the store file and the API.
type LadderDef struct {
	Description       string  `json:"description,omitempty"`
	Seed              bool    `json:"seed,omitempty"`
	MaxratePercent    int     `json:"maxrate_percent,omitempty"`
	BufsizeMultiplier float64 `json:"bufsize_multiplier,omitempty"`
	// Output timing that defines the profile as much as its bitrates: HLS segment
	// length, LL-HLS partial length ("0" = off → plain VOD, no parts), and GOP /
	// keyframe interval. Strings so "" means "inherit the global default" while
	// "0" is an explicit value; a per-encode job value still overrides. Live
	// profiles set 6/0.2/1.0; a VOD profile sets 6/0/6.
	SegmentDuration string `json:"segment_duration,omitempty"`
	PartialDuration string `json:"partial_duration,omitempty"`
	GopDuration     string `json:"gop_duration,omitempty"`
	// OutputTag, when set, is appended to the output directory name (e.g. "6s" →
	// "<stem>_6s_<codec>"). It marks the profile in the filename so a downstream
	// consumer (e.g. go-live) can tell a repackage-once profile from the default
	// repackage-into-1s/2s/6s one. Empty = no tag (dir names unchanged).
	OutputTag string `json:"output_tag,omitempty"`
	// Codecs maps a codec ("h264"/"hevc"/"av1") to its rungs, each a
	// [width, height, bitrate_kbps] triple. Preset defaults to "medium".
	Codecs map[string][][]int `json:"codecs"`
}

type ladderFile struct {
	Version int                  `json:"version"`
	Ladders map[string]LadderDef `json:"ladders"`
}

type LadderStore struct {
	mu      sync.RWMutex
	path    string
	ladders map[string]LadderDef
}

// defaultSeedLadders returns the built-in read-only ladders. Mirrors
// scripts/encoder/ladder.py SEED_LADDERS (av1 == hevc). Kept in sync by hand;
// the store persists a copy so both languages read the same file thereafter.
func defaultSeedLadders() map[string]LadderDef {
	legacyHEVC := [][]int{{640, 360, 300}, {960, 540, 1001}, {1280, 720, 1662}, {1920, 1080, 4273}, {2560, 1440, 10547}, {3840, 2160, 16458}}
	appleH264 := [][]int{{416, 234, 145}, {640, 360, 365}, {768, 432, 730}, {768, 432, 1100}, {960, 540, 2000}, {1280, 720, 3000}, {1280, 720, 4500}, {1920, 1080, 6000}, {1920, 1080, 7800}}
	appleHEVC := [][]int{{640, 360, 145}, {768, 432, 300}, {960, 540, 600}, {960, 540, 900}, {960, 540, 1600}, {1280, 720, 2400}, {1280, 720, 3400}, {1920, 1080, 4500}, {1920, 1080, 5800}, {2560, 1440, 8100}, {3840, 2160, 11600}, {3840, 2160, 16800}}
	appleUniqH264 := [][]int{{416, 234, 145}, {640, 360, 365}, {704, 396, 730}, {768, 432, 1100}, {960, 540, 2000}, {1216, 684, 3000}, {1280, 720, 4500}, {1856, 1044, 6000}, {1920, 1080, 7800}}
	appleUniqHEVC := [][]int{{640, 360, 145}, {768, 432, 300}, {832, 468, 600}, {896, 504, 900}, {960, 540, 1600}, {1216, 684, 2400}, {1280, 720, 3400}, {1856, 1044, 4500}, {1920, 1080, 5800}, {2560, 1440, 8100}, {3776, 2124, 11600}, {3840, 2160, 16800}}
	return map[string]LadderDef{
		"legacy": {
			Description: "Default distinct-height geometric ladder (one rung per resolution per codec).",
			Seed:        true,
			Codecs: map[string][][]int{
				"h264": {{640, 360, 600}, {960, 540, 1722}, {1280, 720, 2779}, {1920, 1080, 6957}, {2560, 1440, 16995}, {3840, 2160, 26453}},
				"hevc": legacyHEVC,
				"av1":  legacyHEVC,
			},
		},
		"apple": {
			Description: "Apple HLS Authoring Spec bitrates — per-codec, multi-rung.",
			Seed:        true,
			Codecs: map[string][][]int{
				"h264": appleH264,
				"hevc": appleHEVC,
				"av1":  appleHEVC,
			},
		},
		"apple-uniq": {
			Description: "Apple bitrates with every rung given a unique 16:9 resolution.",
			Seed:        true,
			Codecs: map[string][][]int{
				"h264": appleUniqH264,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-live": {
			Description:       "apple-uniq bitrates under Apple's live/linear VBV: peak <= 1.25x avg. maxrate 110% + tight 0.10x buffer keep delivered peak <=~1.20x even at 1s segments; unique resolutions keep the bands distinct.",
			Seed:              true,
			MaxratePercent:    110,
			BufsizeMultiplier: 0.10,
			// No pinned segment_duration: this is the FLEXIBLE base — the tight VBV
			// is safe to repackage into 1s/2s/6s, so the ladder page shows all three
			// segment charts. partial/gop are its LL-HLS live settings.
			PartialDuration: "0.2",
			GopDuration:     "1.0",
			Codecs: map[string][][]int{
				"h264": appleUniqH264,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-live-6s": {
			Description:       "apple-uniq LL-HLS for 6s segments ONLY. The tight 110%/0.10x VBV on apple-uniq-live existed to keep the delivered per-segment peak reasonable even at 1s (delivered peak ~= maxrate + bufsize/T). Fixed at 6s the bufsize/T term is 6x smaller, so relax to 150%/1.0x for better quality on complex scenes while the delivered peak stays ~1.67x avg. Keeps LL-HLS parts (0.2s) + 1s GOP.",
			Seed:              true,
			MaxratePercent:    150,
			BufsizeMultiplier: 1.0,
			SegmentDuration:   "6", // fixed → suffix auto-derives to "_6s"
			PartialDuration:   "0.2",
			GopDuration:       "1.0",
			Codecs: map[string][][]int{
				"h264": appleUniqH264,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
		"apple-uniq-vod": {
			Description:       "apple-uniq bitrates tuned for VOD: 6s segments, NO LL-HLS parts, long 6s GOP (fewer keyframes -> better efficiency), and a relaxed VBV (peak <= 2x avg per Apple's VOD guidance, 2.0x buffer). Bits redistribute toward complex scenes; average bitrate and size are unchanged.",
			Seed:              true,
			MaxratePercent:    200,
			BufsizeMultiplier: 2.0,
			SegmentDuration:   "6",
			PartialDuration:   "0",
			GopDuration:       "6",
			Codecs: map[string][][]int{
				"h264": appleUniqH264,
				"hevc": appleUniqHEVC,
				"av1":  appleUniqHEVC,
			},
		},
	}
}

// ladderDefsEqual compares two ladder definitions by their JSON encoding — a
// cheap structural equality that ignores map/field ordering. Used to detect a
// seed whose code definition drifted from the persisted copy.
func ladderDefsEqual(a, b LadderDef) bool {
	ab, err1 := json.Marshal(a)
	bb, err2 := json.Marshal(b)
	return err1 == nil && err2 == nil && string(ab) == string(bb)
}

// LoadLadderStore loads the store from `path`, seeding it from the built-ins
// (and writing the file) if it doesn't exist yet. Any user-added ladders in an
// existing file are preserved; missing seed ladders are re-added so a built-in
// can never be permanently lost. Never returns nil — on error it falls back to
// an in-memory seed set so encoding still works.
func LoadLadderStore(path string) *LadderStore {
	s := &LadderStore{path: path, ladders: map[string]LadderDef{}}
	seeds := defaultSeedLadders()

	if data, err := os.ReadFile(path); err == nil {
		var f ladderFile
		if json.Unmarshal(data, &f) == nil && f.Ladders != nil {
			s.ladders = f.Ladders
		}
	}
	changed := false
	// Prune stale seeds: a stored ladder marked seed:true that is no longer a
	// current built-in (renamed/removed in code) — e.g. an old "live" after it
	// became "apple-uniq-live". User ladders (seed:false) are never pruned.
	for name, def := range s.ladders {
		if def.Seed {
			if _, ok := seeds[name]; !ok {
				delete(s.ladders, name)
				changed = true
			}
		}
	}
	// Ensure every current seed exists + is up to date (re-add if removed, and
	// refresh a seed whose code definition changed).
	for name, def := range seeds {
		if cur, ok := s.ladders[name]; !ok || !ladderDefsEqual(cur, def) {
			s.ladders[name] = def
			changed = true
		}
	}
	if len(s.ladders) == 0 {
		s.ladders = seeds
		changed = true
	}
	if changed {
		s.persist() // best-effort
	}
	return s
}

// persist writes the store to disk atomically. Caller holds no lock or the
// write lock; persist itself takes a read snapshot under RLock is unsafe when
// called with the write lock held, so callers pass an already-built map — see
// usage. Best-effort: logs nothing, returns the error for the caller to ignore.
func (s *LadderStore) persist() error {
	if s.path == "" {
		return nil
	}
	f := ladderFile{Version: 1, Ladders: s.ladders}
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0755); err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

// List returns all ladder definitions (a copy of the name->def map).
func (s *LadderStore) List() map[string]LadderDef {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]LadderDef, len(s.ladders))
	for k, v := range s.ladders {
		out[k] = v
	}
	return out
}

// Get returns a ladder by name (ok=false if unknown).
func (s *LadderStore) Get(name string) (LadderDef, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	d, ok := s.ladders[name]
	return d, ok
}

// Has reports whether a ladder name is known.
func (s *LadderStore) Has(name string) bool {
	_, ok := s.Get(name)
	return ok
}

// isSeedName reports whether a name is a built-in ladder (read-only: may not be
// overwritten or deleted via the API). Derived from defaultSeedLadders so it
// never drifts as seeds are added/renamed.
func isSeedName(name string) bool {
	_, ok := defaultSeedLadders()[name]
	return ok
}

// Put adds or replaces a user-defined ladder. Seed ladders are read-only and
// cannot be overwritten. The def is validated and force-marked non-seed.
func (s *LadderStore) Put(name string, def LadderDef) error {
	if name == "" {
		return fmt.Errorf("ladder name is required")
	}
	if isSeedName(name) {
		return fmt.Errorf("%q is a built-in ladder and cannot be modified", name)
	}
	if err := validateLadderDef(def); err != nil {
		return err
	}
	def.Seed = false
	s.mu.Lock()
	s.ladders[name] = def
	err := s.persist()
	s.mu.Unlock()
	return err
}

// Delete removes a user-defined ladder. Seed ladders cannot be deleted.
func (s *LadderStore) Delete(name string) error {
	if isSeedName(name) {
		return fmt.Errorf("%q is a built-in ladder and cannot be deleted", name)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.ladders[name]; !ok {
		return fmt.Errorf("ladder %q not found", name)
	}
	delete(s.ladders, name)
	return s.persist()
}

// validateLadderDef checks a user-supplied ladder is well-formed: at least one
// codec with at least one rung, every rung a [width, height, bitrate] triple of
// positive ints. Keeps a malformed ladder from ever reaching an encode.
func validateLadderDef(def LadderDef) error {
	if len(def.Codecs) == 0 {
		return fmt.Errorf("ladder needs at least one codec")
	}
	total := 0
	for codec, rows := range def.Codecs {
		if codec != "h264" && codec != "hevc" && codec != "av1" {
			return fmt.Errorf("unknown codec %q (want h264/hevc/av1)", codec)
		}
		for i, r := range rows {
			if len(r) < 3 {
				return fmt.Errorf("%s rung %d: need [width, height, bitrate]", codec, i)
			}
			if r[0] <= 0 || r[1] <= 0 || r[2] <= 0 {
				return fmt.Errorf("%s rung %d: width/height/bitrate must be positive", codec, i)
			}
		}
		total += len(rows)
	}
	if total == 0 {
		return fmt.Errorf("ladder needs at least one rung")
	}
	if def.MaxratePercent < 0 || def.BufsizeMultiplier < 0 {
		return fmt.Errorf("maxrate_percent / bufsize_multiplier must be >= 0")
	}
	return nil
}

// resolveRungs returns the rungs to encode for a (ladder, codec), filtered to
// those that fit the source (no upscale) and --max-res, with ordinal labels
// for repeated resolutions. Mirrors ladder.select_rungs. av1 reuses the hevc
// column. Returns nil for an unknown ladder/codec.
func (s *LadderStore) resolveRungs(ladderName, codec, maxRes string, sourceWidth int) []ladderRung {
	def, ok := s.Get(ladderName)
	if !ok {
		return nil
	}
	col := codec
	if codec == "av1" {
		col = "hevc"
	}
	rows := def.Codecs[col]
	if len(rows) == 0 {
		return nil
	}

	// Count resolution occurrences (by height) for label disambiguation over
	// the FULL ladder, so filtering never changes a rung's label.
	counts := map[string]int{}
	for _, r := range rows {
		if len(r) < 3 {
			continue
		}
		counts[fmt.Sprintf("%dp", r[1])]++
	}

	maxH, capByRes := maxResHeight[maxRes]
	idx := map[string]int{}
	var out []ladderRung
	for _, r := range rows {
		if len(r) < 3 {
			continue
		}
		w, h, b := r[0], r[1], r[2]
		rn := fmt.Sprintf("%dp", h)
		label := rn
		if counts[rn] > 1 {
			idx[rn]++
			label = fmt.Sprintf("%s_%d", rn, idx[rn])
		}
		if sourceWidth > 0 && w > sourceWidth {
			continue
		}
		if capByRes && h > maxH {
			continue
		}
		out = append(out, ladderRung{
			Label: label, ResName: rn, Width: w, Height: h,
			Bitrate: b, Preset: "medium",
		})
	}
	return out
}
