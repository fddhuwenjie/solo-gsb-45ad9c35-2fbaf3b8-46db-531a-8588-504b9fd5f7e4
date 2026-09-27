"""Tests for fixpoint oscillation and conflict witnesses.

Natural inputs converge monotonically, so oscillation is exercised with a
synthetic layout history: two distinct cross-reference width vectors that
keep recurring (1 digit <-> 2 digits) place an atom on different pages.
"""
import unittest

from app.planner import (
    normalize_request,
    build_atoms,
    packing_dp,
    reconstruct,
    nodes_to_pages,
    render_xref_cells,
    _oscillation,
    _resolve_targets,
)
from app.rules import compile_rules


def make_req():
    body = "aa bb cc dd ee ff"
    return normalize_request({
        "body": body,
        "paper": {"cells_per_line": 6, "lines_per_page": 1},
        "cross_references": [
            {"id": "x1", "char_start": 0, "char_end": 2, "target_start": 15},
        ],
    })


class OscillationTests(unittest.TestCase):
    def test_repeated_vector_reported_as_oscillation(self):
        req = make_req()
        table = compile_rules(req["rules"], req["body"])
        atoms = build_atoms(req, table)
        xref_ids = [a["id"] for a in atoms if a["type"] == "xref"]

        def layout_for(page):
            pages_map = {rid: page for rid in xref_ids}
            render_xref_cells(atoms, pages_map)
            width = req["paper"]["cells_per_line"]
            ok, terminal, cursor, _ties = packing_dp(
                atoms, req["unbreakable"], width,
                req["paper"]["lines_per_page"], strict=True)
            self.assertTrue(ok)
            chain = reconstruct(terminal)
            pages, records = nodes_to_pages(chain)
            resolved = _resolve_targets(req, records)
            vector = tuple(len(str(resolved[r])) for r in xref_ids)
            return chain, cursor, pages, records, resolved, vector

        l1 = layout_for(1)
        l9 = layout_for(9)
        v1, v9 = l1[5], l9[5]
        # Force a two-vector cycle regardless of the small example's actual
        # target pages: the detector keys purely on recurring width vectors
        # and the page placement each one implies.
        v_a = (1,)
        v_b = (2,)
        history = [v_a, v_b, v_a]
        pages_for = {
            v_a: l1[:5],
            v_b: l9[:5],
        }
        result = _oscillation(req, atoms, history, pages_for, xref_ids)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["conflict"]["kind"],
                         "page_number_oscillation")
        self.assertIn("cycle_width_vectors", result["conflict"])
        # an oscillating xref is named and a minimal segment is given
        self.assertIn("x1", result["conflict"]["oscillating_xref_ids"])
        seg = result["conflict"]["minimal_segment"]
        self.assertIsNotNone(seg)
        self.assertLess(seg["char_start"], seg["char_end"])
        # a verified prefix (possibly empty) is always returned
        vp = result["verified_prefix"]
        self.assertIn("pages", vp)
        self.assertIn("char_end", vp)

    def test_capped_iteration_marks_reason(self):
        req = make_req()
        table = compile_rules(req["rules"], req["body"])
        atoms = build_atoms(req, table)
        xref_ids = [a["id"] for a in atoms if a["type"] == "xref"]
        render_xref_cells(atoms, {rid: 1 for rid in xref_ids})
        ok, terminal, _, _ties = packing_dp(
            atoms, req["unbreakable"], req["paper"]["cells_per_line"],
            req["paper"]["lines_per_page"], strict=True)
        chain = reconstruct(terminal)
        pages, records = nodes_to_pages(chain)
        resolved = _resolve_targets(req, records)
        vector = tuple(len(str(resolved[r])) for r in xref_ids)
        result = _oscillation(req, atoms, [vector],
                              {vector: (chain, (1, 1, 0), pages, records,
                                        resolved)},
                              xref_ids, capped=True)
        self.assertEqual(result["conflict"]["reason"],
                         "fixpoint_iteration_cap")


if __name__ == "__main__":
    unittest.main()
