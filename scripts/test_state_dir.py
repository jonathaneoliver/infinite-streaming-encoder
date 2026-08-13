#!/usr/bin/env python3
"""STATE_DIR / RECORD_DIR (#331): the joins that fail silently.

The irreplaceable state — user-authored ladders, a learned speed model with
tens of thousands of samples behind it — moved out of $TMP_DIR so that a
directory called "tmp" can actually be cleared. The code change is small; what
is not small is that the path crosses a language boundary twice and BOTH
crossings fail without an error on either side:

  - `spot_samples.json` is written by Go and read by inventory.py. A
    disagreement about the directory reads as an empty file, and the AWS view's
    spot savings show zero — indistinguishable from a quiet fleet.
  - `ladders.json` reaches Python as LADDER_STORE. A path the container cannot
    open means the encoder falls back to the built-in ladders, so a custom
    ladder encodes as something else and says nothing.

And a third, entirely inside the config: a var the Go side reads which the
compose `environment:` block omits is INERT under the only configuration that
ships. It works under `go run ./cmd/server` on the host and does nothing in the
container.

Read as text — this asserts about source, not behaviour, because behaviour here
needs a container, a farm and a finished cloud run.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_compose_passes_the_vars_the_server_reads() -> None:
    """The env allow-list. TMP_STAGING_MAX_AGE_H, DEFAULT_LADDER and four
    others were each inert this way; this one would be worse than inert, since
    an unset STATE_DIR means the migration never runs and the state quietly
    stays in the disposable directory the operator just tried to move it out
    of."""
    compose = (ROOT / "docker-compose.yml").read_text()
    for var in ("STATE_DIR:", "RECORD_DIR:", "HOST_STATE_DIR:"):
        assert var in compose, f"{var} is not in the server's environment block"
    # The `:+` form is load-bearing: it must stay EMPTY when the operator has
    # not set it, or the server sees /media/state != /media/tmp and migrates
    # into what is actually the same directory.
    assert "STATE_DIR: ${STATE_DIR:+/media/state}" in compose, (
        "STATE_DIR does not degrade to empty when unset")
    assert "RECORD_DIR: ${RECORD_DIR:+/media/record}" in compose, (
        "RECORD_DIR does not degrade to empty when unset")
    # ...and the mounts have to exist in BOTH cases, so they fall back to
    # TMP_DIR rather than to a path that does not exist on the host.
    assert "${STATE_DIR:-${TMP_DIR:-/TMP_DIR-not-set}}:/media/state" in compose, (
        "no /media/state mount, or it does not fall back to TMP_DIR")
    assert "${RECORD_DIR:-${TMP_DIR:-/TMP_DIR-not-set}}:/media/record" in compose, (
        "no /media/record mount, or it does not fall back to TMP_DIR")


def test_go_exports_the_resolved_state_dir() -> None:
    """Python resolves the directory from the environment, Go from a flag that
    defaults to the environment. They agree only because Go writes its RESOLVED
    answer back into its own env before spawning anything."""
    main = (ROOT / "cmd" / "server" / "main.go").read_text()
    assert 'os.Setenv("STATE_DIR"' in main, (
        "the Python helpers inherit the raw env, so -state-dir and STATE_DIR "
        "can disagree with nothing to report it")
    assert "MigrateDurableState" in main, "nothing migrates the old layout"
    assert main.index("MigrateDurableState") < main.index("encode.NewManager"), (
        "the migration runs AFTER NewManager, which loads three of the stores "
        "in its constructor — it would read the old path and write the new one")


def test_inventory_reads_the_same_directory_go_writes() -> None:
    """One helper, both readers. cost_samples.json is written by Python only,
    so its directory has no Go-side reader to disagree with — but it still has
    to MOVE with the rest, which is why Go's migration list names it too."""
    inv = (ROOT / "scripts" / "infinite_streaming_encoder" / "cloud"
           / "inventory.py").read_text()
    assert 'os.environ.get("STATE_DIR") or os.environ.get("TMP_DIR")' in inv, (
        "inventory.py does not prefer STATE_DIR, or does not fall back to "
        "TMP_DIR — the pre-#331 location every unmigrated install still uses")
    for name in ("spot_samples.json", "cost_samples.json"):
        assert f'_state_dir(), "{name}")' in inv, (
            f"{name} is not resolved through the shared state dir")

    state_go = (ROOT / "internal" / "encode" / "statedir.go").read_text()
    listed = set(re.findall(r'"([\w.-]+\.json)"', state_go))
    for name in ("spot_samples.json", "cost_samples.json"):
        assert name in listed, f"{name} is not in Go's migration list"


def test_the_ladder_store_is_reachable_from_a_worker_container() -> None:
    """Two halves, and the second is the one that is easy to forget: the path
    has to be one the CONTAINER can open. A state dir outside TmpDir is not
    covered by any existing mount."""
    job = (ROOT / "internal" / "encode" / "job.go").read_text()
    assert '"LADDER_STORE=" + m.StatePath("ladders.json")' in job, (
        "LADDER_STORE no longer follows the state dir")
    assert 'runArgs = append(runArgs, "-v", hs+":"+m.stateDir()+":ro")' in job, (
        "nothing mounts a separate state dir into spawned containers, so "
        "LADDER_STORE points at a path that does not exist there")

    ladder = (ROOT / "scripts" / "infinite_streaming_encoder" / "ladder.py").read_text()
    assert '"STATE_DIR", "TMPDIR"' in ladder, (
        "ladder.py's fallback probe does not look in STATE_DIR first")


def test_nothing_still_joins_the_moved_names_onto_tmpdir() -> None:
    """The regression shape: one call site left behind writes into $TMP_DIR
    while everything else reads the new location. Half-migrated, and quiet."""
    moved = ("ladders.json", "quality-curves.json", "encode_speeds.json",
             "spot_samples.json", "settings.json", "logs", "history.md", "failed")
    for rel in ("internal/encode/job.go", "internal/encode/settings.go",
                "internal/api/handlers.go"):
        src = (ROOT / rel).read_text()
        for name in moved:
            bad = f'TmpDir, "{name}"'
            assert bad not in src, f"{rel} still joins {name} onto TmpDir"


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    print("state-dir tests:", "FAILED" if failures else "all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
