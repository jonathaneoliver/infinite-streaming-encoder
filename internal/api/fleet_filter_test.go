package api

import (
	"testing"

	"github.com/jonathaneoliver/infinite-streaming-encoder/internal/encode"
)

func inst(id, state string) any { return map[string]any{"id": id, "state": state} }

func TestFilterToLiveInstances(t *testing.T) {
	fleet := []encode.FleetCPUEntry{{Machine: "i-alive"}, {Machine: "i-dead"}, {Machine: "i-pending"}}

	// The real case: 1 alive, 2 terminated.
	got := filterToLiveInstances(fleet, []any{
		inst("i-alive", "running"), inst("i-dead", "terminated"), inst("i-pending", "pending"),
	})
	if len(got) != 2 {
		t.Fatalf("want 2 kept (running+pending), got %d", len(got))
	}
	for _, e := range got {
		if e.Machine == "i-dead" {
			t.Error("terminated instance was kept")
		}
	}

	// Degradation: must NOT blank the panel.
	if n := len(filterToLiveInstances(fleet, nil)); n != 3 {
		t.Errorf("nil inventory should pass through, got %d", n)
	}
	if n := len(filterToLiveInstances(fleet, []any{})); n != 3 {
		t.Errorf("empty inventory should pass through, got %d", n)
	}
	if n := len(filterToLiveInstances(fleet, "garbage")); n != 3 {
		t.Errorf("unparseable inventory should pass through, got %d", n)
	}
	// A fully scaled-down fleet is authoritative, not suspicious: drop everything.
	// This is the state at the end of every run, and the one an earlier version
	// of this function got wrong by bailing out and showing the whole dead fleet.
	allDead := []any{inst("i-alive", "terminated"), inst("i-dead", "terminated"), inst("i-pending", "terminated")}
	if n := len(filterToLiveInstances(fleet, allDead)); n != 0 {
		t.Errorf("all-terminated inventory should empty the fleet, got %d", n)
	}
	// shutting-down is treated as gone.
	if n := len(filterToLiveInstances(fleet, []any{inst("i-alive", "running"), inst("i-dead", "shutting-down"), inst("i-pending", "running")})); n != 2 {
		t.Errorf("shutting-down should be dropped, got %d", n)
	}
}
