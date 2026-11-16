"""Error reports from beta builds.

A beta app is testing on other people's phones, so the crash that matters is the
one nobody writes down. A build that carries `build = beta` posts what broke -
the exception, the stack, the device, a short trail of what it was doing - and
this keeps the last few hundred in the library database.

Nothing here is a general-purpose telemetry endpoint: the body is capped, one
address can only post a handful, and the app says in its own settings that it is
sending. Turning the beta off turns the reports off.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from .sqlite_utils import connect_sqlite

#: A report is a few kilobytes of text; anything past this is not a report.
MAX_REPORT_BYTES = 256 * 1024

#: Rows kept. A beta round produces tens, not thousands; the oldest go first.
MAX_ROWS = 1000
RETENTION_DAYS = 30

#: One address, this many reports per window. A crash loop on five phones still fits.
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 600

_FIELD_LIMITS = {
    'app': 40,
    'build': 20,
    'version': 40,
    'device': 80,
    'android': 40,
    'kind': 20,
    'message': 2000,
    'note': 4000,
    'stack': 32_000,
    'install_id': 64,
    'breadcrumbs': 8000,
}
_ALLOWED_KINDS = {'crash', 'manual', 'anr'}


def _clip(value: Any, limit: int) -> str:
    text = '' if value is None else str(value)
    return text[:limit]


class BetaReportStore:
    """The reports themselves, in the library database."""

    def __init__(self, db_path: Path) -> None:
        self._path = str(db_path)
        self._hits: dict[str, list[float]] = {}
        self._init_db()

    def _connect(self):
        return connect_sqlite(self._path, row_factory=True)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS beta_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at REAL NOT NULL,
                    at TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'crash',
                    app TEXT NOT NULL DEFAULT '',
                    build TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '',
                    install_id TEXT NOT NULL DEFAULT '',
                    device TEXT NOT NULL DEFAULT '',
                    android TEXT NOT NULL DEFAULT '',
                    user_id INTEGER NOT NULL DEFAULT 0,
                    device_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    stack TEXT NOT NULL DEFAULT '',
                    breadcrumbs TEXT NOT NULL DEFAULT '',
                    ip TEXT NOT NULL DEFAULT '',
                    bytes INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute(
                'CREATE INDEX IF NOT EXISTS beta_reports_at '
                'ON beta_reports(received_at DESC)'
            )

    # ── rate limiting ───────────────────────────────────────────────────

    def allowed(self, ip: str) -> bool:
        """Whether *ip* may post now; counts the attempt when it may."""

        now = time.time()
        window = [
            stamp
            for stamp in self._hits.get(ip, [])
            if now - stamp < RATE_WINDOW_SECONDS
        ]
        if len(window) >= RATE_LIMIT:
            self._hits[ip] = window
            return False
        window.append(now)
        self._hits[ip] = window
        return True

    # ── writes ──────────────────────────────────────────────────────────

    def store(
        self,
        payload: dict[str, Any],
        *,
        ip: str = '',
        user_id: int = 0,
        device_id: str = '',
        size: int = 0,
    ) -> int:
        kind = _clip(payload.get('kind') or 'crash', 20)
        if kind not in _ALLOWED_KINDS:
            kind = 'crash'
        crumbs = payload.get('breadcrumbs')
        if isinstance(crumbs, list):
            crumbs = '\n'.join(str(item) for item in crumbs)
        record = {
            'received_at': time.time(),
            'at': _clip(payload.get('at'), 40),
            'kind': kind,
            'app': _clip(payload.get('app') or 'downtify-android', _FIELD_LIMITS['app']),
            'build': _clip(payload.get('build'), _FIELD_LIMITS['build']),
            'version': _clip(payload.get('version'), _FIELD_LIMITS['version']),
            'install_id': _clip(payload.get('install_id'), _FIELD_LIMITS['install_id']),
            'device': _clip(payload.get('device'), _FIELD_LIMITS['device']),
            'android': _clip(payload.get('android'), _FIELD_LIMITS['android']),
            'user_id': int(user_id or payload.get('user_id') or 0),
            'device_id': _clip(device_id or payload.get('device_id'), 40),
            'message': _clip(payload.get('message'), _FIELD_LIMITS['message']),
            'note': _clip(payload.get('note'), _FIELD_LIMITS['note']),
            'stack': _clip(payload.get('stack'), _FIELD_LIMITS['stack']),
            'breadcrumbs': _clip(crumbs, _FIELD_LIMITS['breadcrumbs']),
            'ip': _clip(ip, 64),
            'bytes': int(size or 0),
        }
        columns = ', '.join(record)
        marks = ', '.join('?' for _ in record)
        with self._connect() as conn:
            cursor = conn.execute(
                f'INSERT INTO beta_reports ({columns}) VALUES ({marks})',
                tuple(record.values()),
            )
            self._prune(conn)
            return int(cursor.lastrowid or 0)

    def _prune(self, conn) -> None:
        oldest = time.time() - RETENTION_DAYS * 86400
        conn.execute('DELETE FROM beta_reports WHERE received_at < ?', (oldest,))
        conn.execute(
            """
            DELETE FROM beta_reports WHERE id NOT IN (
                SELECT id FROM beta_reports ORDER BY received_at DESC LIMIT ?
            )
            """,
            (MAX_ROWS,),
        )

    # ── reads ───────────────────────────────────────────────────────────

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM beta_reports ORDER BY received_at DESC LIMIT ?',
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute('SELECT count(*) FROM beta_reports').fetchone()[0]
            newest = conn.execute(
                'SELECT received_at FROM beta_reports ORDER BY received_at DESC LIMIT 1'
            ).fetchone()
        return {'total': int(total), 'newest': newest[0] if newest else None}

    def clear(self) -> int:
        with self._connect() as conn:
            cursor = conn.execute('DELETE FROM beta_reports')
            return int(cursor.rowcount or 0)


def looks_like_report(payload: Any) -> bool:
    """A shallow sanity check: a report is a JSON object with a message or a stack."""

    if not isinstance(payload, dict):
        return False
    if not (payload.get('message') or payload.get('stack') or payload.get('note')):
        return False
    # A note is prose, not a document; a stack is not megabytes either.
    for field, limit in _FIELD_LIMITS.items():
        value = payload.get(field)
        if isinstance(value, str) and len(value) > limit * 4:
            return False
    return True


_SAFE_INSTALL = re.compile(r'[^A-Za-z0-9_.:-]')


def clean_install_id(value: Any) -> str:
    return _SAFE_INSTALL.sub('', str(value or ''))[:64]


def decode_body(raw: bytes, encoding: str) -> Optional[dict[str, Any]]:
    """The report inside *raw*, gunzipped when the client compressed it."""

    import gzip

    data = raw
    if 'gzip' in (encoding or '').lower():
        try:
            data = gzip.decompress(raw)
        except OSError:
            return None
        if len(data) > MAX_REPORT_BYTES * 8:
            return None
    elif len(raw) > MAX_REPORT_BYTES:
        return None
    try:
        parsed = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
