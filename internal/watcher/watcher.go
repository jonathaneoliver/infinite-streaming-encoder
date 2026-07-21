package watcher

import (
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/jonathaneoliver/encoder/internal/encode"
)

type Watcher struct {
	dir      string
	interval time.Duration
	manager  *encode.Manager
	defaults encode.JobConfig

	mu   sync.Mutex
	seen map[string]int64
}

func New(dir string, interval time.Duration, manager *encode.Manager, defaults encode.JobConfig) *Watcher {
	return &Watcher{
		dir:      dir,
		interval: interval,
		manager:  manager,
		defaults: defaults,
		seen:     make(map[string]int64),
	}
}

func (w *Watcher) Run() {
	w.scan(true)
	ticker := time.NewTicker(w.interval)
	defer ticker.Stop()
	for range ticker.C {
		w.scan(false)
	}
}

func (w *Watcher) scan(initial bool) {
	// Runtime toggle (persisted): when the watcher is disabled we don't scan
	// or submit at all. Re-enabling resumes on the next tick — the first scan
	// after re-enable re-seeds file sizes and only submits once a file is seen
	// stable, so flipping it back on won't instantly flood.
	if !w.manager.WatcherEnabled() {
		return
	}
	entries, err := os.ReadDir(w.dir)
	if err != nil {
		log.Printf("watcher: read dir %s: %v", w.dir, err)
		return
	}

	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		ext := filepath.Ext(e.Name())
		if !isVideoExt(ext) {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		size := info.Size()
		name := e.Name()

		w.mu.Lock()
		prevSize, known := w.seen[name]
		w.seen[name] = size
		w.mu.Unlock()

		if initial {
			continue
		}

		if !known {
			log.Printf("watcher: new file detected: %s (%d bytes, waiting for write to complete)", name, size)
			continue
		}

		if prevSize != size {
			// Still being written — wait for next scan
			continue
		}

		if size == 0 {
			continue
		}

		// File is stable (known, size unchanged, non-zero).
		// Check if output exists — this runs every scan so we detect
		// deleted output directories without needing a restart.
		if w.alreadyEncoded(name) {
			continue
		}

		log.Printf("watcher: auto-encoding %s (%d bytes)", name, size)
		cfg := w.defaults
		cfg.Files = []string{name}
		w.manager.Submit(cfg)
	}
}

func (w *Watcher) alreadyEncoded(name string) bool {
	// Check in-memory jobs first (covers current session)
	for _, j := range w.manager.Jobs() {
		for _, f := range j.Config.Files {
			if f == name {
				return true
			}
		}
	}
	// Check output directory for existing encode output using the same
	// naming convention as the encoder (stem + partial + padding + codec).
	stem := w.defaults.OutputStem(name)
	entries, err := os.ReadDir(w.manager.OutputDir)
	if err != nil {
		return false
	}
	for _, e := range entries {
		if e.IsDir() && strings.HasPrefix(e.Name(), stem+"_") && !encode.IsDatedBackup(e.Name()) {
			return true
		}
	}
	return false
}

func isVideoExt(ext string) bool {
	switch ext {
	case ".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m3u8":
		return true
	}
	return false
}
