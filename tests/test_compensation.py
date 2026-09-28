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
    MAX_PASSES, NORMAL, ROTATE_180, compensate,
    parse_compensation_request,
)
from app.errors import InfeasibleCompensation, InputError
from app.planner import plan
from app.server import build_server, get_store

ALL = list(range(1, 7))


def request_obj():
    # one cell with dots 1 and 6: forcing both orientations when pin 1
    # is broken (normal makes dot 6 on pin 6, rotate makes dot 1 on pin 6).
    return {"body": "x",
            "paper": {"cells_per_line": 4, "lines_per_page": 3},
            "rules": {"rules": [
                {"id": "r", "pattern": "x", "cells": ["16"]}]}}


def layout_of(obj=None):
    obj = obj or request_obj()
    layout = plan(obj)
    assert layout["status"] == "ok"
    return layout


def flattened(layout):
    """Finalized non-blank cells as {(page,line,col): mask}."""
    out = {}
    for pg in layout["pages"]:
        for ln in pg["lines"]:
            for c in ln["cells"]:
                if c["mask"]:
                    out[(pg["page"], ln["line"], c["col"])] = c["mask"]
    return out


def merged_masks(report, paper):
    """Inverse-map every emitted pass cell back to finalized dot masks."""
    width = paper["cells_per_line"]
    lpp = paper["lines_per_page"]
    merged = {}
    for ps in report["passes"]:
        rot = ps["orientation"] == ROTATE_180
        for pg in ps["pages"]:
            for ln in pg["lines"]:
                for c in ln["cells"]:
                    if rot:
                        sp = pg["page"]
                        sl = lpp + 1 - ln["line"]
                        sc = width + 1 - c["col"]
                        # device pin p strikes logical dot 7-p
                        dot_mask = 0
                        for pin in c["dots"]:
                            dot_mask |= 1 << (7 - pin - 1)
                    else:
                        sp, sl, sc = pg["page"], ln["line"], c["col"]
                        dot_mask = c["mask"]
                    key = (sp, sl, sc)
                    merged[key] = merged.get(key, 0) | dot_mask
    return merged


def assert_exact(test, report, layout, paper):
    expected = flattened(layout)
    merged = merged_masks(report, paper)
    test.assertEqual(set(merged), set(expected))
    for key, mask in expected.items():
        test.assertEqual(merged[key], mask, f"mismatch at {key}")
    test.assertEqual(report["merged_cell_count"], len(expected))
    # every pass source maps back to its claimed finalized cell
    for ps in report["passes"]:
        rot = ps["orientation"] == ROTATE_180
        for pg in ps["pages"]:
            for ln in pg["lines"]:
                for c in ln["cells"]:
                    src = c["source"]
                    if rot:
                        test.assertEqual(
                            (pg["page"], ln["line"], c["col"]),
                            (src["page"],
                             paper["lines_per_page"] + 1 - src["line"],
                             paper["cells_per_line"] + 1 - src["col"]))
                    else:
                        test.assertEqual(
                            (pg["page"], ln["line"], c["col"]),
                            (src["page"], src["line"], src["col"]))
                    # fired pins correspond exactly to the source dots
                    expected_dots = (sorted(7 - d for d in src["dots"])
                                     if rot else sorted(src["dots"]))
                    test.assertEqual(c["dots"], expected_dots)
                    # assigned dots must be a subset of the finalized cell
                    src_mask = expected[(src["page"], src["line"],
                                         src["col"])]
                    for d in src["dots"]:
                        test.assertTrue(src_mask >> (d - 1) & 1)


class ParseTests(unittest.TestCase):
    def test_ok(self):
        wd, ori, mp = parse_compensation_request(
            {"working_dots": [6, 1, 3],
             "orientations": ["rotate_180", "normal"],
             "max_passes": 2})
        self.assertEqual(wd, [1, 3, 6])
        self.assertEqual(ori, ["rotate_180", "normal"])
        self.assertEqual(mp, 2)

    def test_empty_working_dots_ok(self):
        wd, _o, _m = parse_compensation_request(
            {"working_dots": [], "orientations": ["normal"],
             "max_passes": 1})
        self.assertEqual(wd, [])

    def test_bad_dot(self):
        for bad in (0, 7, -1, "1", 1.0, True, None):
            with self.assertRaises(InputError):
                parse_compensation_request(
                    {"working_dots": [bad], "orientations": ["normal"],
                     "max_passes": 1})

    def test_duplicate_dot(self):
        with self.assertRaises(InputError):
            parse_compensation_request(
                {"working_dots": [1, 1], "orientations": ["normal"],
                 "max_passes": 1})

    def test_bad_orientation(self):
        for bad in ("sideways", 1, None, True):
            with self.assertRaises(InputError):
                parse_compensation_request(
                    {"working_dots": ALL, "orientations": [bad],
                     "max_passes": 1})

    def test_duplicate_orientation(self):
        with self.assertRaises(InputError):
            parse_compensation_request(
                {"working_dots": ALL,
                 "orientations": ["normal", "normal"],
                 "max_passes": 2})

    def test_empty_orientations(self):
        with self.assertRaises(InputError):
            parse_compensation_request(
                {"working_dots": ALL, "orientations": [],
                 "max_passes": 1})

    def test_bad_max_passes(self):
        for bad in (0, 3, -1, "2", 1.0, True, None):
            with self.assertRaises(InputError):
                parse_compensation_request(
                    {"working_dots": ALL, "orientations": ["normal"],
                     "max_passes": bad})

    def test_non_object(self):
        with self.assertRaises(InputError):
            parse_compensation_request([])


class CompensateTests(unittest.TestCase):
    def setUp(self):
        self.obj = request_obj()
        self.layout = layout_of(self.obj)
        self.fin = {"pages": self.layout["pages"]}
        self.paper = self.obj["paper"]

    def test_all_pins_single_normal_pass(self):
        rep = compensate(self.fin, self.paper, ALL,
                         [NORMAL, ROTATE_180], 2)
        self.assertEqual(rep["pass_count"], 1)
        self.assertEqual(rep["orientations"], [NORMAL])
        self.assertEqual(rep["target_dot_count"], 2)
        assert_exact(self, rep, self.layout, self.paper)

    def test_tie_break_prefers_normal_single_pass(self):
        # only pins {1,6} work; both a single normal and a single rotate
        # pass cover dots {1,6} -- normal must win.
        rep = compensate(self.fin, self.paper, [1, 6],
                         [NORMAL, ROTATE_180], 1)
        self.assertEqual(rep["orientations"], [NORMAL])

    def test_broken_pin_needs_two_passes_normal_first(self):
        # pin 1 broken: dot 6 -> normal pin 6; dot 1 -> rotate pin 6.
        rep = compensate(self.fin, self.paper, [2, 3, 4, 5, 6],
                         [NORMAL, ROTATE_180], 2)
        self.assertEqual(rep["pass_count"], 2)
        self.assertEqual(rep["orientations"], [NORMAL, ROTATE_180])
        assert_exact(self, rep, self.layout, self.paper)
        normal, rotate = rep["passes"]
        self.assertEqual(normal["pass_no"], 1)
        self.assertEqual(rotate["pass_no"], 2)
        # normal pass device cell = finalized (1,1,1), only dot 6 -> pin 6
        self.assertEqual(normal["pages"][0]["lines"][0]["cells"][0]["mask"],
                         1 << 5)
        rcell = rotate["pages"][0]["lines"][0]["cells"][0]
        # rotate: line 3+1-1=3, col 4+1-1=4, dot 1 -> pin 6
        self.assertEqual((rotate["pages"][0]["page"],
                          rotate["pages"][0]["lines"][0]["line"],
                          rcell["col"]), (1, 3, 4))
        self.assertEqual(rcell["mask"], 1 << 5)
        self.assertEqual(rcell["source"]["dots"], [1])

    def test_single_rotate_pass_when_normal_disallowed(self):
        # dots {1,2}, pin 1 broken: rotate maps them onto working pins
        # 6 and 5, so a rotate-only pass covers everything.
        obj = {"body": "ab",
               "paper": {"cells_per_line": 4, "lines_per_page": 3},
               "rules": {"rules": []}}
        layout = layout_of(obj)
        rep = compensate({"pages": layout["pages"]}, obj["paper"],
                         [2, 3, 4, 5, 6], [ROTATE_180], 1)
        self.assertEqual(rep["orientations"], [ROTATE_180])
        assert_exact(self, rep, layout, obj["paper"])

    def test_cap_one_chooses_rotate_when_normal_cannot_cover(self):
        # dots {1,2}; pin 1 broken: normal misses dot 1, but rotate maps
        # dot 1 -> pin 6 and dot 2 -> pin 5 (both work): one rotate pass.
        obj = {"body": "ab",
               "paper": {"cells_per_line": 4, "lines_per_page": 3},
               "rules": {"rules": []}}
        layout = layout_of(obj)
        rep = compensate({"pages": layout["pages"]}, obj["paper"],
                         [2, 3, 4, 5, 6], [NORMAL, ROTATE_180], 1)
        self.assertEqual(rep["pass_count"], 1)
        self.assertEqual(rep["orientations"], [ROTATE_180])
        assert_exact(self, rep, layout, obj["paper"])

    def test_cap_one_infeasible_when_cell_needs_both_orientations(self):
        # dots {1,6}, pin 1 broken: normal only makes dot 6, rotate only
        # makes dot 1 (dot 6 rotates onto broken pin 1): one pass cannot
        # cover the cell, two passes can.
        with self.assertRaises(InfeasibleCompensation) as ctx:
            compensate(self.fin, self.paper, [2, 3, 4, 5, 6],
                       [NORMAL, ROTATE_180], 1)
        d = ctx.exception.details
        cell = d["first_unreachable_cell"]
        self.assertEqual((cell["page"], cell["line"], cell["col"]),
                         (1, 1, 1))
        # pass-cap report is against the normal baseline: dot 1 is what it
        # cannot carry.
        self.assertEqual(cell["unreachable_dots"], [1])
        self.assertEqual(d["missing_dots"], [1])
        # and the same inputs succeed with two passes.
        rep = compensate(self.fin, self.paper, [2, 3, 4, 5, 6],
                         [NORMAL, ROTATE_180], 2)
        assert_exact(self, rep, self.layout, self.paper)

    def test_union_gap_409(self):
        # pins 1 and 6 broken: neither orientation can make dots 1/6.
        try:
            compensate(self.fin, self.paper, [2, 3, 4, 5],
                       [NORMAL, ROTATE_180], 2)
        except InfeasibleCompensation as exc:
            d = exc.details
            self.assertEqual(d["first_unreachable_cell"]["page"], 1)
            self.assertEqual(d["first_unreachable_cell"]["line"], 1)
            self.assertEqual(d["first_unreachable_cell"]["col"], 1)
            self.assertEqual(
                d["first_unreachable_cell"]["unreachable_dots"], [1, 6])
            self.assertEqual(d["missing_dots"], [1, 6])
            self.assertEqual(d["verified_prefix"]["page_count"], 0)
        else:
            self.fail("expected InfeasibleCompensation")

    def test_rotate_coordinates_multi_cell(self):
        obj = {"body": "ab",
               "paper": {"cells_per_line": 4, "lines_per_page": 3},
               "rules": {"rules": []}}
        layout = layout_of(obj)
        rep = compensate({"pages": layout["pages"]}, obj["paper"], ALL,
                         [ROTATE_180], 1)
        assert_exact(self, rep, layout, obj["paper"])

    def test_verified_prefix_counts_full_pages(self):
        # One cell per page.  Page 1 has dots {3,4} (reachable via pins
        # 3/4 in either orientation), page 2 has dots {1,6} (with pins
        # 1 and 6 broken, no orientation reaches them).  Page 1 is the
        # verified prefix; page 2 is the first blocking cell.
        obj = {"body": "k x",
               "paper": {"cells_per_line": 1, "lines_per_page": 1},
               "rules": {"rules": [
                   {"id": "ok", "pattern": "k", "cells": ["34"]},
                   {"id": "bad", "pattern": "x", "cells": ["16"]}]}}
        layout = layout_of(obj)
        self.assertEqual(layout["page_count"], 2)
        with self.assertRaises(InfeasibleCompensation) as ctx:
            compensate({"pages": layout["pages"]}, obj["paper"],
                       [2, 3, 4, 5], [NORMAL, ROTATE_180], 2)
        d = ctx.exception.details
        prefix = d["verified_prefix"]
        self.assertEqual(prefix["page_count"], 1)
        self.assertEqual(prefix["pages"], [1])
        self.assertEqual(prefix["target_dots"], 2)
        self.assertEqual(d["first_unreachable_cell"]["page"], 2)
        self.assertEqual(
            d["first_unreachable_cell"]["unreachable_dots"], [1, 6])
        self.assertEqual(d["missing_dots"], [1, 6])


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
        self.obj = request_obj()
        layout = plan(self.obj)
        self.no, _ = self.store.add_revision("d", self.obj, layout)
        from app.exporter import export_all
        rev = self.store.get_revision("d", self.no)
        art = export_all(
            {"doc_id": "d", "revision_no": self.no,
             "request_sha256": rev["request_sha256"],
             "layout_sha256": rev["layout_sha256"]},
            rev["layout"], self.obj)
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
            {"working_dots": [2, 3, 4, 5, 6],
             "orientations": ["normal", "rotate_180"],
             "max_passes": 2})
        self.assertEqual(status, 200)
        self.assertEqual(rep["status"], "compensated")
        self.assertEqual(rep["orientations"],
                         ["normal", "rotate_180"])
        self.assertTrue(rep["exact"])

    def test_infeasible_is_409(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [2, 3, 4, 5],
             "orientations": ["normal", "rotate_180"],
             "max_passes": 2})
        self.assertEqual(status, 409)
        self.assertEqual(rep["error"], "infeasible_compensation")
        self.assertIn("first_unreachable_cell", rep)
        self.assertIn("missing_dots", rep)
        self.assertIn("verified_prefix", rep)

    def test_bad_input_is_400(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": [9],
             "orientations": ["normal"], "max_passes": 1})
        self.assertEqual(status, 400)
        self.assertEqual(rep["error"], "invalid_request")

    def test_unknown_finalization_is_404(self):
        status, rep = self._post(
            "/documents/d/finalizations/99/compensate",
            {"working_dots": ALL, "orientations": ["normal"],
             "max_passes": 1})
        self.assertEqual(status, 404)

    def test_bad_revision_number_is_400(self):
        status, rep = self._post(
            "/documents/d/finalizations/abc/compensate",
            {"working_dots": ALL, "orientations": ["normal"],
             "max_passes": 1})
        self.assertEqual(status, 400)
        self.assertEqual(rep["error"], "invalid_request")

    def test_max_passes_too_large_is_400(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/compensate",
            {"working_dots": ALL,
             "orientations": ["normal", "rotate_180"],
             "max_passes": 3})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
