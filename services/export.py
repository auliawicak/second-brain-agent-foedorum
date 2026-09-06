"""Phase 3 — data hygiene: retention pruning, Markdown vault export, GCS backup.

- `run_retention` (03:00): exports conversation rows older than
  `RETENTION_DAYS` to `data/export/conversations/YYYY-MM.md`, deletes them,
  runs `PRAGMA wal_checkpoint(TRUNCATE)`, and `VACUUM`s on the first Sunday.
- `run_markdown_export` (03:15): writes an Obsidian-compatible vault of
  notes and tasks. Incremental — only files whose source rows changed.
- `run_backup` (03:30): hot-copies the SQLite DB and tars the export vault
  into `BACKUP_BUCKET`. Skipped cleanly when the bucket is unset.

Tasks, notes, preferences and corrections are NEVER deleted — only
conversation transcripts, and only after they have been exported.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from config import Config
from storage.database import Database
from storage.models import ConversationEntry, Note, Task

logger = logging.getLogger(__name__)


# ─── Shared helpers ────────────────────────────────────────────────────────


def _month_key(ts_iso: str) -> str:
    """Return the YYYY-MM bucket for a timestamp."""
    return (ts_iso or "")[:7]


def is_first_sunday(dt: datetime) -> bool:
    """First Sunday of the month (when the monthly VACUUM runs)."""
    return dt.day <= 7 and dt.weekday() == 6


def _slugify(content: str) -> str:
    """Turn note content into an Obsidian-safe filename slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", content.lower()).strip("-")
    words = [w for w in s.split("-") if w][:8]
    return "-".join(words)[:60] or "note"


def _frontmatter(fields: dict) -> str:
    """Render Obsidian-compatible YAML frontmatter."""
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ─── Conversation files ────────────────────────────────────────────────────


def _conversation_month_md(entries: list[ConversationEntry], month: str) -> str:
    parts = [_frontmatter({"title": f"Conversations {month}", "month": month}), ""]
    for e in entries:
        stamp = (e.timestamp or "").replace("T", " ")
        parts.append(f"## {stamp} ({e.role})")
        parts.append(e.content.strip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ─── 3.1 Retention job ─────────────────────────────────────────────────────


async def run_retention(
    db: Database,
    *,
    export_dir: Path | None = None,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Export and prune conversation rows older than `retention_days`.

    Never touches non-conversation tables. Returns stats for logging.
    """
    export_dir = _ensure_dir(export_dir or (Config.EXPORT_DIR / "conversations"))
    retention_days = retention_days if retention_days is not None else Config.RETENTION_DAYS
    now = now or datetime.now(Config.TIMEZONE)
    cutoff = now - timedelta(days=retention_days)
    cutoff_iso = cutoff.isoformat()

    stats = {"exported": 0, "months": 0, "deleted": 0, "vacuumed": False}

    all_rows = await db.get_all_conversations()
    by_month: dict[str, list[ConversationEntry]] = defaultdict(list)
    for row in all_rows:
        by_month[_month_key(row.timestamp)].append(row)

    old_months = sorted({_month_key(r.timestamp) for r in all_rows if r.timestamp < cutoff_iso})
    for month in old_months:
        body = _conversation_month_md(by_month.get(month, []), month)
        (export_dir / f"{month}.md").write_text(body, encoding="utf-8")
    stats["months"] = len(old_months)
    stats["exported"] = sum(len(by_month.get(m, [])) for m in old_months)

    stats["deleted"] = await db.delete_conversations_before(cutoff_iso)
    await db.checkpoint_truncate()

    if is_first_sunday(now):
        await db.vacuum()
        stats["vacuumed"] = True

    logger.info(
        "Retention: exported %d rows across %d month file(s), deleted %d rows%s",
        stats["exported"], stats["months"], stats["deleted"],
        ", vacuumed" if stats["vacuumed"] else "",
    )
    return stats


# ─── 3.2 Markdown vault export ──────────────────────────────────────────────


def _note_filename(note: Note) -> str:
    date = (note.created_at or "")[:10]
    return f"{date}-{_slugify(note.content)}.md"


def _note_frontmatter(note: Note) -> str:
    return _frontmatter(
        {
            "id": note.id or 0,
            "tags": note.tags or [],
            "category": note.category or "general",
            "created": note.created_at or "",
        }
    )


def _file_has_id(path: Path, note_id: int) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return f"id: {note_id}" in text


def _task_month_md(tasks: list[Task], month: str) -> str:
    parts = [
        _frontmatter(
            {
                "title": f"Tasks {month}",
                "month": month,
                "fingerprint": _task_fingerprint(tasks),
            }
        ),
        "",
    ]
    for t in tasks:
        status_icon = {"pending": "⬜", "in_progress": "🔄", "done": "✅", "archived": "📦"}.get(
            t.status.value, "❓"
        )
        due = f" (due: {t.due_date})" if t.due_date else ""
        lines = ["", f"{status_icon} **{t.description}**{due}"]
        lines.append(f"- **#:** {t.id} | **priority:** {t.priority.value} | **category:** {t.category}")
        lines.append(f"- **status:** {t.status.value} | **created:** {t.created_at}")
        if t.completed_at:
            lines.append(f"- **completed:** {t.completed_at}")
        lines.append("")
        parts.extend(lines)
    return "\n".join(parts).rstrip() + "\n"


def _task_fingerprint(tasks: list[Task]) -> str:
    """Latest created/completed stamp (iso) across a month's tasks.

    Stored in the file's frontmatter so changes (e.g. a task completing)
    rewrite the month file and leave untouched months alone.
    """
    return max(
        (t.completed_at or t.created_at or "" for t in tasks),
        default="",
    )


def _file_fingerprint(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("fingerprint:") and len(line) > 13:
                return line[13:].strip()
    except OSError:
        pass
    return ""


async def run_markdown_export(
    db: Database,
    *,
    export_dir: Path | None = None,
) -> dict:
    """Write the Obsidian vault: notes/YYYY-MM-DD-<slug>.md and tasks/YYYY-MM.md.

    Incremental: notes are skipped when their target file already carries the
    same note id; task month files rewrite only when their frontmatter
    fingerprint (latest created/completed stamp in the month) changes.
    """
    base = Path(export_dir or Config.EXPORT_DIR)
    notes_dir = _ensure_dir(base / "notes")
    tasks_dir = _ensure_dir(base / "tasks")

    stats = {"notes": 0, "notes_skipped": 0, "tasks_files": 0, "tasks_skipped": 0}

    tasks_by_month: dict[str, list[Task]] = defaultdict(list)
    for note in await db.get_all_notes():
        path = notes_dir / _note_filename(note)
        if path.exists() and _file_has_id(path, note.id or -1):
            stats["notes_skipped"] += 1
            continue
        path.write_text(
            f"{_note_frontmatter(note)}\n\n{note.content.strip()}\n",
            encoding="utf-8",
        )
        stats["notes"] += 1

    for task in await db.get_all_tasks():
        tasks_by_month[_month_key(task.created_at)].append(task)

    for month, tasks in sorted(tasks_by_month.items()):
        path = tasks_dir / f"{month}.md"
        fingerprint = _task_fingerprint(tasks)
        if path.exists() and _file_fingerprint(path) == fingerprint and fingerprint:
            stats["tasks_skipped"] += 1
            continue
        path.write_text(_task_month_md(tasks, month), encoding="utf-8")
        stats["tasks_files"] += 1

    # Phase 6 §6.9 — learning-loop artifacts (incremental, fingerprint-gated).
    pstats = await _export_preferences(db, base)
    vstats = await _export_persona_versions(db, base)
    stats.update(await _export_consolidation_log(db, base))
    stats.update(pstats)
    stats.update(vstats)

    logger.info(
        "Markdown export: %d note(s) written, %d skipped; %d task file(s) written, %d skipped",
        stats["notes"], stats["notes_skipped"],
        stats["tasks_files"], stats["tasks_skipped"],
    )
    return stats


# ─── Phase 6 §6.9 — learning-loop vault artifacts ─────────────────────────────


def _preferences_fingerprint(prefs: list) -> str:
    return ",".join(
        f"{p.id}:{p.last_seen or ''}:{p.confidence:.2f}:{p.evidence_count}"
        for p in prefs
    )


async def _export_preferences(db: Database, base: Path) -> dict:
    """Write _meta/preferences.md with the full preference history."""
    meta_dir = _ensure_dir(base / "_meta")
    prefs = await db.get_all_preferences()
    if not prefs:
        return {"prefs_files": 0, "prefs_skipped": 0}

    live_core = [p for p in prefs if p.superseded_by is None and p.is_core]
    live_reg = [p for p in prefs if p.superseded_by is None and not p.is_core]
    superseded = [p for p in prefs if p.superseded_by is not None]

    fingerprint = _preferences_fingerprint([p for p in prefs if p.superseded_by is None])
    path = meta_dir / "preferences.md"
    if path.exists() and _file_fingerprint(path) == fingerprint and fingerprint:
        return {"prefs_files": 0, "prefs_skipped": 1}

    by_id = {p.id: p for p in prefs}
    lines = [
        _frontmatter({"title": "Preferences & Habits", "fingerprint": fingerprint}),
        "",
        "# Preferences & Habits (learning loop)",
        "",
    ]

    def _bullet(p, header: str) -> None:
        lines.append(
            f"- **{header}** {p.fact} "
            f"[{p.category}, conf {p.confidence:.2f}, ev {p.evidence_count}]"
        )

    if live_core:
        lines.append("## Core (always kept, always injected)")
        for p in sorted(live_core, key=lambda x: (-x.confidence, -x.evidence_count)):
            _bullet(p, "⭐")
    if live_reg:
        lines.append("\n## Learning (retrievable by topic)")
        for p in sorted(live_reg, key=lambda x: (-x.confidence, -x.evidence_count)):
            _bullet(p, "•")
    if superseded:
        lines.append("\n## Superseded history (never overwritten)")
        for p in sorted(superseded, key=lambda x: x.first_seen or ""):
            succ = by_id.get(p.superseded_by)
            note = f" → superseded by #{p.superseded_by}" if succ else "→ dismissed"
            _bullet(p, note)

    path.write_text("\n".join(lines), encoding="utf-8")
    return {"prefs_files": 1, "prefs_skipped": 0}


async def _export_persona_versions(db: Database, base: Path) -> dict:
    """Write _meta/persona.md with all operating-principles versions."""
    meta_dir = _ensure_dir(base / "_meta")
    versions = await db.get_all_persona_versions()
    if not versions:
        return {"persona_files": 0, "persona_skipped": 0}

    latest = versions[-1]
    fingerprint = f"{latest['id']}:{latest['applied']}"
    path = meta_dir / "persona.md"
    if path.exists() and _file_fingerprint(path) == fingerprint and fingerprint:
        return {"persona_files": 0, "persona_skipped": 1}

    lines = [
        _frontmatter({"title": "Operating Principles", "fingerprint": fingerprint}),
        "",
        "# Operating Principles (versioned)",
        "",
        "> Applying a monthly proposal writes a new version here. The active "
        "version steers every future system prompt.",
        "",
    ]
    for v in versions:
        state = "active" if v["applied"] else ("rejected" if not v["applied"] else "inactive")
        lines.append(f"## v{v['version']} — {state} — {v['created_at'][:10]}")
        lines.append(v["content"].strip())
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return {"persona_files": 1, "persona_skipped": 0}


async def _export_consolidation_log(db: Database, base: Path) -> dict:
    """Write _meta/nightly-consolidation.md — append-only, idempotent.

    One dated entry per run; the whole file rewrites each night but contents
    (one appended line per new day) stay stable across runs.
    """
    meta_dir = _ensure_dir(base / "_meta")
    path = meta_dir / "nightly-consolidation.md"
    today = datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")

    entries: list[str] = []
    if path.exists():
        text = path.read_text(encoding="utf-8")
        body_lines = "\n".join(
            line for line in text.splitlines() if not line.startswith("fingerprint:")
        ).strip()
        if body_lines:
            entries.append(body_lines)
        if f"## {today}" in text:
            return {"consolidation_files": 0, "consolidation_skipped": 1}

    prefs = await db.get_all_preferences()
    live = [p for p in prefs if p.superseded_by is None]
    entries.append(
        f"## {today}\n"
        f"- consolidated: {sum(1 for p in prefs) - len(live)} superseded row(s)\n"
        f"- live preferences: {len(live)}\n"
        f"- full current state in _meta/preferences.md"
    )

    fingerprint = f"daily-{today}"
    head = f"---\nfingerprint: {fingerprint}\n---\n"
    path.write_text(head + "\n" + "\n\n".join(entries) + "\n", encoding="utf-8")
    return {"consolidation_files": 1, "consolidation_skipped": 0}


# ─── 3.3 GCS backup ─────────────────────────────────────────────────────────


def _gcloud_binary() -> str:
    gcloud = shutil.which("gcloud")
    if not gcloud:
        raise RuntimeError("gcloud CLI not found on PATH — cannot run backup job.")
    return gcloud


def _hot_copy_db(src_path: Path, dst_path: Path) -> None:
    """Online-safe SQLite backup using the sqlite3 backup API."""
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def run_backup(
    db: Database,
    *,
    bucket: str | None = None,
    export_dir: Path | None = None,
    date_suffix: str | None = None,
) -> dict:
    """Back up the DB dump and export vault to GCS.

    Returns {"skipped": True} (logged, no alert) when the bucket is unset.
    Any real failure raises and the scheduler alerts the owner.
    """
    bucket = bucket if bucket is not None else Config.BACKUP_BUCKET
    if not bucket:
        logger.info("BACKUP_BUCKET is unset — skipping nightly backup job.")
        return {"skipped": True}

    gcloud = _gcloud_binary()
    base = Path(export_dir or Config.EXPORT_DIR)
    stamp = date_suffix or datetime.now(Config.TIMEZONE).strftime("%Y-%m-%d")

    tmpdir = Path(Config.PROJECT_DIR) / "data" / "tmp_backup"
    _ensure_dir(tmpdir)
    db_dump = tmpdir / "second_brain.db"
    db_gz = tmpdir / f"second_brain-{stamp}.db.gz"
    vault_tar = tmpdir / f"export-{stamp}.tar.gz"

    try:
        # sqlite3 backup is synchronous + blocking; run it off the asyncio
        # loop so it never deadlocks against the in-process aiosqlite worker,
        # and give it a hard budget so a wedged DB alerts instead of hanging.
        await asyncio.wait_for(
            asyncio.to_thread(_hot_copy_db, db.db_path, db_dump),
            timeout=Config.BACKUP_TIMEOUT_SECONDS,
        )
        with open(db_dump, "rb") as raw, gzip.open(db_gz, "wb", compresslevel=9) as gz:
            shutil.copyfileobj(raw, gz)

        subprocess.run(
            [gcloud, "storage", "cp", str(db_gz), f"gs://{bucket}/db/{stamp}.gz"],
            check=True, timeout=Config.BACKUP_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info("DB backup uploaded: gs://%s/db/%s.gz", bucket, stamp)

        if base.exists():
            subprocess.run(
                ["tar", "czf", str(vault_tar), "-C", str(base), "."],
                check=True, timeout=Config.BACKUP_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [gcloud, "storage", "cp", str(vault_tar), f"gs://{bucket}/export/{stamp}.tar.gz"],
                check=True, timeout=Config.BACKUP_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("Export vault uploaded: gs://%s/export/%s.tar.gz", bucket, stamp)

        return {"skipped": False, "bucket": bucket, "stamp": stamp}
    finally:
        for p in (db_dump, db_gz, vault_tar):
            p.unlink(missing_ok=True)