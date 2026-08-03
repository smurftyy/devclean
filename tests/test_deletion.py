"""Tests for the deletion backend (delete_items)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from devklean.cli.commands.clean import run_standard
from devklean.deletion import delete_items
from devklean.deletion.compression import CompressionVerificationError
from devklean.deletion.metadata import DeletionArchive, MetadataManager
from devklean.models import CleanableItem, DeleteFailure, DeleteResult


def test_delete_items_delegates_to_send2trash(tmp_path: Path, fake_trash) -> None:
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)
    (source / "package.json").write_text("{}")

    item = CleanableItem(str(source), "node_modules", 1024, "Node.js")

    manager = MetadataManager(storage_dir=tmp_path / "m")
    result = delete_items([item], 1024, metadata_manager=manager)

    assert fake_trash == [str(source)]  # send2trash called with the item path
    assert not source.exists()
    assert result.deleted == (str(source),)
    assert result.failed == ()
    assert result.total_size == 1024


def test_delete_items_compresses_directory_before_trashing(tmp_path: Path, fake_trash) -> None:
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)
    for index in range(64):
        (source / f"file-{index}.txt").write_text("A" * 2048, encoding="utf-8")
    original_size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

    item = CleanableItem(str(source), "node_modules", original_size, "Node.js")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    # compress_min_size=0 forces compression regardless of size, so the test
    # doesn't need to write a real 10 MiB+ fixture to cross the default gate.
    result = delete_items(
        [item], item.size, metadata_manager=manager, compress=True, compress_min_size=0
    )

    assert result.deleted == (str(source),)
    assert result.failed == ()
    assert not source.exists()  # removed only after the archive was confirmed trashed

    [trashed_path] = fake_trash
    archive = Path(trashed_path)
    assert archive.name.startswith(".node_modules-")
    assert archive.name.endswith(".tar.gz")
    assert not archive.exists()  # fake_trash removed it, same as a real send2trash call would

    records = manager.load_records()
    (stored,) = records.records
    archive_record = stored.record.archive
    assert archive_record is not None
    assert archive_record.format == "gzip"
    assert archive_record.compressed is True
    assert archive_record.original_size == original_size
    assert archive_record.compressed_size is not None
    assert archive_record.compressed_size < original_size


def test_delete_items_skips_compression_below_min_size(tmp_path: Path, fake_trash) -> None:
    source = tmp_path / "workspace" / ".cache"
    source.mkdir(parents=True)
    (source / "small.txt").write_text("hi", encoding="utf-8")

    item = CleanableItem(str(source), ".cache", 2, "Cache")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    result = delete_items(
        [item],
        item.size,
        metadata_manager=manager,
        compress=True,
        compress_min_size=10 * 1024 * 1024,
    )

    assert result.deleted == (str(source),)
    assert fake_trash == [str(source)]  # trashed as-is, never compressed
    records = manager.load_records()
    (stored,) = records.records
    assert stored.record.archive is None


def test_delete_items_leaves_original_intact_when_verification_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Reproduces the PR #12 failure mode: a failure partway through the
    compress-before-trash sequence must never delete the source. PR #12's bug
    was calling shutil.rmtree(source) unconditionally right after building the
    archive, with no verification and before send2trash ran at all — so a
    later send2trash failure meant the data was already gone. This asserts
    the fix: verification (or send2trash) failing leaves the source directory
    completely untouched, with no partial deletion."""
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("A" * 2048, encoding="utf-8")
    (source / "b.txt").write_text("B" * 2048, encoding="utf-8")
    original_size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

    def _boom(result) -> None:
        raise CompressionVerificationError("simulated corruption detected mid-verification")

    monkeypatch.setattr("devklean.deletion.trash.verify_archive", _boom)

    item = CleanableItem(str(source), "node_modules", original_size, "Node.js")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    result = delete_items(
        [item], item.size, metadata_manager=manager, compress=True, compress_min_size=0
    )

    assert result.deleted == ()
    assert result.failed[0].path == str(source)
    assert source.exists()
    assert (source / "a.txt").exists()
    assert (source / "b.txt").exists()
    # the failed temp archive must not be left behind either
    assert list((tmp_path / "workspace").glob(".node_modules-*")) == []
    assert manager.load_records().records == ()  # nothing recorded for a failed item


def test_delete_items_leaves_original_intact_when_send2trash_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Same failure-mode reproduction, one step later: the archive verifies
    fine but send2trash itself fails (disk full, permission denied, ...).
    The original directory must still be completely untouched — it is only
    ever removed *after* send2trash confirms the archive made it to trash."""
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("A" * 2048, encoding="utf-8")
    original_size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

    def _boom(path) -> None:
        raise OSError("simulated send2trash failure")

    monkeypatch.setattr("devklean.deletion.trash.send2trash", _boom)

    item = CleanableItem(str(source), "node_modules", original_size, "Node.js")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    result = delete_items(
        [item], item.size, metadata_manager=manager, compress=True, compress_min_size=0
    )

    assert result.deleted == ()
    assert result.failed[0].path == str(source)
    assert source.exists()
    assert (source / "a.txt").exists()
    assert list((tmp_path / "workspace").glob(".node_modules-*")) == []


def test_delete_items_surfaces_error_when_original_removal_fails_after_trash(
    tmp_path: Path, fake_trash, monkeypatch
) -> None:
    """One step later still: send2trash of the archive succeeds (the data is
    genuinely safe), but the final shutil.rmtree(source) fails (e.g. a file
    inside is briefly locked). This must not be swallowed or reported as a
    plain success — it's a distinct outcome (archive safely trashed, original
    left behind) and the caller/CLI must be told plainly, not silently."""
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("A" * 2048, encoding="utf-8")
    original_size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

    def _boom(path) -> None:
        raise OSError("simulated: directory busy")

    monkeypatch.setattr("shutil.rmtree", _boom)

    item = CleanableItem(str(source), "node_modules", original_size, "Node.js")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    result = delete_items(
        [item], item.size, metadata_manager=manager, compress=True, compress_min_size=0
    )

    # Not a silent success: the item is a reported failure, not a deletion.
    assert result.deleted == ()
    assert len(result.failed) == 1
    assert result.failed[0].path == str(source)

    error = result.failed[0].error
    # Distinct from an ordinary failure: says the archive made it to trash...
    assert "compressed archive was trashed" in error
    # ...names the actual reason (proves {exc} was interpolated, not hardcoded)...
    assert "simulated: directory busy" in error
    # ...and tells the user what to do about it.
    assert "remove it manually" in error

    # Distinct from a verify/send2trash failure: the archive really did make
    # it to (fake) trash and must not be rolled back — it's the only
    # recoverable copy left once the original couldn't be removed.
    assert len(fake_trash) == 1
    assert fake_trash[0].endswith(".tar.gz")

    # The original is genuinely still there — nothing was silently lost.
    assert source.exists()
    assert (source / "a.txt").exists()

    # A failed item is never recorded as a successful deletion.
    assert manager.load_records().records == ()


def test_delete_items_compress_path_calls_happen_in_safety_order(
    tmp_path: Path, fake_trash, monkeypatch
) -> None:
    """Pins the exact call order of the compress-before-trash contract:
    compress, then verify, then send2trash, then (only now) remove the
    original. The other tests above prove the *symptom* (a failure at each
    step leaves the source alone); this one asserts the *cause* directly, so
    a refactor that reorders these four calls fails immediately here even if
    it doesn't happen to break any single failure-injection test. This is
    precisely the invariant PR #12 violated: it called rmtree(source) before
    send2trash ran at all.
    """
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("A" * 2048, encoding="utf-8")
    original_size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

    calls: list[str] = []

    import devklean.deletion.trash as trash_module

    real_compress_path = trash_module.compress_path
    real_verify_archive = trash_module.verify_archive
    real_send2trash = trash_module.send2trash
    real_rmtree = trash_module.shutil.rmtree

    def _compress_path(*args, **kwargs):
        calls.append("compress")
        return real_compress_path(*args, **kwargs)

    def _verify_archive(*args, **kwargs):
        calls.append("verify")
        return real_verify_archive(*args, **kwargs)

    def _send2trash(*args, **kwargs):
        calls.append("send2trash")
        return real_send2trash(*args, **kwargs)

    def _rmtree(*args, **kwargs):
        calls.append("rmtree")
        return real_rmtree(*args, **kwargs)

    monkeypatch.setattr(trash_module, "compress_path", _compress_path)
    monkeypatch.setattr(trash_module, "verify_archive", _verify_archive)
    monkeypatch.setattr(trash_module, "send2trash", _send2trash)
    monkeypatch.setattr(trash_module.shutil, "rmtree", _rmtree)

    item = CleanableItem(str(source), "node_modules", original_size, "Node.js")
    manager = MetadataManager(storage_dir=tmp_path / "m")

    result = delete_items(
        [item], item.size, metadata_manager=manager, compress=True, compress_min_size=0
    )

    assert result.deleted == (str(source),)
    assert calls == ["compress", "verify", "send2trash", "rmtree"]


def test_delete_items_does_not_call_send2trash_on_dry_run(tmp_path: Path, fake_trash) -> None:
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)

    item = CleanableItem(str(source), "node_modules", 1024, "Node.js")

    result = delete_items([item], 1024, dry_run=True)

    assert fake_trash == []  # structural guard: no trashing under dry-run
    assert source.exists()
    assert result.deleted == (str(source),)


def test_delete_items_translates_failure_into_delete_failure(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "workspace" / "node_modules"
    source.mkdir(parents=True)

    def _boom(path) -> None:
        raise PermissionError("trash denied")

    monkeypatch.setattr("devklean.deletion.trash.send2trash", _boom)

    item = CleanableItem(str(source), "node_modules", 10, "Node.js")
    result = delete_items([item], 10)

    assert result.deleted == ()
    assert len(result.failed) == 1
    assert result.failed[0].path == str(source)
    assert "trash denied" in result.failed[0].error


@dataclass
class _RecordingRenderer:
    summary_calls: int = 0
    prompt_count: int | None = None
    aborted_calls: int = 0
    dry_run_calls: int = 0
    result: DeleteResult | None = None

    def scan_start(self, root: str) -> None:
        raise AssertionError("scan_start should not be called")

    def scan_summary(self, items: list[CleanableItem]) -> int:
        self.summary_calls += 1
        return sum(item.size for item in items)

    def nothing_to_clean(self) -> None:
        raise AssertionError("nothing_to_clean should not be called")

    def dry_run_nothing_deleted(self) -> None:
        self.dry_run_calls += 1

    def dry_run_selected(self, count: int) -> None:
        self.dry_run_calls += 1

    def aborted(self) -> None:
        self.aborted_calls += 1

    def no_items_selected(self) -> None:
        raise AssertionError("no_items_selected should not be called")

    def invalid_directory(self, path: str) -> None:
        raise AssertionError("invalid_directory should not be called")

    def confirm_prompt(self, count: int, total_size: int = 0) -> str:
        self.prompt_count = count
        return "delete?"

    def deletion_result(self, result: DeleteResult) -> None:
        self.result = result


def _fake_delete_recorder(calls: list[bool]):
    """A stand-in for delete_items that records that it was invoked."""

    def _fake(items, total_size, **kwargs) -> DeleteResult:
        calls.append(True)
        return DeleteResult(
            deleted=tuple(item.path for item in items),
            failed=(),
            total_size=total_size,
        )

    return _fake


def test_run_standard_honors_dry_run_and_skips_backend(monkeypatch) -> None:
    renderer = _RecordingRenderer()
    calls: list[bool] = []
    monkeypatch.setattr("devklean.cli.commands.clean.delete_items", _fake_delete_recorder(calls))
    items = [CleanableItem("/tmp/node_modules", "node_modules", 100, "Node.js")]

    def fail_input(*args, **kwargs):
        raise AssertionError("input should not be called during dry-run")

    monkeypatch.setattr("builtins.input", fail_input)

    run_standard(renderer, items, True)

    assert renderer.summary_calls == 1
    assert renderer.dry_run_calls == 1
    assert calls == []  # delete_items never invoked under dry-run
    assert renderer.result is None
    assert renderer.prompt_count is None


def test_run_standard_invokes_delete_items(monkeypatch) -> None:
    renderer = _RecordingRenderer()
    calls: list[bool] = []
    monkeypatch.setattr("devklean.cli.commands.clean.delete_items", _fake_delete_recorder(calls))
    items = [CleanableItem("/tmp/node_modules", "node_modules", 100, "Node.js")]

    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    run_standard(renderer, items, False)

    assert renderer.summary_calls == 1
    assert renderer.prompt_count == 1
    assert calls == [True]  # delete_items invoked once
    assert renderer.result is not None
    assert renderer.result.deleted == ("/tmp/node_modules",)


def test_delete_items_records_only_successes(tmp_path: Path, monkeypatch) -> None:
    storage_dir = tmp_path / "metadata"
    manager = MetadataManager(storage_dir=storage_dir)
    items = [
        CleanableItem("/tmp/a", "a", 10, "A"),
        CleanableItem("/tmp/b", "b", 20, "B"),
    ]

    # /tmp/b fails to trash; only the successful /tmp/a should be recorded.
    def _selective(path) -> None:
        if path == "/tmp/b":
            raise PermissionError("denied")

    monkeypatch.setattr("devklean.deletion.trash.send2trash", _selective)

    result = delete_items(items, 30, metadata_manager=manager)

    records = sorted(storage_dir.glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))

    assert result.deleted == ("/tmp/a",)
    assert payload["schema_version"] == 5
    assert payload["deletion"]["strategy"] == "trash"
    assert isinstance(payload["deletion"]["run_id"], str) and payload["deletion"]["run_id"]
    assert payload["item"]["original_path"] == "/tmp/a"
    assert payload["item"]["display_name"] == "A"
    assert payload["item"]["size"] == 10


def test_metadata_manager_records_archive_details(tmp_path: Path) -> None:
    storage_dir = tmp_path / "metadata"
    manager = MetadataManager(storage_dir=storage_dir)
    item = CleanableItem("/tmp/a", "a", 10, "A")
    result = DeleteResult(deleted=("/tmp/a",), failed=(), total_size=10)

    manager.record_successes(
        [item],
        result,
        "trash",
        archives={"/tmp/a": DeletionArchive(path="/tmp/a.zip", format="zip")},
    )

    records = sorted(storage_dir.glob("*.json"))
    payload = json.loads(records[0].read_text(encoding="utf-8"))

    assert payload["schema_version"] == 5
    assert payload["archive"] == {"path": "/tmp/a.zip", "format": "zip", "compressed": True}


def test_metadata_manager_skips_failed_deletions(tmp_path: Path) -> None:
    storage_dir = tmp_path / "metadata"
    manager = MetadataManager(storage_dir=storage_dir)
    items = [
        CleanableItem("/tmp/a", "a", 10, "A"),
        CleanableItem("/tmp/b", "b", 20, "B"),
    ]
    result = DeleteResult(
        deleted=("/tmp/a",),
        failed=(DeleteFailure(path="/tmp/b", error="permission denied"),),
        total_size=30,
    )

    manager.record_successes(items, result, "trash")

    records = sorted(storage_dir.glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["item"]["original_path"] == "/tmp/a"


def test_record_successes_shares_one_run_id_across_a_batch(tmp_path: Path) -> None:
    storage_dir = tmp_path / "metadata"
    manager = MetadataManager(storage_dir=storage_dir)
    items = [
        CleanableItem("/tmp/a", "a", 10, "A"),
        CleanableItem("/tmp/b", "b", 20, "B"),
    ]
    result = DeleteResult(deleted=("/tmp/a", "/tmp/b"), failed=(), total_size=30)

    manager.record_successes(items, result, "trash")

    snapshot = manager.load_records()
    run_ids = {entry.record.run_id for entry in snapshot.records}
    timestamps = {entry.record.timestamp for entry in snapshot.records}
    assert len(snapshot.records) == 2
    assert len(run_ids) == 1 and None not in run_ids
    assert len(timestamps) == 1


def test_load_records_parses_legacy_v2_file_without_run_id(tmp_path: Path) -> None:
    storage_dir = tmp_path / "metadata"
    storage_dir.mkdir()
    legacy = {
        "schema_version": 2,
        "deletion": {
            "id": "legacy1",
            "timestamp": "2026-06-01T12:00:00+00:00",
            "strategy": "trash",
        },
        "item": {"original_path": "/tmp/old", "display_name": "Old", "size": 42},
    }
    (storage_dir / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")

    snapshot = MetadataManager(storage_dir=storage_dir).load_records()

    assert snapshot.invalid_count == 0
    assert len(snapshot.records) == 1
    assert snapshot.records[0].record.run_id is None
