#!/usr/bin/env python3
"""Minimal read-only web dashboard for the second-brain database.

Binds to 127.0.0.1 only (SSH tunnel to reach it). Bearer-token auth.
Usage: DASHBOARD_TOKEN=<token> python3 services/dashboard.py --port 8765
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_URL = os.environ.get(
    "SECOND_BRAIN_DB",
    "/opt/second-brain/data/second_brain.db?mode=ro",
)
SCHED_DB = os.environ.get(
    "SECOND_BRAIN_SCHED_DB",
    "/opt/second-brain/data/scheduler_jobs.db",
)
HOST = "127.0.0.1"
PORT = 8765

HEADERS = "<meta charset='utf-8'><title>Second Brain</title>"
STYLE = (
    "body{font:14px/1.5 system-ui,sans-serif;margin:2rem;max-width:1100px}"
    "h1{font-size:1.4rem}table{border-collapse:collapse;width:100%;margin-bottom:2rem}"
    "th,td{border:1px solid #ddd;padding:6px 10px;text-align:left;vertical-align:top;font-size:13px}"
    "th{background:#f4f4f4}.ok{color:#0a7d33}.bad{color:#b00020}.tag{background:#eef;border-radius:4px;padding:1px 6px;font-size:12px}"
    "code{background:#f4f4f4;padding:1px 4px}"
)
PAGE = (
    "<!doctype html><html><head>" + HEADERS + "<style>" + STYLE + "</style></head><body>"
    "<h1>Second Brain — read-only dashboard</h1>"
    "<div id='app'>Loading…</div>"
    "<script>const T=getApiToken();"
    "async function j(p){const r=await fetch(p,{headers:{Authorization:'Bearer '+T}});"
    "if(!r.ok)throw new Error(p+' '+r.status);return r.json()}"
    "async function go(){const d=await j('/api/overview');"
    "let h='<table><tr><th>Tasks</th><th>Notes</th><th>Reminders active</th>"
    "<th>Preferences</th><th>Model calls</th><th>Digests</th></tr>';"
    "h+='<tr><td>'+d.tasks+'</td><td>'+d.notes+'</td><td>'+d.reminders_active+'</td>"
    "td>'+d.preferences+'</td><td>'+d.model_calls+'</td><td>'+d.digests+'</td></tr></table>';"
    "const T2=await j('/api/tasks');if(T2.length){" +
    "h+='<h2>Tasks</h2><table><tr><th>#</th><th>Description</th><th>Status</th>"
    "<th>Priority</th><th>Due</th><th>Category</th><th>Created</th></tr>';" +
    "T2.forEach(r=>{h+='<tr><td>'+r.id+'</td><td>'+esc(r.description)+'</td><td>'+esc(r.status)+'</td>"
    "td>'+esc(r.priority)+'</td><td>'+esc(r.due_date||'')+'</td><td>'+esc(r.category||'')+'</td>"
    "<td>'+esc(r.created_at||'')+'</td></tr>'})" +
    "h+='</table>'}" +
    "const N=await j('/api/notes');if(N.length){" +
    "h+='<h2>Notes</h2><table><tr><th>#</th><th>Content</th><th>Tags</th><th>Category</th><th>Created</th></tr>';" +
    "N.forEach(r=>{h+='<tr><td>'+r.id+'</td><td>'+esc(r.content)+'</td>"
    "td>'+r.tags.map(t=>'<span class=\\'tag\\'>'+esc(t)+'</span>').join(' ')+'</td>"
    "<td>'+esc(r.category||'')+'</td><td>'+esc(r.created_at||'')+'</td></tr>'})" +
    "h+='</table>'}" +
    "const R=await j('/api/reminders');h+='<h2>Reminders</h2><table><tr><th>#</th>"
    "<th>Message</th><th>Trigger (UTC)</th><th>Recurring</th><th>Active</th></tr>';" +
    "R.forEach(r=>{h+='<tr><td>'+r.id+'</td><td>'+esc(r.message)+'</td><td>'+esc(r.trigger_time||'')+'</td>"
    "td>'+(r.cron_expression||'-')+'</td><td>'+(r.is_active?'<span class=\\'ok\\'>yes</span>':'no')+'</td></tr>'})" +
    "h+='</table>';" +
    "const U=await j('/api/model');h+='<h2>Model usage (last 14d)</h2>"
    "<table><tr><th>Model</th><th>Day</th><th>Calls</th><th>Errors</th><th>Output tok</th></tr>';" +
    "U.forEach(r=>{h+='<tr><td>'+esc(r.model_id)+'</td><td>'+esc(r.day)+'</td>" +
    "td>'+r.calls+'</td><td>'+(r.errors?'<span class=\\'bad\\'>'+r.errors+'</span>':r.errors)+'</td>" +
    "<td>'+r.output_tokens_est+'</td></tr>'})" +
    "h+='</table>';" +
    "const P=await j('/api/preferences');if(P.length){" +
    "h+='<h2>Known preferences</h2><table><tr><th>Fact</th><th>Confidence</th><th>Keywords</th></tr>';" +
    "P.forEach(r=>{h+='<tr><td>'+esc(r.fact)+'</td><td>'+(r.confidence||'')+'</td>" +
    "td>'+esc((r.keywords||'').join(', '))+'</td></tr>'})" +
    "h+='</table>'}" +
    "document.getElementById('app').innerHTML=h}"
    "const esc=s=>(String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\\'':'&#39;'}[c])))"
    "function getApiToken(){const m=location.search.match(/[?&]token=([^&]+)/);return m?m[1]:''}"
    "go().catch(e=>document.getElementById('app').textContent='Error: '+e) "
    "</script></body></html>"
)


def _connect(db_url: str):
    return sqlite3.connect(f"file:{db_url}", uri=True, timeout=10)


def _read(query: str, params: tuple = ()) -> list:
    conn = _connect(DB_URL)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _count(q: str, params: tuple = ()) -> int:
    rows = _read(q, params)
    return int(rows[0]["n"] if rows else 0)


def _overview() -> dict:
    return {
        "tasks": _count("SELECT COUNT(*) AS n FROM tasks"),
        "notes": _count("SELECT COUNT(*) AS n FROM notes"),
        "reminders_active": _count("SELECT COUNT(*) AS n FROM reminders WHERE is_active=1"),
        "preferences": _count("SELECT COUNT(*) AS n FROM preferences"),
        "model_calls": _count("SELECT COALESCE(SUM(calls),0) AS n FROM model_usage"),
        "digests": _count("SELECT COUNT(*) AS n FROM daily_digests"),
        "heartbeat": _read("SELECT last_seen FROM heartbeat ORDER BY rowid DESC LIMIT 1"),
    }


def _routes(path: str, query: dict) -> tuple:
    api = {
        "/api/tasks": lambda: _read(
            "SELECT id,description,status,priority,due_date,category,created_at,completed_at "
            "FROM tasks ORDER BY COALESCE(due_date,'9999') ASC LIMIT 50"
        ),
        "/api/notes": lambda: _read(
            "SELECT id,content,tags,category,created_at FROM notes ORDER BY id DESC LIMIT 50"
        ),
        "/api/reminders": lambda: _read(
            "SELECT id,message,trigger_time,is_recurring,cron_expression,is_active,created_at "
            "FROM reminders ORDER BY trigger_time LIMIT 50"
        ),
        "/api/preferences": lambda: _read(
            "SELECT fact,confidence,keywords,category,is_core FROM preferences "
            "ORDER BY is_core DESC, confidence DESC LIMIT 50"
        ),
        "/api/model": lambda: _read(
            "SELECT model_id,day,calls,errors,output_tokens_est FROM model_usage "
            "ORDER BY day DESC LIMIT 200"
        ),
        "/api/overview": _overview,
    }
    if path in api:
        return api[path](), 200
    if path == "/api/conversations":
        limit = min(int(query.get("limit", ["100"])[0]), 300)
        return _read(
            "SELECT id,role,substr(content,1,500) AS content,timestamp FROM conversations "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ), 200
    return {"error": "not found"}, 404


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("dash %s - %s\n" % (self.address_string(), fmt % args))

    def _authed(self) -> bool:
        token = os.environ.get("DASHBOARD_TOKEN", "changeme")
        given = self.headers.get("Authorization", "")
        if given.startswith("Bearer ") and given[7:] == token:
            return True
        q = parse_qs(urlparse(self.path).query)
        return q.get("token", [""])[0] == token

    def _send(self, code: int, data, ctype: str):
        body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self._send(200, {"ok": True, "ts": time.time()}, "application/json")
        if not self._authed():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path in ("/", "/index.html") or url.path == "":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        try:
            data, code = _routes(url.path, parse_qs(url.query))
            self._send(code, data, "application/json")
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)}, "application/json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", 8765)))
    args = ap.parse_args()
    if not os.environ.get("DASHBOARD_TOKEN"):
        print("WARNING: no DASHBOARD_TOKEN, using default 'changeme'", file=sys.stderr)
    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"second-brain dashboard on http://{HOST}:{args.port} (read-only, token auth)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())