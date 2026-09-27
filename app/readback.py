"""Embossed-proof readback verification against a finalized revision.

The scanner of an embossed page reports the raised dots it actually read,
arranged per page/line as 6-dot masks (integers 0..63).  This module
compares that measurement with the immutable per-page JSON of an
already-finalized revision.  It **never re-plans or rewrites** the
finalization: the stored layout is the sole source of truth.

For every line the measured cells are aligned to the expected cells with a
minimum-edit-cost alignment (unit costs for substitution / omission /
extra).  Among equal-cost alignments the lexicographically first operation
stream is chosen, with operations ordered ``match > substitution >
omission (漏印) > extra (额外单元)``.  That preference is what keeps a
single missing embossed cell from shifting every following cell out of
place: an omission pairs the remaining cells one-for-one at the same total
cost as one substitution plus a trailing extra, and the omission is
preferred.

The comparison is line-local: page and line counts must be exactly those
of the finalization (structural mismatch -> ``InputError``), while the
number of measured cells per line may differ and is resolved by the
alignment.
"""
from .errors import InputError

# Edit operation codes (also the order of the equal-cost tie break).
MATCH = "match"
SUBSTITUTE = "substitute"
OMISSION = "omission"   # expected cell was not embossed/read: 漏印
EXTRA = "extra"         # measured cell with no expected counterpart: 额外单元

# Unit edit costs; the operation order above resolves equal-cost ties.
_OP_COST = {MATCH: 0, SUBSTITUTE: 1, OMISSION: 1, EXTRA: 1}


def dots_of(mask):
    """Raised dot numbers (1..6) of a mask, ascending."""
    return [d for d in range(1, 7) if (mask >> (d - 1)) & 1]


def _is_mask(value):
    return isinstance(value, int) and not isinstance(value, bool) \
        and 0 <= value <= 63


def parse_readback(payload):
    """Validate the request payload into ``[[[mask, ...], ...], ...]``.

    The canonical shape is ``{"pages": [{"lines": [[mask, ...], ...]},
    ...]}``; a page may also be given as a bare list of lines and a line
    as a bare list of masks.  Structural problems and masks outside 0..63
    raise :class:`InputError`.
    """
    if not isinstance(payload, dict):
        raise InputError("readback request body must be a JSON object")
    pages_in = payload.get("pages")
    if not isinstance(pages_in, list):
        raise InputError("readback 'pages' must be a list")

    def parse_line(line_in, p, l):
        where = f"pages[{p}].lines[{l}]"
        if not isinstance(line_in, list):
            raise InputError(f"{where} must be a list of cell masks")
        for i, value in enumerate(line_in):
            if not _is_mask(value):
                raise InputError(
                    f"{where}[{i}] mask {value!r} is invalid: "
                    "expected an integer 0..63")
        return list(line_in)

    pages = []
    for p, page_in in enumerate(pages_in):
        if isinstance(page_in, dict):
            lines_in = page_in.get("lines")
        elif isinstance(page_in, list):
            lines_in = page_in
        else:
            raise InputError(
                f"pages[{p}] must be an object or a list of lines")
        if not isinstance(lines_in, list):
            raise InputError(f"pages[{p}].lines must be a list")
        lines = [parse_line(ln, p, l) for l, ln in enumerate(lines_in)]
        pages.append(lines)
    return pages


def expected_pages(layout_pages):
    """Extract ``[[expected_cell, ...], ...]`` from finalized pages JSON."""
    return [[ln["cells"] for ln in pg["lines"]] for pg in layout_pages]


def align_line(expected, measured):
    """Minimum-edit-cost alignment of one line.

    Returns ``(ops, cost)`` where ``ops`` is a list of tuples
    ``(op, expected_index_or_None, measured_index_or_None)`` and ``cost``
    is the edit distance.  Equal-cost alignments are resolved with the
    ``match > substitute > omission > extra`` lexicographic preference via
    a suffix (remainder) DP table plus a greedy walk.
    """
    n, m = len(expected), len(measured)
    # rest[i][j] = minimum cost to align expected[i:] with measured[j:].
    rest = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        rest[i][m] = n - i
    for j in range(m - 1, -1, -1):
        rest[n][j] = m - j
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            pair = 0 if expected[i]["mask"] == measured[j] else 1
            rest[i][j] = min(rest[i + 1][j + 1] + pair,
                             rest[i + 1][j] + 1,
                             rest[i][j + 1] + 1)

    ops = []
    i = j = 0
    while i < n or j < m:
        candidates = []
        if i < n and j < m:
            op = MATCH if expected[i]["mask"] == measured[j] else SUBSTITUTE
            candidates.append((op, i + 1, j + 1, i, j))
        if i < n:
            candidates.append((OMISSION, i + 1, j, i, None))
        if j < m:
            candidates.append((EXTRA, i, j + 1, None, j))
        # candidates are already emitted in the tie-break order; keep the
        # first step whose remainder realizes the optimal total.
        total = rest[i][j]
        chosen = None
        for opname, ni, nj, ei, mi in candidates:
            if _OP_COST[opname] + rest[ni][nj] == total:
                chosen = (opname, ei, mi)
                break
        assert chosen is not None
        opname, ei, mi = chosen
        ops.append(chosen)
        if opname in (MATCH, SUBSTITUTE, OMISSION):
            i += 1
        if opname in (MATCH, SUBSTITUTE, EXTRA):
            j += 1
    return ops, rest[0][0]


def _expected_position(page_no, line_no, cell):
    return {"page": page_no, "line": line_no, "col": cell["col"]}


def _measured_position(page_no, line_no, measured_index):
    return {"page": page_no, "line": line_no,
            "cell_index": measured_index + 1}


def _source_ref(cell):
    return {
        "grapheme_start": cell["grapheme_start"],
        "grapheme_end": cell["grapheme_end"],
        "char_start": cell["char_start"],
        "char_end": cell["char_end"],
    }


def verify_readback(layout, body, readback_pages):
    """Compare measured masks with a finalized layout.

    ``layout`` is the stored (status ``ok``) planner output and ``body``
    the NFC-normalized source text of the revision.  Returns a report with
    omission / extra / dot-difference lists, per-page and overall counts.
    Raises :class:`InputError` when page/line counts differ or the
    readback structure is invalid.
    """
    if layout.get("status") != "ok":
        raise InputError("only an 'ok' finalization can be verified")
    exp_pages = expected_pages(layout.get("pages", []))
    if len(readback_pages) != len(exp_pages):
        raise InputError(
            f"readback has {len(readback_pages)} pages but the finalized "
            f"revision has {len(exp_pages)}")

    omissions, extras, dot_diffs = [], [], []
    page_reports = []
    totals = {"matched": 0, "substituted": 0, "omitted": 0, "extra": 0,
              "dot_differences": 0, "missing_dots": 0, "extra_dots": 0,
              "edit_cost": 0, "expected_cells": 0, "measured_cells": 0}

    for p, (exp_lines, meas_lines) in enumerate(
            zip(exp_pages, readback_pages), start=1):
        if len(meas_lines) != len(exp_lines):
            raise InputError(
                f"page {p}: readback has {len(meas_lines)} lines but the "
                f"finalized page has {len(exp_lines)}")

        pg = {"page": p, "matched": 0, "substituted": 0, "omitted": 0,
              "extra": 0, "dot_differences": 0, "missing_dots": 0,
              "extra_dots": 0, "edit_cost": 0,
              "expected_cells": 0, "measured_cells": 0}
        for l, (exp_cells, meas_masks) in enumerate(
                zip(exp_lines, meas_lines), start=1):
            ops, cost = align_line(exp_cells, meas_masks)
            pg["edit_cost"] += cost
            pg["expected_cells"] += len(exp_cells)
            pg["measured_cells"] += len(meas_masks)
            for opname, ei, mi in ops:
                if opname == MATCH:
                    pg["matched"] += 1
                    continue
                if opname == OMISSION:
                    cell = exp_cells[ei]
                    rec = {
                        "kind": "omission",
                        "expected": _expected_position(p, l, cell),
                        "expected_mask": cell["mask"],
                        "expected_dots": dots_of(cell["mask"]),
                        "grapheme_start": cell["grapheme_start"],
                        "grapheme_end": cell["grapheme_end"],
                        "char_start": cell["char_start"],
                        "char_end": cell["char_end"],
                        "text": body[cell["char_start"]:cell["char_end"]],
                    }
                    omissions.append(rec)
                    pg["omitted"] += 1
                elif opname == EXTRA:
                    mask = meas_masks[mi]
                    rec = {
                        "kind": "extra",
                        "measured": _measured_position(p, l, mi),
                        "measured_mask": mask,
                        "measured_dots": dots_of(mask),
                    }
                    extras.append(rec)
                    pg["extra"] += 1
                else:  # SUBSTITUTE
                    cell = exp_cells[ei]
                    mask = meas_masks[mi]
                    missing = [d for d in dots_of(cell["mask"])
                               if not ((mask >> (d - 1)) & 1)]
                    added = [d for d in dots_of(mask)
                             if not ((cell["mask"] >> (d - 1)) & 1)]
                    rec = {
                        "kind": "dot_difference",
                        "expected": _expected_position(p, l, cell),
                        "measured": _measured_position(p, l, mi),
                        "expected_mask": cell["mask"],
                        "measured_mask": mask,
                        "expected_dots": dots_of(cell["mask"]),
                        "measured_dots": dots_of(mask),
                        "missing_dots": missing,
                        "extra_dots": added,
                        "grapheme_start": cell["grapheme_start"],
                        "grapheme_end": cell["grapheme_end"],
                        "char_start": cell["char_start"],
                        "char_end": cell["char_end"],
                        "text": body[cell["char_start"]:cell["char_end"]],
                    }
                    dot_diffs.append(rec)
                    pg["substituted"] += 1
                    pg["dot_differences"] += 1
                    pg["missing_dots"] += len(missing)
                    pg["extra_dots"] += len(added)
        page_reports.append(pg)
        for k in pg:
            if k != "page":
                totals[k] += pg[k]

    total_discrepancies = (totals["omitted"] + totals["extra"]
                           + totals["dot_differences"])
    return {
        "status": "verified",
        "perfect": total_discrepancies == 0,
        "page_count": len(exp_pages),
        "totals": totals,
        "pages": page_reports,
        "omissions": omissions,
        "extras": extras,
        "dot_differences": dot_diffs,
        "discrepancy_count": total_discrepancies,
    }
