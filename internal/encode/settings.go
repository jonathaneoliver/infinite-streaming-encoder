package encode

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Settings holds persisted, user-toggleable global preferences. Kept small and
// JSON-shaped so the UI can read/write it wholesale. Persisted to
// $TmpDir/settings.json; the persisted value wins over env/flag defaults on
// startup so a toggle sticks across restarts.
type Settings struct {
	// WatcherEnabled gates the source-dir auto-encode watcher. When false the
	// watcher goroutine still runs but submits nothing.
	WatcherEnabled bool `json:"watcher_enabled"`
}

func (m *Manager) settingsPath() string {
	return filepath.Join(m.TmpDir, "settings.json")
}

// InitSettings seeds settings with the given defaults, then overlays any
// persisted values. Unmarshaling into the pre-filled struct means fields
// absent from an older settings.json keep their default. Call once at startup.
func (m *Manager) InitSettings(defaultWatcherEnabled bool) {
	m.settingsMu.Lock()
	defer m.settingsMu.Unlock()
	m.settings = Settings{WatcherEnabled: defaultWatcherEnabled}
	if data, err := os.ReadFile(m.settingsPath()); err == nil {
		_ = json.Unmarshal(data, &m.settings)
	}
}

// Settings returns a copy of the current settings.
func (m *Manager) Settings() Settings {
	m.settingsMu.Lock()
	defer m.settingsMu.Unlock()
	return m.settings
}

// WatcherEnabled reports whether the auto-encode watcher should submit jobs.
func (m *Manager) WatcherEnabled() bool {
	m.settingsMu.Lock()
	defer m.settingsMu.Unlock()
	return m.settings.WatcherEnabled
}

// SetWatcherEnabled flips the watcher on/off and persists the change.
func (m *Manager) SetWatcherEnabled(v bool) {
	m.settingsMu.Lock()
	m.settings.WatcherEnabled = v
	snapshot := m.settings
	m.settingsMu.Unlock()
	m.persistSettings(snapshot)
}

// persistSettings writes settings.json atomically (tmp + rename).
func (m *Manager) persistSettings(s Settings) {
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return
	}
	tmp := m.settingsPath() + ".tmp"
	if os.WriteFile(tmp, data, 0644) == nil {
		_ = os.Rename(tmp, m.settingsPath())
	}
}
