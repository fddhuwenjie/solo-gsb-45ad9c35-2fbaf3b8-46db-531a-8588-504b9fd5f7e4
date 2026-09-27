"""Regression tests for the iterative in-word solver.

The word DP used to be a recursive function over grapheme positions, so a
single whitespace-free word longer than the interpreter's recursion limit
(~1000 frames) exhausted the call stack: the revision endpoint answered
``500`` and nothing was persisted.  The solver is now an iterative
bottom-up DP over suffix frontiers; these tests pin the boundary (a
3163-grapheme word plans, persists and finalizes) and prove the iterative
output is identical to the original recursive algorithm.
"""
import json
import os
import pathlib
import random
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.errors import InputError
from app.graphemes import graphemes
from app.planner import (
    _solve_word,
    build_atoms,
    normalize_request,
    plan,
)
from app.rules import NUMBER_SIGN, compile_rules
from app.server import build_server, get_store

# The boundary from the field report: 3163 consecutive "a", one per cell.
BOUNDARY = 3163


def _reference_solve_word(table, word):
    """The original recursive solver, kept verbatim as an oracle.

    Any behavioral drift of the iterative ``_solve_word`` (rule matching,
    grapheme mapping, candidate dedup, Pareto pruning, stable ordering)
    shows up as a mismatch against this reference.
    """
    k = len(word)
    starts, end = [], 0
    for g in word:
        starts.append(end)
        end += len(g)

    def coff(i):
        return (starts[i], starts[i + 1] if i + 1 < k else end)

    memo = {}

    def stream_key(cells):
        return tuple((c[0], c[5] or "") for c in cells)

    def solve(pos):
        if pos == k:
            return [(0, [], 0)]
        if pos in memo:
            return memo[pos]
        candidates = {}
        for m in table.matches_at(word, pos):
            n = len(m["key"])
            for tw, tcells, tu in solve(pos + n):
                cells = [
                    (c, pos, pos + n, coff(pos)[0], coff(pos + n - 1)[1],
                     m["id"])
                    for c in m["cells"]
                ] + tcells
                cand = (len(m["cells"]) + tw, cells, tu)
                candidates.setdefault((cand[0], cand[2], stream_key(cells)),
                                      cand)
        masks, kind = table.char_cells(word[pos])
        cs, ce = coff(pos)
        prev_digit = pos > 0 and table.char_cells(word[pos - 1])[1] == "digit"
        cells = []
        if kind == "digit" and not prev_digit:
            cells.append((NUMBER_SIGN, pos, pos + 1, cs, ce, None))
        for msk in masks:
            cells.append((msk, pos, pos + 1, cs, ce, None))
        add_u = 0 if kind == "digit" else 1
        for tw, tcells, tu in solve(pos + 1):
            full = cells + tcells
            cand = (len(cells) + tw, full, add_u + tu)
            candidates.setdefault((cand[0], cand[2], stream_key(full)), cand)
        if not candidates:
            memo[pos] = None
            return None
        options = list(candidates.values())
        kept = []
        for i, (w, c, u) in enumerate(options):
            dominated = False
            for j, (w2, c2, u2) in enumerate(options):
                if i != j and w2 <= w and u2 <= u and (w2 < w or u2 < u):
                    dominated = True
                    break
            if not dominated:
                kept.append((w, c, u))
        kept.sort(key=lambda r: (r[0], r[2], stream_key(r[1])))
        memo[pos] = kept
        return kept

    options = solve(0)
    if options is None:
        raise InputError("word could not be rendered in braille")
    return options


def _all_cells(result):
    return [c for pg in result["pages"] for ln in pg["lines"]
            for c in ln["cells"]]


class LongWordSolverTests(unittest.TestCase):
    def test_boundary_word_beyond_recursion_limit(self):
        # 3163 "a" in one word: the recursive solver died past ~1000
        # frames; the iterative DP solves it and maps every cell back to
        # its source grapheme/codepoint.
        r = plan({"body": "a" * BOUNDARY,
                  "paper": {"cells_per_line": BOUNDARY, "lines_per_page": 25},
                  "rules": {}})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["page_count"], 1)
        cells = _all_cells(r)
        self.assertEqual(len(cells), BOUNDARY)
        for i, c in enumerate(cells):
            self.assertEqual(
                (c["grapheme_start"], c["grapheme_end"],
                 c["char_start"], c["char_end"], c["col"]),
                (i, i + 1, i, i + 1, i + 1))
            self.assertIsNone(c["rule_id"])

    def test_long_word_with_rule_matches(self):
        # Rule matches at every other position: the multi-candidate path
        # (dedup + Pareto + sort) also runs past the old recursion limit.
        r = plan({"body": "ab" * 600,
                  "paper": {"cells_per_line": 1200, "lines_per_page": 25},
                  "rules": {"rules": [{"id": "ab", "pattern": "ab",
                                       "cells": ["12", "34"]}]}})
        self.assertEqual(r["status"], "ok")
        cells = _all_cells(r)
        self.assertEqual(len(cells), 1200)
        self.assertTrue(all(c["rule_id"] == "ab" for c in cells))

    def test_long_digit_run_single_number_sign(self):
        r = plan({"body": "7" * 1500,
                  "paper": {"cells_per_line": 1501, "lines_per_page": 10},
                  "rules": {}})
        self.assertEqual(r["status"], "ok")
        cells = _all_cells(r)
        # one number sign heads the run; 1500 digit cells follow
        self.assertEqual(len(cells), 1501)
        self.assertEqual(cells[0]["mask"], NUMBER_SIGN)
        self.assertTrue(all(c["mask"] != NUMBER_SIGN for c in cells[1:]))

    def test_template_cache_still_shared_per_word(self):
        req = normalize_request(
            {"body": "hello hello hello",
             "paper": {"cells_per_line": 40, "lines_per_page": 3}})
        table = compile_rules(req["rules"], req["body"])
        atoms = build_atoms(req, table)
        words = [a for a in atoms if a["type"] == "word"]
        self.assertEqual(len(words), 3)
        # identical words solve once; the cached template is shifted into
        # document coordinates per occurrence
        self.assertEqual(len(table._word_cache), 1)
        first, second = words[0]["options"], words[1]["options"]
        self.assertEqual([o["width"] for o in first],
                         [o["width"] for o in second])
        shift = words[1]["g_start"] - words[0]["g_start"]
        for o1, o2 in zip(first, second):
            for c1, c2 in zip(o1["cells"], o2["cells"]):
                self.assertEqual(c2["grapheme_start"],
                                 c1["grapheme_start"] + shift)
                self.assertEqual(c2["mask"], c1["mask"])

    def test_iterative_output_matches_recursive_reference(self):
        # Seeded fuzz: words stay <= 24 graphemes so the recursive oracle
        # itself never approaches the recursion limit.
        rng = random.Random(20260927)
        alphabet = "abcingls9A1.,☃"
        patterns = ["a", "ab", "ing", "abc", "ll", "s", "in", "b"]
        compared = 0
        for _ in range(120):
            n = rng.randint(1, 24)
            word_text = "".join(rng.choice(alphabet) for _ in range(n))
            rules, used = [], set()
            for i in range(rng.randint(0, 4)):
                pat = rng.choice(patterns)
                if pat in used:
                    continue
                used.add(pat)
                rules.append({
                    "id": f"r{pat}{i}",
                    "pattern": pat,
                    "cells": ["".join(rng.sample("123456",
                                                 rng.randint(1, 3)))
                              for _ in range(rng.randint(1, 3))],
                    "priority": rng.choice([1, 5, 100]),
                })
            payload = {"rules": rules,
                       "casefold": rng.random() < 0.5,
                       "unknown_char": rng.choice(["warn", "keep"])}
            table = compile_rules(payload, word_text)
            if table.ambiguity is not None:
                continue
            word = tuple(g for g, _, _ in graphemes(word_text))
            expected = _reference_solve_word(table, word)
            self.assertEqual(_solve_word(table, word), expected,
                             f"word={word_text!r} rules={rules}")
            compared += 1
        self.assertGreater(compared, 80)


class LongWordHttpTests(unittest.TestCase):
    """End-to-end: the revision endpoint persists and finalizes the
    3163-grapheme word instead of answering 500."""

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

    def _post(self, path, obj=None):
        data = None if obj is None else json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_revision_201_and_finalize(self):
        payload = {"body": "a" * BOUNDARY,
                   "paper": {"cells_per_line": BOUNDARY,
                             "lines_per_page": 25},
                   "rules": {}}
        status, rep = self._post("/documents/book/revisions", payload)
        self.assertEqual(status, 201)
        self.assertEqual(rep["revision_no"], 1)
        self.assertFalse(rep["deduplicated"])
        self.assertEqual(rep["status"], "ok")
        self.assertTrue(rep["finalizable"])
        self.assertEqual(rep["layout"]["page_count"], 1)

        # the revision really persisted: an identical body dedupes onto it
        status, rep2 = self._post("/documents/book/revisions", payload)
        self.assertEqual(status, 200)
        self.assertTrue(rep2["deduplicated"])
        self.assertEqual(rep2["revision_no"], 1)

        status, fin = self._post("/documents/book/revisions/1/finalize")
        self.assertEqual(status, 201)
        self.assertTrue(fin["finalized"])

        with urllib.request.urlopen(
                self._url("/documents/book/finalizations/1/pages.json"),
                timeout=30) as resp:
            self.assertEqual(resp.status, 200)
            pages = json.loads(resp.read())
        self.assertEqual(pages["page_count"], 1)
        cells = [c for pg in pages["pages"] for ln in pg["lines"]
                 for c in ln["cells"]]
        self.assertEqual(len(cells), BOUNDARY)
        self.assertEqual(cells[0]["grapheme_start"], 0)
        self.assertEqual(cells[-1]["grapheme_end"], BOUNDARY)


if __name__ == "__main__":
    unittest.main()
