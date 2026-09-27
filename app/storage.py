"""SQLite-backed, append-only document store.

Schema (versioned, append-only -- rows are never updated or deleted):

* ``documents``       one row per document id;
* ``revisions``       every submitted revision, full canonical request;
* ``layouts``         the planner output for each revision;
* ``finalizations``   created once per finalized revision; its exports
                      (per-page JSON, printable SVG, replay request) are
                      written as immutable files and referenced by path.

All stored JSON is serialized with sorted keys and compact separators, so
identical requests produce identical digests independent of the order in
which object keys were sent.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time

from .errors import Conflict, NotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    doc_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (doc_id, revision_no),
    UNIQUE (doc_id, request_sha256)
);
CREATE TABLE IF NOT EXISTS layouts (
    doc_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    layout_json TEXT NOT NULL,
    layout_sha256 TEXT NOT NULL,
    PRIMARY KEY (doc_id, revision_no),
    FOREIGN KEY (doc_id, revision_no)
        REFERENCES revisions (doc_id, revision_no)
);
CREATE TABLE IF NOT EXISTS finalizations (
    doc_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    finalized_at REAL NOT NULL,
    json_path TEXT NOT NULL,
    svg_path TEXT NOT NULL,
    replay_path TEXT NOT NULL,
    summary_sha256 TEXT NOT NULL,
    PRIMARY KEY (doc_id, revision_no),
    FOREIGN KEY (doc_id, revision_no)
        REFERENCES revisions (doc_id, revision_no)
);
"""


def canonical_json(obj) -> str:
    """Order-independent, deterministic JSON serialization."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=_default)


def _default(o):  # pragma: no cover - defensive
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serializable: {type(o)!r}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, db_path, exports_dir):
        self.db_path = str(db_path)
        self.exports_dir = str(exports_dir)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        os.makedirs(self.exports_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # -- documents / revisions --------------------------------------------
    def create_document(self, doc_id: str):
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO documents (doc_id, created_at) VALUES (?, ?)",
                    (doc_id, time.time()))
                self._conn.commit()
            except sqlite3.IntegrityError:
                pass

    def document_exists(self, doc_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            return row is not None

    def add_revision(self, doc_id: str, request_obj, layout_obj):
        """Append a revision + its computed layout.

        Returns ``(revision_no, deduped)`` where ``deduped`` is True if the
        identical request already exists for this document (the existing
        revision number is returned, nothing new is written).
        """
        req_json = canonical_json(request_obj)
        lay_json = canonical_json(layout_obj)
        req_hash = sha256_text(req_json)
        lay_hash = sha256_text(lay_json)
        with self._lock:
            existing = self._conn.execute(
                "SELECT revision_no FROM revisions "
                "WHERE doc_id = ? AND request_sha256 = ?",
                (doc_id, req_hash)).fetchone()
            if existing:
                return existing["revision_no"], True
            row = self._conn.execute(
                "SELECT COALESCE(MAX(revision_no), 0) AS m "
                "FROM revisions WHERE doc_id = ?", (doc_id,)).fetchone()
            no = row["m"] + 1
            self._conn.execute(
                "INSERT INTO revisions (doc_id, revision_no, request_json, "
                "request_sha256, created_at) VALUES (?, ?, ?, ?, ?)",
                (doc_id, no, req_json, req_hash, time.time()))
            self._conn.execute(
                "INSERT INTO layouts (doc_id, revision_no, status, "
                "layout_json, layout_sha256) VALUES (?, ?, ?, ?, ?)",
                (doc_id, no, layout_obj.get("status"), lay_json, lay_hash))
            self._conn.commit()
            return no, False

    def get_revision(self, doc_id: str, revision_no: int):
        self._require_document(doc_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT r.request_json, r.request_sha256, r.created_at, "
                "       l.status, l.layout_json, l.layout_sha256 "
                "FROM revisions r JOIN layouts l "
                "  ON l.doc_id = r.doc_id AND l.revision_no = r.revision_no "
                "WHERE r.doc_id = ? AND r.revision_no = ?",
                (doc_id, revision_no)).fetchone()
        if row is None:
            raise NotFound(f"revision {revision_no} not found")
        return {
            "revision_no": revision_no,
            "request": json.loads(row["request_json"]),
            "request_sha256": row["request_sha256"],
            "created_at": row["created_at"],
            "status": row["status"],
            "layout": json.loads(row["layout_json"]),
            "layout_sha256": row["layout_sha256"],
        }

    def latest_revision_no(self, doc_id: str) -> int:
        self._require_document(doc_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(revision_no) AS m FROM revisions WHERE doc_id = ?",
                (doc_id,)).fetchone()
        if row["m"] is None:
            raise NotFound("document has no revisions")
        return row["m"]

    def list_revisions(self, doc_id: str):
        self._require_document(doc_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.revision_no AS revision_no, r.request_sha256, "
                "r.created_at, l.status "
                "FROM revisions r JOIN layouts l "
                "  ON l.doc_id=r.doc_id AND l.revision_no=r.revision_no "
                "WHERE r.doc_id = ? ORDER BY r.revision_no", (doc_id,)).fetchall()
        return [dict(r) for r in rows]

    def _require_document(self, doc_id):
        if not self.document_exists(doc_id):
            raise NotFound(f"document {doc_id!r} not found")

    # -- finalization ------------------------------------------------------
    def finalize(self, doc_id: str, revision_no: int, json_text, svg_text,
                 replay_text):
        self._require_document(doc_id)
        rev = self.get_revision(doc_id, revision_no)
        if rev["status"] != "ok":
            raise Conflict(
                f"revision {revision_no} has status {rev['status']!r}; "
                "only 'ok' revisions can be finalized"
            )
        with self._lock:
            prior = self._conn.execute(
                "SELECT 1 FROM finalizations WHERE doc_id = ? "
                "AND revision_no = ?", (doc_id, revision_no)).fetchone()
            if prior:
                raise Conflict(
                    f"revision {revision_no} is already finalized"
                )
            base = os.path.join(self.exports_dir, doc_id,
                                f"rev{revision_no}")
            os.makedirs(base, exist_ok=True)
            json_path = os.path.join(base, "pages.json")
            svg_path = os.path.join(base, "pages.svg")
            replay_path = os.path.join(base, "replay.json")
            for path, text in ((json_path, json_text), (svg_path, svg_text),
                               (replay_path, replay_text)):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.chmod(path, 0o444)
            summary = sha256_text(
                sha256_text(json_text) + sha256_text(svg_text)
                + sha256_text(replay_text)
            )
            self._conn.execute(
                "INSERT INTO finalizations (doc_id, revision_no, finalized_at,"
                " json_path, svg_path, replay_path, summary_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_id, revision_no, time.time(), json_path, svg_path,
                 replay_path, summary))
            self._conn.commit()
            return {
                "json_path": json_path, "svg_path": svg_path,
                "replay_path": replay_path, "summary_sha256": summary,
            }

    def get_finalization(self, doc_id: str, revision_no: int):
        self._require_document(doc_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM finalizations WHERE doc_id = ? "
                "AND revision_no = ?", (doc_id, revision_no)).fetchone()
        if row is None:
            raise NotFound(
                f"revision {revision_no} is not finalized")
        return dict(row)
