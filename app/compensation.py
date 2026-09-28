"""Faulty-pin compensation for an already-finalized revision.

When some embosser pins (压针) are broken, the same raised dots can often
still be produced by rotating the paper by 180 degrees: a page dot
positioned at (line, col, dot d) lands on the *device* coordinates

    device_line = lines_per_page + 1 - line
    device_col  = cells_per_line + 1 - col
    device_dot  = 7 - d

because a half-turn swaps the two dot columns top-to-bottom
(1<->6, 2<->5, 3<->4).

Given the set of still-working device pins, the paper orientations that
may be used (``normal`` and/or ``rotate_180``) and a bound on the number
of embossing passes (压印遍数), this module:

* enumerates orientation sequences shortest-first, breaking equal-length
  ties in the fixed order ``normal`` before ``rotate_180``;
* greedily assigns every target dot to the *first* pass whose orientation
  drives it with a working pin;
* emits, per pass, the device-coordinate masks to emboss plus the mapping
  of every emitted device cell back to its original page/line/col.

The union of the emitted masks, transformed back into page coordinates,
is bit-for-bit equal to the finalized per-page cells and never contains an
extra dot -- the module verifies this before answering.

Only the stored, finalized per-page cells are read.  The revision is
never re-paginated and the finalization is never rewritten.
"""
from .errors import Conflict, InputError

NORMAL = "normal"
ROTATE_180 = "rotate_180"
ORIENTATIONS = (NORMAL, ROTATE_180)

# Two physical orientations exist, so a sequence longer than two passes
# can never cover an additional dot: repeating an orientation drives the
# same device pins at the same coordinates again.
MAX_PASSES_HARD_LIMIT = 2


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------
def parse_compensation(payload):
    """Validate the compensation request.

    Returns ``(working_dots, orientations, max_passes)`` where
    ``working_dots`` is the sorted list of usable device pin numbers,
    ``orientations`` the allowed orientations in caller-listed order and
    ``max_passes`` the bound on the number of embossing passes.
    """
    if not isinstance(payload, dict):
        raise InputError("compensation request body must be a JSON object")

    pins_in = payload.get("working_dots")
    if not isinstance(pins_in, list):
        raise InputError("working_dots must be a list of pin numbers")
    working, seen = [], set()
    for i, value in enumerate(pins_in):
        if not isinstance(value, int) or isinstance(value, bool) \
                or not 1 <= value <= 6:
            raise InputError(
                f"working_dots[{i}] {value!r} is invalid: "
                "expected a pin number 1..6")
        if value in seen:
            raise InputError(f"working_dots[{i}] repeats pin {value}")
        seen.add(value)
        working.append(value)
    working.sort()

    dirs_in = payload.get("orientations")
    if not isinstance(dirs_in, list) or not dirs_in:
        raise InputError(
            "orientations must be a non-empty list of 'normal'/"
            "'rotate_180'")
    orientations, seen_dirs = [], set()
    for i, value in enumerate(dirs_in):
        if not isinstance(value, str) or value not in ORIENTATIONS:
            raise InputError(
                f"orientations[{i}] {value!r} is invalid: expected "
                "'normal' or 'rotate_180'")
        if value in seen_dirs:
            raise InputError(
                f"orientations[{i}] repeats orientation {value!r}")
        seen_dirs.add(value)
        orientations.append(value)

    max_passes = payload.get("max_passes")
    if not isinstance(max_passes, int) or isinstance(max_passes, bool):
        raise InputError("max_passes must be an integer")
    if not 1 <= max_passes <= MAX_PASSES_HARD_LIMIT:
        raise InputError(
            f"max_passes must be between 1 and {MAX_PASSES_HARD_LIMIT}")
    return working, orientations, max_passes


# --------------------------------------------------------------------------
# Geometry: page coordinates <-> device coordinates
# --------------------------------------------------------------------------
def rotate_dot(dot):
    """Device pin driven by page dot ``dot`` when paper is rotated 180."""
    return 7 - dot


def _device_position(line, col, lpp, cpl, orientation):
    if orientation == NORMAL:
        return line, col
    return lpp + 1 - line, cpl + 1 - col


def _dots_of(mask):
    return [d for d in range(1, 7) if (mask >> (d - 1)) & 1]


# --------------------------------------------------------------------------
# Target collection / feasibility
# --------------------------------------------------------------------------
def _collect_targets(pages):
    """Flatten finalized pages into target-dot records in scan order."""
    targets = []
    for pg in pages:
        page_no = pg["page"]
        for ln in pg["lines"]:
            line_no = ln["line"]
            for cell in ln["cells"]:
                if cell["mask"] == 0:
                    continue
                for dot in _dots_of(cell["mask"]):
                    targets.append({
                        "page": page_no, "line": line_no,
                        "col": cell["col"], "dot": dot, "mask": cell["mask"],
                        "grapheme_start": cell["grapheme_start"],
                        "grapheme_end": cell["grapheme_end"],
                        "char_start": cell["char_start"],
                        "char_end": cell["char_end"],
                    })
    return targets


def _reachable(dot, orientation, working_set):
    device_dot = dot if orientation == NORMAL else rotate_dot(dot)
    return device_dot in working_set


def _globally_unreachable(targets, orientations, working_set):
    """Dots no allowed orientation can drive, in scan order."""
    return [t for t in targets
            if not any(_reachable(t["dot"], d, working_set)
                       for d in orientations)]


def _assign_passes(targets, sequence, working_set):
    """Assign each target dot to the first pass that can drive it.

    Returns ``(passes, uncovered)``: ``passes`` maps a pass index to the
    target records embossed in that pass; ``uncovered`` lists targets no
    pass of ``sequence`` can reach.
    """
    passes = [[] for _ in sequence]
    uncovered = []
    for t in targets:
        for k, orientation in enumerate(sequence):
            if _reachable(t["dot"], orientation, working_set):
                passes[k].append(t)
                break
        else:
            uncovered.append(t)
    return passes, uncovered


def _candidate_sequences(orientations, max_passes):
    """All sequences up to ``max_passes`` long, shortest first.

    Equal-length ties use the fixed orientation order (``normal`` before
    ``rotate_180``), independent of the order the caller listed them in.
    """
    ordered = [d for d in ORIENTATIONS if d in set(orientations)]

    def extend(prefix, depth):
        if depth <= 0:
            return
        for d in ordered:
            seq = prefix + [d]
            yield seq
            yield from extend(seq, depth - 1)

    yield from extend([], max_passes)


# --------------------------------------------------------------------------
# 409 reporting
# --------------------------------------------------------------------------
def _prefix_before(pages, page_no):
    """Page-coordinate reconstruction strictly before ``page_no``."""
    out = []
    for pg in pages:
        if pg["page"] >= page_no:
            break
        lines = []
        for ln in pg["lines"]:
            cells = [{
                "line": ln["line"], "col": cell["col"],
                "mask": cell["mask"],
                "dots": "".join(str(d) for d in _dots_of(cell["mask"])),
            } for cell in ln["cells"]]
            lines.append({"line": ln["line"], "cells": cells})
        out.append({"page": pg["page"], "lines": lines})
    return out


def _conflict_payload(reason, first_missing, missing_dot_set, pages,
                      required_passes, max_passes, orientations,
                      working_set):
    prefix_pages = _prefix_before(pages, first_missing["page"])
    # For every globally unreachable page dot, report the device pin each
    # allowed orientation would need (all of them are broken).
    missing_detail = []
    for dot in sorted(missing_dot_set):
        missing_detail.append({
            "dot": dot,
            "needs_pin": {
                d: (dot if d == NORMAL else rotate_dot(dot))
                for d in ORIENTATIONS if d in set(orientations)},
            "available": {
                d: (dot if d == NORMAL else rotate_dot(dot))
                in working_set
                for d in ORIENTATIONS if d in set(orientations)},
        })
    payload = {
        "reason": reason,
        "first_unreachable_cell": {
            "page": first_missing["page"],
            "line": first_missing["line"],
            "col": first_missing["col"],
            "dot": first_missing["dot"],
            "expected_mask": first_missing["mask"],
            "grapheme_start": first_missing["grapheme_start"],
            "grapheme_end": first_missing["grapheme_end"],
            "char_start": first_missing["char_start"],
            "char_end": first_missing["char_end"],
        },
        "missing_dots": sorted(missing_dot_set),
        "missing_dot_detail": missing_detail,
        "required_passes": required_passes,
        "max_passes_allowed": max_passes,
        "verified_prefix": {
            "page_count": len(prefix_pages),
            "pages": prefix_pages,
            "emitted_cells": sum(
                len(ln["cells"]) for p in prefix_pages
                for ln in p["lines"]),
            "emitted_dots": sum(
                len(_dots_of(cell["mask"])) for p in prefix_pages
                for ln in p["lines"] for cell in ln["cells"]),
        },
    }
    return payload


# --------------------------------------------------------------------------
# Pass construction
# --------------------------------------------------------------------------
def _build_pass(pass_no, orientation, assigned, lpp, cpl):
    """Aggregate one pass's target dots into device-coordinate masks."""
    cells_at = {}  # (page, device_line, device_col) -> aggregation
    for t in assigned:
        dline, dcol = _device_position(t["line"], t["col"], lpp, cpl,
                                       orientation)
        device_dot = t["dot"] if orientation == NORMAL \
            else rotate_dot(t["dot"])
        key = (t["page"], dline, dcol)
        entry = cells_at.get(key)
        if entry is None:
            entry = {"mask": 0, "source_dots": 0,
                     "orig": (t["page"], t["line"], t["col"]),
                     "char": (t["char_start"], t["char_end"]),
                     "grapheme": (t["grapheme_start"],
                                  t["grapheme_end"])}
            cells_at[key] = entry
        entry["mask"] |= 1 << (device_dot - 1)
        entry["source_dots"] |= 1 << (t["dot"] - 1)

    page_map = {}
    for (page_no, dline, dcol), entry in cells_at.items():
        page_map.setdefault(page_no, {}).setdefault(dline, []).append(
            (dcol, entry))

    out_pages, total_cells, total_dots = [], 0, 0
    for page_no in sorted(page_map):
        lines = []
        for dline in sorted(page_map[page_no]):
            cells = []
            for dcol, entry in sorted(page_map[page_no][dline]):
                op, ol, oc = entry["orig"]
                cs, ce = entry["char"]
                gs, ge = entry["grapheme"]
                mask = entry["mask"]
                source_dots = entry["source_dots"]
                cells.append({
                    "device_line": dline,
                    "device_col": dcol,
                    "device_dots": _dots_of(mask),
                    "mask": mask,
                    "source": {"page": op, "line": ol, "col": oc,
                               "dots": _dots_of(source_dots),
                               "grapheme_start": gs, "grapheme_end": ge,
                               "char_start": cs, "char_end": ce},
                })
            lines.append({"device_line": dline, "cells": cells})
            total_cells += len(cells)
            total_dots += sum(len(c["device_dots"]) for c in cells)
        out_pages.append({"page": page_no, "lines": lines})
    return {
        "pass": pass_no,
        "orientation": orientation,
        "page_count": len(out_pages),
        "cell_count": total_cells,
        "dot_count": total_dots,
        "pages": out_pages,
    }


def _finalized_dots(pages):
    """Map page -> {(line, col): mask}, keeping only non-empty cells."""
    out = {}
    for pg in pages:
        for ln in pg["lines"]:
            for cell in ln["cells"]:
                if cell["mask"]:
                    out.setdefault(pg["page"], {})[
                        (ln["line"], cell["col"])] = cell["mask"]
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def compensate(layout, request_obj, payload):
    """Plan faulty-pin compensation passes for a finalized layout.

    Raises :class:`InputError` for a malformed request and
    :class:`Conflict` (HTTP 409) when no allowed orientation combination
    reproduces the finalized dots within ``max_passes``.
    """
    if layout.get("status") != "ok":
        raise InputError("only an 'ok' finalization can be compensated")
    working, orientations, max_passes = parse_compensation(payload)
    working_set = set(working)

    paper = request_obj.get("paper", {})
    cpl, lpp = paper.get("cells_per_line"), paper.get("lines_per_page")
    if not isinstance(cpl, int) or not isinstance(lpp, int):
        raise InputError("finalized request lacks paper capacity")

    pages = layout.get("pages", [])
    targets = _collect_targets(pages)

    # Case 1: a target dot that no allowed orientation can drive with a
    # working pin -- infeasible regardless of the pass budget.
    globally_bad = _globally_unreachable(targets, orientations,
                                         working_set)
    if globally_bad:
        first = globally_bad[0]
        raise Conflict(_conflict_payload(
            "unreachable_dot", first, {t["dot"] for t in globally_bad},
            pages, None, max_passes, orientations, working_set))

    # Case 2: search orientation combinations shortest-first; ties are
    # resolved by _candidate_sequences in the fixed normal->rotate order.
    chosen = None
    last_sequence = None
    for seq in _candidate_sequences(orientations, max_passes):
        last_sequence = seq
        _, uncovered = _assign_passes(targets, seq, working_set)
        if not uncovered:
            chosen = seq
            break

    if chosen is None:
        # Every dot is individually reachable, but covering all of them
        # needs more passes than the budget allows.  The lexicographically
        # first max-length sequence leaves the smallest (tie-broken) set of
        # uncovered dots; report its first uncovered target.
        _, uncovered = _assign_passes(targets, last_sequence, working_set)
        first = uncovered[0]
        # Shortest feasible length without the budget, for the report.
        required = None
        for length in range(1, MAX_PASSES_HARD_LIMIT + 1):
            for cand in _candidate_sequences(orientations, length):
                _, miss = _assign_passes(targets, cand, working_set)
                if not miss:
                    required = length
                    break
            if required is not None:
                break
        raise Conflict(_conflict_payload(
            "max_passes_exceeded", first,
            {t["dot"] for t in uncovered}, pages, required, max_passes,
            orientations, working_set))

    groups, uncovered = _assign_passes(targets, chosen, working_set)
    assert not uncovered

    passes = []
    for k, (orientation, assigned) in enumerate(zip(chosen, groups)):
        if assigned:
            passes.append(_build_pass(k + 1, orientation, assigned,
                                      lpp, cpl))
    # Empty passes are dropped; renumber the emitted passes.
    for k, report in enumerate(passes, start=1):
        report["pass"] = k

    # Safety net: transform every emitted device mask back into page
    # coordinates and demand bit-for-bit equality with the finalization
    # (no missing and no extra dots).
    rebuilt = {}
    for report in passes:
        orientation = report["orientation"]
        for pg in report["pages"]:
            for ln in pg["lines"]:
                for cell in ln["cells"]:
                    if orientation == NORMAL:
                        sline, scol = cell["device_line"], cell["device_col"]
                    else:
                        sline = lpp + 1 - cell["device_line"]
                        scol = cpl + 1 - cell["device_col"]
                    # Remap device pins back to page dot numbers: the source
                    # mapping already records which page dots this device
                    # cell contributes.
                    page_mask = 0
                    for d in cell["source"]["dots"]:
                        page_mask |= 1 << (d - 1)
                    bucket = rebuilt.setdefault(pg["page"], {})
                    key = (sline, scol)
                    bucket[key] = bucket.get(key, 0) | page_mask
    if rebuilt != _finalized_dots(pages):
        raise Conflict({  # defensive: mathematically unreachable
            "reason": "compensation_mismatch",
            "message": "merged passes do not reproduce the finalized dots",
        })

    return {
        "status": "compensated",
        "working_dots": working,
        "orientations": orientations,
        "max_passes": max_passes,
        "pass_count": len(passes),
        "target_dot_count": len(targets),
        "page_count": len(pages),
        "passes": passes,
    }
