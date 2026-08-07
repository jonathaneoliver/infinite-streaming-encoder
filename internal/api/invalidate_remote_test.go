package api

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// The "invalidate on purpose" half of #225 reads which prefixes were removed out
// of cleanup.py's own JSON report, so the shape of that report is a
// cross-language contract — and a broken one fails SILENTLY here: the marking
// simply never happens, the outputs keep advertising media that is gone, and
// the only symptom is a click that errors days later.

func sidecarAt(t *testing.T, outDir, name, prefix string) string {
	t.Helper()
	dir := filepath.Join(outDir, name)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(encode.RemoteInfo{S3Prefix: prefix, PendingFiles: 40})
	if err := os.WriteFile(filepath.Join(dir, encode.RemoteSidecar), b, 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestInvalidateReadsDeletedPrefixesFromTheCleanupReport(t *testing.T) {
	out := t.TempDir()
	s := &Server{Manager: &encode.Manager{OutputDir: out}}
	deleted := sidecarAt(t, out, "clip_h264", "s3://b/jobs/1786-clip/output_h264")
	kept := sidecarAt(t, out, "other_h264", "s3://b/jobs/1999-other/output_h264")

	// Exactly the shape cleanup.py's CleanupReport.as_json() produces, including
	// the trailing slash _delete_s3_prefix appends and the mixed action kinds a
	// real job teardown returns.
	report := []byte(`{"scope":"job:1786-clip","actions":[
      {"kind":"instance","id":"i-abc","job_id":"1786-clip","action":"terminated","detail":""},
      {"kind":"s3_prefix","id":"s3://b/jobs/1786-clip/","job_id":"1786-clip","action":"deleted","detail":"812 object version(s)"},
      {"kind":"s3_prefix","id":"s3://b/jobs/1999-other/","job_id":null,"action":"failed","detail":"AccessDenied"},
      {"kind":"s3_prefix","id":"s3://b/mezz/xyz/","job_id":null,"action":"skipped","detail":"refused"}
    ]}`)
	s.invalidateRemoteOutputs(report, "job cleanup")

	if r := encode.ReadRemote(deleted); r == nil || !r.Gone {
		t.Fatal("a deleted prefix did not mark its output — the UI keeps offering Download")
	}
	// "failed" is not "deleted": a partial delete may have left the objects
	// there, and claiming loss would be worse than the click discovering it.
	if r := encode.ReadRemote(kept); r == nil || r.Gone {
		t.Fatal("a FAILED delete marked its output gone")
	}
}

func TestInvalidateSurvivesAnUnparseableReport(t *testing.T) {
	// The cleanup itself worked; the bookkeeping afterwards must not turn that
	// into an error, and must not mark anything on a guess.
	out := t.TempDir()
	s := &Server{Manager: &encode.Manager{OutputDir: out}}
	dir := sidecarAt(t, out, "clip_h264", "s3://b/jobs/1786-clip/output_h264")

	for _, bad := range [][]byte{nil, []byte(""), []byte("not json"), []byte(`{"actions":null}`)} {
		s.invalidateRemoteOutputs(bad, "job cleanup") // must not panic
	}
	if r := encode.ReadRemote(dir); r == nil || r.Gone {
		t.Fatal("an unreadable report marked an output gone")
	}
}
