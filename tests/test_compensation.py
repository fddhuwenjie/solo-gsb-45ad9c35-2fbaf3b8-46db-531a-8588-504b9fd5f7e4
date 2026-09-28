"""Tests for faulty-pin compensation (stdlib unittest)."""
import json
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
import urllib.request

from app.compensation import (
    MAX_PASSES_HARD_LIMIT, compensate, parse_compensation, rotate_dot,
)
from app.errors import Conflict, InputError
from app.exporter import export_all
from app.planner import plan
from app.server import build_server, get_store


def good_request():
    return {"body": "hello world singing test",
            "paper": {"cells_per_line": 10, "lines_per_page": 3},
            "rules": {"rules": [
                {"id": "ing", "pattern": "ing", "cells": ["346"]},
                {"id": "ll", "pattern": "ll", "cells": ["123"]}]}}


def _finalized_masks_by_cell(layout):
    """{(page, line, col): mask} for every non-empty finalized cell."""
    out = {}
    for pg in layout["pages"]:
        for ln in pg["lines"]:
            for cell in ln["cells"]:
                if cell["mask"]:
                    out[(pg["page"], ln["line"], cell["col"])] = cell["mask"]
    return out


def rebuild_page_masks(report, cpl, lpp):
    """Merge every pass back into page-coordinate (line,col) masks."""
    rebuilt = {}
    for pss in report["passes"]:
        rot = pss["orientation"] == "rotate_180"
        for pg in pss["pages"]:
            for ln in pg["lines"]:
                for cell in ln["cells"]:
                    if rot:
                        sline = lpp + 1 - cell["device_line"]
                        scol = cpl + 1 - cell["device_col"]
                    else:
                        sline, scol = cell["device_line"], cell["device_col"]
                    page_mask = 0
                    for d in cell["source"]["dots"]:
                        page_mask |= 1 << (d - 1)
                    key = (pg["page"], sline, scol)
                    rebuilt[key] = rebuilt.get(key, 0) | page_mask
    return rebuilt


def rebuild_device_masks(report):
    """{(pass, page, device_line, device_col): device mask}."""
    out = {}
    for pss in report["passes"]:
        for pg in pss["pages"]:
            for ln in pg["lines"]:
                for cell in ln["cells"]:
                    out[(pss["pass"], pg["page"], cell["device_line"],
                         cell["device_col"])] = cell["mask"]
    return out


class ParseTests(unittest.TestCase):
    def test_accepts_valid(self):
        working, dirs, mp = parse_compensation(
            {"working_dots": [6, 1, 3],
             "orientations": ["rotate_180", "normal"], "max_passes": 2})
        self.assertEqual(working, [1, 3, 6])  # sorted
        self.assertEqual(dirs, ["rotate_180", "normal"])  # caller order
        self.assertEqual(mp, 2)

    def test_empty_working_dots_is_syntactically_ok(self):
        working, _, _ = parse_compensation(
            {"working_dots": [], "orientations": ["normal"],
             "max_passes": 1})
        self.assertEqual(working, [])

    def test_bad_working_dots(self):
        cases = [
            [0], [7], [-1], ["1"], [True], [1.0], [None], "123", None,
        ]
        for bad in cases:
            with self.assertRaises(InputError):
                parse_compensation(
                    {"working_dots": bad,
                     "orientations": ["normal"], "max_passes": 1})

    def test_duplicate_pin(self):
        with self.assertRaises(InputError):
            parse_compensation(
                {"working_dots": [1, 1],
                 "orientations": ["normal"], "max_passes": 1})

    def test_bad_orientations(self):
        for bad in ([], ["sideways"], ["NORMAL"], [1], None, "normal",
                    ["normal", "normal"],
                    ["rotate_180", "rotate_180"]):
            with self.assertRaises(InputError):
                parse_compensation(
                    {"working_dots": [1], "orientations": bad,
                     "max_passes": 1})

    def test_bad_max_passes(self):
        for bad in (0, 3, -1, "1", True, 1.0, None,
                    MAX_PASSES_HARD_LIMIT + 1):
            with self.assertRaises(InputError):
                parse_compensation(
                    {"working_dots": [1],
                     "orientations": ["normal"], "max_passes": bad})

    def test_body_must_be_object(self):
        with self.assertRaises(InputError):
            parse_compensation([1, 2, 3])


class GeometryTests(unittest.TestCase):
    def test_rotate_dot_is_the_half_turn_swap(self):
        self.assertEqual([rotate_dot(d) for d in range(1, 7)],
                         [6, 5, 4, 3, 2, 1])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.req = good_request()
        self.layout = plan(self.req)
        self.assertEqual(self.layout["status"], "ok")
        self.cpl = self.req["paper"]["cells_per_line"]
        self.lpp = self.req["paper"]["lines_per_page"]

    def _compensate(self, pins, dirs, mp):
        return compensate(self.layout, self.req,
                          {"working_dots": pins, "orientations": dirs,
                           "max_passes": mp})

    def test_all_pins_normal_single_pass_reproduces_exactly(self):
        rep = self._compensate([1, 2, 3, 4, 5, 6], ["normal"], 1)
        self.assertEqual(rep["status"], "compensated")
        self.assertEqual(rep["pass_count"], 1)
        self.assertEqual(rep["passes"][0]["orientation"], "normal")
        rebuilt = rebuild_page_masks(rep, self.cpl, self.lpp)
        self.assertEqual(rebuilt, _finalized_masks_by_cell(self.layout))

    def test_merged_passes_equal_finalization_no_more_no_less(self):
        # pin 3 broken; dot-3 targets must be salvaged via rotation
        rep = self._compensate([1, 2, 4, 5, 6],
                               ["normal", "rotate_180"], 2)
        self.assertEqual(rep["pass_count"], 2)
        self.assertEqual([p["orientation"] for p in rep["passes"]],
                         ["normal", "rotate_180"])
        total = sum(p["dot_count"] for p in rep["passes"])
        self.assertEqual(total, rep["target_dot_count"])
        rebuilt = rebuild_page_masks(rep, self.cpl, self.lpp)
        # exactly the finalized dots -- no missing, no extra
        self.assertEqual(rebuilt, _finalized_masks_by_cell(self.layout))

    def test_first_pass_takes_every_dot_it_can_drive(self):
        rep = self._compensate([1, 2, 4, 5, 6],
                               ["normal", "rotate_180"], 2)
        p1, p2 = rep["passes"]
        # every emitted device dot must be driven by a working pin
        allowed_pins = {1, 2, 4, 5, 6}
        for pss in (p1, p2):
            for pg in pss["pages"]:
                for ln in pg["lines"]:
                    for cell in ln["cells"]:
                        self.assertTrue(
                            set(cell["device_dots"]) <= allowed_pins)
        # pass 2 only carries page dot 3 (the broken normal pin): under
        # rotation page dot 3 drives device pin 4.
        for pg in p2["pages"]:
            for ln in pg["lines"]:
                for cell in ln["cells"]:
                    self.assertEqual(cell["source"]["dots"], [3])
                    self.assertEqual(cell["device_dots"], [4])

    def test_rotated_device_coordinates_are_physical_inverse(self):
        rep = self._compensate([1, 2, 4, 5, 6],
                               ["normal", "rotate_180"], 2)
        p2 = rep["passes"][1]
        for pg in p2["pages"]:
            for ln in pg["lines"]:
                for cell in ln["cells"]:
                    src = cell["source"]
                    self.assertEqual(
                        cell["device_line"],
                        self.lpp + 1 - src["line"])
                    self.assertEqual(
                        cell["device_col"],
                        self.cpl + 1 - src["col"])

    def test_tie_break_prefers_normal_independent_of_request_order(self):
        # with every pin working a single pass suffices; the tie must go
        # to normal even when rotate_180 is listed first
        rep = self._compensate([1, 2, 3, 4, 5, 6],
                               ["rotate_180", "normal"], 2)
        self.assertEqual(rep["pass_count"], 1)
        self.assertEqual(rep["passes"][0]["orientation"], "normal")

    def test_rotate_only_uses_physical_coordinates(self):
        rep = self._compensate([1, 2, 3, 4, 5, 6], ["rotate_180"], 1)
        self.assertEqual(rep["pass_count"], 1)
        self.assertEqual(rep["passes"][0]["orientation"], "rotate_180")
        rebuilt = rebuild_page_masks(rep, self.cpl, self.lpp)
        self.assertEqual(rebuilt, _finalized_masks_by_cell(self.layout))

    def test_unreachable_dot_is_409_with_prefix(self):
        # pin 3 broken and rotation forbidden -> dot-3 cells impossible
        with self.assertRaises(Conflict) as ctx:
            self._compensate([1, 2, 4, 5, 6], ["normal"], 1)
        data = ctx.exception.args[0]
        self.assertEqual(data["reason"], "unreachable_dot")
        cell = data["first_unreachable_cell"]
        self.assertEqual(cell["dot"], 3)
        self.assertEqual(cell["page"], 1)
        self.assertEqual(data["missing_dots"], [3])
        self.assertIsNone(data["required_passes"])
        self.assertIn("verified_prefix", data)
        self.assertLessEqual(
            data["verified_prefix"]["page_count"], cell["page"] - 1)

    def test_rotate_cannot_save_when_counterpart_pin_broken(self):
        # page dot 3 needs device pin 4 under rotation; with pins 3 and 4
        # broken no orientation can produce dot 3
        with self.assertRaises(Conflict) as ctx:
            self._compensate([1, 2, 5, 6],
                             ["normal", "rotate_180"], 2)
        self.assertEqual(ctx.exception.args[0]["reason"],
                         "unreachable_dot")
        self.assertIn(3, ctx.exception.args[0]["missing_dots"])

    def test_max_passes_exceeded_is_409(self):
        # every dot individually reachable, but dot 1 and dot 6 force one
        # pass per orientation; pin 6 broken -> dot 6 only via rotation
        with self.assertRaises(Conflict) as ctx:
            self._compensate([1, 2, 3, 4, 5],
                             ["normal", "rotate_180"], 1)
        data = ctx.exception.args[0]
        self.assertEqual(data["reason"], "max_passes_exceeded")
        self.assertEqual(data["required_passes"], 2)
        self.assertEqual(data["max_passes_allowed"], 1)
        # and the same request succeeds with a two-pass budget
        rep = self._compensate([1, 2, 3, 4, 5],
                               ["normal", "rotate_180"], 2)
        self.assertEqual(rep["pass_count"], 2)
        self.assertEqual(
            rebuild_page_masks(rep, self.cpl, self.lpp),
            _finalized_masks_by_cell(self.layout))

    def test_409_verified_prefix_lists_pages_before_bad_page(self):
        req = {"body": "ab cd ef gh ij kl mn op",
               "paper": {"cells_per_line": 6, "lines_per_page": 2}}
        layout = plan(req)
        self.assertEqual(layout["status"], "ok")
        self.assertGreaterEqual(layout["page_count"], 2)
        with self.assertRaises(Conflict) as ctx:
            compensate(layout, req,
                       {"working_dots": [1, 2, 4, 5, 6],
                        "orientations": ["normal"], "max_passes": 1})
        data = ctx.exception.args[0]
        first_page = data["first_unreachable_cell"]["page"]
        vp = data["verified_prefix"]
        self.assertEqual(vp["page_count"], first_page - 1)
        self.assertEqual([p["page"] for p in vp["pages"]],
                         list(range(1, first_page)))
        # prefix pages are themselves fully embossable
        prefix_pages = {p["page"] for p in vp["pages"]}
        for pg in layout["pages"]:
            if pg["page"] in prefix_pages:
                for ln in pg["lines"]:
                    for cell in ln["cells"]:
                        for d in range(1, 7):
                            if (cell["mask"] >> (d - 1)) & 1:
                                self.assertIn(d, {1, 2, 4, 5, 6})

    def test_blank_document_shape_request_uses_zero_passes(self):
        # body with only spaces yields no raised dots
        req = {"body": "   ", "paper": {"cells_per_line": 5,
                                        "lines_per_page": 2}}
        layout = plan(req)
        self.assertEqual(layout["status"], "ok")
        rep = compensate(layout, req,
                         {"working_dots": [], "orientations": ["normal"],
                          "max_passes": 1})
        self.assertEqual(rep["pass_count"], 0)
        self.assertEqual(rep["target_dot_count"], 0)
        self.assertEqual(rep["passes"], [])

    def test_non_ok_layout_rejected(self):
        bad = {"status": "conflict", "pages": []}
        with self.assertRaises(InputError):
            compensate(bad, self.req,
                       {"working_dots": [1], "orientations": ["normal"],
                        "max_passes": 1})


class HttpEndpointTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        import app.server as server
        from app import config as cfg
        cfg.DATA_DIR = pathlib.Path(self.dir)
        cfg.DB_PATH = os.path.join(self.dir, "t.db")
        server._STORE = None
        self.store = get_store()
        self.store.create_document("d")
        layout = plan(good_request())
        self.req = good_request()
        self.no, _ = self.store.add_revision("d", self.req, layout)
        rev = self.store.get_revision("d", self.no)
        art = export_all(
            {"doc_id": "d", "revision_no": self.no,
             "request_sha256": rev["request_sha256"],
             "layout_sha256": rev["layout_sha256"]},
            rev["layout"], self.req)
        self.store.finalize("d", self.no, art["json"], art["svg"],
                            art["replay"])
        self.server = build_server(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _post(self, path, obj):
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_compensate_ok_over_http(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [1, 2, 3, 4, 5, 6],
             "orientations": ["normal"], "max_passes": 1})
        self.assertEqual(status, 200)
        self.assertEqual(rep["status"], "compensated")
        self.assertEqual(rep["document_id"], "d")
        self.assertEqual(rep["revision_no"], self.no)
        self.assertEqual(rep["pass_count"], 1)

    def test_compensate_409_over_http(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [1, 2, 4, 5, 6],
             "orientations": ["normal"], "max_passes": 1})
        self.assertEqual(status, 409)
        self.assertEqual(rep["error"], "conflict")
        self.assertEqual(rep["reason"], "unreachable_dot")
        self.assertEqual(rep["first_unreachable_cell"]["dot"], 3)
        self.assertIn("verified_prefix", rep)

    def test_compensate_bad_pin_is_400(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [9], "orientations": ["normal"],
             "max_passes": 1})
        self.assertEqual(status, 400)
        self.assertEqual(rep["error"], "invalid_request")

    def test_compensate_duplicate_orientation_is_400(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [1], "orientations": ["normal", "normal"],
             "max_passes": 1})
        self.assertEqual(status, 400)

    def test_compensate_passes_out_of_range_is_400(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [1], "orientations": ["normal"],
             "max_passes": 5})
        self.assertEqual(status, 400)

    def test_compensate_unknown_finalization_is_404(self):
        status, rep = self._post(
            "/documents/d/finalizations/99/compensate",
            {"working_dots": [1], "orientations": ["normal"],
             "max_passes": 1})
        self.assertEqual(status, 404)
        self.assertEqual(rep["error"], "not_found")

    def test_compensate_non_numeric_revision_is_400(self):
        status, rep = self._post(
            "/documents/d/finalizations/abc/compensate",
            {"working_dots": [1], "orientations": ["normal"],
             "max_passes": 1})
        self.assertEqual(status, 400)

    def test_compensate_does_not_modify_finalization(self):
        import sqlite3
        before = self._artifact_hash()
        self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [1, 2, 3, 4, 5],
             "orientations": ["normal", "rotate_180"], "max_passes": 2})
        self.assertEqual(before, self._artifact_hash())

    def _artifact_hash(self):
        row = self.store._conn.execute(
            "SELECT summary_sha256 FROM finalizations "
            "WHERE doc_id='d' AND revision_no=?", (self.no,)).fetchone()
        return row["summary_sha256"]


if __name__ == "__main__":
    unittest.main()
