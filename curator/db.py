import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,          -- content hash
    src TEXT NOT NULL,            -- original filename within the export
    taken_at INTEGER,             -- unix seconds, from Takeout sidecar or EXIF
    width INTEGER, height INTEGER, -- of the working copy
    url TEXT                      -- photos.google.com link, from the Takeout sidecar
);
CREATE TABLE IF NOT EXISTS sidecars (
    key TEXT PRIMARY KEY,         -- 'archive/dir/IMG_1.JPG' from a Takeout JSON sidecar
    taken_at INTEGER,
    url TEXT,
    src TEXT                      -- the sidecar's own path, so remote re-runs skip it
);
CREATE TABLE IF NOT EXISTS face_embeddings (
    photo_id TEXT, idx INTEGER,
    box TEXT, frac REAL, sharpness REAL,  -- box in working-copy pixels; frac = face height / image height
    emb BLOB,                     -- 128 float32 SFace embedding, L2-normalized
    PRIMARY KEY (photo_id, idx)
);
CREATE TABLE IF NOT EXISTS faces (
    photo_id TEXT PRIMARY KEY,
    n_faces INTEGER, matched INTEGER, similarity REAL,
    face_frac REAL, is_largest INTEGER, sharpness REAL,
    box TEXT,                     -- JSON [x, y, w, h] in working-copy pixels
    scan INTEGER                  -- prefilter.SCAN_VERSION that produced face_embeddings
);
CREATE TABLE IF NOT EXISTS prefilter (
    photo_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,         -- keep | reject
    reason TEXT,
    dup_of TEXT,
    phash TEXT,
    moment TEXT                   -- earliest photo id of its moment (photos taken seconds apart)
);
CREATE TABLE IF NOT EXISTS playoff (
    photo_id TEXT, judge TEXT,
    moment TEXT, bucket TEXT,
    winner INTEGER,               -- 1 = best of its category in its moment; goes on to ranking
    score REAL,
    PRIMARY KEY (photo_id, judge)
);
CREATE TABLE IF NOT EXISTS classify (
    photo_id TEXT, judge TEXT,
    bucket TEXT, bucket_probs TEXT, flags TEXT, tokens INTEGER, ms INTEGER,
    PRIMARY KEY (photo_id, judge)
);
CREATE TABLE IF NOT EXISTS comparisons (
    judge TEXT, prompt_version TEXT, first TEXT, second TEXT,
    p_first REAL,                 -- P(first photo is better)
    confidence REAL, tokens_in INTEGER, tokens_out INTEGER, note TEXT, ms INTEGER,
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (judge, prompt_version, first, second)
);
CREATE TABLE IF NOT EXISTS ratings (
    photo_id TEXT, judge TEXT, bucket TEXT, score REAL, stderr REAL, n_comparisons INTEGER,
    PRIMARY KEY (photo_id, judge)
);
CREATE TABLE IF NOT EXISTS finals (
    photo_id TEXT PRIMARY KEY, judge TEXT, bucket TEXT,
    score REAL, stderr REAL       -- within this category's finalists only
);
CREATE TABLE IF NOT EXISTS explanations (
    photo_id TEXT PRIMARY KEY, reason TEXT, crop TEXT
);
CREATE TABLE IF NOT EXISTS opinions (  -- another judge's view of the finalists; decides nothing
    photo_id TEXT, judge TEXT, bucket TEXT, score REAL, stderr REAL,
    PRIMARY KEY (photo_id, judge)
);
CREATE TABLE IF NOT EXISTS looks (     -- expression, gaze and pose, for the gallery's variety toggle
    photo_id TEXT, judge TEXT,
    answers TEXT,                 -- JSON question -> {choice, probabilities}
    PRIMARY KEY (photo_id, judge)
);
CREATE TABLE IF NOT EXISTS refined (  -- categories `refine` has finished, so re-runs don't pay twice
    bucket TEXT, judge TEXT, prompt_version TEXT,
    PRIMARY KEY (bucket, judge, prompt_version)
);
CREATE TABLE IF NOT EXISTS spend (    -- calls not tracked elsewhere (classify output, explain)
    judge TEXT, tokens_in INTEGER, tokens_out INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class DB:
    """Thin SQLite wrapper; one connection guarded by a lock so judge threads can write."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # Columns added after the first release; older databases get them here.
        for table, col, kind in (("classify", "ms", "INTEGER"), ("comparisons", "ms", "INTEGER"),
                                 ("photos", "url", "TEXT"), ("sidecars", "url", "TEXT"),
                                 ("sidecars", "src", "TEXT"), ("faces", "scan", "INTEGER"),
                                 ("prefilter", "moment", "TEXT")):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        self.lock = threading.Lock()

    def execute(self, sql: str, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows):
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
