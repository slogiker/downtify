"""What each device is playing, and the jam a group of devices shares.

Two primitives, both stored in the library database so they survive a restart:

* :class:`PlaybackStateStore` - the last report each device sent (track,
  position, playing, the queue it plays). The web dashboard's ``NowPlaying``
  view forgets a player after 90 seconds; this one does not, so "continue on
  this phone where the other one stopped" still works the next morning.
* :class:`JamStore` - a shared session: its members and one authoritative
  state whose ``revision`` rises on every accepted write. A client sends the
  revision it acted on and a stale one is refused (``409``) with the current
  state, so two thumbs on two phones converge instead of overwriting each
  other.

Nothing here knows about playback itself: a client reports what it plays and
applies what the session says. The routes live in
:mod:`downtify.mobile_routes` (``/api/v1/playback``).
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from .sqlite_utils import connect_sqlite

#: No I, L, O, U - the same rule the pairing codes follow, so a jam code read
#: out loud is unambiguous.
_CODE_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
_CODE_LENGTH = 6

#: A jam's queue is a list of track ids, not a document: 2,000 is far past any
#: real queue and keeps one client from filling the database.
MAX_QUEUE_ITEMS = 2000

#: How long a code stays free of reuse checks: an id is opaque, the code is
#: the human handle, and a closed session's code is never handed out again.
_MAX_CODE_TRIES = 40


def _now() -> float:
    return time.time()


def _clean_queue(queue: Any) -> list[str]:
    if not isinstance(queue, list):
        return []
    out = [item for item in queue if isinstance(item, str) and item]
    return out[:MAX_QUEUE_ITEMS]


class PlaybackStateStore:
    """The last playback report of every device, per user."""

    def __init__(self, db_path: Path) -> None:
        self._path = str(db_path)
        self._init_db()

    def _connect(self):
        return connect_sqlite(self._path, row_factory=True)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS playback_state (
                    user_id INTEGER NOT NULL,
                    device_id TEXT NOT NULL,
                    track_id TEXT NOT NULL DEFAULT '',
                    queue_json TEXT NOT NULL DEFAULT '[]',
                    position REAL NOT NULL DEFAULT 0,
                    playing INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, device_id)
                )
            """)

    def report(
        self,
        user_id: int,
        device_id: str,
        *,
        track_id: str = '',
        position: float = 0.0,
        playing: bool = False,
        queue: Any = None,
    ) -> dict[str, Any]:
        """Store what *device_id* plays now; answers the stored row."""

        track_id = track_id if isinstance(track_id, str) else ''
        try:
            position = max(0.0, float(position or 0))
        except (TypeError, ValueError):
            position = 0.0
        queue_json = json.dumps(_clean_queue(queue))
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO playback_state
                    (user_id, device_id, track_id, queue_json, position,
                     playing, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, device_id) DO UPDATE SET
                    track_id = excluded.track_id,
                    queue_json = excluded.queue_json,
                    position = excluded.position,
                    playing = excluded.playing,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user_id),
                    device_id or '',
                    track_id,
                    queue_json,
                    position,
                    1 if playing else 0,
                    now,
                ),
            )
        return {
            'device_id': device_id or '',
            'track_id': track_id,
            'queue': json.loads(queue_json),
            'position': position,
            'playing': bool(playing),
            'updated_at': now,
        }

    def devices(
        self, user_id: int, *, exclude_device: str = ''
    ) -> list[dict[str, Any]]:
        """Every device of *user_id* that ever played something, newest first."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT device_id, track_id, queue_json, position, playing,
                       updated_at
                FROM playback_state
                WHERE user_id = ? AND device_id != ? AND track_id != ''
                ORDER BY updated_at DESC
                """,
                (int(user_id), exclude_device or ''),
            ).fetchall()
        return [
            {
                'device_id': row['device_id'],
                'track_id': row['track_id'],
                'queue': json.loads(row['queue_json'] or '[]'),
                'position': row['position'],
                'playing': bool(row['playing']),
                'updated_at': row['updated_at'],
            }
            for row in rows
        ]


class JamStore:
    """A shared playback session: members and one revisioned state."""

    def __init__(self, db_path: Path) -> None:
        self._path = str(db_path)
        self._init_db()

    def _connect(self):
        return connect_sqlite(self._path, row_factory=True)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jam_sessions (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    owner_user_id INTEGER NOT NULL,
                    owner_device_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    closed_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jam_members (
                    session_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    device_id TEXT NOT NULL DEFAULT '',
                    joined_at REAL NOT NULL,
                    PRIMARY KEY (session_id, user_id, device_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jam_state (
                    session_id TEXT PRIMARY KEY,
                    track_id TEXT NOT NULL DEFAULT '',
                    queue_json TEXT NOT NULL DEFAULT '[]',
                    position REAL NOT NULL DEFAULT 0,
                    playing INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    updated_by_user_id INTEGER NOT NULL DEFAULT 0,
                    updated_by_device_id TEXT NOT NULL DEFAULT ''
                )
            """)

    # ── sessions ────────────────────────────────────────────────────────

    def _new_code(self, conn) -> str:
        for _ in range(_MAX_CODE_TRIES):
            code = ''.join(
                secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH)
            )
            taken = conn.execute(
                'SELECT 1 FROM jam_sessions WHERE code = ?', (code,)
            ).fetchone()
            if taken is None:
                return code
        raise RuntimeError('could not allocate a jam code')

    def create(
        self, owner_user_id: int, owner_device_id: str = ''
    ) -> dict[str, Any]:
        """Start a session owned by *owner_user_id*; the owner joins it."""

        now = _now()
        session_id = secrets.token_urlsafe(12)
        with self._connect() as conn:
            code = self._new_code(conn)
            conn.execute(
                """
                INSERT INTO jam_sessions
                    (id, code, owner_user_id, owner_device_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    code,
                    int(owner_user_id),
                    owner_device_id or '',
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO jam_members
                    (session_id, user_id, device_id, joined_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, int(owner_user_id), owner_device_id or '', now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO jam_state (session_id, updated_at)
                VALUES (?, ?)
                """,
                (session_id, now),
            )
        return self.session(session_id) or {}

    def session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM jam_sessions WHERE id = ? AND closed_at IS NULL',
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            'id': row['id'],
            'code': row['code'],
            'owner_user_id': row['owner_user_id'],
            'owner_device_id': row['owner_device_id'],
            'created_at': row['created_at'],
        }

    def by_code(self, code: str) -> Optional[dict[str, Any]]:
        clean = ''.join(ch for ch in str(code or '').upper() if ch.isalnum())
        if not clean:
            return None
        with self._connect() as conn:
            row = conn.execute(
                'SELECT id FROM jam_sessions WHERE code = ? '
                'AND closed_at IS NULL',
                (clean,),
            ).fetchone()
        return self.session(row['id']) if row else None

    def session_for_user(self, user_id: int) -> Optional[dict[str, Any]]:
        """The session *user_id* is in, newest first, if any."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.id FROM jam_sessions s
                JOIN jam_members m ON m.session_id = s.id
                WHERE m.user_id = ? AND s.closed_at IS NULL
                ORDER BY s.created_at DESC
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        return self.session(row['id']) if row else None

    def join(
        self, session_id: str, user_id: int, device_id: str = ''
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jam_members
                    (session_id, user_id, device_id, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, user_id, device_id) DO NOTHING
                """,
                (session_id, int(user_id), device_id or '', _now()),
            )
        return self.session(session_id) or {}

    def leave(self, session_id: str, user_id: int, device_id: str) -> bool:
        """Drop one device of *user_id*; answers whether the session ended.

        The owner leaving ends the session for everyone - a jam whose host is
        gone has nobody to follow.
        """

        session = self.session(session_id)
        if session is None:
            return True
        with self._connect() as conn:
            conn.execute(
                'DELETE FROM jam_members WHERE session_id = ? '
                'AND user_id = ? AND device_id = ?',
                (session_id, int(user_id), device_id or ''),
            )
            if int(user_id) == session['owner_user_id']:
                conn.execute(
                    'UPDATE jam_sessions SET closed_at = ? WHERE id = ?',
                    (_now(), session_id),
                )
                return True
        return False

    def close(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                'UPDATE jam_sessions SET closed_at = ? WHERE id = ?',
                (_now(), session_id),
            )

    def members(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, device_id, joined_at FROM jam_members
                WHERE session_id = ? ORDER BY joined_at
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                'user_id': row['user_id'],
                'device_id': row['device_id'],
                'joined_at': row['joined_at'],
            }
            for row in rows
        ]

    # ── state ───────────────────────────────────────────────────────────

    def state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM jam_state WHERE session_id = ?', (session_id,)
            ).fetchone()
        if row is None:
            return {
                'track_id': '',
                'queue': [],
                'position': 0.0,
                'playing': False,
                'revision': 0,
                'updated_at': 0.0,
                'updated_by_user_id': 0,
                'updated_by_device_id': '',
            }
        return {
            'track_id': row['track_id'],
            'queue': json.loads(row['queue_json'] or '[]'),
            'position': row['position'],
            'playing': bool(row['playing']),
            'revision': row['revision'],
            'updated_at': row['updated_at'],
            'updated_by_user_id': row['updated_by_user_id'],
            'updated_by_device_id': row['updated_by_device_id'],
        }

    def publish(
        self,
        session_id: str,
        user_id: int,
        device_id: str,
        *,
        track_id: str = '',
        position: float = 0.0,
        playing: bool = False,
        queue: Any = None,
        expected_revision: Optional[int] = None,
        queue_supplied: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Write the session's state; answers ``(state, applied)``.

        *expected_revision* is what the caller acted on. A mismatch means
        someone else wrote in between: nothing is stored and the current
        state comes back, so the caller can re-apply on top of it. Omitting
        it (a fresh join sending its first state) writes unconditionally.

        *queue_supplied* ``False`` keeps the stored queue - a client that
        only seeks or pauses must not wipe it.
        """

        track_id = track_id if isinstance(track_id, str) else ''
        try:
            position = max(0.0, float(position or 0))
        except (TypeError, ValueError):
            position = 0.0
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM jam_state WHERE session_id = ?', (session_id,)
            ).fetchone()
            if row is None:
                return self.state(session_id), False
            if (
                expected_revision is not None
                and int(expected_revision) != int(row['revision'])
            ):
                return self.state(session_id), False
            queue_json = (
                json.dumps(_clean_queue(queue))
                if queue_supplied
                else (row['queue_json'] or '[]')
            )
            revision = int(row['revision']) + 1
            conn.execute(
                """
                UPDATE jam_state SET
                    track_id = ?, queue_json = ?, position = ?, playing = ?,
                    revision = ?, updated_at = ?,
                    updated_by_user_id = ?, updated_by_device_id = ?
                WHERE session_id = ?
                """,
                (
                    track_id,
                    queue_json,
                    position,
                    1 if playing else 0,
                    revision,
                    now,
                    int(user_id),
                    device_id or '',
                    session_id,
                ),
            )
        return self.state(session_id), True
