"""Real-interruption tests for the compress-before-trash safety contract.

Every other test in ``test_deletion.py`` and ``test_compression.py`` simulates
a mid-operation failure by having ``monkeypatch`` make some step *raise*. That
is a good proxy for most failures (permission errors, ``OSError``, a
corrupt-archive detection), but it is not the same thing as a process getting
killed: a raised Python exception still runs any ``except``/``finally`` blocks
around it, while a crash, a SIGKILL, or the OOM killer stops execution dead at
whatever instruction it's on — no cleanup code downstream of that point ever
runs.

These tests use ``os.fork()`` + ``os._exit()`` (which, like a real kill,
skips exception handlers, ``finally`` blocks, and atexit hooks) to check the
same safety invariant under that harsher, more realistic form of
interruption: the source directory must never be touched until a verified
archive has already been confirmed safe in the trash.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path

import pytest

from devklean.deletion import delete_items
from devklean.deletion.metadata import MetadataManager
from devklean.models import CleanableItem

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="os.fork is only available on POSIX platforms"
)


def _make_source(tmp_path: Path) -> tuple[Path, int]:
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("A" * 4096, encoding="utf-8")
    (source / "b.txt").write_text("B" * 4096, encoding="utf-8")
    size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())
    return source, size


def _run_in_child(child_body) -> int:
    """Fork, run ``child_body`` in the child, and return its exit status.

    ``child_body`` must end the child process itself (typically by letting
    the deliberately-triggered ``os._exit`` fire); as a safety net, a child
    that returns normally without being "killed" exits with code 99 so a
    broken test setup fails loudly instead of hanging or passing by
    accident.
    """
    pid = os.fork()
    if pid == 0:
        try:
            child_body()
        finally:
            os._exit(99)
    _, status = os.waitpid(pid, 0)
    return status


def test_kill_mid_archive_write_never_touches_source(tmp_path: Path) -> None:
    """A kill landing while the archive is still being built must leave the
    source completely intact — the source is never read-and-deleted, only
    ever read (for the archive) and left alone until much later.
    """
    source, size = _make_source(tmp_path)
    item = CleanableItem(str(source), "node_modules", size, "Node.js")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    def _child() -> None:
        # Kill the process at the first byte tarfile tries to write for the
        # source tree -- i.e. the earliest possible moment inside the
        # archive-build step, well before any verification or trashing.
        def _killed(*_args, **_kwargs):
            os._exit(37)

        tarfile.TarFile.add = _killed
        delete_items(
            [item], item.size, metadata_manager=manager, compress=True, compress_min_size=0
        )

    status = _run_in_child(_child)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 37, "child never reached the archive-write step"

    # The source is untouched: compress_path only ever reads it while
    # building the archive, never deletes anything from it.
    assert source.exists()
    assert (source / "a.txt").read_text(encoding="utf-8") == "A" * 4096
    assert (source / "b.txt").read_text(encoding="utf-8") == "B" * 4096

    # A stray partial temp archive may be left behind -- a real kill skips
    # the `except: archive_path.unlink()` cleanup in compress_path, unlike
    # the monkeypatch-raise tests in test_compression.py. That's expected
    # and harmless (it's disk litter, not data loss); nothing here asserts
    # it's absent.


def test_kill_between_trash_confirmation_and_original_removal_loses_nothing(
    tmp_path: Path,
) -> None:
    """A kill landing exactly when the original would start being removed --
    i.e. *after* the archive has already been verified and confirmed safe in
    the trash -- must never lose data overall. The live source directory may
    end up partially removed (the kill can land mid-``rmtree``), but the
    archive already sitting in the trash is a complete, verified copy, so
    nothing is actually lost.

    This is the exact ordering PR #12 got backwards: it ran ``rmtree(source)``
    unconditionally right after building the archive, before ``send2trash``
    even ran. Here ``send2trash`` has already succeeded by construction
    before the kill fires, which is the fixed, safe order.
    """
    source, size = _make_source(tmp_path)
    item = CleanableItem(str(source), "node_modules", size, "Node.js")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()

    def _child() -> None:
        import devklean.deletion.trash as trash_module

        # A "real" trash: actually relocate the archive to trash_dir (rather
        # than deleting it, like the fake_trash fixture does) so the parent
        # process can inspect the surviving bytes after the kill and prove
        # the data really is still there.
        def _real_move_to_trash(path) -> None:
            shutil.move(os.fspath(path), trash_dir / Path(path).name)

        trash_module.send2trash = _real_move_to_trash

        # Kill after one file has been removed from the source -- i.e. mid
        # rmtree, having already deleted part of the original -- to prove a
        # *partial* removal still isn't data loss, since the archive was
        # already relocated to the trash before rmtree ever started.
        def _killed_mid_rmtree(path, *_args, **_kwargs):
            first_child = next(Path(path).iterdir())
            first_child.unlink()
            os._exit(37)

        trash_module.shutil.rmtree = _killed_mid_rmtree

        delete_items(
            [item], item.size, metadata_manager=manager, compress=True, compress_min_size=0
        )

    status = _run_in_child(_child)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 37, "child never reached the original-removal step"

    # The live source is now (as simulated) partially removed -- exactly one
    # file gone, the rest still sitting there. That's an acceptable outcome,
    # not the invariant under test.
    remaining = sorted(p.name for p in source.iterdir())
    assert len(remaining) == 1

    # What actually matters: the archive made it to "trash" *before* the
    # kill (send2trash returned, or _killed_mid_rmtree could never have
    # run), and it is a complete, byte-correct copy -- so the union of
    # {trashed archive} recovers everything, even though the live directory
    # is now partial.
    [archived] = list(trash_dir.glob("*.tar.gz"))
    with tarfile.open(archived, mode="r:gz") as tar:
        names = {Path(member.name).name for member in tar.getmembers() if member.isreg()}
        assert names == {"a.txt", "b.txt"}
        for member in tar.getmembers():
            if member.isreg() and Path(member.name).name == "a.txt":
                extracted = tar.extractfile(member)
                assert extracted is not None
                assert extracted.read().decode("utf-8") == "A" * 4096
