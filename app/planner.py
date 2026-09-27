"""Braille contraction + pagination engine.

Two levels of dynamic programming are coupled here:

1. **Word segmentation DP** chooses, for every whitespace-delimited word,
   a contraction (longest / highest-priority rule) or a spelled grade-1
   fallback at every position.  It keeps the *full non-dominated frontier*
   of segmentations under the trade-off ``(width, uncontracted cells)``:
   a wider option whose contractions save more spelled cells is retained
   alongside the narrowest option (and alongside fully spelled output when
   that is itself non-dominated).  Every option is annotated with
   source-grapheme spans for every emitted braille cell.
2. **Packing DP** packs the resulting atoms (words / spaces / cross
   references) into lines and pages.  The state is the cursor
   ``(page, line, col)`` plus accumulated uncontracted cells; a Pareto
   frontier per page removes only *strictly* worse cursors (a wider cursor
   with no more uncontracted cells is never pruned, since it can end in the
   same final cursor and produce a different dot stream), so the choice of
   contraction and the placement of every page break is one joint
   optimization.  When several layouts tie under the global preference
   (fewest pages, fewest uncontracted cells, smallest last-page row/column)
   but emit different dot streams, the result is a rule ambiguity and the
   document is not finalized.

Cross-references render as ``number-sign + target-page digits`` whose width
depends on the pagination itself.  Layouts are re-run until the width
vector reaches a fixpoint; a recurring vector is *page-number oscillation*.

The packing DP has two modes:

* ``strict``  -- page breaks inside an unbreakable interval, and atoms
  wider than a line, are forbidden;
* ``relaxed`` -- each violation is recorded on the path and treated as a
  huge cost.  Terminal selection maximizes the index of the *first*
  violation, pinpointing the minimal conflict and maximizing the verified
  prefix (the emissions before that node).
"""
import unicodedata

from .errors import InputError
from .graphemes import graphemes
from .rules import (
    _DIGIT_DOTS,
    ALL_DOTS,
    BLANK,
    NUMBER_SIGN,
    cell_unicode,
    compile_rules,
    parse_cell,
)

FIXPOINT_LIMIT = 64


# --------------------------------------------------------------------------
# Input validation / canonicalization
# --------------------------------------------------------------------------
def normalize_request(payload):
    """Validate a revision request and fill canonical default values."""
    if not isinstance(payload, dict):
        raise InputError("request body must be a JSON object")
    body = payload.get("body")
    if not isinstance(body, str):
        raise InputError("body must be a string")
    body = unicodedata.normalize("NFC", body)
    if not body:
        raise InputError("body must not be empty")

    paper_in = payload.get("paper", {})
    if not isinstance(paper_in, dict):
        raise InputError("paper must be an object")
    cpl = paper_in.get("cells_per_line")
    lpp = paper_in.get("lines_per_page")
    if not isinstance(cpl, int) or not isinstance(lpp, int):
        raise InputError("paper.cells_per_line and lines_per_page are integers")
    if cpl < 1 or lpp < 1:
        raise InputError("paper capacity must be >= 1")
    max_pages = paper_in.get("max_pages")
    if max_pages is not None and (
        not isinstance(max_pages, int) or max_pages < 1
    ):
        raise InputError("paper.max_pages must be a positive integer")
    paper = {"cells_per_line": cpl, "lines_per_page": lpp,
             "max_pages": max_pages}

    binding_in = payload.get("binding", {}) or {}
    if not isinstance(binding_in, dict):
        raise InputError("binding must be an object")
    enabled = bool(binding_in.get("enabled", False))
    duplex = bool(binding_in.get("duplex", False))
    gutter = binding_in.get("gutter_cells", 0)
    max_sheets = binding_in.get("max_sheets")
    if not isinstance(gutter, int) or gutter < 0:
        raise InputError("binding.gutter_cells must be a non-negative integer")
    if enabled and gutter >= cpl:
        raise InputError("binding.gutter_cells must leave at least one cell")
    if max_sheets is not None and (
        not isinstance(max_sheets, int) or max_sheets < 1
    ):
        raise InputError("binding.max_sheets must be a positive integer")
    edge = binding_in.get("edge", "left")
    if edge not in ("left", "right"):
        raise InputError("binding.edge must be 'left' or 'right'")
    binding = {
        "enabled": enabled, "duplex": duplex,
        "gutter_cells": gutter if enabled else 0,
        "max_sheets": max_sheets if enabled else None,
        "edge": edge,
    }

    clusters = graphemes(body)
    char_starts = {s for _, s, _ in clusters}
    char_ends = {e for _, _, e in clusters}

    def span(obj, label):
        if not isinstance(obj, dict):
            raise InputError(f"{label} must be an object")
        s, e = obj.get("char_start"), obj.get("char_end")
        if not isinstance(s, int) or not isinstance(e, int):
            raise InputError(f"{label} offsets must be integers")
        if not (0 <= s < e <= len(body)):
            raise InputError(f"{label} span out of range: [{s},{e})")
        if s not in char_starts or e not in char_ends:
            raise InputError(
                f"{label} span [{s},{e}) must start/end on grapheme boundaries"
            )
        gs = next(i for i, (_, lo, _) in enumerate(clusters) if lo == s)
        ge = next(i for i, (_, _, hi) in enumerate(clusters) if hi == e) + 1
        return s, e, gs, ge

    def span_list(raw, label, target=False):
        items = raw or []
        if not isinstance(items, list):
            raise InputError(f"{label} must be a list")
        out = []
        for i, obj in enumerate(items):
            rid = obj.get("id") if isinstance(obj, dict) else None
            label_i = f"{label}[{rid if rid is not None else i}]"
            if not isinstance(rid, str):
                raise InputError(f"{label_i} needs a string id")
            s, e, gs, ge = span(obj, label_i)
            rec = {"id": rid, "char_start": s, "char_end": e,
                   "g_start": gs, "g_end": ge}
            if target:
                t = obj.get("target_start", s)
                if not isinstance(t, int) or not (0 <= t < len(body)):
                    raise InputError(f"{label_i}.target_start out of range")
                rec["target_start"] = t
            out.append(rec)
        out.sort(key=lambda r: (r["char_start"], r["char_end"], r["id"]))
        for a, b in zip(out, out[1:]):
            if a["char_end"] > b["char_start"]:
                raise InputError(
                    f"{label} spans overlap: {a['id']!r} and {b['id']!r}"
                )
        return out

    xrefs = span_list(payload.get("cross_references"),
                      "cross_references", target=True)
    unbreakable = span_list(payload.get("unbreakable"), "unbreakable")
    for x in xrefs:
        t = x["target_start"]
        for other in xrefs:
            if other is not x and other["char_start"] <= t < other["char_end"]:
                raise InputError(
                    f"cross_reference {x['id']!r} targets another cross "
                    "reference span"
                )
    return {
        "body": body,
        "cross_references": xrefs,
        "unbreakable": unbreakable,
        "rules": payload.get("rules", {}) or {},
        "paper": paper,
        "binding": binding,
        "_clusters": clusters,
    }


# --------------------------------------------------------------------------
# Cell / atom construction
# --------------------------------------------------------------------------
def _cell(mask, glo, ghi, cstart, cend, rule_id=None):
    return {"mask": mask, "grapheme_start": glo, "grapheme_end": ghi,
            "char_start": cstart, "char_end": cend, "rule_id": rule_id}


def build_word_options(table, clusters, lo, hi, char_of):
    """Inner DP over one whitespace-delimited word.

    Returns every non-dominated option, each
    ``(width, cells, uncontracted)``.  Cell spans are *relative* cluster
    indices within the word and relative character offsets within the word;
    ``build_atoms`` shifts them into document coordinates.  Templates are
    cached per identical word.
    """
    word = tuple(clusters[lo:hi])
    cache = getattr(table, "_word_cache", None)
    if cache is None:
        cache = table._word_cache = {}
    cached = cache.get(word)
    if cached is None:
        cached = cache[word] = _solve_word(table, word)
    gbase = lo
    cbase = char_of(lo)[0]
    out = []
    for width, tpl_cells, uncontracted in cached:
        cells = []
        for msk, rgs, rge, rcs, rce, rid in tpl_cells:
            cells.append(_cell(msk, gbase + rgs, gbase + rge,
                               cbase + rcs, cbase + rce, rid))
        out.append((width, cells, uncontracted))
    return out


def _solve_word(table, word):
    """Relative-offset segmentation of one cluster tuple.

    Returns the Pareto frontier of segmentations ordered by
    ``(width, uncontracted, cell masks, rule ids)``.  Two options with the
    same ``(width, uncontracted)`` and the same emitted cell stream collapse
    into one; distinct streams at equal cost are both retained because the
    global planner can only resolve them at pagination time.

    The DP is iterative: every suffix at ``pos`` depends only on strictly
    larger positions (a rule match jumps by its key length, the grade-1
    fallback advances by one), so the suffix frontiers are filled from the
    end of the word backwards.  The call stack stays flat no matter how long
    the word is -- a single whitespace-free word may be as long as the
    document grapheme limit without approaching the interpreter's recursion
    limit (the former recursive solver overflowed it past ~1000 clusters).
    """
    k = len(word)
    starts, end = [], 0
    for g in word:
        starts.append(end)
        end += len(g)

    def coff(i):
        return (starts[i], starts[i + 1] if i + 1 < k else end)

    def stream_key(cells):
        return tuple((c[0], c[5] or "") for c in cells)

    # solved[pos] is the Pareto frontier for the suffix word[pos:], or None
    # when that suffix cannot be rendered -- one slot per position instead
    # of one recursion frame per grapheme.
    solved = [None] * (k + 1)
    solved[k] = [(0, [], 0)]
    for pos in range(k - 1, -1, -1):
        matches = table.matches_at(word, pos)
        masks, kind = table.char_cells(word[pos])
        cs, ce = coff(pos)
        prev_digit = pos > 0 and table.char_cells(word[pos - 1])[1] == "digit"
        cells = []
        if kind == "digit" and not prev_digit:
            cells.append((NUMBER_SIGN, pos, pos + 1, cs, ce, None))
        for msk in masks:
            cells.append((msk, pos, pos + 1, cs, ce, None))
        add_u = 0 if kind == "digit" else 1
        tail = solved[pos + 1]
        if not matches and len(tail) == 1:
            # A lone candidate: the dedup key, the Pareto pass and the
            # final sort below are all identity transforms for a single
            # option, so the stream key never needs to be materialized.
            # (This keeps a rule-free word of N clusters at O(N) stream-key
            # work instead of O(N^2); the emitted option is unchanged.)
            tw, tcells, tu = tail[0]
            solved[pos] = [(len(cells) + tw, cells + tcells, add_u + tu)]
            continue
        candidates = {}
        for m in matches:
            n = len(m["key"])
            for tw, tcells, tu in solved[pos + n]:
                mcells = [
                    (c, pos, pos + n, coff(pos)[0], coff(pos + n - 1)[1],
                     m["id"])
                    for c in m["cells"]
                ] + tcells
                cand = (len(m["cells"]) + tw, mcells, tu)
                candidates.setdefault(
                    (cand[0], cand[2], stream_key(mcells)), cand)
        for tw, tcells, tu in tail:
            full = cells + tcells
            cand = (len(cells) + tw, full, add_u + tu)
            candidates.setdefault((cand[0], cand[2], stream_key(full)), cand)
        if not candidates:
            solved[pos] = None
            continue
        options = list(candidates.values())
        # Pareto pruning on (width, uncontracted): keep an option unless
        # another is no wider, needs no more spelled cells and is strictly
        # better on at least one axis.
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
        solved[pos] = kept

    options = solved[0]
    if options is None:
        raise InputError("word could not be rendered in braille")
    return options


def build_atoms(req, table):
    tuples = req["_clusters"]
    clusters = [g for g, _, _ in tuples]
    char_of = lambda i: (tuples[i][1], tuples[i][2])  # noqa: E731
    xref_at = {}
    for sp in req["cross_references"]:
        for g in range(sp["g_start"], sp["g_end"]):
            xref_at[g] = sp
    atoms, i, n = [], 0, len(clusters)
    while i < n:
        if i in xref_at:
            sp = xref_at[i]
            atoms.append({
                "type": "xref", "id": sp["id"],
                "g_start": sp["g_start"], "g_end": sp["g_end"],
                "char_start": sp["char_start"], "char_end": sp["char_end"],
                "target_start": sp["target_start"],
            })
            i = sp["g_end"]
            continue
        if clusters[i].isspace():
            atoms.append({
                "type": "space", "g_start": i, "g_end": i + 1,
                "char_start": tuples[i][1], "char_end": tuples[i][2],
            })
            i += 1
            continue
        j = i
        while j < n and j not in xref_at and not clusters[j].isspace():
            j += 1
        opts = build_word_options(table, clusters, i, j, char_of)
        atoms.append({
            "type": "word", "g_start": i, "g_end": j,
            "char_start": tuples[i][1], "char_end": tuples[j - 1][2],
            "options": [
                {"width": w, "cells": cells, "uncontracted": u}
                for (w, cells, u) in opts
            ],
        })
        i = j
    return atoms


def render_xref_cells(atoms, xref_pages):
    """Assign rendered cells + width to every xref atom for this iteration."""
    for atom in atoms:
        if atom["type"] != "xref":
            continue
        page = xref_pages.get(atom["id"], 1)
        gs, ge = atom["g_start"], atom["g_end"]
        cs, ce = atom["char_start"], atom["char_end"]
        cells = [_cell(NUMBER_SIGN, gs, ge, cs, ce, None)]
        for d in str(page):
            cells.append(_cell(parse_cell(_DIGIT_DOTS[d]), gs, ge, cs, ce,
                               None))
        atom["cells"] = cells
        atom["width"] = len(cells)


# --------------------------------------------------------------------------
# Packing DP
# --------------------------------------------------------------------------
def packing_dp(atoms, intervals, width, lines_per_page, strict=True):
    """Pack atoms with all non-dominated word options;
    ``(ok, terminal_node, final_cursor, tie_nodes)``.

    ``frontier[cursor] -> state`` where a state is
    ``(uncontracted, node, col, violations, first_violation)`` and ``node``
    is the head of one chosen emission chain.  Every equal-cost chain
    reaching the same cursor is attached to that head's ``alts`` list;
    strictly worse chains are dropped.  ``tie_nodes`` lists terminal heads
    of every other chain tying the chosen one under the global preference
    (``_terminal_rank``), including chains ending at a different cursor.

    Global preference among terminals: feasibility first (first violation
    as late as possible, then fewest violations), then page count, then
    total uncontracted cells, then the last page's occupied row/column.

    Per-page pruning only removes a state that is *strictly* outranked in
    cost by another state with at least as much room; an equal-cost state
    that merely starts further left/right is retained because it can wrap
    later atoms differently and reach a different (or the same but
    differently dotted) terminal.
    """
    init_key = (1, 1, 0)
    frontier = {init_key: (0, None, 0, 0, None)}
    locked = lambda pos: [  # noqa: E731
        iv for iv in intervals if iv["g_start"] < pos < iv["g_end"]
    ]
    locked_any = lambda pos: [  # noqa: E731
        iv for iv in intervals if iv["g_start"] <= pos < iv["g_end"]
    ]
    break_candidates = set()
    for idx, atom in enumerate(atoms):
        if atom["type"] == "space":
            continue
        for iv in intervals:
            if atom["g_start"] <= iv["g_start"] < atom["g_end"]:
                break_candidates.add(idx)
                break
    for idx, atom in enumerate(atoms):
        nxt = {}
        for key, (u, prev, _c, viol, first_v) in frontier.items():
            _emit(nxt, atom, idx, key, u, prev, viol, first_v,
                  locked, locked_any, width, lines_per_page, strict,
                  idx in break_candidates)
        frontier = _prune(nxt, width, lines_per_page, len(atoms))
        if not frontier:
            return False, None, None, []
    best, best_key, ties = None, None, []
    for key in sorted(frontier):
        u, node, _c, viol, first_v = frontier[key]
        cand = _terminal_rank(key, u, viol, first_v, len(atoms))
        if best is None or cand < best:
            best, best_key, ties = cand, key, []
        elif cand == best:
            ties.append(node)
    return True, frontier[best_key][1], best_key, ties


def _terminal_rank(key, u, viol, first_v, n_atoms):
    return (first_v if first_v is not None else n_atoms,
            viol, key[0], u, key[1], key[2])


def _cost_rank(first_v, viol, u, n_atoms):
    """State ordering that ignores room: first violation, count, spelled."""
    return (first_v if first_v is not None else n_atoms, viol, u)


def _same_emission(cells_a, cells_b):
    """Two emissions are the same dot stream iff mask sequence matches."""
    return [c["mask"] for c in cells_a] == [c["mask"] for c in cells_b]


def _chain_amb(node):
    """Whether an emission chain already forks into equal-cost streams."""
    return bool(node is not None and node.get("alt_amb"))


def _emit(nxt, atom, idx, key, u, prev, viol, first_v,
          locked, locked_any, width, lpp, strict, may_break_voluntarily):
    page, line, col = key
    BIG = 10 ** 9  # cost ranks never need the exact atom count

    def commit(out_key, cells, dropped, v, cu):
        if v is not None and strict:
            return
        new_viol = viol + (1 if v is not None else 0)
        new_first = first_v if first_v is not None or v is None else idx
        node = {"i": idx, "atom": atom, "emitted": cells, "dropped": dropped,
                "prev": prev, "key_before": key, "key_after": out_key,
                "violation": v, "alt_amb": _chain_amb(prev)}
        cand = (cu, node, out_key[2], new_viol, new_first)
        old = nxt.get(out_key)
        if old is None:
            nxt[out_key] = cand
            return
        new_rank = _cost_rank(new_first, new_viol, cu, BIG)
        old_rank = _cost_rank(old[4], old[3], old[0], BIG)
        if new_rank < old_rank:
            nxt[out_key] = cand
        elif new_rank == old_rank:
            # Equal cost at the same cursor.  The streams are identical
            # only when they extend identical prefixes with identical
            # masks; otherwise this cursor is a fork and the document may
            # not be finalized.  Any alternatives carried by either chain
            # are merged onto the surviving head.
            same = (not node["alt_amb"] and not old[1].get("alt_amb")
                    and node["key_before"] == old[1]["key_before"]
                    and _same_emission(cells, old[1]["emitted"]))
            if not same:
                if node.get("alts"):
                    old[1].setdefault("alts", []).extend(node["alts"])
                old[1].setdefault("alts", []).append(node)
                old[1]["alt_amb"] = True

    if atom["type"] == "space":
        if col == 0 or col + 1 > width:
            node = {"i": idx, "atom": atom, "emitted": [], "dropped": True,
                    "prev": prev, "key_before": key, "key_after": key,
                    "violation": None, "alt_amb": _chain_amb(prev)}
            old = nxt.get(key)
            if old is None:
                nxt[key] = (u, node, col, viol, first_v)
            elif _cost_rank(first_v, viol, u, BIG) < _cost_rank(
                    old[4], old[3], old[0], BIG):
                nxt[key] = (u, node, col, viol, first_v)
            elif _cost_rank(first_v, viol, u, BIG) == _cost_rank(
                    old[4], old[3], old[0], BIG):
                # Equal-rank space drops at the same cursor: a fork unless
                # the prefixes (and the empty emission) coincide.
                if (node["alt_amb"] or old[1].get("alt_amb")
                        or node["key_before"] != old[1]["key_before"]):
                    if node.get("alts"):
                        old[1].setdefault("alts", []).extend(node["alts"])
                    old[1].setdefault("alts", []).append(node)
                    old[1]["alt_amb"] = True
            return
        out_key = (page, line, col + 1)
        cells = [_cell(BLANK, atom["g_start"], atom["g_end"],
                       atom["char_start"], atom["char_end"], None)]
        commit(out_key, cells, False, None, u)
        return

    options = (
        atom["options"] if atom["type"] == "word"
        else [{"width": atom["width"], "cells": atom["cells"],
               "uncontracted": 0}]
    )
    for oi, opt in enumerate(options):
        w = opt["width"]
        np_, nl, nc = page, line, col
        v = None
        crossed = locked(atom["g_start"])
        if col + w > width:
            nl = line + 1
            nc = 0
            if nl > lpp:
                nl, np_ = 1, page + 1
            if np_ > page and crossed:
                v = ("lock", atom["g_start"], crossed)
        if w > width:
            v = ("overflow", atom, locked_any(atom["g_start"]))
        nc += w
        out_key = (np_, nl, nc)
        new_u = u + opt.get("uncontracted", 0)
        commit(out_key, opt["cells"], False, v, new_u)

        # Voluntary page break (only the atom overlapping an interval
        # start).  Terminal selection prefers smaller page numbers, so it
        # is used only when required to keep an interval whole.
        if may_break_voluntarily and (v is None or v[0] != "overflow"):
            top_key = (page + 1, 1, w)
            inside = [iv for iv in crossed
                      if iv["g_start"] < atom["g_start"]]
            if inside and not strict:
                commit(top_key, opt["cells"], False,
                       ("lock", atom["g_start"], inside), new_u)
            elif not inside:
                commit(top_key, opt["cells"], False, None, new_u)


def _prune(frontier, width, lpp, n_atoms=10 ** 9):
    """Pareto-prune states belonging to the same page.

    State ``a`` may prune state ``b`` on the same page only when ``a`` has
    at least as much usable room left (more full lines remaining, or the
    same remaining lines with a no-wider cursor) **and** a strictly smaller
    cost vector (later-or-equal first violation, fewer-or-equal violations,
    fewer-or-equal uncontracted cells, strict on at least one axis).

    Equal-cost states with different cursors are always retained: the last
    global tie-break is the final cursor, so an equal-cost wider cursor can
    only tie (never lose to) the narrower one and may emit a different dot
    stream.  Different pages are never merged.
    """
    groups = {}
    for (p, l, c), state in frontier.items():
        u, node, _c0, viol, first_v = state
        room = (lpp - l, width - c)
        groups.setdefault(p, []).append((room, l, c, state))
    out = {}
    for p, entries in groups.items():
        keep = []

        def dominates(a, b):
            """True if entry ``a`` can make entry ``b`` unreachable."""
            ra, _la, _ca, (ua, _na, _xa, va, fa) = a
            rb, _lb, _cb, (ub, _nb, _xb, vb, fb) = b
            if not (ra[0] >= rb[0] and ra[1] >= rb[1]):
                return False
            if not (ua <= ub and _no_worse_viol(fa, va, fb, vb)):
                return False
            room_strict = ra[0] > rb[0] or ra[1] > rb[1]
            cost_strict = _cost_rank(fa, va, ua, n_atoms) < _cost_rank(
                fb, vb, ub, n_atoms)
            return room_strict or cost_strict

        for entry in entries:
            if any(dominates(k, entry) for k in keep):
                continue
            keep = [k for k in keep if not dominates(entry, k)]
            keep.append(entry)
        for _room, l, c, state in keep:
            out[(p, l, c)] = state
    return out


def _no_worse_viol(first_a, count_a, first_b, count_b):
    fa = first_a if first_a is not None else 10 ** 12
    fb = first_b if first_b is not None else 10 ** 12
    return fa >= fb and count_a <= count_b


# --------------------------------------------------------------------------
# Reconstruction
# --------------------------------------------------------------------------
def reconstruct(terminal):
    chain = []
    node = terminal
    while node is not None:
        chain.append(node)
        node = node["prev"]
    chain.reverse()
    return chain


def nodes_to_pages(chain, stop_atom=None, gutter=0, edge="left"):
    """Group emitted cells into page/line structures.

    Columns are absolute on the physical line: a left gutter shifts all
    text columns right by ``gutter``; a right gutter merely caps the width.
    """
    gshift = gutter if edge == "left" else 0
    pages, atom_records = {}, []
    for node in chain:
        if stop_atom is not None and node["i"] >= stop_atom:
            break
        p, l, c = node["key_after"]
        if node["dropped"]:
            atom_records.append({"atom_index": node["i"], "page": None,
                                 "line": None, "dropped": True,
                                 "violation": node.get("violation"),
                                 "atom": node["atom"]})
            continue
        start_col = c - len(node["emitted"])
        for k, cell in enumerate(node["emitted"]):
            pages.setdefault(p, {}).setdefault(l, []).append(
                {**cell, "col": start_col + k + 1 + gshift})
        atom_records.append({
            "atom_index": node["i"], "page": p, "line": l,
            "start_col": start_col + 1, "dropped": False,
            "atom": node["atom"], "violation": node.get("violation"),
        })
    out_pages = [
        {"page": p,
         "lines": [{"line": ln,
                    "cells": sorted(pages[p][ln], key=lambda x: x["col"])}
                   for ln in sorted(pages[p])]}
        for p in sorted(pages)
    ]
    return out_pages, atom_records


# --------------------------------------------------------------------------
# Fixpoint driver
# --------------------------------------------------------------------------
def plan(payload):
    req = normalize_request(payload)
    if len(req["_clusters"]) > 200_000:
        raise InputError("document exceeds the configured grapheme limit")
    table = compile_rules(req["rules"], req["body"])
    if table.ambiguity is not None:
        return _ambiguity_result(table.ambiguity)

    atoms = build_atoms(req, table)
    width = req["paper"]["cells_per_line"] - req["binding"]["gutter_cells"]
    lpp = req["paper"]["lines_per_page"]
    intervals = req["unbreakable"]
    xref_ids = [a["id"] for a in atoms if a["type"] == "xref"]
    resolved = {rid: 1 for rid in xref_ids}

    history, pages_for = [], {}
    for iteration in range(FIXPOINT_LIMIT):
        render_xref_cells(atoms, resolved)
        ok, terminal, cursor, ties = packing_dp(
            atoms, intervals, width, lpp, strict=True)
        if not ok:
            return _relaxed_conflict(atoms, intervals, width, lpp, req)
        chain = reconstruct(terminal)
        pages_out, records = nodes_to_pages(
            chain, gutter=req["binding"]["gutter_cells"],
            edge=req["binding"]["edge"])
        resolved = _resolve_targets(req, records)
        vector = tuple(len(str(resolved[rid])) for rid in xref_ids)
        history.append(vector)
        pages_for[vector] = (chain, cursor, pages_out, records, resolved)
        if len(history) >= 2 and vector == history[-2]:
            # Legality (capacity/binding) takes precedence over a tie:
            # only a fully legal final layout is checked for equal-optimal
            # dot streams.
            result = _success(req, chain, cursor, pages_out, records,
                              resolved, iteration + 1)
            if result["status"] == "conflict":
                return result
            conflict = _dot_stream_conflict(chain, ties, req)
            if conflict is not None:
                return conflict
            return result
        if vector in history[:-1]:
            return _oscillation(req, atoms, history, pages_for, xref_ids)
    return _oscillation(req, atoms, history, pages_for, xref_ids, capped=True)


def _resolve_targets(req, records):
    starts = [cl[1] for cl in req["_clusters"]]
    target_g = {
        x["id"]: max(i for i, s in enumerate(starts) if s <= x["target_start"])
        for x in req["cross_references"]
    }
    page_of = {}
    for rec in records:
        if rec["dropped"]:
            continue
        atom = rec["atom"]
        for g in range(atom["g_start"], atom["g_end"]):
            page_of[g] = rec["page"]
    out = {}
    for x in req["cross_references"]:
        g = target_g[x["id"]]
        if g not in page_of:
            g = max(k for k in page_of if k <= g)
        out[x["id"]] = page_of[g]
    return out


def _decorate_pages(pages, gutter):
    out = []
    for pg in pages:
        lines = []
        for ln in pg["lines"]:
            cells = []
            for c in ln["cells"]:
                cells.append({
                    "dots": "".join(str(d) for d in range(1, 7)
                                    if (c["mask"] >> (d - 1)) & 1),
                    "unicode": cell_unicode(c["mask"]),
                    "mask": c["mask"],
                    "col": c["col"],
                    "grapheme_start": c["grapheme_start"],
                    "grapheme_end": c["grapheme_end"],
                    "char_start": c["char_start"],
                    "char_end": c["char_end"],
                    "rule_id": c["rule_id"],
                })
            lines.append({"line": ln["line"], "cells": cells})
        out.append({"page": pg["page"], "gutter_cells": gutter,
                    "lines": lines})
    return out


def _binding_summary(req, page_count):
    b = req["binding"]
    if not b["enabled"]:
        return {"bound": False, "sheets": page_count, "duplex": False,
                "blank_padding_pages": 0}
    if b["duplex"]:
        sheets = (page_count + 1) // 2
        blank = (2 - page_count % 2) % 2
    else:
        sheets, blank = page_count, 0
    return {"bound": True, "sheets": sheets, "duplex": b["duplex"],
            "gutter_cells": b["gutter_cells"], "edge": b["edge"],
            "blank_padding_pages": blank, "max_sheets": b["max_sheets"]}


def _success(req, chain, cursor, pages, records, xref_pages, iterations):
    pages = _decorate_pages(pages, req["binding"]["gutter_cells"])
    if not pages:
        pages = [{"page": 1,
                  "gutter_cells": req["binding"]["gutter_cells"],
                  "lines": []}]
    page_count = max(cursor[0], len(pages))
    binding = _binding_summary(req, page_count)

    overflow = None
    mp = req["paper"]["max_pages"]
    if mp is not None and page_count > mp:
        overflow = ("paper", mp, page_count)
    ms = req["binding"]["max_sheets"]
    if ms is not None and binding["sheets"] > ms:
        overflow = ("binding", ms, binding["sheets"])
    if overflow:
        return _capacity_conflict(req, pages, records, overflow)

    warnings = []
    seen = set()
    for pg in pages:
        for ln in pg["lines"]:
            for c in ln["cells"]:
                if c["mask"] == ALL_DOTS and (c["char_start"],
                                              c["char_end"]) not in seen:
                    seen.add((c["char_start"], c["char_end"]))
                    warnings.append({
                        "char_start": c["char_start"],
                        "char_end": c["char_end"],
                        "message": "unmapped character rendered as all-dots",
                    })
    return {
        "status": "ok",
        "iterations": iterations,
        "page_count": page_count,
        "binding": binding,
        "cross_references": [
            {"id": x["id"], "target_page": xref_pages[x["id"]]}
            for x in req["cross_references"]
        ],
        "pages": pages,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Conflict reporting
# --------------------------------------------------------------------------
def _segment_of(req, g_start, g_end):
    clusters = req["_clusters"]
    return {
        "grapheme_start": g_start,
        "grapheme_end": g_end,
        "char_start": clusters[g_start][1],
        "char_end": clusters[g_end - 1][2],
        "text": req["body"][clusters[g_start][1]:clusters[g_end - 1][2]],
    }


def _verified_prefix(chain, stop_atom, req):
    pages, records = nodes_to_pages(
        chain, stop_atom=stop_atom,
        gutter=req["binding"]["gutter_cells"], edge=req["binding"]["edge"])
    pages = _decorate_pages(pages, req["binding"]["gutter_cells"])
    g_end = c_end = 0
    for rec in records:
        if rec["atom_index"] >= stop_atom:
            break
        atom = rec["atom"]
        g_end = max(g_end, atom["g_end"])
        c_end = max(c_end, atom["char_end"])
    return {
        "pages": pages,
        "grapheme_end": g_end,
        "char_end": c_end,
        "page_count": len(pages),
        "emitted_cells": sum(
            1 for pg in pages for ln in pg["lines"] for c in ln["cells"]),
    }


def _relaxed_conflict(atoms, intervals, width, lpp, req):
    ok, terminal, _cursor, _ties = packing_dp(
        atoms, intervals, width, lpp, strict=False)
    if not ok:  # pragma: no cover - relaxed mode always has a path
        raise InputError("layout failed even in relaxed mode")
    chain = reconstruct(terminal)
    stop = len(atoms)
    witness = None
    for node in chain:
        if node.get("violation"):
            stop, witness = node["i"], node
            break
    kind, detail, segment = _describe_violation(witness, req)
    return {
        "status": "conflict",
        "conflict": {"kind": kind, **detail, "minimal_segment": segment},
        "verified_prefix": _verified_prefix(chain, stop, req),
    }


def _describe_violation(node, req):
    vtype, payload, crossed = node["violation"]
    capacity = req["paper"]["cells_per_line"] - req["binding"]["gutter_cells"]
    if vtype == "overflow":
        atom = payload
        w = _atom_width(atom)
        if crossed:
            iv = min(crossed, key=lambda x: (x["g_start"], x["id"]))
            return ("interval_too_wide",
                    {"interval_id": iv["id"],
                     "reason": "content wider than one line",
                     "atom_width": w, "line_capacity": capacity},
                    _segment_of(req, iv["g_start"], iv["g_end"]))
        return ("content_too_wide",
                {"reason": "atom wider than a single line",
                 "atom_width": w, "line_capacity": capacity},
                _segment_of(req, atom["g_start"], atom["g_end"]))
    _pos, crossed_iv = payload, crossed
    iv = min(crossed_iv, key=lambda x: (x["g_start"], x["id"]))
    return ("interval_too_wide",
            {"interval_id": iv["id"],
             "reason": "page break forced inside unbreakable interval",
             "boundary_atom": node["i"],
             "line_capacity": capacity},
            _segment_of(req, iv["g_start"], iv["g_end"]))


def _atom_width(atom):
    if atom["type"] == "word":
        return min(o["width"] for o in atom["options"])
    return atom.get("width", len(atom.get("cells", [])))


def _capacity_conflict(req, pages, records, overflow):
    scope, cap, used = overflow
    duplex = scope == "binding" and req["binding"]["duplex"]
    cap_pages = cap * (2 if duplex else 1)
    stop = len(records)
    first_atom = None
    for rec in records:
        if rec["dropped"]:
            continue
        if rec["page"] > cap_pages and stop == len(records):
            stop = rec["atom_index"]
        if rec["page"] == cap_pages + 1 and first_atom is None:
            first_atom = rec["atom"]
    segment = (_segment_of(req, first_atom["g_start"], first_atom["g_end"])
               if first_atom else None)
    prefix = _verified_prefix_from(req, pages[:cap_pages], records, stop)
    return {
        "status": "conflict",
        "conflict": {
            "kind": "binding_overflow" if scope == "binding"
                    else "capacity_overflow",
            "scope": scope,
            "capacity": cap,
            "used": used,
            "unit": "sheets" if scope == "binding" else "pages",
            "minimal_segment": segment,
        },
        "verified_prefix": prefix,
    }


def _verified_prefix_from(req, pages, records, stop):
    g_end = c_end = 0
    for rec in records:
        if rec["atom_index"] >= stop:
            break
        atom = rec["atom"]
        g_end = max(g_end, atom["g_end"])
        c_end = max(c_end, atom["char_end"])
    return {
        "pages": pages,
        "grapheme_end": g_end,
        "char_end": c_end,
        "page_count": len(pages),
        "emitted_cells": sum(
            1 for pg in pages for ln in pg["lines"] for c in ln["cells"]),
    }


def _ambiguity_result(amb):
    return {
        "status": "conflict",
        "conflict": {"kind": "rule_ambiguity", "reason": amb["reason"],
                     "pattern": amb.get("pattern"),
                     "rule_ids": amb.get("rule_ids"),
                     "minimal_segment": amb.get("segment")},
        "verified_prefix": {"pages": [], "grapheme_end": 0, "char_end": 0,
                            "page_count": 0, "emitted_cells": 0},
    }


def _dot_stream_conflict(chain, tie_terminals, req):
    """Detect equal-optimal layouts whose dot streams differ.

    The packing DP records two kinds of equal-optimal alternatives: forks
    attached to a chosen node (``alts``, same cursor and cost) and terminal
    heads at other cursors that tie under ``_terminal_rank``.  Either way
    the alternatives tie on pages / uncontracted cells / last row-column
    while embossing different dots, so the document cannot be finalized.
    The earliest fork over all alternatives is the minimal conflict
    segment and the shared prefix is verified.
    """
    forks = []  # (first atom index where the streams differ, alt head)

    def register(alt_head):
        # Index the alternative chain by atom index, then walk the main
        # chain backwards to find the last node the two chains share.
        alt_by_i = {}
        cur = alt_head
        while cur is not None:
            alt_by_i[cur["i"]] = cur
            cur = cur.get("prev")
        shared_i = -1
        main_node = chain[-1]
        while main_node is not None:
            if alt_by_i.get(main_node["i"]) is main_node:
                shared_i = main_node["i"]
                break
            main_node = main_node.get("prev")
        forks.append((shared_i + 1, alt_head))

    for node in chain:
        for alt in node.get("alts", []):
            register(alt)
    for tie in tie_terminals:
        register(tie)
        for alt in tie.get("alts", []):
            register(alt)
    if not forks:
        return None
    stop = min(f for f, _ in forks)
    witness = chain[stop]
    segment = _segment_of(req, witness["atom"]["g_start"],
                          witness["atom"]["g_end"])
    return {
        "status": "conflict",
        "conflict": {
            "kind": "rule_ambiguity",
            "reason": "equal_optimal_dot_stream",
            "boundary_atom": stop,
            "minimal_segment": segment,
        },
        "verified_prefix": _verified_prefix(chain, stop, req),
    }


def _oscillation(req, atoms, history, pages_for, xref_ids, capped=False):
    last = history[-1]
    first = history.index(last)
    cycle = history[first:]
    cycle_layouts = [pages_for[v] for v in cycle]
    per_atom = [
        {rec["atom_index"]: rec for rec in lay[3] if not rec["dropped"]}
        for lay in cycle_layouts
    ]
    stop = len(atoms)
    for i in range(len(atoms)):
        ps = {m[i]["page"] for m in per_atom if i in m}
        if len(ps) > 1:
            stop = i
            break
    oscillating = {
        rid for col, rid in enumerate(xref_ids)
        if len({v[col] for v in cycle}) > 1
    }
    chosen_chain = cycle_layouts[0][0]
    segment = None
    for atom in atoms[:stop + 1]:
        if atom["type"] == "xref" and atom["id"] in oscillating:
            segment = _segment_of(req, atom["g_start"], atom["g_end"])
            break
    if segment is None:
        atom = atoms[min(stop, len(atoms) - 1)]
        segment = _segment_of(req, atom["g_start"], atom["g_end"])
    return {
        "status": "conflict",
        "conflict": {
            "kind": "page_number_oscillation",
            "reason": ("fixpoint_iteration_cap" if capped
                       else "xref page digit width does not converge"),
            "cycle_width_vectors": [list(v) for v in cycle],
            "oscillating_xref_ids": sorted(oscillating),
            "minimal_segment": segment,
        },
        "verified_prefix": _verified_prefix(chosen_chain, stop, req),
    }
