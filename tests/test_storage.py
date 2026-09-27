"""Tests for the append-only SQLite store and exports (stdlib unittest)."""
import json
import os
import shutil
import tempfile
import unittest

from app.errors import Conflict
from app.exporter import export_all
from app.planner import plan
from app.storage import Store, canonical_json, sha256_text


def good_request():
    return {"body": "the quick brown fox",
            "paper": {"cells_per_line": 12, "lines_per_page": 2},
            "rules": {"rules": [
                {"id": "the", "pattern": "the", "cells": ["2346"]}]}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"),
                           os.path.join(self.dir, "exports"))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _add(self, doc="d", req=None):
        req = req or good_request()
        self.store.create_document(doc)
        layout = plan(req)
        no, deduped = self.store.add_revision(doc, req, layout)
        return no, deduped, layout

    def test_revisions_append_only(self):
        n1, d1, _ = self._add()
        self.assertEqual((n1, d1), (1, False))
        n2, d2, _ = self._add()
        self.assertEqual((n2, d2), (1, True))  # identical -> deduped
        # a genuinely different request appends
        req2 = dict(good_request())
        req2["body"] = "a totally different body of text here now"
        n3, d3, _ = self._add(req=req2)
        self.assertEqual((n3, d3), (2, False))
        revs = self.store.list_revisions("d")
        self.assertEqual([r["revision_no"] for r in revs], [1, 2])

    def test_key_order_independent_digest(self):
        a = good_request()
        b = {"rules": {"rules": [{"cells": ["2346"], "pattern": "the",
                                  "id": "the"}]},
             "paper": {"lines_per_page": 2, "cells_per_line": 12},
             "body": "the quick brown fox"}
        self.assertEqual(canonical_json(a), canonical_json(b))
        self.assertEqual(sha256_text(canonical_json(a)),
                         sha256_text(canonical_json(b)))

    def test_cannot_finalize_conflicting_revision(self):
        bad = {"body": "ab cd ef gh",
               "paper": {"cells_per_line": 5, "lines_per_page": 1},
               "unbreakable": [
                   {"id": "u1", "char_start": 3, "char_end": 11}]}
        no, _, layout = self._add(req=bad)
        self.assertEqual(layout["status"], "conflict")
        with self.assertRaises(Conflict):
            self.store.finalize("d", no, "{}", "<svg/>", "{}")

    def test_finalize_writes_immutable_artifacts_and_is_idempotent_block(self):
        no, _, layout = self._add()
        req = good_request()
        art = export_all({"doc_id": "d", "revision_no": no,
                          "request_sha256": "x", "layout_sha256": "y"},
                         layout, req)
        info = self.store.finalize("d", no, art["json"], art["svg"],
                                   art["replay"])
        # files exist and are read-only
        for key in ("json_path", "svg_path", "replay_path"):
            self.assertTrue(os.path.exists(info[key]))
            self.assertFalse(os.access(info[key], os.W_OK))
        # replay reproduces a request with the same digest
        with open(info["replay_path"], encoding="utf-8") as fh:
            replay = json.load(fh)
        self.assertEqual(
            sha256_text(canonical_json(replay["request"])),
            sha256_text(canonical_json(req)))
        # second finalization is rejected (append-only, one finalization)
        with self.assertRaises(Conflict):
            self.store.finalize("d", no, art["json"], art["svg"],
                                art["replay"])

    def test_svg_is_well_formed(self):
        import xml.etree.ElementTree as ET
        no, _, layout = self._add()
        art = export_all({"doc_id": "d", "revision_no": no,
                          "request_sha256": "x", "layout_sha256": "y"},
                         layout, good_request())
        root = ET.fromstring(art["svg"])
        self.assertEqual(root.tag.split("}")[-1], "svg")
        circles = art["svg"].count("<circle")
        # one circle per *raised* dot (blank/space cells raise none)
        raised = sum(
            bin(c["mask"]).count("1")
            for pg in layout["pages"] for ln in pg["lines"]
            for c in ln["cells"] if c["mask"] != 0)
        self.assertEqual(circles, raised)
        self.assertGreater(circles, 0)


if __name__ == "__main__":
    unittest.main()
