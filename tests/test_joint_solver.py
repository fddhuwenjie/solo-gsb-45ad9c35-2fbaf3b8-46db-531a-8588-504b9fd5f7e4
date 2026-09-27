"""Tests for the joint contraction/pagination solver.

These cover the fix where the word DP used to hand the packing DP only the
narrowest and the fully spelled segmentation: intermediate non-dominated
segmentations (wider than the narrowest option but spending fewer
uncontracted cells) disappeared before pagination, and per-page pruning
discarded equal-cost wider cursors that could reach a different optimum.
"""
import unittest

from app.graphemes import graphemes
from app.planner import (
    build_atoms,
    build_word_options,
    normalize_request,
    plan,
)
from app.rules import compile_rules


def cells(result):
    out = []
    for pg in result["pages"]:
        for ln in pg["lines"]:
            out.extend(ln["cells"])
    return out


def frontier(body, rules):
    req = normalize_request(
        {"body": body, "paper": {"cells_per_line": 80, "lines_per_page": 10},
         "rules": {"rules": rules}})
    table = compile_rules(req["rules"], req["body"])
    atoms = build_atoms(req, table)
    word = next(a for a in atoms if a["type"] == "word")
    return word["options"]


class WordFrontierTests(unittest.TestCase):
    def test_keeps_all_non_dominated_segmentations(self):
        # For "abcd":
        #   spelled ................ width 4, uncontracted 4
        #   "ab" -> 3 cells ........ width 5, uncontracted 2
        #   "abc" -> 5 cells ....... width 6, uncontracted 1
        # Each option is wider than the previous but saves spelled cells,
        # so all three are Pareto-optimal and must survive to pagination.
        rules = [
            {"id": "ab", "pattern": "ab", "cells": ["1", "2", "3"]},
            {"id": "abc", "pattern": "abc",
             "cells": ["4", "5", "6", "1", "2"]},
        ]
        opts = frontier("abcd", rules)
        self.assertEqual(
            [(o["width"], o["uncontracted"]) for o in opts],
            [(4, 4), (5, 2), (6, 1)])
        # the middle option really is the "ab" contraction, not spelled
        self.assertEqual([c["rule_id"] for c in opts[1]["cells"][:3]],
                         ["ab", "ab", "ab"])
        # every option keeps complete grapheme mappings
        for o in opts:
            for c in o["cells"]:
                self.assertGreaterEqual(c["grapheme_end"],
                                        c["grapheme_start"])

    def test_dominated_segmentations_are_dropped(self):
        # A rule strictly better than spelling dominates the spelled path;
        # no duplicate fully-spelled option is emitted.
        opts = frontier("ing", [{"id": "ing", "pattern": "ing",
                                 "cells": ["346"]}])
        self.assertEqual(
            [(o["width"], o["uncontracted"]) for o in opts], [(1, 0)])


class JointSelectionTests(unittest.TestCase):
    RULES = [
        {"id": "ab", "pattern": "ab", "cells": ["1", "2", "3"]},
        {"id": "abc", "pattern": "abc",
         "cells": ["4", "5", "6", "1", "2"]},
    ]

    def test_narrow_option_chosen_when_pages_differ(self):
        # Line width 4: only the spelled (narrowest) option fits, so the
        # wider segmentations must not force an extra page.
        r = plan({"body": "abcd",
                  "paper": {"cells_per_line": 4, "lines_per_page": 1},
                  "rules": {"rules": self.RULES}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["page_count"], 1)
        self.assertIsNone(cells(r)[0]["rule_id"])

    def test_wider_cheaper_option_chosen_when_pages_tie(self):
        # Line width 6: every option fits on one page, so the option with
        # the fewest uncontracted cells ("abc", u=1) must win even though
        # it is the widest one.
        r = plan({"body": "abcd",
                  "paper": {"cells_per_line": 6, "lines_per_page": 1},
                  "rules": {"rules": self.RULES}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["page_count"], 1)
        used = {c["rule_id"] for c in cells(r)}
        self.assertIn("abc", used)

    def test_last_page_row_column_is_final_tie_break(self):
        r = plan({"body": "ab",
                  "paper": {"cells_per_line": 10, "lines_per_page": 2},
                  "rules": {"rules": [
                      {"id": "ab", "pattern": "ab", "cells": ["1"]}]}})
        self.assertEqual(r["status"], "ok")
        last_line = r["pages"][-1]["lines"][-1]
        self.assertEqual(last_line["cells"][-1]["col"], 1)


class EqualOptimalDotStreamTests(unittest.TestCase):
    RULES = [
        {"id": "abc", "pattern": "abc", "cells": ["1", "2", "3"]},
        {"id": "bcd", "pattern": "bcd", "cells": ["4", "5", "6"]},
    ]

    def test_equal_optimal_streams_are_rule_ambiguity(self):
        # "abc"+d (w4,u1) and a+"bcd" (w4,u1) tie on pages, uncontracted
        # count and final cursor but emboss different dots: no finalization.
        r = plan({"body": "abcd",
                  "paper": {"cells_per_line": 4, "lines_per_page": 1},
                  "rules": {"rules": self.RULES}})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["kind"], "rule_ambiguity")
        self.assertEqual(r["conflict"]["reason"],
                         "equal_optimal_dot_stream")
        self.assertEqual(r["conflict"]["boundary_atom"], 0)
        self.assertEqual(r["conflict"]["minimal_segment"]["text"], "abcd")
        self.assertEqual(r["verified_prefix"]["emitted_cells"], 0)

    def test_verified_prefix_stops_at_first_fork(self):
        # The first word "xy" is common to both equal-optimal layouts; the
        # conflict only starts at the forking word "abcd".
        r = plan({"body": "xy abcd",
                  "paper": {"cells_per_line": 9, "lines_per_page": 1},
                  "rules": {"rules": self.RULES}})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["boundary_atom"], 2)
        vp = r["verified_prefix"]
        # shared emissions are the two letters plus the following space
        self.assertEqual(vp["char_end"], 3)
        self.assertEqual(vp["emitted_cells"], 3)

    def test_wider_cheaper_path_survives_page_pruning(self):
        # A narrow cursor with more uncontracted cells must not prune a
        # wider cursor with fewer: the latter packs subsequent content into
        # fewer pages and is the global optimum.
        r = plan({"body": "abcd",
                  "paper": {"cells_per_line": 5, "lines_per_page": 1},
                  "rules": {"rules": [
                      {"id": "ab", "pattern": "ab",
                       "cells": ["1", "2", "3"]}]}})
        self.assertEqual(r["status"], "ok")
        # with "ab" contracted (w5,u2) everything fits on one page
        used = [c["rule_id"] for c in cells(r)]
        self.assertIn("ab", used)


CROSS_TIE_RULES = [
    # "abcd" keeps two Pareto options: ab->2 cells + c,d (w4,u2) and
    # abc->4 cells + d (w5,u1).  Balancing (4,2)+(5,1) against
    # (5,1)+(4,2) across two words yields equal page/u/final-cursor
    # layouts with different dot streams.
    {"id": "ab2", "pattern": "ab", "cells": ["1", "2"]},
    {"id": "abc4", "pattern": "abc", "cells": ["3", "4", "5", "6"]},
]


class CrossCursorTieTests(unittest.TestCase):
    def test_terminal_tie_at_different_cursors_is_conflict(self):
        r = plan({"body": "abcd abcd",
                  "paper": {"cells_per_line": 10, "lines_per_page": 1},
                  "rules": {"rules": CROSS_TIE_RULES}})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["reason"],
                         "equal_optimal_dot_stream")

    def test_terminal_tie_prefix_is_from_one_final_chain(self):
        r = plan({"body": "zz abcd abcd",
                  "paper": {"cells_per_line": 13, "lines_per_page": 1},
                  "rules": {"rules": CROSS_TIE_RULES}})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["boundary_atom"], 2)
        vp = r["verified_prefix"]
        self.assertEqual(vp["char_end"], 3)
        # the prefix is rendered by the chosen (single) chain
        self.assertEqual(
            [c["mask"] for pg in vp["pages"] for ln in pg["lines"]
             for c in ln["cells"]],
            [53, 53, 0])

    def test_capacity_conflict_takes_precedence_over_tie(self):
        # The tie layout needs two pages (one word per line); exceeding
        # max_pages is a legality failure and is reported first.
        r = plan({"body": "abcd abcd",
                  "paper": {"cells_per_line": 6, "lines_per_page": 1,
                            "max_pages": 1},
                  "rules": {"rules": CROSS_TIE_RULES}})
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflict"]["kind"], "capacity_overflow")


class DeterminismTests(unittest.TestCase):
    def test_identical_requests_still_deterministic(self):
        import json
        payload = {"body": "lorem ipsum dolor sit amet " * 20,
                   "paper": {"cells_per_line": 18, "lines_per_page": 4},
                   "rules": {"rules": [
                       {"id": "lo", "pattern": "lo",
                        "cells": ["1", "2", "3"]},
                       {"id": "or", "pattern": "or",
                        "cells": ["4", "5", "6"]}]}}
        a, b = plan(payload), plan(payload)
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
