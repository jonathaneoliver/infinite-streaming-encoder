package encode

import (
	"fmt"
	"strings"
)

// ValidPathSegment rejects a string that must not be able to escape the
// directory it will be joined to.
//
// One definition, because there were already two subtly different copies of
// this rule — Promote's (`ContainsAny("/\\") || "" || HasPrefix(".")`) and
// outputDirFor's (`"" || "." || ".." || ContainsAny("/\\")`) — and a third was
// about to be written for OutputTag. Two implementations of one rule is how one
// of them ends up weaker; here the weaker one was "no rule at all".
//
// Deliberately stricter than "reject ..": a leading dot is refused outright, so
// neither ".." nor "..." nor ".hidden" can be a segment. Backslash is refused
// as well as slash — the Go side runs on Linux, but the value is also handed to
// Python and embedded in S3 keys, and a rule that depends on the reader's path
// separator is a rule that differs between readers.
//
// It does NOT check existence: callers that need the segment to resolve to a
// real directory (outputDirFor, Promote) still stat it afterwards. This answers
// only "can this escape", which is the security question.
func ValidPathSegment(what, name string) error {
	if name == "" {
		return fmt.Errorf("%s must not be empty", what)
	}
	if strings.ContainsAny(name, `/\`) {
		return fmt.Errorf("%s must not contain a path separator: %q", what, name)
	}
	if strings.HasPrefix(name, ".") {
		return fmt.Errorf("%s must not start with a dot: %q", what, name)
	}
	// A NUL truncates the name for every C-level consumer (and every exec'd
	// process), so a value containing one is not the value it appears to be.
	if strings.ContainsRune(name, 0) {
		return fmt.Errorf("%s must not contain a NUL byte", what)
	}
	return nil
}
