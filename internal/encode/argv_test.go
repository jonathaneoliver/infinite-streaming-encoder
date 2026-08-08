package encode

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
)

// The ffmpeg argv is the last unrecorded input to a rendition, so the ways it
// can go wrong are (a) the payload not surviving the marker format and (b) the
// per-pass keys colliding. Both are silent: a mangled argv looks exactly like an
// older output that never had one.

func argvMarker(codec, label string, pass int, argv []string) string {
	b, _ := json.Marshal(argv)
	return "[[ENCODER-ARGV codec=" + codec + " label=" + label +
		" pass=" + string(rune('0'+pass)) + " argv=" +
		base64.StdEncoding.EncodeToString(b) + "]]"
}

// A filter chain carries spaces, quotes and commas — drawtext alone has all
// three — which is exactly why the payload is base64 of JSON rather than a
// shell string. If this survives, nothing in a real command won't.
func TestFfmpegArgvSurvivesTheMarkerFormat(t *testing.T) {
	argv := []string{"ffmpeg", "-y", "-i", "in.mp4", "-vf",
		`drawtext=text='a b',fontsize=24,x=(w-tw)/2:y=10`, "-c:v", "libx265", "out.mp4"}
	j := &Job{}
	if !j.parseMarker(argvMarker("hevc", "1080p", 0, argv)) {
		t.Fatal("marker not consumed")
	}
	got := j.FfmpegArgv["hevc/1080p"]
	if len(got) != len(argv) {
		t.Fatalf("argv length %d, want %d: %v", len(got), len(argv), got)
	}
	for i := range argv {
		if got[i] != argv[i] {
			t.Fatalf("token %d: %q != %q", i, got[i], argv[i])
		}
	}
}

// pass=0 is a single-pass encode. A two-pass encode's pass 1 is a DIFFERENT
// command from a single-pass encode of the same rung — it writes stats and
// discards its output — so the two must never collapse onto one key.
func TestTwoPassArgvKeysDoNotCollide(t *testing.T) {
	j := &Job{}
	j.parseMarker(argvMarker("hevc", "1080p", 1, []string{"ffmpeg", "-pass", "1"}))
	j.parseMarker(argvMarker("hevc", "1080p", 2, []string{"ffmpeg", "-pass", "2"}))
	j.parseMarker(argvMarker("h264", "1080p", 0, []string{"ffmpeg", "-single"}))
	for _, want := range []string{"hevc/1080p (pass 1)", "hevc/1080p (pass 2)", "h264/1080p"} {
		if _, ok := j.FfmpegArgv[want]; !ok {
			t.Fatalf("missing key %q in %v", want, keysOf(j.FfmpegArgv))
		}
	}
	if len(j.FfmpegArgv) != 3 {
		t.Fatalf("expected 3 distinct commands, got %v", keysOf(j.FfmpegArgv))
	}
}

// Every worker encoding a chunk of a rung reports the same command bar its own
// input window and output path. First wins — later ones add nothing, and
// letting them overwrite would make the record depend on which worker finished
// last.
func TestFirstArgvWinsPerRung(t *testing.T) {
	j := &Job{}
	j.parseMarker(argvMarker("h264", "720p", 0, []string{"ffmpeg", "chunk0"}))
	j.parseMarker(argvMarker("h264", "720p", 0, []string{"ffmpeg", "chunk7"}))
	if got := j.FfmpegArgv["h264/720p"]; got[1] != "chunk0" {
		t.Fatalf("later worker overwrote the record: %v", got)
	}
}

// A payload that does not decode is CONSUMED, not logged. It is still a marker;
// letting it fall through would dump base64 into the user's log viewer.
func TestUnparseableArgvIsSwallowed(t *testing.T) {
	j := &Job{}
	if !j.parseMarker("[[ENCODER-ARGV codec=h264 label=720p pass=0 argv=not-base64!!]]") {
		t.Fatal("a malformed marker must still be consumed")
	}
	if len(j.FfmpegArgv) != 0 {
		t.Fatalf("nothing should have been recorded: %v", j.FfmpegArgv)
	}
}

// An output records only its own codec's commands, for the same reason it
// records only its own codec's phases.
func TestArgvNarrowsToTheOutputsCodec(t *testing.T) {
	all := map[string][]string{
		"hevc/1080p":          {"ffmpeg", "hevc"},
		"hevc/1080p (pass 2)": {"ffmpeg", "hevc2"},
		"h264/1080p":          {"ffmpeg", "h264"},
	}
	got := argvForCodec(all, "hevc")
	if len(got) != 2 {
		t.Fatalf("hevc output should carry 2 commands, got %v", keysOf(got))
	}
	for k := range got {
		if !strings.HasPrefix(k, "hevc/") {
			t.Fatalf("leaked another codec's command: %q", k)
		}
	}
	if argvForCodec(all, "av1") != nil {
		t.Fatal("a codec with no recorded commands must be absent, not empty")
	}
}

func keysOf(m map[string][]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
