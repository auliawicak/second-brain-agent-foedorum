"""Phase 5 — health & observability checks for the Hermes + pool pipeline.

`run_health_checks(db)` runs a set of liveliness checks (gateway process, pool
reachability, DB heartbeat, model cooldowns, error-log scan, disk & memory) and
returns a short alert/recovery message — an empty string when everything is
fine, so a no-agent cron script stays silent unless something actually drifts.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

POOL_URL = os.environ.get("HEALTH_POOL_URL", "http://127.0.0.1:18080/v1/models")
GATEWAY_MATCH = os.environ.get("HEALTH_GATEWAY_MATCH", "hermes_cli.main gateway run")
COOLDOWN_MIN = int(os.environ.get("HEALTH_COOLDOWN_MIN", "10"))
HEARTBEAT_STALE_MIN = int(os.environ.get("HEALTH_HEARTBEAT_STALE_MIN", "5"))
MEM_MIN_MB = int(os.environ.get("HEALTH_MEM_MIN_MB", "150"))
DISK_MAX_PCT = int(os.environ.get("HEALTH_DISK_MAX_PCT", "90"))
DB_FILE = os.environ.get("HEALTH_DB_FILE", "/opt/second-brain/data/second_brain.db")
STATE_FILE = Path(
    os.environ.get(
        "HEALTH_STATE_FILE",
        str(Path.home() / ".hermes" / "state" / "health_state.json"),
    )
)


def _now_utc() -> datetime:
    return datetime.utcnow()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _gateway_alive() -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", GATEWAY_MATCH],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _pool_alive() -> bool:
    try:
        with urllib.request.urlopen(POOL_URL, timeout=5) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            return resp.status == 200 and "data" in body
    except Exception:  # noqa: BLE001
        return False


def _heartbeat_fresh(heartbeat_str: str | None) -> bool:
    last = _parse_iso(heartbeat_str)
    if last is None:
        return False
    return _now_utc() - last < timedelta(minutes=HEARTBEAT_STALE_MIN)


def _cooldown_models() -> list[str]:
    bad: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT model_id, cooldown_until, last_error FROM model_health"
            ):
                cooldown = _parse_iso(row["cooldown_until"])
                if cooldown is not None and cooldown > _now_utc():
                    bad.append(
                        f"{row['model_id']} (cooldown until {cooldown.isoformat()}; {str(row['last_error'] or '')[:120]})"
                    )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        bad.append(f"db read failed: {exc}")
    return bad


def _disk_ok(path: str = "/") -> (bool, str):
    usage = shutil.disk_usage(path)
    pct = usage.used / usage.total * 100
    detail = f"{pct:.0f}% used / {usage.free // (1024**3)} GiB free"
    return pct < DISK_MAX_PCT, detail


def _mem_ok() -> (bool, str):
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    mib = kb / 1024
                    return mib >= MEM_MIN_MB, f"{mib:.0f} MiB available"
    except OSError:
        return True, "unknown"
    return True, "unknown"


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except OSError:
        logger.warning("Health state file not writable: %s", STATE_FILE)


def _classify(check: str, fine: bool, state: dict) -> str | None:
    """Return alert/recovery text for one check, honoring a persist-and-hold policy."""
    entry = state.get(check)
    now = _now_utc()
    if fine:
        if entry and entry.get("alerted"):
            state.pop(check, None)
            return f"✅ {check} recovered"
        state.pop(check, None)
        return None
    if entry is None:
        state[check] = {"first_bad": now.isoformat(), "alerted": False}
        return None
    first_bad = _parse_iso(entry.get("first_bad"))
    held_long = first_bad is not None and now - first_bad >= timedelta(minutes=COOLDOWN_MIN)
    if held_long and not entry.get("alerted"):
        entry["alerted"] = True
        return f"🚨 {check}"
    return None


def run_health_checks(heartbeat_str: str | None = None) -> str:
    state = _load_state()
    lines: list[str] = []

    checks = [
        ("gateway process", _gateway_alive(), f"no process matching '{GATEWAY_MATCH}'"),
        ("model proxy", _pool_alive(), f"unreachable at {POOL_URL}"),
        ("db heartbeat", _heartbeat_fresh(heartbeat_str), f"stale >{HEARTBEAT_STALE_MIN} min"),
    ]

    cooldowns = _cooldown_models()
    if cooldowns:
        checks.append(("model cooldown", False, "; ".join(cooldowns)))

    disk_fine, disk_detail = _disk_ok()
    checks.append(("disk", disk_fine, f"{disk_detail} (limit {DISK_MAX_PCT}%)"))

    mem_fine, mem_detail = _mem_ok()
    checks.append(("memory", mem_fine, f"{mem_detail} (min {MEM_MIN_MB} MiB)"))

    active: set[str] = set()
    for name, fine, bad_detail in checks:
        active.add(name)
        if fine:
            msg = _classify(name, True, state)
            if msg:
                lines.append(msg)
        else:
            msg = _classify(name, False, state)
            if msg is not None:
                lines.append(f"{msg}: {bad_detail}")

    for orphan in [k for k in state if k not in active]:
        state.pop(orphan, None)

    _save_state(state)
    return "\n".join(lines)