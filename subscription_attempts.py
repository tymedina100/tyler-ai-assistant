"""Durable, at-most-once subscription attempts for one configured account ledger.

No prompt text or credentials are stored. A crashed running attempt remains held
for explicit reconciliation; a different attempt ID is not an automatic retry.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import UUID

from codex_subscription import SubscriptionUnavailable, analyze


def _connect(path: str) -> sqlite3.Connection:
    target = Path(path).expanduser()
    repository = Path(__file__).resolve().parent
    if not target.is_absolute() or target.is_symlink() or target.resolve().is_relative_to(repository):
        raise ValueError("Keep the private attempt ledger outside the repository.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    db = sqlite3.connect(target, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("""create table if not exists attempts (
        id text primary key, request_hash text not null,
        status text not null check(status in ('running','succeeded','failed')),
        started_at real not null, finished_at real, result text, error_type text
    )""")
    db.execute("create unique index if not exists one_running_attempt on attempts(status) where status='running'")
    return db


def analyze_once(prompt: str, *, attempt_id: str, ledger_path: str, **options) -> dict:
    """The queue must persist attempt_id before calling and reuse it after restart."""
    attempt_id = str(UUID(attempt_id))
    # Quota observations and timeout are execution conditions, not analysis intent.
    intent = {"prompt": prompt, "model": options.get("model"), "effort": options.get("effort"),
              "binary": options.get("binary"), "adapter": 1}
    fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
    db = _connect(ledger_path)
    try:
        db.execute("begin immediate")
        previous = db.execute("select * from attempts where id=?", (attempt_id,)).fetchone()
        if previous:
            db.rollback()
            if previous["request_hash"] != fingerprint:
                raise SubscriptionUnavailable("Attempt ID already belongs to different analysis input.")
            if previous["status"] == "succeeded":
                return json.loads(previous["result"])
            raise SubscriptionUnavailable("Previous attempt is running, interrupted, or failed; reconcile it explicitly.")
        if db.execute("select 1 from attempts where status='running'").fetchone():
            db.rollback()
            raise SubscriptionUnavailable("Another subscription attempt holds this account ledger.")
        db.execute("insert into attempts(id,request_hash,status,started_at) values(?,?,'running',?)",
                   (attempt_id, fingerprint, time.time()))
        db.commit()
        try:
            result = analyze(prompt, **options)
            encoded = json.dumps(result)
        except Exception as error:
            db.execute("update attempts set status='failed',finished_at=?,error_type=? where id=?",
                       (time.time(), type(error).__name__, attempt_id))
            raise
        db.execute("update attempts set status='succeeded',finished_at=?,result=? where id=?",
                   (time.time(), encoded, attempt_id))
        return result
    finally:
        db.close()
