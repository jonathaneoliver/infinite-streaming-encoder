package encode

import (
	"fmt"
	"os"
)

// ResolveSourceFiles maps client-supplied filenames onto the matching entries in
// SourceDir and returns THOSE entries' names — never the caller's strings.
//
// Everything downstream of Submit treats Config.Files as trusted. A filename is
// joined into the job tmp dir (`sfn-input-<name>.json`), folded into output dir
// names via OutputStem, stat'd for the mezzanine cache key, and handed to the
// orchestrators as argv. A request body earns none of that trust: "../" walks
// out of the tmp dir, and a leading "-" is read as an option by the argparse on
// the other side of the exec.
//
// So the name is not sanitised, it is *replaced* — matched against the directory
// listing, then discarded in favour of the listing's own string. A name with no
// match is an error, which rejects traversal and flag-shaped values for free
// rather than by enumerating what they look like.
//
// One ReadDir for the whole request: a multi-file selection resolves against a
// single listing, and a bad name in it fails the request before any job starts.
func (m *Manager) ResolveSourceFiles(names []string) ([]string, error) {
	entries, err := os.ReadDir(m.SourceDir)
	if err != nil {
		return nil, fmt.Errorf("read source dir: %w", err)
	}
	out := make([]string, 0, len(names))
	for _, want := range names {
		match := ""
		for _, e := range entries {
			if !e.IsDir() && e.Name() == want {
				match = e.Name() // the listing's string, never the caller's
				break
			}
		}
		if match == "" {
			return nil, fmt.Errorf("no such source file: %q", want)
		}
		out = append(out, match)
	}
	return out, nil
}
