"""Tests for the chunk prefetch (#311) — download each chunk as it finishes
encoding instead of pulling the whole ladder after the last one lands.

Run directly (`python3 scripts/test_prefetch.py`) or via `make check`.

The property that makes this safe is that the prefetch is an ACCELERATOR and
never an inventory: phase_package_all still lists the staging prefix to decide
what it needs, and still fails on a chunk that is missing there. So every
failure mode of the stream — a lost completion, a dead prefetcher, a stale copy
— has to cost one download and nothing else. That is what most of this file
pins, because none of it fails loudly: a prefetch that silently misses
everything looks exactly like a prefetch that was never built.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder import cli_local_dist as D  # noqa: E402
from infinite_streaming_encoder import cli_phase as P  # noqa: E402
from infinite_streaming_encoder.chunking import variant_object_name  # noqa: E402


def _staged(root: Path, name: str, body: bytes = b"chunkchunk",
            recorded: int | None = None) -> Path:
    """Write `name` + its .done sidecar into root, as _upload_with_done's
    downloader would. `recorded` overrides the size the sidecar claims."""
    root.mkdir(parents=True, exist_ok=True)
    f = root / name
    f.write_bytes(body)
    (root / f"{name}.done").write_text(
        str(len(body) if recorded is None else recorded))
    return f


def _with_prefetch_dir(root: Path | None, fn):
    old = os.environ.get("ENCODER_PREFETCH_DIR")
    if root is None:
        os.environ.pop("ENCODER_PREFETCH_DIR", None)
    else:
        os.environ["ENCODER_PREFETCH_DIR"] = str(root)
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("ENCODER_PREFETCH_DIR", None)
        else:
            os.environ["ENCODER_PREFETCH_DIR"] = old


def test_the_object_name_is_derived_in_one_place() -> None:
    """The prefetch reads objects the variant phase wrote and the packaging
    phase expects — three processes that never talk to each other. A second
    speller of this name fails silently in BOTH directions: the prefetch caches
    nothing and the fetch downloads everything, which is precisely the
    before-state, so the only symptom is a saving that never arrives."""
    assert variant_object_name("h264", "1080p", 7) == "h264_1080p_chunk007.mp4"
    assert variant_object_name("h264", "1080p") == "h264_1080p.mp4"
    # Zero-padded to three, so a lexical listing is also chunk order.
    assert variant_object_name("av1", "720p", 12) == "av1_720p_chunk012.mp4"

    src = inspect.getsource(P)
    assert 'f"{args.codec}_{base}_chunk' not in src, (
        "cli_phase spells the chunk name itself again")
    assert "variant_object_name" in inspect.getsource(D), (
        "the orchestrator no longer derives names from the shared helper")


def test_a_verified_prefetch_is_used_instead_of_downloading(tmp: Path) -> None:
    pre, work = tmp / "pre", tmp / "work"
    name = variant_object_name("h264", "540p", 3)
    _staged(pre, name)
    work.mkdir(parents=True, exist_ok=True)

    assert _with_prefetch_dir(pre, lambda: P._link_prefetched(work / name))
    assert (work / name).read_bytes() == b"chunkchunk"
    assert (work / f"{name}.done").is_file(), (
        "the .done did not come across; the packaging phase's own size check "
        "has nothing to verify against")
    # Hardlink, not copy: both dirs are in the job's tmp, and a 3.5 GB ladder
    # must not exist twice on the way through.
    assert (work / name).stat().st_ino == (pre / name).stat().st_ino


def test_a_stale_prefetch_is_ignored(tmp: Path) -> None:
    """A chunk re-encoded after a spot reclaim has different bytes under the
    same key. Trusting presence would package the stale copy — the one outcome
    here that is WRONG rather than merely slow."""
    pre, work = tmp / "pre", tmp / "work"
    name = variant_object_name("h264", "540p", 4)
    _staged(pre, name, body=b"old", recorded=999)   # sidecar disagrees
    work.mkdir(parents=True, exist_ok=True)

    assert not _with_prefetch_dir(pre, lambda: P._link_prefetched(work / name))
    assert not (work / name).exists(), "a stale chunk was linked in anyway"


def test_a_half_written_prefetch_is_ignored(tmp: Path) -> None:
    """The prefetcher downloads the sidecar first and the object second, so an
    interrupted fetch leaves a sidecar whose size does not match — or no
    sidecar at all. Both read as a miss."""
    pre, work = tmp / "pre", tmp / "work"
    work.mkdir(parents=True, exist_ok=True)

    name = variant_object_name("h264", "432p", 0)
    (pre).mkdir(parents=True, exist_ok=True)
    (pre / name).write_bytes(b"partial")            # object, no sidecar
    assert not _with_prefetch_dir(pre, lambda: P._link_prefetched(work / name))

    other = variant_object_name("h264", "432p", 1)
    (pre / f"{other}.done").write_text("10")        # sidecar, no object
    assert not _with_prefetch_dir(pre, lambda: P._link_prefetched(work / other))


def test_no_prefetch_dir_is_the_old_behaviour(tmp: Path) -> None:
    """Unset env, missing directory, missing file — every one has to answer
    'not here' rather than raise, or a run without the prefetch fails at the
    step that used to just download."""
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    name = variant_object_name("hevc", "1080p", 9)

    assert not _with_prefetch_dir(None, lambda: P._link_prefetched(work / name))
    assert not _with_prefetch_dir(tmp / "nope", lambda: P._link_prefetched(work / name))
    assert not _with_prefetch_dir(tmp, lambda: P._link_prefetched(work / name))


def test_packaging_still_decides_what_it_needs_by_listing() -> None:
    """The stream is at-least-once and best-effort, so it can never be the
    inventory. package-all lists the staging prefix and checks every expected
    chunk explicitly — which is what makes a lost completion cost one download
    rather than a missing rung."""
    src = inspect.getsource(P.phase_package_all)
    assert "_list_prefix" in src or "list_objects" in src or "_s3_list" in src, (
        "package-all no longer lists the prefix; the prefetch would become the "
        "only record of what exists")
    assert "missing/incomplete under" in src, (
        "the per-chunk completeness check is gone — a chunk the prefetch missed "
        "and the fetch failed on would package silently short")


def test_the_egress_line_is_unchanged(tmp: Path) -> None:
    """cli_batch._PKG_FETCH_RE scans package-all's own fetch line to recover
    egress for a host-packaged run. Reword it and the run reports zero egress,
    which makes this optimisation look like a saving rather than a shift of
    when the bytes move. The prefetch reports on its OWN line."""
    import re
    src = inspect.getsource(P.phase_package_all)
    assert 'f"[phase package-all] fetched {total_objs} objects, "' in src, (
        "the fetch line changed shape; cli_batch's egress regex reads it")
    # And the regex still matches what that print produces.
    rendered = "[phase package-all] fetched 336 objects, 3535 MB in 41s (85.9 MB/s, 32 threads)"
    assert re.compile(
        r"\[phase package-all\] fetched (\d+) objects, ([\d.]+) MB").search(rendered)


def test_only_host_packaged_codecs_are_prefetched() -> None:
    """A codec packaged in a WORKER is fetched by that worker from inside the
    cluster. Pulling it here as well would move the whole ladder across the LAN
    for nothing — the exact waste host packaging was introduced to remove."""
    src = inspect.getsource(D.run_temporal)
    assert "if host_package and not args.no_prefetch:" in src, (
        "the prefetcher is no longer gated on host packaging")
    assert "set(host_package)" in src, (
        "the prefetcher is no longer scoped to the codecs packaged here")


def test_the_prefetch_dir_is_a_sibling_of_the_work_dir() -> None:
    """Two rules meet here. cli_phase rmtree's ENCODER_WORK_DIR on entry, so the
    prefetch cannot live inside it. And moveTmpToOutput renames EVERY top-level
    entry of the job's tmp dir into OUTPUT_DIR, so it must be removed before the
    run finishes — dot-prefixed does not exempt it."""
    src = inspect.getsource(D._ChunkPrefetcher.dir_for)
    assert '.prefetch-' in src, "the prefetch dir name changed"
    pkg = inspect.getsource(D._package_on_host)
    assert "shutil.rmtree(prefetch_dir" in pkg, (
        "the prefetch dir is not cleaned up; moveTmpToOutput would rename it "
        "into OUTPUT_DIR beside the encode")


def test_a_completion_is_only_forwarded_once() -> None:
    """History is re-read every second for the life of the run, so every
    completed chunk is 'new' on every poll. Forwarding each one every time would
    re-submit 336 downloads a second."""
    src = inspect.getsource(D._emit_temporal_progress)
    assert 'emitted.get(f"pf:{aid}")' in src, (
        "the prefetch hand-off is no longer deduped across polls")


def main() -> int:
    import tempfile
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        if "tmp" in inspect.signature(t).parameters:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        else:
            t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
