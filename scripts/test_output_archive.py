#!/usr/bin/env python3
"""OUTPUT_DIR/.archive (#332): the parts a Go test cannot see.

The sweeper's rules are pinned by internal/outarchive's own tests. What is
pinned here is everything OUTSIDE the Go tree that has to agree with them:

  - the compose `environment:` allow-list, where a var the Go side reads and
    this file omits is INERT under the only configuration that ships — and here
    the omitted var is the one that DISABLES a sweeper, so the failure is
    "cannot turn it off in the deployed server", not "setting ignored";
  - CLAUDE.md, which described `.archive/` for years while the code renamed
    backups in place. That is the specific failure this issue was about, so the
    claim and the constant are pinned together.

Read as text: this asserts about source, not behaviour.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_compose_can_reach_the_sweeper_knobs() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    for var in ("OUTPUT_ARCHIVE_MAX_AGE_D:", "OUTPUT_ARCHIVE_SWEEP_ORPHANS:"):
        assert var in compose, (
            f"{var} is not in the server's environment block, so it is inert "
            f"under compose — including the 0 that turns the sweep off")
    main = (ROOT / "cmd" / "server" / "main.go").read_text()
    for var in ("OUTPUT_ARCHIVE_MAX_AGE_D", "OUTPUT_ARCHIVE_SWEEP_ORPHANS"):
        assert var in main, f"nothing reads {var}"


def test_claude_md_describes_the_archive_the_code_writes() -> None:
    """The doc claimed OutputDir/.archive/<name>_<timestamp> and a delete, and
    the code did neither for years. Pin the spelling of the directory and the
    fact that nothing is deleted."""
    arch = (ROOT / "internal" / "encode" / "archive.go").read_text()
    m = re.search(r'ArchiveDirName = "([^"]+)"', arch)
    assert m, "no ArchiveDirName constant"
    name = m.group(1)
    assert name.startswith("."), (
        "the archive is not dot-prefixed, so the existing hidden-entry guards "
        "no longer cover it and /api/outputs walks it again")
    claude = (ROOT / "CLAUDE.md").read_text()
    assert f"OutputDir/{name}/" in claude, (
        f"CLAUDE.md does not describe OutputDir/{name}/")
    assert "otherwise it's deleted" not in claude, (
        "CLAUDE.md still claims a prior output is deleted; nothing deletes one")


def test_the_sweeper_holds_every_sidecar_the_repo_defines() -> None:
    """A new sidecar meaning 'S3 holds the only copy' must be added to the hold
    list. Both current ones are named there; this fails when a third appears."""
    sweeper = (ROOT / "internal" / "outarchive" / "watch.go").read_text()
    for const in ("encode.RemoteSidecar", "encode.PendingSidecar",
                  "encode.ArchiveKeepFile"):
        assert const in sweeper, f"the sweeper does not hold {const} directories"
    assert "SweepOrphans" in sweeper, "orphans are not held behind an opt-in"


def test_the_archive_is_swept_and_migrated_from_the_server() -> None:
    """Both halves are wired, or the feature is a package nothing calls."""
    main = (ROOT / "cmd" / "server" / "main.go").read_text()
    assert "outarchive.Run" in main, "the sweeper never starts"
    assert "MigrateDatedBackups" in main, (
        "nothing collects the backups older servers left beside the live "
        "outputs, so /api/outputs keeps walking them")
    assert main.index("MigrateDatedBackups") < main.index("mgr.Reconcile()"), (
        "the migration runs after Reconcile, so a resumed job's move can race it")


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
    print("output-archive tests:", "FAILED" if failures else "all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
