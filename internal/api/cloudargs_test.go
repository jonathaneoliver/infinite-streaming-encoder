package api

import (
	"strings"
	"testing"
)

func TestValidCloudArg(t *testing.T) {
	ok := []string{
		"1754051234567",
		"arn:aws:states:us-east-1:123456789012:execution:Encode:job-7",
		"jobs/1754051234567-my clip/",
		"jobs/1754051234567-clip (final).mp4/",
		"jobs/1754051234567-clíp/",
		"c0ffee-1234-5678",
	}
	for _, a := range ok {
		if !validCloudArg(a) {
			t.Errorf("validCloudArg(%q) = false, want true", a)
		}
	}
	bad := []string{
		"",
		"--sweep-all",
		"-x",
		" leading space",
		"has\nnewline",
		"has\ttab",
	}
	for _, a := range bad {
		if validCloudArg(a) {
			t.Errorf("validCloudArg(%q) = true, want false", a)
		}
	}
}

// Two regressions this guards, one per abandoned way of inferring which arg is
// a value. Position: the old rule checked odd indices only, so `--arn`/`--id` —
// index 2, behind a subcommand — reached argv unchecked. Spelling: skipping
// args that look like known flags let a VALUE of `--sweep-all` through, which
// is cleanup.py's delete-the-account switch.
//
// Every one of these must be refused BEFORE python3 is spawned.
func TestRunPythonCloudRefusesFlagShapedValues(t *testing.T) {
	for _, tc := range []struct {
		module string
		args   []any
	}{
		{"cleanup", []any{"--job-id", cloudVal("--sweep-all")}},
		{"cleanup", []any{"--delete-prefix", cloudVal("-rf")}},
		{"batch_admin", []any{"stop-execution", "--arn", cloudVal("--stop-all")}},
		{"batch_admin", []any{"terminate-job", "--id", cloudVal("--stop-all")}},
		{"batch_admin", []any{"terminate-job", "--id", cloudVal("job\nid")}},
	} {
		_, err := runPythonCloud(tc.module, tc.args...)
		if err == nil {
			t.Fatalf("runPythonCloud(%q, %v) accepted a flag-shaped value", tc.module, tc.args)
		}
		if !strings.Contains(err.Error(), "refusing argument") {
			t.Errorf("runPythonCloud(%q, %v): got %v, want a refusal", tc.module, tc.args, err)
		}
	}
}
