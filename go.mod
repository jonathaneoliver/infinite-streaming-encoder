module github.com/jonathaneoliver/infinite-streaming-encoder

go 1.22

// Minimum TOOLCHAIN, deliberately separate from the language floor above.
//
// govulncheck found 13 REACHABLE standard-library vulnerabilities on go1.26.0 —
// crypto/x509 verification panics and constraint bypasses, crypto/tls, net/http,
// net/textproto, net/url — reached through http.ListenAndServe in cmd/server.
// They are fixed across a range of patch releases; go1.26.5 is the first that
// clears all of them (1.26.1 still left 9). There are NO module dependencies, so
// the toolchain is the only place a Go-side vulnerability can enter this repo.
//
// `go 1.22` stays as the LANGUAGE version. Nothing here needs newer semantics,
// and raising it is an unrelated compatibility decision that should be made on
// its own merits rather than smuggled in with a security bump.
//
// This line is load-bearing for CI, not documentation: ci.yml resolves Go via
// setup-go's `go-version-file: go.mod`, which reads the toolchain directive.
// Before it existed CI built on go1.22 — OLDER than the 1.26.0 developers were
// running locally, and correspondingly more exposed.
toolchain go1.26.5
