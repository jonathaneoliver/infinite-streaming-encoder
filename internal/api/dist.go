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
func activePollers() map[string]bool {
	active := map[string]bool{}
	url := fmt.Sprintf("%s/api/v1/namespaces/default/task-queues/%s?taskQueueType=1",
		strings.TrimRight(envOrDefault("TEMPORAL_UI_ADDR", "http://host.docker.internal:8233"), "/"),
		envOrDefault("TEMPORAL_TASK_QUEUE", "encode"))
	client := &http.Client{Timeout: 3 * time.Second}
	if resp, err := client.Get(url); err == nil {
		defer resp.Body.Close()
		var body struct {
			Pollers []struct {
				Identity string `json:"identity"`
			} `json:"pollers"`
		}
		if json.NewDecoder(resp.Body).Decode(&body) == nil {
			for _, p := range body.Pollers {
				if p.Identity != "" {
					active[p.Identity] = true
				}
			}
		}
	}
	return active
}

type machineOut struct {
	Name  string `json:"name"`
	On    bool   `json:"on"`
	Local bool   `json:"local"`
}

// distWorkers reports each configured machine + whether it's on (its worker is
// polling and not user-disabled), plus any unexpected pollers. Feeds the
// "Local machines" pills.
func (s *Server) distWorkers(w http.ResponseWriter, r *http.Request) {
	active := activePollers()
	s.distMu.Lock()
	disabled := make(map[string]bool, len(s.distDisabled))
	for k := range s.distDisabled {
		disabled[k] = true
	}
	s.distMu.Unlock()

	seen := map[string]bool{}
	out := []machineOut{}
	on := 0
	for _, m := range configuredMachines() {
		seen[m.Name] = true
		isOn := active[m.Name] && !disabled[m.Name]
		if isOn {
			on++
		}
		out = append(out, machineOut{Name: m.Name, On: isOn, Local: m.SSHTarget == ""})
	}
	extras := []string{}
	for name := range active {
		if !seen[name] {
			extras = append(extras, name)
		}
	}
	sort.Strings(extras)
	for _, name := range extras {
		on++
		out = append(out, machineOut{Name: name, On: true})
	}
	writeJSON(w, map[string]any{"count": on, "machines": out,
		"fleet": s.Manager.FleetCPU()})
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
