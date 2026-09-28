"""Faulty-pin compensation for an already-finalized revision.

Some embosser pins may be broken: only a subset of the six braille pins
(``1``..``6``) still fires.  The operator can still emboss a finalized
document by combining paper orientations across multiple embossing passes:

* ``normal``      -- device coordinates equal the finalized page/line/col;
                     logical dot ``d`` is struck by pin ``d``.
* ``rotate_180``  -- the sheet is turned 180 degrees.  A finalized cell at
                     page/line/col ``(p, l, c)`` lands on the device cell
                     ``(p, L+1-l, W+1-c)`` (``L`` lines/page, ``W``
                     cells/line, gutter included), and logical dot ``d`` is
                     struck by pin ``7-d`` (the 180-degree rotation of the
                     six-dot cell: 1<->6, 2<->5, 3<->4).

This module **only reads the immutable per-page units of a finalization**:
it never re-paginates and never rewrites the finalization.  Physical
coordinates and pin numbers are obtained by inverse-transforming the
finalized rows/columns/dots; the search then finds the minimum number of
orientations (passes) whose union covers every raised dot, breaking ties in
the fixed order ``normal`` before ``rotate_180``.  Every target dot is
assigned to the first pass that can produce it, so merging the emitted
device masks reconstructs the finalized document dot-for-dot and never
strikes an extra dot.

When no feasible combination exists (or the best combination needs more
passes than allowed), :class:`InfeasibleCompensation` (HTTP 409) reports the
first unreachable cell, the dot numbers that cannot be struck and the
verified page prefix before it.
"""
import itertools

from .errors import InfeasibleCompensation, InputError
from .readback import dots_of

NORMAL = "normal"
ROTATE_180 = "rotate_180"
# Fixed tie-break / pass order: normal before rotate_180.
ORIENTATION_ORDER = (NORMAL, ROTATE_180)
# At most one pass per distinct orientation can ever help.
MAX_PASSES = len(ORIENTATION_ORDER)


def _is_dot_number(value):
    return isinstance(value, int) and not isinstance(value, bool) \
        and 1 <= value <= 6


def parse_compensation_request(payload):
    """Validate ``{working_dots, orientations, max_passes}``.

    Returns ``(working_dots, orientations, max_passes)`` with working dots
    deduplicated/sorted and orientations preserved as sent (their relative
    order never affects the fixed tie-break).  Illegal dot numbers, unknown
    or repeated orientations, and pass counts outside ``1..2`` raise
    :class:`InputError`.
    """
    if not isinstance(payload, dict):
        raise InputError("compensation request body must be a JSON object")

    raw_dots = payload.get("working_dots")
    if not isinstance(raw_dots, list):
        raise InputError("working_dots must be a list of pin dot numbers")
    working = set()
    for i, value in enumerate(raw_dots):
        if not _is_dot_number(value):
            raise InputError(
                f"working_dots[{i}] {value!r} is invalid: "
                "expected an integer dot number 1..6")
        if value in working:
            raise InputError(
                f"working_dots[{i}] repeats dot number {value}")
        working.add(value)

    orientations = payload.get("orientations")
    if not isinstance(orientations, list) or not orientations:
        raise InputError(
            "orientations must be a non-empty list of 'normal' / "
            "'rotate_180'")
    seen = set()
    for i, value in enumerate(orientations):
        if value not in ORIENTATION_ORDER:
            raise InputError(
                f"orientations[{i}] {value!r} is invalid: expected "
                "'normal' or 'rotate_180'")
        if value in seen:
            raise InputError(
                f"orientations[{i}] repeats orientation {value!r}")
        seen.add(value)

    max_passes = payload.get("max_passes")
    if (not isinstance(max_passes, int) or isinstance(max_passes, bool)
            or not 1 <= max_passes <= MAX_PASSES):
        raise InputError(
            f"max_passes {max_passes!r} is invalid: expected an integer "
            f"1..{MAX_PASSES}")
    return sorted(working), list(orientations), max_passes


def _strikable(dot, orientation, working_dots):
    """Whether a working pin can make logical ``dot`` in ``orientation``."""
    pin = dot if orientation == NORMAL else 7 - dot
    return pin in working_dots


def _iter_cells(pages):
    """Finalized non-blank cells in reading order with raised dots."""
    for pg in pages:
        p = pg["page"]
        for ln in pg["lines"]:
            l = ln["line"]
            for cell in ln["cells"]:
                dots = dots_of(cell["mask"])
                if dots:
                    yield p, l, cell, dots


def _covers(dot_set, combo, working_dots):
    return all(any(_strikable(d, o, working_dots) for o in combo)
               for d in dot_set)


def _choose_combo(dot_set, allowed, max_passes, working_dots):
    """Smallest covering combination, normal before rotate_180 on ties."""
    ordered = [o for o in ORIENTATION_ORDER if o in allowed]
    if not dot_set:
        return []
    for size in range(1, max_passes + 1):
        for combo in itertools.combinations(ordered, size):
            if _covers(dot_set, combo, working_dots):
                return list(combo)
    return None


def _infeasible(pages, allowed, max_passes, working_dots, needed_dots):
    """Build the 409 report: first blocking cell + verified page prefix."""
    ordered = [o for o in ORIENTATION_ORDER if o in allowed]
    union_gap = {
        d for d in needed_dots
        if not any(_strikable(d, o, working_dots) for o in ordered)
    }

    if union_gap:
        # Dots that no allowed orientation can make at all.
        def is_missing(d):
            return d in union_gap
        global_missing = union_gap
    else:
        # Every dot is reachable, but no combination fits the pass cap:
        # report what the first fixed-order orientation cannot carry.
        baseline = ordered[0]

        def is_missing(d):
            return not _strikable(d, baseline, working_dots)

        global_missing = {
            d for d in needed_dots if is_missing(d)}

    first_cell = None
    missing_at_cell = []
    for p, l, cell, dots in _iter_cells(pages):
        miss = [d for d in dots if is_missing(d)]
        if miss and first_cell is None:
            first_cell = (p, l, cell)
            missing_at_cell = miss
    assert first_cell is not None  # a blocking cell witnesses the failure

    p = first_cell[0]
    prefix_dots = sum(
        len(dots) for pp, _l, _c, dots in _iter_cells(pages) if pp < p)
    raise InfeasibleCompensation(
        "no orientation combination within max_passes covers every "
        f"finalized dot with working pins {sorted(working_dots)!r}",
        {
            "working_dots": sorted(working_dots),
            "allowed_orientations": list(allowed),
            "max_passes": max_passes,
            "first_unreachable_cell": {
                "page": first_cell[0], "line": first_cell[1],
                "col": first_cell[2]["col"],
                "mask": first_cell[2]["mask"],
                "dots": dots_of(first_cell[2]["mask"]),
                "unreachable_dots": sorted(missing_at_cell),
            },
            "missing_dots": sorted(global_missing),
            "verified_prefix": {
                "page_count": p - 1,
                "pages": list(range(1, p)),
                "target_dots": prefix_dots,
            },
        })


def _source_ref(cell, assigned_dots):
    return {
        "page": cell["__p"], "line": cell["__l"], "col": cell["col"],
        "mask": cell["mask"], "dots": sorted(assigned_dots),
        "grapheme_start": cell["grapheme_start"],
        "grapheme_end": cell["grapheme_end"],
        "char_start": cell["char_start"],
        "char_end": cell["char_end"],
        "rule_id": cell.get("rule_id"),
    }


def compensate(finalized, paper, working_dots, orientations, max_passes):
    """Plan compensation passes against a finalized per-page document.

    ``finalized`` is the immutable ``pages.json`` object (or any object with
    the same ``pages`` structure) and ``paper`` the finalized revision's
    paper capacity (``cells_per_line`` / ``lines_per_page``), used only for
    the inverse 180-degree transform.  Raises :class:`InputError` for a
    non-``ok`` shape and :class:`InfeasibleCompensation` (409) when no
    feasible combination exists.
    """
    pages = finalized.get("pages")
    if not isinstance(pages, list):
        raise InputError("finalization has no readable per-page units")
    width = paper["cells_per_line"]
    lines_per_page = paper["lines_per_page"]
    allowed = set(orientations)

    # Target dots in finalized reading order; the first-available-pass
    # assignment below consumes them in exactly this order.
    targets = []
    needed = set()
    for p, l, cell, dots in _iter_cells(pages):
        cell = dict(cell)
        cell["__p"], cell["__l"] = p, l
        for d in dots:
            targets.append((p, l, cell, d))
            needed.add(d)

    combo = _choose_combo(needed, allowed, max_passes, set(working_dots))
    if combo is None:
        _infeasible(pages, orientations, max_passes, set(working_dots),
                    needed)
        raise AssertionError("unreachable")  # pragma: no cover

    # accum[pass_index][(dev_page, dev_line, dev_col)] = (pins:set, cell)
    accum = [{} for _ in combo]
    # Exactness guard: rebuilt source dots per finalized cell.
    rebuilt = {}
    for p, l, cell, d in targets:
        for oi, orientation in enumerate(combo):
            if not _strikable(d, orientation, set(working_dots)):
                continue
            if orientation == NORMAL:
                dev_pos = (p, l, cell["col"])
                pin = d
            else:
                dev_pos = (p, lines_per_page + 1 - l,
                           width + 1 - cell["col"])
                pin = 7 - d
            entry = accum[oi].get(dev_pos)
            if entry is None:
                entry = (set(), cell)
                accum[oi][dev_pos] = entry
            entry[0].add(pin)
            rebuilt.setdefault((p, l, cell["col"]), set()).add(d)
            break
        else:  # pragma: no cover - combo selection guarantees coverage
            raise RuntimeError(
                f"finalized dot {d} at page {p} line {l} col "
                f"{cell['col']} could not be assigned")

    # The merge must equal the finalization dot-for-dot, with no extra dots.
    for p, l, cell, dots in _iter_cells(pages):
        if rebuilt.get((p, l, cell["col"]), set()) != set(dots):
            raise RuntimeError(  # pragma: no cover - defensive
                "compensation merge diverged from the finalization")
    if len(rebuilt) != sum(1 for _ in _iter_cells(pages)):
        raise RuntimeError(  # pragma: no cover - defensive
            "compensation merge embossed extra device cells")

    passes = []
    for oi, orientation in enumerate(combo):
        page_map = {}
        for (dp, dl, dc), (pins, cell) in accum[oi].items():
            mask = 0
            for pin in pins:
                mask |= 1 << (pin - 1)
            # each fired pin corresponds to one assigned finalized dot
            assigned = sorted(pin if orientation == NORMAL else 7 - pin
                              for pin in pins)
            page_map.setdefault(dp, {}).setdefault(dl, []).append({
                "col": dc,
                "mask": mask,
                "dots": dots_of(mask),
                "source": _source_ref(cell, assigned),
            })
        dev_pages = []
        for dp in sorted(page_map):
            dev_lines = []
            for dl in sorted(page_map[dp]):
                cells_out = sorted(page_map[dp][dl],
                                   key=lambda x: x["col"])
                dev_lines.append({"line": dl, "cells": cells_out})
            dev_pages.append({"page": dp, "lines": dev_lines})
        passes.append({
            "pass_no": oi + 1,
            "orientation": orientation,
            "dot_count": sum(len(e[0]) for e in accum[oi].values()),
            "cell_count": len(accum[oi]),
            "pages": dev_pages,
        })

    return {
        "status": "compensated",
        "working_dots": list(working_dots),
        "allowed_orientations": list(orientations),
        "max_passes": max_passes,
        "pass_count": len(combo),
        "orientations": combo,
        "target_dot_count": len(targets),
        "merged_cell_count": len(rebuilt),
        "exact": True,
        "passes": passes,
    }
