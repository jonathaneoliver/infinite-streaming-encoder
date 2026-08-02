"""Tests for the per-instance mezzanine cache (#203).

Run directly (`python3 scripts/test_mezz_cache.py`) or via `make check`. No test
framework: the repo has none, and this needs nothing beyond assert.

Why these and not others: the cache's failure modes are the quiet kind. A cache
that never hits looks exactly like no cache — it just downloads, which is what it
did before — and the one log line it prints ("caching … first chunk on this box")
says the same thing whether it worked or not. So the sizing and eviction rules
are tested directly rather than inferred from a green encode.

The download path itself is not tested here; it needs S3. What is tested is
everything that decides WHETHER to download, which is where the disk fills up.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import cli_phase  # noqa: E402


def _mezz(cache: Path, name: str, size: int, age_s: float = 0.0) -> Path:
    """A cached mezzanine + its .done sidecar, optionally backdated."""
    p = cache / f"mezz-{name}.mp4"
    p.write_bytes(b"\0" * size)
    p.with_suffix(".mp4.done").write_text(str(size))
    (cache / f"mezz-{name}.lock").write_text("")
    if age_s:
        when = time.time() - age_s
        os.utime(p, (when, when))
    return p


def _cached(cache: Path) -> set[str]:
    return {p.name for p in cache.glob("mezz-*.mp4")}


def test_keep_defaults_to_two() -> None:
    os.environ.pop("MEZZ_CACHE_KEEP", None)
    assert cli_phase._cache_keep() == 2, "default must be 2, not the old 6"

    # A count is only safe because it is small: the mezzanine is a stream copy,
    # so it is the size of the SOURCE. 6 x 474 MB was 2.8 GB; 6 x 10 GB (a
    # 2-hour clip) is 61 GB against a 30 GiB Batch root.
    os.environ["MEZZ_CACHE_KEEP"] = "4"
    assert cli_phase._cache_keep() == 4
    for bad in ("", "nonsense", "0", "-3"):
        os.environ["MEZZ_CACHE_KEEP"] = bad
        got = cli_phase._cache_keep()
        assert got >= 1, f"MEZZ_CACHE_KEEP={bad!r} gave {got}; must never be < 1"
    os.environ.pop("MEZZ_CACHE_KEEP", None)


def test_prune_keeps_the_newest_two_and_their_sidecars() -> None:
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d)
        _mezz(cache, "old", 16, age_s=300)
        _mezz(cache, "mid", 16, age_s=200)
        _mezz(cache, "new", 16, age_s=100)
        _mezz(cache, "newest", 16, age_s=0)

        cli_phase._prune_mezz_cache(cache)

        assert _cached(cache) == {"mezz-newest.mp4", "mezz-new.mp4"}, _cached(cache)
        # Sidecars must go with the file. A .done left behind for a deleted .mp4
        # is worse than useless: _download_if_complete gates on the .done, so an
        # orphan is a claim about a file that no longer exists.
        assert not (cache / "mezz-old.mp4.done").exists()
        assert not (cache / "mezz-old.lock").exists()
        assert (cache / "mezz-newest.mp4.done").exists()


def test_prune_handles_vmafref_independently() -> None:
    # #109's per-box reference files share the dir but not the budget — pruning
    # them together would let a run with VMAF on evict its own mezzanine.
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d)
        for i in range(3):
            _mezz(cache, f"m{i}", 16, age_s=100 - i)
        for i in range(3):
            p = cache / f"vmafref-r{i}.mp4"
            p.write_bytes(b"\0" * 16)
            when = time.time() - (100 - i)
            os.utime(p, (when, when))

        cli_phase._prune_mezz_cache(cache)

        assert len(list(cache.glob("mezz-*.mp4"))) == 2
        assert len(list(cache.glob("vmafref-*.mp4"))) == 2


def test_room_check_evicts_oldest_until_it_fits() -> None:
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d)
        _mezz(cache, "old", 4096, age_s=300)
        _mezz(cache, "new", 4096, age_s=100)

        # Free space just under what's needed, then plentiful once one is gone.
        sizes = iter([1000, 10_000])

        class FakeUsage:
            def __init__(self, free: int) -> None:
                self.free = free

        real = cli_phase.shutil.disk_usage
        cli_phase.shutil.disk_usage = lambda p: FakeUsage(next(sizes))
        try:
            ok = cli_phase._ensure_cache_room(cache, 5000, "mezzanine")
        finally:
            cli_phase.shutil.disk_usage = real

        assert ok is True
        # Oldest first, and only as many as needed — evicting more than the
        # shortfall requires would throw away the hit the cache exists for.
        assert _cached(cache) == {"mezz-new.mp4"}, _cached(cache)


def test_room_check_gives_up_rather_than_evicting_everything() -> None:
    # A clip too big for the volume must still ENCODE. Returning False sends the
    # caller down the private-download path, which is exactly the behaviour
    # before the cache existed — a cache miss, not a failed chunk.
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d)
        _mezz(cache, "a", 64, age_s=200)

        class FakeUsage:
            free = 10

        real = cli_phase.shutil.disk_usage
        cli_phase.shutil.disk_usage = lambda p: FakeUsage()
        try:
            ok = cli_phase._ensure_cache_room(cache, 10**12, "mezzanine")
        finally:
            cli_phase.shutil.disk_usage = real

        assert ok is False
        assert _cached(cache) == set(), "should have emptied the cache trying"


def test_room_check_does_not_block_when_free_space_is_unreadable() -> None:
    # Refusing to cache because statvfs failed would trade a real optimisation
    # for a hypothetical one. Fail open — the download reports any real error.
    with tempfile.TemporaryDirectory() as d:
        def boom(_p):
            raise OSError("no statvfs here")

        real = cli_phase.shutil.disk_usage
        cli_phase.shutil.disk_usage = boom
        try:
            assert cli_phase._ensure_cache_room(Path(d), 10**9, "mezzanine") is True
        finally:
            cli_phase.shutil.disk_usage = real


def test_lock_timeout_gives_up_on_a_dead_leader() -> None:
    # Waiting on flock has no upper bound. A leader killed mid-download (spot
    # reclaim, OOM) would otherwise hang every other chunk on the box for the
    # rest of the job.
    import fcntl

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "held.lock"
        holder = open(path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        os.environ["MEZZ_CACHE_LOCK_TIMEOUT_S"] = "1"
        try:
            waiter = open(path, "w")
            t0 = time.monotonic()
            got = cli_phase._lock_with_timeout(waiter, "mezzanine")
            elapsed = time.monotonic() - t0
            assert got is False, "must not claim a lock it never got"
            assert 0.9 <= elapsed < 5, f"gave up after {elapsed:.1f}s"
            waiter.close()
        finally:
            os.environ.pop("MEZZ_CACHE_LOCK_TIMEOUT_S", None)
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


def test_lock_is_acquired_when_free() -> None:
    with tempfile.TemporaryDirectory() as d:
        f = open(Path(d) / "free.lock", "w")
        try:
            assert cli_phase._lock_with_timeout(f, "mezzanine") is True
        finally:
            f.close()


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

