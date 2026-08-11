package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

// distMachine is a distributed-local worker box the server knows how to control.
type distMachine struct {
	Name      string // Temporal worker identity / WORKER_LABEL (e.g. "mac", "ubuntu")
	SSHTarget string // ssh target for remote boxes; empty = the local (master) box
}

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// configuredMachines lists the worker boxes the server can toggle: the local
// master (LOCAL_WORKER_LABEL, default "mac") plus each entry in DIST_WORKERS
// ("label=ssh_target" pairs — the same var `make dist-deploy-workers` uses).
func configuredMachines() []distMachine {
	machines := []distMachine{{Name: envOrDefault("LOCAL_WORKER_LABEL", "mac")}}
	for _, wkr := range strings.Fields(os.Getenv("DIST_WORKERS")) {
		if label, target, ok := strings.Cut(wkr, "="); ok && label != "" {
			machines = append(machines, distMachine{Name: label, SSHTarget: target})
		}
	}
	return machines
}

// activePollers is the set of worker identities currently polling the Temporal
// encode task queue, via the Temporal UI HTTP API (no SDK dep).
//
// Returns the LAST POLL TIME per identity, not a bare presence bit. That time is
// the discriminator #294 needed: a box polling two minutes ago and not now is a
// different thing from one that has never appeared, and collapsing both to
// "not on" is what made a SLEEPING machine indistinguishable from a disabled one.
func activePollers() map[string]time.Time {
	active := map[string]time.Time{}
	url := fmt.Sprintf("%s/api/v1/namespaces/default/task-queues/%s?taskQueueType=1",
		strings.TrimRight(envOrDefault("TEMPORAL_UI_ADDR", "http://host.docker.internal:8233"), "/"),
		envOrDefault("TEMPORAL_TASK_QUEUE", "encode"))
	client := &http.Client{Timeout: 3 * time.Second}
	if resp, err := client.Get(url); err == nil {
		defer resp.Body.Close()
		var body struct {
			Pollers []struct {
				Identity       string `json:"identity"`
				LastAccessTime string `json:"lastAccessTime"`
			} `json:"pollers"`
		}
		if json.NewDecoder(resp.Body).Decode(&body) == nil {
			for _, p := range body.Pollers {
				if p.Identity == "" {
					continue
				}
				// Prefer Temporal's own stamp; fall back to now if it is missing
				// or unparseable, since PRESENCE is still solid information.
				t := time.Now()
				if p.LastAccessTime != "" {
					if parsed, err := time.Parse(time.RFC3339Nano, p.LastAccessTime); err == nil {
						t = parsed
					}
				}
				active[p.Identity] = t
			}
		}
	}
	return active
}

// Worker states, replacing the single `on` boolean (#294).
//
// `on` conflated "the user disabled it" with "it is not polling" — and the
// second covers asleep, crashed, unreachable, network-partitioned and
// still-starting. Those need different responses from a human, and the pill
// could not tell them apart: a macmini that slept through 15 minutes of a run
// rendered exactly like one someone had switched off.
const (
	WorkerPolling   = "polling"  // polling the task queue now
	WorkerDisabled  = "disabled" // a user turned it off; its container is stopped
	WorkerStale     = "stale"    // configured and seen before, but silent now
	WorkerNeverSeen = "never"    // configured and never observed polling
)

// pollerFreshWindow is how recently a worker must have polled to count as
// polling. PRESENCE in the listing is not enough on its own: Temporal keeps a
// poller record for ~5 minutes after its last poll, so a box that went to sleep
// stays listed — looking healthy for the entire window in which someone would
// notice their machine had stopped contributing, which is precisely the #294
// case. And the record cannot simply be read as "polling now" the other way
// either: a worker long-polls with a 60s timeout, so an IDLE but perfectly
// healthy box has a lastAccessTime up to a minute old. 150s clears that with
// room for one missed cycle.
const pollerFreshWindow = 150 * time.Second

type machineOut struct {
	Name  string `json:"name"`
	On    bool   `json:"on"`
	Local bool   `json:"local"`
	// State is the reason behind On, which On alone cannot carry (#294).
	// On stays for compatibility and means exactly State == WorkerPolling.
	State string `json:"state"`
	// LastSeenAgoS is seconds since this box was last observed polling, or nil
	// when it never has been. It is what separates "asleep two minutes ago" from
	// "never came up", and it is the number the tooltip shows.
	LastSeenAgoS *int64 `json:"last_seen_ago_s,omitempty"`
}

// distWorkers reports each configured machine + whether it's on (its worker is
// polling and not user-disabled), plus any unexpected pollers. Feeds the
// "Local machines" pills.
func (s *Server) distWorkers(w http.ResponseWriter, r *http.Request) {
	active := activePollers()
	now := time.Now()

	// One critical section: read the disable set, and fold this poll into the
	// last-seen memory. Temporal drops a vanished poller from the listing
	// outright, so this map is the only place that memory can live.
	s.distMu.Lock()
	disabled := make(map[string]bool, len(s.distDisabled))
	for k := range s.distDisabled {
		disabled[k] = true
	}
	if s.lastPoll == nil {
		s.lastPoll = map[string]time.Time{}
	}
	for name, t := range active {
		if prev, ok := s.lastPoll[name]; !ok || t.After(prev) {
			s.lastPoll[name] = t
		}
	}
	lastPoll := make(map[string]time.Time, len(s.lastPoll))
	for k, v := range s.lastPoll {
		lastPoll[k] = v
	}
	s.distMu.Unlock()

	// Seconds since this box last polled, or nil if it never has. This is the
	// number the tooltip shows, and the thing that separates "asleep since
	// 13:04" from "never came up".
	agoOf := func(name string) *int64 {
		t, ok := lastPoll[name]
		if !ok {
			return nil
		}
		secs := int64(now.Sub(t).Seconds())
		if secs < 0 {
			secs = 0
		}
		return &secs
	}
	// Polling requires BOTH: still in the listing, and having actually polled
	// recently. Presence alone is too generous (Temporal keeps the record for
	// ~5 minutes after a box goes quiet); recency alone is too generous the
	// other way (once Temporal drops the record the box is definitively not
	// polling, whatever the remembered stamp says).
	polling := func(name string) bool {
		if _, listed := active[name]; !listed {
			return false
		}
		ago := agoOf(name)
		return ago != nil && *ago <= int64(pollerFreshWindow/time.Second)
	}

	// stateOf answers the question the pill asks, in priority order. Disabled
	// wins even over a live poller: the user turned it off, and their intent is
	// the more useful answer during the seconds before the container stops (or
	// forever, if stopping it failed).
	stateOf := func(name string) string {
		switch {
		case disabled[name]:
			return WorkerDisabled
		case polling(name):
			return WorkerPolling
		case agoOf(name) != nil:
			return WorkerStale
		}
		return WorkerNeverSeen
	}

	seen := map[string]bool{}
	out := []machineOut{}
	on := 0
	for _, m := range configuredMachines() {
		seen[m.Name] = true
		state := stateOf(m.Name)
		if state == WorkerPolling {
			on++
		}
		out = append(out, machineOut{
			Name: m.Name, On: state == WorkerPolling, Local: m.SSHTarget == "",
			State: state, LastSeenAgoS: agoOf(m.Name),
		})
	}
	extras := []string{}
	for name := range active {
		if !seen[name] {
			extras = append(extras, name)
		}
	}
	sort.Strings(extras)
	for _, name := range extras {
		state := stateOf(name)
		if state == WorkerPolling {
			on++
		}
		out = append(out, machineOut{
			Name: name, On: state == WorkerPolling,
			State: state, LastSeenAgoS: agoOf(name),
		})
	}
	// Filter the CPU history to LOCAL machines. Manager.FleetCPU is one map keyed
	// by machine, shared with the cloud inventory on purpose (attachFleetCPU) so
	// both targets carry identical data in an identical shape. It therefore also
	// holds EC2 instance ids once a cloud encode is running, and returning it
	// whole listed those instances as local boxes — 7 "machines", 72 "cores".
	//
	// Filtering on membership rather than an "i-" prefix: `local` is exactly the
	// set this handler already built from configured machines plus active
	// Temporal pollers, and a cloud instance is neither.
	local := make(map[string]bool, len(out))
	for _, mo := range out {
		local[mo.Name] = true
	}
	fleet := []encode.FleetCPUEntry{}
	for _, e := range s.Manager.FleetCPU() {
		if local[e.Machine] {
			fleet = append(fleet, e)
		}
	}
	// Version skew across the boxes currently encoding (#248). Surfaced here
	// rather than left for the reader to diff `fleet[].version` themselves,
	// because the whole point is that nobody thinks to look: a mixed fleet
	// produces an encode that PASSES with telemetry that is quietly a subset.
	// `versions_unknown` is reported separately from `version_mixed` — a box
	// that never said cannot be called agreement.
	mixed, byMachine, unknown := s.Manager.FleetVersionSkew()
	writeJSON(w, map[string]any{
		"count": on, "machines": out, "fleet": fleet,
		"version_mixed":    mixed,
		"versions":         byMachine,
		"versions_unknown": unknown,
	})
}

// toggleDistWorker enables/disables a machine's worker. Disable HARD-stops the
// container (docker stop -t 0 → immediate SIGKILL) so its in-flight chunks are
// abandoned and Temporal reschedules them onto the remaining machines; the
// stopped state keeps `--restart unless-stopped` from bringing it back. Enable
// starts it again.
func (s *Server) toggleDistWorker(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("machine")
	var body struct {
		Enabled bool `json:"enabled"`
	}
	_ = json.NewDecoder(r.Body).Decode(&body)

	var machine *distMachine
	for _, m := range configuredMachines() {
		if m.Name == name {
			mm := m
			machine = &mm
			break
		}
	}
	if machine == nil {
		http.Error(w, "unknown machine", http.StatusNotFound)
		return
	}

	container := envOrDefault("DIST_WORKER_CONTAINER", "encode-worker")
	dockerArgs := []string{"stop", "-t", "0", container}
	if body.Enabled {
		dockerArgs = []string{"start", container}
	}

	var cmd *exec.Cmd
	if machine.SSHTarget == "" {
		cmd = exec.Command("docker", dockerArgs...)
	} else {
		// ssh directly (not docker -H ssh://) so we can pass the same opts the
		// promote path uses — the mounted mac ~/.ssh/config has UseKeychain,
		// which Linux ssh rejects without IgnoreUnknown.
		cmd = exec.Command("ssh",
			"-o", "IgnoreUnknown=UseKeychain",
			"-o", "StrictHostKeyChecking=accept-new",
			"-o", "BatchMode=yes",
			machine.SSHTarget, "docker "+strings.Join(dockerArgs, " "))
	}
	if out, err := cmd.CombinedOutput(); err != nil {
		http.Error(w, fmt.Sprintf("toggle %s: %v: %s", name, err,
			strings.TrimSpace(string(out))), http.StatusInternalServerError)
		return
	}

	s.distMu.Lock()
	if body.Enabled {
		delete(s.distDisabled, name)
	} else {
		s.distDisabled[name] = true
	}
	s.distMu.Unlock()

	writeJSON(w, map[string]any{"machine": name, "on": body.Enabled})
}
