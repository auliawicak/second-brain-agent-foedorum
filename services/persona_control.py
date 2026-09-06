"""Phase 8 §8.2 — /persona command logic (view/edit/rollback persona data).

The persona is pure data in the `persona` table: three layers (voice,
principles, mode_rules), versioned snapshots with exactly one row active.
Every edit inserts a new inactive snapshot carrying the untouched layers
forward, then flips the active flag to it. Rollback is the same primitive —
a flag flip back to a previous version. No restart is ever needed because
the bot re-reads the active row on every turn.
"""

from __future__ import annotations

from storage.database import Database

LAYERS = ("voice", "principles", "mode_rules")


async def persona_show(db: Database) -> str:
    """Render the currently active persona snapshot."""
    persona = await db.get_active_persona_config()
    if persona is None:
        return "⚠️ No persona data yet (migrations may not have run)."
    created = persona["created_at"][:16] if persona.get("created_at") else "?"
    parts = [
        f"🎭 **Persona v{persona['version']}** *(active since {created})*",
        "",
        f"**Voice:**\n{persona['voice']}",
        "",
        f"**Principles:**\n{persona['principles']}",
    ]
    if persona.get("mode_rules"):
        parts += ["", f"**Mode rules:**\n{persona['mode_rules']}"]
    return "\n".join(parts)


async def persona_set(db: Database, layer: str, text: str) -> str:
    """Set one layer of the persona. The other two layers are carried
    forward unchanged; a new active snapshot is created."""
    if layer not in LAYERS:
        return (
            f"⚠️ Unknown layer `{layer}`. Use one of: "
            + ", ".join(f"/persona set {l} <text>" for l in LAYERS)
        )
    text = (text or "").strip()
    if not text:
        return f"⚠️ Missing text. Usage: `/persona set {layer} <text>`"

    snapshot = await db.save_persona_snapshot(**{layer: text})
    await db.set_persona_active(snapshot["version"])
    return (
        f"✅ **Persona updated** → v{snapshot['version']} (active).\n\n"
        f"Changed **{layer}**. Your next message already uses it — no restart needed."
    )


async def persona_history(db: Database) -> str:
    """List persona snapshot history, newest first."""
    rows = await db.list_persona_versions(limit=20)
    if not rows:
        return "⚠️ No persona history yet."
    lines = ["🎭 **Persona history** (newest first):", ""]
    for row in rows:
        mark = " 🟢 active" if row["active"] else ""
        created = row["created_at"][:16]
        voice_preview = " ".join((row["voice"] or "").split()[:8])
        lines.append(f"• `v{row['version']}` {created}{mark}: _{voice_preview}_")
    return "\n".join(lines)


async def persona_rollback(db: Database, version: int) -> str:
    """Roll the active persona back to a previous snapshot (flag flip)."""
    snapshot = await db.get_persona_snapshot(version)
    if snapshot is None:
        return f"⚠️ No persona v{version} exists. See `/persona history`."
    active = await db.get_active_persona_config()
    if active and active["version"] == version:
        return f"ℹ️ Persona v{version} is already active."
    ok = await db.set_persona_active(version)
    if not ok:
        return f"⚠️ Rollback to v{version} failed."
    return f"↩️ **Rolled back to persona v{version}** (now active)."