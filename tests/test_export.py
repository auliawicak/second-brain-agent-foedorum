"""Phase 3 — data hygiene unit tests (retention, vault export, GCS backup)."""

from __future__ import annotations

import gzip
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services.export import (
    _slugify,
    is_first_sunday,
    run_backup,
    run_markdown_export,
    run_retention,
)
from config import Config
from storage.database import Database
from storage.models import PreferenceCreate, TaskCreate


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


def _epoch_dt(days_ago: int) -> datetime:
    return datetime.now(ZoneInfo("Asia/Jakarta")) - timedelta(days=days_ago)


async def _seed_conversations(db: Database, n: int, days_ago: int) -> None:
    cut = _epoch_dt(days_ago)
    await db.db.executemany(
        "INSERT INTO conversations (role, content, timestamp) VALUES (?, ?, ?)",
        [
            ("user" if i % 2 == 0 else "assistant", f"msg {i}", cut.isoformat())
            for i in range(n)
        ],
    )
    await db.db.commit()


# ─── 3.1 retention ──────────────────────────────────────────────────────────


async def test_retention_exports_and_prunes_10000(tmp_path: Path, db: Database) -> None:
    await _seed_conversations(db, 10_000, days_ago=400)
    await _seed_conversations(db, 100, days_ago=10)

    stats = await run_retention(
        db, export_dir=tmp_path / "export", retention_days=60,
        now=_epoch_dt(0),
    )

    assert stats["exported"] == 10_000
    assert stats["deleted"] == 10_000
    assert stats["months"] == 1

    # Files live directly in the passed export dir (retention's "conversations" dir).
    files = list((tmp_path / "export").glob("*.md"))
    assert len(files) == 1
    exported_month = _epoch_dt(400).strftime("%Y-%m")
    assert files[0].name == f"{exported_month}.md"
    assert len(files[0].read_text().split("## ")) - 1 == 10_000

    remaining = await db.get_all_conversations()
    assert len(remaining) == 100                      # recent kept
    assert all(r.timestamp >= (datetime.now(ZoneInfo("Asia/Jakarta")) - timedelta(days=60)).isoformat() for r in remaining)

    # Second run is a no-op (already exported + deleted).
    stats2 = await run_retention(
        db, export_dir=tmp_path / "export", retention_days=60, now=_epoch_dt(0)
    )
    assert stats2["exported"] == 0 and stats2["deleted"] == 0


async def test_retention_never_touches_non_conversations(tmp_path: Path, db: Database) -> None:
    await _seed_conversations(db, 5, days_ago=400)
    task = await db.add_task(TaskCreate(description="keep"))
    note = await db.add_note(type("N", (), {"content": "keep", "tags": [], "category": "general"})())
    await db.save_preference(PreferenceCreate(key="k", value="v"))
    await db.add_correction(trigger="explicit", user_message="x", correction="y")

    await run_retention(db, export_dir=tmp_path / "export", retention_days=60, now=_epoch_dt(0))

    assert len(await db.get_tasks()) == 1
    assert len(await db.get_all_notes()) == 1
    assert len(await db.get_all_preferences()) == 1
    assert await db.get_correction_counts(days=365) == 1


async def test_retention_vacuums_on_first_sunday(tmp_path: Path, db: Database) -> None:
    await _seed_conversations(db, 3, days_ago=400)
    first_sun = datetime(2026, 2, 1, 3, 0, tzinfo=ZoneInfo("Asia/Jakarta"))  # 2026-02-01 is a Sunday
    assert first_sun.weekday() == 6
    stats = await run_retention(
        db, export_dir=tmp_path / "export", retention_days=60, now=first_sun
    )
    assert stats["vacuumed"] is True

    stats2 = await run_retention(
        db, export_dir=tmp_path / "export", retention_days=60, now=first_sun
    )
    assert stats2["vacuumed"] is True  # VACUUM is idempotent


def test_is_first_sunday() -> None:
    assert is_first_sunday(datetime(2026, 2, 1, 3, 0))
    assert not is_first_sunday(datetime(2026, 2, 8, 3, 0))
    assert not is_first_sunday(datetime(2026, 2, 1, 3, 0) + timedelta(days=1))


# ─── 3.2 markdown vault export ──────────────────────────────────────────────


async def test_markdown_export_vault_obsidian_shape(tmp_path: Path, db: Database) -> None:
    note = await db.add_note(type("N", (), {"content": "Big Idea: garden shed", "tags": ["work", "idea"], "category": "personal"})())
    task = await db.add_task(TaskCreate(description="buy seeds", priority="high", category="personal"))
    await db.complete_task(task.id)

    stats = await run_markdown_export(db, export_dir=tmp_path / "export")
    assert stats["notes"] == 1

    note_file = next((tmp_path / "export" / "notes").glob("*.md"))
    text = note_file.read_text()
    assert text.startswith("---")
    assert "id:" in text
    assert "- work" in text
    assert "category: personal" in text
    assert "garden shed" in text

    task_file = next((tmp_path / "export" / "tasks").glob("*.md"))
    task_text = task_file.read_text()
    assert "buy seeds" in task_text
    assert "**status:** done" in task_text
    assert "**priority:** high" in task_text


async def test_markdown_export_is_incremental(tmp_path: Path, db: Database) -> None:
    for i in range(3):
        await db.add_note(type("N", (), {"content": f"note {i}", "tags": [], "category": "x"})())
    t1 = await db.add_task(TaskCreate(description="first task"))

    s1 = await run_markdown_export(db, export_dir=tmp_path / "export")
    assert s1["notes"] == 3

    note_files = sorted((tmp_path / "export" / "notes").glob("*.md"))
    mtimes = {p.stat().st_mtime for p in note_files}
    task_file = next((tmp_path / "export" / "tasks").glob("*.md"))
    task_mtime = task_file.stat().st_mtime

    s2 = await run_markdown_export(db, export_dir=tmp_path / "export")
    assert s2["notes"] == 0 and s2["notes_skipped"] == 3
    assert {p.stat().st_mtime for p in (tmp_path / "export" / "notes").glob("*.md")} == mtimes
    assert task_file.stat().st_mtime == task_mtime  # unmodified task month not rewritten

    # Completing a task changes its month fingerprint → file rewrites.
    await db.complete_task(t1.id)
    s3 = await run_markdown_export(db, export_dir=tmp_path / "export")
    assert s3["tasks_files"] == 1
    assert "**status:** done" in task_file.read_text()


def test_slugify() -> None:
    assert _slugify("Buy Milk & Eggs!") == "buy-milk-eggs"
    assert _slugify("---+++") == "note"


# ─── 3.3 GCS backup ─────────────────────────────────────────────────────────


async def test_backup_skips_when_bucket_unset(db: Database, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("gcloud must not run when bucket is unset")

    monkeypatch.setattr("services.export.subprocess.run", boom)
    result = await run_backup(db, bucket="")
    assert result == {"skipped": True}


async def test_backup_uploads_and_cleans(db: Database, tmp_path: Path, monkeypatch) -> None:
    await _seed_conversations(db, 3, days_ago=5)
    export_dir = tmp_path / "export"
    (export_dir / "notes").mkdir(parents=True)
    (export_dir / "notes" / "x.md").write_text("hello", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr("services.export.subprocess.run", fake_run)
    monkeypatch.setattr("services.export.shutil.which", lambda _name: "/usr/bin/env")

    result = await run_backup(
        db, bucket="sb-test-bucket", export_dir=export_dir, date_suffix="2026-09-06"
    )
    assert result["skipped"] is False

    cmd_text = " ".join(" ".join(c) for c in calls)
    assert "gs://sb-test-bucket/db/2026-09-06.gz" in cmd_text
    assert "gs://sb-test-bucket/export/2026-09-06.tar.gz" in cmd_text

    # No temp artifacts left behind.
    tmp_backup = Path(Config.PROJECT_DIR) / "data" / "tmp_backup"
    assert tmp_backup.exists()
    assert not list(tmp_backup.iterdir())


def test_hot_copy_produces_valid_db(db: Database, tmp_path: Path) -> None:
    import services.export as ex

    db2 = Database(str(tmp_path / "copy.db"))
    ex._hot_copy_db(db.db_path, Path(db2.db_path))
    # Opening the copy (no migrations — fresh file) + a rough table check.
    con = sqlite3.connect(str(db2.db_path))
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
    )]
    con.close()
    assert "conversations" in tables