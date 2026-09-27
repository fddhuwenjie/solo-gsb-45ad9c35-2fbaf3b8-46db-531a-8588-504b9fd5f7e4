"""Unit tests for the braille pagination engine (stdlib unittest only)."""
import unittest

from app.planner import plan
from app.rules import cell_unicode
from app.graphemes import graphemes


PAPER = {"cells_per_line": 12, "lines_per_page": 3}


def all_cells(result):
    out = []
    for pg in result["pages"]:
        for ln in pg["lines"]:
            out.extend(ln["cells"])
    return out


class BasicLayoutTests(unittest.TestCase):
    def test_simple_pagination_and_mapping(self):
        r = plan({"body": "hello world singing testing",
                  "paper": {"cells_per_line": 10, "lines_per_page": 3},
                  "rules": {"rules": [
                      {"id": "ing", "pattern": "ing", "cells": ["346"]},
                      {"id": "ll", "pattern": "ll", "cells": ["123"]}]}})
        self.assertEqual(r["status"], "ok")
        # 'll' and 'ing' contractions appear and map back to the source
        rules_used = {c["rule_id"] for c in all_cells(r)}
        self.assertIn("ll", rules_used)
        self.assertIn("ing", rules_used)
        for c in all_cells(r):
            self.assertLessEqual(c["char_start"], c["char_end"])
            self.assertGreaterEqual(c["grapheme_start"], 0)
            self.assertEqual(c["unicode"], cell_unicode(c["mask"]))
        # every source character is covered by at least one cell
        body = "hello world singing testing"
        covered = [False] * len(body)
        for c in all_cells(r):
            for i in range(c["char_start"], c["char_end"]):
                covered[i] = True
        for i, ch in enumerate(body):
            if not ch.isspace():
                self.assertTrue(covered[i], f"char {i} {ch!r} uncovered")

    def test_columns_within_capacity(self):
        r = plan({"body": "alpha beta gamma delta",
                  "paper": {"cells_per_line": 8, "lines_per_page": 2}})
        self.assertEqual(r["status"], "ok")
        for pg in r["pages"]:
            for ln in pg["lines"]:
                maxcol = max(c["col"] for c in ln["cells"])
                self.assertLessEqual(maxcol, 8)


class ContractionTests(unittest.TestCase):
    def test_contraction_narrower_than_spelled(self):
        # 'ing' is one cell vs three spelled letters
        r = plan({"body": "singing",
                  "paper": {"cells_per_line": 40, "lines_per_page": 2},
                  "rules": {"rules": [
                      {"id": "ing", "pattern": "ing", "cells": ["346"]}]}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["pages"][0]["lines"][0]["cells"]), 3)  # s+ing+ing

    def test_capital_and_number_signs(self):
        r = plan({"body": "Ab 12",
                  "paper": {"cells_per_line": 40, "lines_per_page": 2}})
        unic = "".join(c["unicode"] for c in all_cells(r))
        # dot-6 capital sign before A (⠠⠁), number sign (⠼) before digits
        self.assertIn("⠠", unic)
        self.assertIn("⠼", unic)


class AmbiguityTests(unittest.TestCase):
    def test_static_duplicate_pattern_conflict(self):
        r = plan({"body": "abc", "paper": PAPER,
                  "rules": {"rules": [
                      {"id": "r1", "pattern": "ab", "cells": ["12"]},
                      {"id": "r2", "pattern": "ab", "cells": ["34"]}]}})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["kind"], "rule_ambiguity")
        self.assertEqual(r["conflict"]["reason"], "duplicate_pattern")
        self.assertEqual(r["verified_prefix"]["emitted_cells"], 0)

    def test_equal_best_match_conflict(self):
        r = plan({"body": "abcd", "paper": PAPER,
                  "rules": {"rules": [
                      {"id": "r1", "pattern": "ab", "cells": ["12"],
                       "priority": 5},
                      {"id": "r2", "pattern": "ab", "cells": ["34"],
                       "priority": 5}]}})
        self.assertEqual(r["conflict"]["kind"], "rule_ambiguity")

    def test_priority_resolves_otherwise_tie(self):
        r = plan({"body": "abcd", "paper": PAPER,
                  "rules": {"rules": [
                      {"id": "r1", "pattern": "ab", "cells": ["12"],
                       "priority": 10},
                      {"id": "r2", "pattern": "ab", "cells": ["34"],
                       "priority": 5}]}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["pages"][0]["lines"][0]["cells"][0]["rule_id"],
                         "r1")


class IntervalTests(unittest.TestCase):
    def test_interval_moved_wholesale(self):
        r = plan({"body": "ab cd ef gh",
                  "paper": {"cells_per_line": 6, "lines_per_page": 1},
                  "unbreakable": [
                      {"id": "u1", "char_start": 3, "char_end": 8}]})
        self.assertEqual(r["status"], "ok")
        pages = {(c["char_start"], c["char_end"])
                 for pg in r["pages"]
                 for ln in pg["lines"] for c in ln["cells"]}
        # the interval content 'cd' and 'ef' must share a page
        def page_of(s, e):
            for pg in r["pages"]:
                for ln in pg["lines"]:
                    for c in ln["cells"]:
                        if c["char_start"] == s and c["char_end"] == e:
                            return pg["page"]
            return None
        cd = page_of(3, 5)
        ef = page_of(6, 8)
        self.assertEqual(cd, ef)

    def test_interval_too_wide_conflict(self):
        r = plan({"body": "ab cd ef gh",
                  "paper": {"cells_per_line": 5, "lines_per_page": 1},
                  "unbreakable": [
                      {"id": "u1", "char_start": 3, "char_end": 11}]})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["kind"], "interval_too_wide")
        self.assertEqual(r["conflict"]["minimal_segment"]["text"],
                         "cd ef gh")
        # verified prefix is page 1 only and ends before the interval tail
        vp = r["verified_prefix"]
        self.assertEqual(vp["page_count"], 1)
        self.assertGreaterEqual(vp["char_end"], 6)

    def test_content_wider_than_line(self):
        r = plan({"body": "abcdefghij",
                  "paper": {"cells_per_line": 4, "lines_per_page": 2}})
        self.assertEqual(r["conflict"]["kind"], "content_too_wide")
        self.assertEqual(r["conflict"]["atom_width"], 10)
        self.assertEqual(r["conflict"]["line_capacity"], 4)


class BindingTests(unittest.TestCase):
    def test_duplex_sheet_count_and_overflow(self):
        r = plan({"body": "a b c d e f",
                  "paper": {"cells_per_line": 2, "lines_per_page": 1},
                  "binding": {"enabled": True, "duplex": True,
                              "gutter_cells": 0, "max_sheets": 1}})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["kind"], "binding_overflow")
        self.assertEqual(r["conflict"]["unit"], "sheets")
        self.assertEqual(r["conflict"]["capacity"], 1)
        # two pages (= 1 duplex sheet) verified before overflow
        self.assertEqual(r["verified_prefix"]["page_count"], 2)

    def test_gutter_reduces_capacity(self):
        # usable width = 6 - 2 gutter = 4 cells
        r = plan({"body": "ab cd ef",
                  "paper": {"cells_per_line": 6, "lines_per_page": 2},
                  "binding": {"enabled": True, "duplex": False,
                              "gutter_cells": 2}})
        self.assertEqual(r["status"], "ok")
        # no line may hold more than 4 text cells
        for pg in r["pages"]:
            for ln in pg["lines"]:
                self.assertLessEqual(
                    max(c["col"] for c in ln["cells"]) - 2, 4)

    def test_duplex_padding_blank(self):
        r = plan({"body": "a b c",
                  "paper": {"cells_per_line": 2, "lines_per_page": 1},
                  "binding": {"enabled": True, "duplex": True}})
        self.assertEqual(r["binding"]["sheets"], 2)
        self.assertEqual(r["binding"]["blank_padding_pages"], 1)


class CrossReferenceTests(unittest.TestCase):
    def test_xref_renders_target_page(self):
        body = "alpha beta gamma delta epsilon zeta eta"
        # put an xref replacing the first word, targeting late content
        r = plan({"body": body, "paper": {"cells_per_line": 12,
                                         "lines_per_page": 2},
                  "cross_references": [
                      {"id": "x1", "char_start": 0, "char_end": 5,
                       "target_start": len(body) - 3}]})
        self.assertEqual(r["status"], "ok")
        x = r["cross_references"][0]
        self.assertEqual(x["id"], "x1")
        # number sign + digit(s) emitted; mapped to xref span
        cells = all_cells(r)
        xc = [c for c in cells if c["char_start"] == 0
              and c["char_end"] == 5]
        self.assertTrue(xc)
        self.assertIn("⠼", "".join(c["unicode"] for c in xc))
        # width matches the rendered digit count + number sign
        self.assertEqual(len(xc), len(str(x["target_page"])) + 1)

    def test_fixpoint_converges(self):
        body = " ".join(f"word{i}" for i in range(40))
        r = plan({"body": body,
                  "paper": {"cells_per_line": 10, "lines_per_page": 3},
                  "cross_references": [
                      {"id": "x", "char_start": 0, "char_end": 5,
                       "target_start": len(body) - 4}]})
        self.assertEqual(r["status"], "ok")
        self.assertLessEqual(r["iterations"], 4)


class DeterminismTests(unittest.TestCase):
    def test_key_order_does_not_change_layout(self):
        a = {"body": "the quick brown fox",
             "paper": {"cells_per_line": 10, "lines_per_page": 2},
             "rules": {"rules": [
                 {"id": "the", "pattern": "the", "cells": ["2346"]}]}}
        b = {"rules": {"rules": [
                {"cells": ["2346"], "pattern": "the", "id": "the"}]},
             "paper": {"lines_per_page": 2, "cells_per_line": 10},
             "body": "the quick brown fox"}
        ra, rb = plan(a), plan(b)
        self.assertEqual(
            [(c["mask"], c["char_start"], c["char_end"], c["col"],
              pg["page"], ln["line"])
             for pg in ra["pages"] for ln in pg["lines"] for c in ln["cells"]],
            [(c["mask"], c["char_start"], c["char_end"], c["col"],
              pg["page"], ln["line"])
             for pg in rb["pages"] for ln in pg["lines"] for c in ln["cells"]])

    def test_repeated_runs_identical(self):
        payload = {"body": "lorem ipsum dolor sit amet " * 20,
                   "paper": {"cells_per_line": 18, "lines_per_page": 4}}
        import json
        r1 = plan(payload)
        r2 = plan(payload)
        self.assertEqual(json.dumps(r1, sort_keys=True),
                         json.dumps(r2, sort_keys=True))


class GraphemeTests(unittest.TestCase):
    def test_crlf_is_one_cluster(self):
        gs = graphemes("a\r\nb")
        texts = [g[0] for g in gs]
        self.assertIn("\r\n", texts)

    def test_combining_mark_absorbed(self):
        gs = graphemes("é")  # e + combining acute
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0][0], "é")


if __name__ == "__main__":
    unittest.main()
