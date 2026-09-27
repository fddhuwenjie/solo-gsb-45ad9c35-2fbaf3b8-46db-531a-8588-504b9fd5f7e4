"""Tests for embossed-proof readback verification (stdlib unittest)."""
import json
import os
import pathlib
import shutil
import tempfile
import threading
import unittest
import urllib.request

from app.errors import InputError
from app.planner import plan
from app.readback import (
    EXTRA, MATCH, OMISSION, SUBSTITUTE, align_line, parse_readback,
    verify_readback,
)
from app.server import build_server, get_store


def good_request():
    return {"body": "hello world singing test",
            "paper": {"cells_per_line": 10, "lines_per_page": 3},
            "rules": {"rules": [
                {"id": "ing", "pattern": "ing", "cells": ["346"]},
                {"id": "ll", "pattern": "ll", "cells": ["123"]}]}}


def cell(mask, col, gs=0, ge=1, cs=0, ce=1):
    return {"dots": "", "unicode": chr(0x2800 | mask), "mask": mask,
            "col": col, "grapheme_start": gs, "grapheme_end": ge,
            "char_start": cs, "char_end": ce, "rule_id": None}


def ops_sig(ops):
    return [(op, ei, mi) for op, ei, mi in ops]


class ParseTests(unittest.TestCase):
    def test_accepts_canonical_and_bare_shapes(self):
        payload = {"pages": [
            {"lines": [[1, 2, 3], [0]]},
            [[63]],
        ]}
        pages = parse_readback(payload)
        self.assertEqual(pages, [[[1, 2, 3], [0]], [[63]]])

    def test_rejects_non_object(self):
        with self.assertRaises(InputError):
            parse_readback([[[1]]])

    def test_rejects_missing_pages(self):
        with self.assertRaises(InputError):
            parse_readback({})

    def test_rejects_bad_mask(self):
        for bad in (64, -1, "1", 1.0, True, None):
            with self.assertRaises(InputError):
                parse_readback({"pages": [{"lines": [[bad]]}]})

    def test_rejects_bad_structure(self):
        with self.assertRaises(InputError):
            parse_readback({"pages": ["nope"]})
        with self.assertRaises(InputError):
            parse_readback({"pages": [{"lines": "nope"}]})
        with self.assertRaises(InputError):
            parse_readback({"pages": [{"lines": ["nope"]}]})


class AlignmentTests(unittest.TestCase):
    def test_perfect_line(self):
        exp = [cell(1, 1), cell(2, 2)]
        ops, cost = align_line(exp, [1, 2])
        self.assertEqual(cost, 0)
        self.assertEqual(ops_sig(ops),
                         [(MATCH, 0, 0), (MATCH, 1, 1)])

    def test_single_omission_does_not_shift_later_cells(self):
        # expected a b c ; measured a c
        # equal-cost options: omit b (match c), or substitute b<-c plus
        # extra c.  The omission must win so later cells stay aligned.
        exp = [cell(1, 1), cell(2, 2), cell(3, 3)]
        ops, cost = align_line(exp, [1, 3])
        self.assertEqual(cost, 1)
        self.assertEqual(ops_sig(ops),
                         [(MATCH, 0, 0), (OMISSION, 1, None),
                          (MATCH, 2, 1)])

    def test_omission_at_start_and_end(self):
        exp = [cell(1, 1), cell(2, 2)]
        ops, _ = align_line(exp, [2])
        self.assertEqual(ops_sig(ops),
                         [(OMISSION, 0, None), (MATCH, 1, 0)])
        ops, _ = align_line(exp, [1])
        self.assertEqual(ops_sig(ops),
                         [(MATCH, 0, 0), (OMISSION, 1, None)])

    def test_extra_cell_does_not_eclipse_a_substitution(self):
        # expected a b ; measured a x b: one extra is the unique minimum.
        exp = [cell(1, 1), cell(2, 2)]
        ops, cost = align_line(exp, [1, 9, 2])
        self.assertEqual(cost, 1)
        self.assertEqual(ops_sig(ops),
                         [(MATCH, 0, 0), (EXTRA, None, 1),
                          (MATCH, 1, 2)])

    def test_equal_cost_substitution_preferred_to_omission_extra(self):
        # exp [a, b], meas [b, a]: minimum cost 2 both for
        # sub a<-b + sub b<-a and for extra/omit pairs.  The stream that
        # substitutes at the first divergence must be preferred.
        exp = [cell(1, 1), cell(2, 2)]
        ops, cost = align_line(exp, [2, 1])
        self.assertEqual(cost, 2)
        self.assertEqual([o[0] for o in ops], [SUBSTITUTE, SUBSTITUTE])

    def test_omission_preferred_to_extra_under_equal_cost(self):
        # exp [a], meas [b]: only substitution fits (cost 1).
        # exp [a, b], meas [c]: either sub a<-c + omit b, or
        # extra c + omit a + match b -- the former is cheaper, so test a
        # genuine omit/extra cost tie: exp [a], meas [] vs exp [] meas[b]
        ops, _ = align_line([cell(1, 1)], [])
        self.assertEqual([o[0] for o in ops], [OMISSION])
        ops, _ = align_line([], [2])
        self.assertEqual([o[0] for o in ops], [EXTRA])

    def test_empty_measured_line_all_omissions(self):
        exp = [cell(1, 1), cell(2, 2)]
        ops, cost = align_line(exp, [])
        self.assertEqual(cost, 2)
        self.assertEqual([o[0] for o in ops], [OMISSION, OMISSION])

    def test_empty_expected_line_all_extras(self):
        ops, cost = align_line([], [5, 6])
        self.assertEqual(cost, 2)
        self.assertEqual([o[0] for o in ops], [EXTRA, EXTRA])


class VerifyReportTests(unittest.TestCase):
    def setUp(self):
        self.layout = plan(good_request())
        self.assertEqual(self.layout["status"], "ok")
        self.body = good_request()["body"]
        self.pages = [
            [[c["mask"] for c in ln["cells"]] for ln in pg["lines"]]
            for pg in self.layout["pages"]
        ]

    def test_perfect_readback(self):
        rep = verify_readback(self.layout, self.body, self.pages)
        self.assertTrue(rep["perfect"])
        self.assertEqual(rep["discrepancy_count"], 0)
        self.assertEqual(rep["totals"]["edit_cost"], 0)
        self.assertEqual(rep["totals"]["omitted"], 0)
        self.assertEqual(rep["totals"]["extra"], 0)
        self.assertEqual(rep["totals"]["dot_differences"], 0)
        self.assertEqual(rep["page_count"], len(self.pages))
        self.assertEqual(
            rep["totals"]["matched"],
            sum(1 for pg in self.layout["pages"] for ln in pg["lines"]
                for c in ln["cells"]))

    def test_missing_cell_reported_as_omission_without_cascade(self):
        # drop the second measured cell on the first non-empty line
        p = l = 0
        while not self.pages[p][l]:
            l += 1
        broken = [list(line) for line in self.pages[p]]
        dropped_mask = broken[l].pop(1)
        pages = [pg for pg in self.pages]
        pages[p] = broken
        rep = verify_readback(self.layout, self.body, pages)
        self.assertFalse(rep["perfect"])
        self.assertEqual(len(rep["omissions"]), 1)
        # no dot differences: the following cells must stay aligned
        self.assertEqual(rep["dot_differences"], [])
        om = rep["omissions"][0]
        self.assertEqual(om["kind"], "omission")
        self.assertEqual(om["expected"]["page"], p + 1)
        exp_cell = self.layout["pages"][p]["lines"][l]["cells"][1]
        self.assertEqual(om["expected_mask"], exp_cell["mask"])
        self.assertEqual(om["expected_mask"], dropped_mask)
        self.assertEqual(om["char_start"], exp_cell["char_start"])
        self.assertEqual(om["char_end"], exp_cell["char_end"])
        self.assertEqual(om["grapheme_start"], exp_cell["grapheme_start"])
        self.assertEqual(om["text"],
                         self.body[exp_cell["char_start"]:
                                  exp_cell["char_end"]])
        self.assertEqual(rep["pages"][p]["omitted"], 1)
        self.assertEqual(rep["totals"]["omitted"], 1)
        self.assertEqual(rep["totals"]["edit_cost"], 1)

    def test_extra_cell_report(self):
        p = 0
        l = next(i for i, line in enumerate(self.pages[p]) if line)
        broken = [list(x) for x in self.pages[p]]
        broken[l].insert(1, 63)
        pages = [pg for pg in self.pages]
        pages[p] = broken
        rep = verify_readback(self.layout, self.body, pages)
        self.assertEqual(len(rep["extras"]), 1)
        ex = rep["extras"][0]
        self.assertEqual(ex["kind"], "extra")
        self.assertEqual(ex["measured"]["page"], p + 1)
        self.assertEqual(ex["measured"]["line"], l + 1)
        self.assertEqual(ex["measured"]["cell_index"], 2)
        self.assertEqual(ex["measured_mask"], 63)
        self.assertEqual(ex["measured_dots"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(rep["totals"]["extra"], 1)

    def test_dot_difference_lists_missing_and_extra_dots(self):
        p = 0
        l = next(i for i, line in enumerate(self.pages[p]) if line)
        exp_cell = self.layout["pages"][p]["lines"][l]["cells"][0]
        broken = [list(x) for x in self.pages[p]]
        # flip dots 1 and 2 on the first cell
        broken[l][0] = exp_cell["mask"] ^ 0b11
        pages = [pg for pg in self.pages]
        pages[p] = broken
        rep = verify_readback(self.layout, self.body, pages)
        self.assertEqual(len(rep["dot_differences"]), 1)
        diff = rep["dot_differences"][0]
        self.assertEqual(diff["expected"]["col"], exp_cell["col"])
        self.assertEqual(diff["measured"]["cell_index"], 1)
        if exp_cell["mask"] & 0b11:
            # original dots among {1,2} are missing, the rest are extra
            self.assertEqual(diff["missing_dots"],
                             [d for d in (1, 2) if exp_cell["mask"] >> (d - 1) & 1])
            self.assertEqual(diff["extra_dots"],
                             [d for d in (1, 2)
                              if not (exp_cell["mask"] >> (d - 1) & 1)])
        else:
            self.assertEqual(diff["missing_dots"], [])
            self.assertEqual(diff["extra_dots"], [1, 2])
        self.assertEqual(diff["char_start"], exp_cell["char_start"])
        self.assertEqual(diff["grapheme_start"], exp_cell["grapheme_start"])
        self.assertEqual(diff["text"],
                         self.body[exp_cell["char_start"]:
                                  exp_cell["char_end"]])
        self.assertEqual(rep["totals"]["dot_differences"], 1)

    def test_page_count_mismatch(self):
        with self.assertRaises(InputError):
            verify_readback(self.layout, self.body,
                            self.pages + [[[0]]])
        with self.assertRaises(InputError):
            verify_readback(self.layout, self.body, self.pages[:-1])

    def test_line_count_mismatch(self):
        pages = [list(pg) for pg in self.pages]
        pages[0].append([1])
        with self.assertRaises(InputError):
            verify_readback(self.layout, self.body, pages)

    def test_page_and_total_counts_consistent(self):
        p = 0
        l = next(i for i, line in enumerate(self.pages[p]) if line)
        broken = [list(x) for x in self.pages[p]]
        broken[l].pop(0)
        broken[l].append(63)
        pages = [pg for pg in self.pages]
        pages[p] = broken
        rep = verify_readback(self.layout, self.body, pages)
        # per-page sums equal the totals
        for key in ("matched", "substituted", "omitted", "extra",
                    "dot_differences", "missing_dots", "extra_dots",
                    "edit_cost", "expected_cells", "measured_cells"):
            self.assertEqual(rep["totals"][key],
                             sum(pg[key] for pg in rep["pages"]),
                             key)


class HttpEndpointTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        import app.server as server
        from app import config as cfg
        # config constants are read at call time; redirect them at the
        # temp dir and force a fresh store.
        cfg.DATA_DIR = pathlib.Path(self.dir)
        cfg.DB_PATH = os.path.join(self.dir, "t.db")
        server._STORE = None
        self.store = get_store()
        self.store.create_document("d")
        layout = plan(good_request())
        self.no, _ = self.store.add_revision("d", good_request(), layout)
        from app.exporter import export_all
        req = good_request()
        rev = self.store.get_revision("d", self.no)
        art = export_all(
            {"doc_id": "d", "revision_no": self.no,
             "request_sha256": rev["request_sha256"],
             "layout_sha256": rev["layout_sha256"]},
            rev["layout"], req)
        self.store.finalize("d", self.no, art["json"], art["svg"],
                            art["replay"])
        self.layout = rev["layout"]
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

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, obj):
        data = json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_verify_perfect_over_http(self):
        pages = [
            {"lines": [[c["mask"] for c in ln["cells"]]
                       for ln in pg["lines"]]}
            for pg in self.layout["pages"]
        ]
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/verify",
            {"pages": pages})
        self.assertEqual(status, 200)
        self.assertTrue(rep["perfect"])
        self.assertEqual(rep["document_id"], "d")

    def test_verify_bad_mask_is_400(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/verify",
            {"pages": [{"lines": [[99]]}] +
                      [{"lines": []} for _ in
                       range(len(self.layout["pages"]) - 1)]})
        self.assertEqual(status, 400)
        self.assertEqual(rep["error"], "invalid_request")

    def test_verify_page_count_mismatch_is_400(self):
        status, rep = self._post(
            f"/documents/d/finalizations/{self.no}/verify",
            {"pages": [{"lines": []}]})
        self.assertEqual(status, 400)

    def test_verify_unknown_finalization_is_404(self):
        status, rep = self._post(
            "/documents/d/finalizations/99/verify", {"pages": []})
        self.assertEqual(status, 404)
        self.assertEqual(rep["error"], "not_found")

    def test_verify_missing_body_is_400(self):
        req = urllib.request.Request(
            self._url(f"/documents/d/finalizations/{self.no}/verify"),
            data=b"", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
