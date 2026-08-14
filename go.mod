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
//
// 1.26.5 -> 1.26.6 (#350): five more reachable stdlib vulnerabilities, four of
// them straight off cmd/server's own http.ListenAndServe — GO-2026-6218
// (net/url), GO-2026-6090 (crypto/tls), GO-2026-6089 (net/http h2c
// ReadHeaderTimeout), GO-2026-5972 (encoding/asn1 recursion depth),
// GO-2026-5026 (idna Punycode labels, via net/http). All five say "Fixed in:
// …@go1.26.6", and go1.26.6 reports clean.
//
// Expect this line to keep moving, and expect the failure to arrive on someone
// else's unrelated PR: with no module dependencies, EVERY govulncheck finding
// this repo can have lands here. The Dockerfile pins `golang:1.26-alpine`,
// which floats — so the shipped binary gets the fix while `make check` and CI
// go red. That divergence is why nothing breaks at runtime to warn you.
toolchain go1.26.6
