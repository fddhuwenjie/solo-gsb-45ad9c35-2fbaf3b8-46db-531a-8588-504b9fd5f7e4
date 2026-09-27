"""HTTP service built only on the standard library (http.server)."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .diff import compare_finalizations
from .errors import Conflict, InputError, NotFound
from .exporter import export_all
from .planner import plan
from .readback import parse_readback, verify_readback
from .storage import Store, canonical_json, sha256_text

_STORE_LOCK = threading.Lock()
_STORE = None


def get_store():
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                exports = config.DATA_DIR / "exports"
                _STORE = Store(config.DB_PATH, exports)
    return _STORE


def public_request(req):
    """Drop planner-internal keys before persisting / echoing."""
    return {k: v for k, v in req.items() if not k.startswith("_")}


class Handler(BaseHTTPRequestHandler):
    server_version = "BraillePager/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        if __debug__:
            super().log_message(fmt, *args)

    # -- helpers -----------------------------------------------------------
    def _send(self, code, obj, headers=None):
        body = canonical_json(obj).encode("utf-8") + b"\n"
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code, text, content_type):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise InputError("missing JSON request body")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InputError(f"invalid JSON: {exc}")

    def _error(self, exc):
        if isinstance(exc, InputError):
            self._send(400, {"error": "invalid_request",
                             "message": str(exc)})
        elif isinstance(exc, NotFound):
            self._send(404, {"error": "not_found", "message": str(exc)})
        elif isinstance(exc, Conflict):
            self._send(409, {"error": "conflict", "message": str(exc)})
        else:
            self._send(500, {"error": "internal",
                             "message": f"{type(exc).__name__}: {exc}"})

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            qs = urllib.parse.parse_qs(parsed.query)
            if parts == ["health"]:
                self._send(200, {"status": "ok"})
            elif len(parts) == 2:
                doc_id = urllib.parse.unquote(parts[1])
                if parts[0] == "documents":
                    self._get_document(doc_id)
                else:
                    self._send(404, {"error": "not_found"})
            elif len(parts) == 4 and parts[0] == "documents" \
                    and parts[2] == "revisions":
                self._get_revision(
                    urllib.parse.unquote(parts[1]), int(parts[3]), qs)
            elif len(parts) == 4 and parts[0] == "documents" \
                    and parts[2] == "finalizations":
                if parts[3] == "diff":
                    self._send(404, {"error": "not_found"})
                else:
                    self._get_finalization(
                        urllib.parse.unquote(parts[1]), int(parts[3]))
            elif len(parts) == 5 and parts[0] == "documents" \
                    and parts[2] == "finalizations" \
                    and parts[3] == "diff" and parts[4] == "compare":
                self._compare_finalizations(
                    urllib.parse.unquote(parts[1]))
            elif len(parts) == 5 and parts[0] == "documents" \
                    and parts[2] == "finalizations":
                self._get_artifact(
                    urllib.parse.unquote(parts[1]), int(parts[3]),
                    parts[4])
            else:
                self._send(404, {"error": "not_found", "path": parsed.path})
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 1 and parts[0] == "plan":
                self._plan(dry_run=True)
            elif len(parts) == 3 and parts[0] == "documents" \
                    and parts[2] == "revisions":
                self._add_revision(urllib.parse.unquote(parts[1]))
            elif len(parts) == 4 and parts[0] == "documents" \
                    and parts[2] == "revisions" and parts[3] == "plan":
                # not expected; fall through
                self._send(404, {"error": "not_found"})
            elif len(parts) == 4 and parts[0] == "documents" \
                    and parts[2] == "revisions":
                self._send(404, {"error": "not_found"})
            elif len(parts) == 5 and parts[0] == "documents" \
                    and parts[2] == "revisions" \
                    and parts[4] == "finalize":
                self._finalize(urllib.parse.unquote(parts[1]),
                               int(parts[3]))
            elif len(parts) == 5 and parts[0] == "documents" \
                    and parts[2] == "finalizations" \
                    and parts[3] == "diff" and parts[4] == "compare":
                self._compare_finalizations(
                    urllib.parse.unquote(parts[1]))
            elif len(parts) == 5 and parts[0] == "documents" \
                    and parts[2] == "finalizations" \
                    and parts[4] == "verify":
                self._verify_readback(urllib.parse.unquote(parts[1]),
                                      int(parts[3]))
            else:
                self._send(404, {"error": "not_found", "path": parsed.path})
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    # -- handlers ----------------------------------------------------------
    def _plan(self, dry_run):
        payload = self._read_json()
        result = plan(payload)
        code = 200 if result["status"] == "ok" else 422
        result = {"input_sha256": sha256_text(canonical_json(payload)),
                  **result}
        self._send(code, result)

    def _add_revision(self, doc_id):
        payload = self._read_json()
        store = get_store()
        store.create_document(doc_id)
        # plan first, persist only after a successful computation
        from .planner import normalize_request
        req = normalize_request(payload)
        layout = plan(payload)
        stored_request = public_request(req)
        no, deduped = store.add_revision(doc_id, stored_request, layout)
        code = 200 if deduped else 201
        self._send(code, {
            "document_id": doc_id,
            "revision_no": no,
            "deduplicated": deduped,
            "request_sha256": sha256_text(canonical_json(stored_request)),
            "status": layout["status"],
            "finalizable": layout["status"] == "ok",
            "layout": _summary_layout(layout),
        })

    def _get_document(self, doc_id):
        store = get_store()
        if not store.document_exists(doc_id):
            raise NotFound(f"document {doc_id!r} not found")
        revs = store.list_revisions(doc_id)
        latest = max((r["revision_no"] for r in revs), default=None)
        self._send(200, {"document_id": doc_id, "latest_revision": latest,
                         "revisions": revs})

    def _get_revision(self, doc_id, no, qs):
        store = get_store()
        rev = store.get_revision(doc_id, no)
        full = qs.get("full", ["0"])[0] in ("1", "true", "yes")
        body = {
            "document_id": doc_id,
            "revision_no": no,
            "request_sha256": rev["request_sha256"],
            "created_at": rev["created_at"],
            "status": rev["status"],
            "request": rev["request"],
            "layout": rev["layout"] if full else _summary_layout(rev["layout"]),
        }
        self._send(200, body)

    def _finalize(self, doc_id, no):
        store = get_store()
        rev_full = store.get_revision(doc_id, no)
        artifacts = export_all(
            {"doc_id": doc_id, "revision_no": no,
             "request_sha256": rev_full["request_sha256"],
             "layout_sha256": rev_full["layout_sha256"]},
            rev_full["layout"], rev_full["request"])
        info = store.finalize(doc_id, no, artifacts["json"],
                              artifacts["svg"], artifacts["replay"])
        self._send(201, {
            "document_id": doc_id,
            "revision_no": no,
            "finalized": True,
            "summary_sha256": info["summary_sha256"],
            "artifacts": {
                "pages_json": f"/documents/{doc_id}/finalizations/{no}/pages.json",
                "pages_svg": f"/documents/{doc_id}/finalizations/{no}/pages.svg",
                "replay_json": f"/documents/{doc_id}/finalizations/{no}/replay.json",
            },
            "artifact_sha256": artifacts["artifact_sha256"],
        })

    def _get_finalization(self, doc_id, no):
        store = get_store()
        info = store.get_finalization(doc_id, no)
        self._send(200, {
            "document_id": doc_id, "revision_no": no,
            "finalized_at": info["finalized_at"],
            "summary_sha256": info["summary_sha256"],
            "artifacts": {
                "pages_json": f"/documents/{doc_id}/finalizations/{no}/pages.json",
                "pages_svg": f"/documents/{doc_id}/finalizations/{no}/pages.svg",
                "replay_json": f"/documents/{doc_id}/finalizations/{no}/replay.json",
            },
        })

    def _get_artifact(self, doc_id, no, name):
        store = get_store()
        info = store.get_finalization(doc_id, no)
        mapping = {
            "pages.json": ("json_path", "application/json; charset=utf-8"),
            "pages.svg": ("svg_path", "image/svg+xml; charset=utf-8"),
            "replay.json": ("replay_path", "application/json; charset=utf-8"),
        }
        if name not in mapping:
            raise NotFound(f"artifact {name!r} not found")
        key, ctype = mapping[name]
        with open(info[key], "r", encoding="utf-8") as fh:
            text = fh.read()
        self._send_text(200, text, ctype)

    def _verify_readback(self, doc_id, no):
        store = get_store()
        # 404 unless the revision is finalized; the finalization is never
        # re-planned or modified, only read.
        store.get_finalization(doc_id, no)
        rev = store.get_revision(doc_id, no)
        payload = self._read_json()
        measured = parse_readback(payload)
        report = verify_readback(rev["layout"], rev["request"]["body"],
                                 measured)
        self._send(200, {
            "document_id": doc_id,
            "revision_no": no,
            "request_sha256": rev["request_sha256"],
            "layout_sha256": rev["layout_sha256"],
            **report,
        })

    def _compare_finalizations(self, doc_id):
        store = get_store()
        payload = self._read_json()
        if not isinstance(payload, dict):
            raise InputError("comparison request body must be a JSON object")
        old_no = _revision_no(payload, "old_revision")
        new_no = _revision_no(payload, "new_revision")
        if old_no == new_no:
            raise InputError(
                "old_revision and new_revision must be different revisions")
        # 404 unless both revisions are finalized; their stored bodies and
        # per-page data are only read -- neither side is re-planned.
        store.get_finalization(doc_id, old_no)
        store.get_finalization(doc_id, new_no)
        old_rev = store.get_revision(doc_id, old_no)
        new_rev = store.get_revision(doc_id, new_no)
        report = compare_finalizations(old_rev, new_rev)
        self._send(200, {"document_id": doc_id, **report})


def _revision_no(payload, key):
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InputError(f"{key} must be a positive integer revision number")
    return value


def _summary_layout(layout):
    """Layout without full per-cell page payloads (for listings)."""
    if layout["status"] != "ok":
        return {
            "status": layout["status"],
            "conflict": layout.get("conflict"),
            "verified_prefix": {
                k: v for k, v in layout.get("verified_prefix", {}).items()
                if k != "pages"
            },
        }
    return {
        "status": "ok",
        "page_count": layout.get("page_count"),
        "binding": layout.get("binding"),
        "cross_references": layout.get("cross_references"),
        "iterations": layout.get("iterations"),
        "warnings": layout.get("warnings", []),
    }


def build_server(host=None, port=None):
    server = ThreadingHTTPServer(
        (host or config.HOST, port or config.PORT), Handler)
    return server
