package encode

import (
	"encoding/json"
	"strings"
	"testing"
)

// The minimal overlay reaches the encoder by two different routes, and both are
// silent when wrong: a local encode would draw the full overlay and a cloud one
// would too, each still succeeding and still looking plausible. So both are
// pinned here, along with the rule that anything unrecognised means full.

func TestBurninLightOnlyWhenAskedFor(t *testing.T) {
	on, off := true, false
	cases := []struct {
		name  string
		cfg   JobConfig
		light bool
		env   string
	}{
		{"default: on, full", JobConfig{}, false, "true"},
		{"explicit full", JobConfig{BurninMode: BurninModeFull}, false, "true"},
		{"light", JobConfig{BurninMode: BurninModeLight}, true, "light"},
		{"light, burn-in explicitly on", JobConfig{Burnin: &on, BurninMode: BurninModeLight}, true, "light"},
		// Off beats the mode: there is no overlay to pick a style for, and a
		// "light" env on a burn-in-off job would switch it back ON at the worker.
		{"off wins over light", JobConfig{Burnin: &off, BurninMode: BurninModeLight}, false, "false"},
		{"off", JobConfig{Burnin: &off}, false, "false"},
		// Unrecognised spellings degrade to the full overlay, the same way
		// cli_phase._burnin_mode does at the other end.
		{"unknown mode", JobConfig{BurninMode: "minimal"}, false, "true"},
		{"empty mode", JobConfig{BurninMode: ""}, false, "true"},
		// Case and whitespace are tolerated; the UI sends neither, but a
		// hand-written job config or an older client might.
		{"case-insensitive", JobConfig{BurninMode: "LIGHT"}, true, "light"},
		{"padded", JobConfig{BurninMode: " light "}, true, "light"},
	}
	for _, tc := range cases {
		cfg := tc.cfg
		if got := cfg.BurninLight(); got != tc.light {
			t.Errorf("%s: BurninLight() = %v, want %v", tc.name, got, tc.light)
		}
		if got := cfg.BurninEnv(); got != tc.env {
			t.Errorf("%s: BurninEnv() = %q, want %q", tc.name, got, tc.env)
		}
	}
}

// The local transport: --burnin-mode light in the orchestrator's argv, and
// NOTHING for a full-overlay job, so an ordinary encode's command line is
// byte-unchanged.
func TestBurninModeInLocalArgs(t *testing.T) {
	off := false
	args := func(cfg JobConfig) string {
		return strings.Join(cfg.distArgsForFile("/src", "/out", "in.mp4", "123", 0), " ")
	}

	full := args(JobConfig{})
	if strings.Contains(full, "--burnin-mode") {
		t.Errorf("a default job must not pass --burnin-mode: %s", full)
	}
	if strings.Contains(full, "--no-burnin") {
		t.Errorf("a default job must not disable burn-in: %s", full)
	}

	light := args(JobConfig{BurninMode: BurninModeLight})
	if !strings.Contains(light, "--burnin-mode light") {
		t.Errorf("light job must pass --burnin-mode light: %s", light)
	}

	// Off still passes --no-burnin and never the mode: the two would contradict.
	none := args(JobConfig{Burnin: &off, BurninMode: BurninModeLight})
	if !strings.Contains(none, "--no-burnin") || strings.Contains(none, "--burnin-mode") {
		t.Errorf("burn-in off must pass --no-burnin alone: %s", none)
	}
}

// The cloud transport: the mode rides INSIDE the existing burnin field, which is
// what lets this ship without touching the state machine or the job definitions.
// If someone ever splits it into its own field, the ASL and both Maps'
// ItemSelectors have to learn it — this test is where that decision is recorded.
func TestBurninModeRidesInTheSFNBurninField(t *testing.T) {
	for _, tc := range []struct {
		name string
		cfg  JobConfig
		want string
	}{
		{"default", JobConfig{}, "true"},
		{"light", JobConfig{BurninMode: BurninModeLight}, "light"},
	} {
		in, _, err := buildSFNInput(LoadLadderStore(""), LoadEncodeSpeedStore(""),
			"s3://in/x.mp4", "s3://p", "s3://m", "apple-uniq-live-xs", "h264",
			"", "", false, false, tc.cfg.BurninEnv(), false, false, 3840, 30, 334.4, 0,
			"12", "6", "0.2", "1.0", 9000, nil, nil)
		if err != nil {
			t.Fatalf("%s: buildSFNInput: %v", tc.name, err)
		}
		var doc map[string]any
		if err := json.Unmarshal([]byte(in), &doc); err != nil {
			t.Fatalf("%s: unmarshal: %v", tc.name, err)
		}
		if got := doc["burnin"]; got != tc.want {
			t.Errorf("%s: input burnin = %v, want %q", tc.name, got, tc.want)
		}
		if _, ok := doc["burnin_mode"]; ok {
			t.Error("burnin_mode must NOT be a separate SFN input field — the ASL " +
				"projects `burnin` only, so a second field would silently never " +
				"reach the worker")
		}
	}
}
