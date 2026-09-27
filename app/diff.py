"""Impact comparison between two finalized revisions of one document.

Given two already-finalized revision numbers of the *same* document, this
module reads each revision's body and its immutable per-page data only --
nothing is re-planned and no finalization is modified.

The comparison proceeds in two stages:

1. **Minimum-edit alignment on Unicode grapheme clusters.**  The two
   (NFC-normalized) bodies are split with the same UAX#29-core splitter
   the planner uses, then aligned with unit costs for substitution,
   deletion (present only in the old revision) and insertion (present only
   in the new one).  Only the grapheme **text** is compared -- the
   codepoint offsets carried by the clusters never take part in the
   equality test, so an insertion/deletion that shifts every later offset
   (old ``aa`` vs new ``acaba`` is the boundary case) cannot turn an
   identical grapheme into a phantom substitution.  The resulting
   operation stream carries the real per-side cluster indices and
   consumes every grapheme at most once on each side.  Among equal-cost
   alignments the lexicographically first operation stream is chosen,
   ordered ``match > substitute > delete > insert``; the same
   suffix-DP-plus-greedy-walk construction as the readback verifier is
   used, so repeated text yields a stable alignment.  The suffix DP is
   exact but not always materialized in full: after common-affix
   trimming, a middle too large for the whole table is evaluated inside
   a diagonal band whose width follows the edit distance, so long
   near-identical revisions align in a few table cells per grapheme
   while the emitted stream stays identical to the full-table walk.

2. **Per-paired-grapheme classification.**  Every matched grapheme is
   examined on both sides:

   * masks (and page/line/column positions) equal  -> ``unchanged``;
   * the braille mask sequences differ              -> ``retranslated``
                                                       (盲文重译);
   * the masks are equal but positions move         -> ``pagination_shift``
                                                       (仅分页位移).

   Substitution / deletion / insertion operations are respectively
   ``body_substituted`` / ``body_deleted`` / ``body_inserted``.

Adjacent changes are merged into diff segments carrying the old/new
codepoint ranges and page ranges.  The report also lists, for each side,
the affected pages split into pages with real content changes
(``content_changed`` -- embossed dots actually differ) and pages touched
only by chained reflow (``cascade_reflow`` -- identical dots, shifted
placement).  A pure deletion owns no grapheme on the new side, but the
gap it leaves still changes the new version: the segment and the
new-side page list therefore also record the **landing page** (落点页) --
the page of the first new-side grapheme after the deletion point (the
content that pulled up into the gap), or of the last retained grapheme
when the deletion runs to the end of the body.
"""
from array import array

from .errors import InputError
from .graphemes import graphemes

# Alignment operation codes, ordered for the equal-cost tie break:
# match > substitute > delete (old only) > insert (new only).
MATCH = "match"
SUBSTITUTE = "substitute"
DELETE = "delete"
INSERT = "insert"

# Classification of every aligned position.
BODY_SUBSTITUTED = "body_substituted"   # 正文改
BODY_DELETED = "body_deleted"           # 正文删
BODY_INSERTED = "body_inserted"         # 正文增
RETRANSLATED = "retranslated"           # 盲文重译
PAGINATION_SHIFT = "pagination_shift"   # 仅分页位移
UNCHANGED = "unchanged"                 # 完全不变

# Marks that change embossed dots (vs. marks that only move them).
_CONTENT_KINDS = frozenset({
    BODY_SUBSTITUTED, BODY_DELETED, BODY_INSERTED, RETRANSLATED})
_CHANGE_KINDS = frozenset({
    BODY_SUBSTITUTED, BODY_DELETED, BODY_INSERTED,
    RETRANSLATED, PAGINATION_SHIFT})

_OP_COST = {MATCH: 0, SUBSTITUTE: 1, DELETE: 1, INSERT: 1}

# Memory guard for the exact alignment tables, counted in 4-byte table
# entries.  A middle whose full suffix table fits (n * m cells) is
# aligned in one exact pass, as before.  Larger middles switch to an
# exact diagonal-band suffix DP whose band only needs to cover every
# minimum-cost alignment: a half-width of k (the edit distance) always
# suffices, so near-identical revisions align in a few cells per
# grapheme no matter how long the bodies are, while genuinely divergent
# long texts still outgrow the guard (their band widens with the edit
# distance) and keep being rejected.
MAX_TABLE_CELLS = 10_000_000

# "Outside the band" sentinel: larger than any real alignment cost (a
# cost never exceeds max(n, m)) and small enough that _INF + 1 still
# fits a signed 32-bit table entry.
_INF = 1 << 30


# --------------------------------------------------------------------------
# Grapheme alignment (minimum edit distance, deterministic tie break)
# --------------------------------------------------------------------------
def align_graphemes(old_clusters, new_clusters):
    """Align two grapheme-cluster lists with minimum edit cost.

    Returns a list of ``(op, old_index_or_None, new_index_or_None)``
    triples.  Equal-cost alignments are resolved with the
    ``match > substitute > delete > insert`` lexicographic preference.

    Only the grapheme **text** decides equality; the codepoint offsets in
    the clusters are ignored here.  The returned indices are the real
    per-side cluster indices (each side keeps its own), and every
    grapheme is consumed at most once per side.

    The common prefix is matched for free first: equal heads are always
    matched by the greedy walk, so trimming them cannot change the
    resulting stream.  The common suffix is trimmed only when none of its
    grapheme texts occurs in either remaining middle -- otherwise forcing
    the suffix matches could re-pair repeated text the full walk would
    match earlier (old ``aa`` vs new ``acaba``), and the trim is
    abandoned instead.  Either way the emitted stream is exactly the one
    the untrimmed suffix-DP-plus-greedy-walk would produce; trimming only
    keeps the middle small for near-identical revisions, and an oversized
    middle is itself aligned exactly inside a diagonal band (see
    ``_align_core``).
    """
    old_texts = [cluster[0] for cluster in old_clusters]
    new_texts = [cluster[0] for cluster in new_clusters]
    n, m = len(old_texts), len(new_texts)

    pre = 0
    while pre < n and pre < m and old_texts[pre] == new_texts[pre]:
        pre += 1

    suf = 0
    limit = min(n - pre, m - pre)
    while (suf < limit
           and old_texts[n - 1 - suf] == new_texts[m - 1 - suf]):
        suf += 1
    if suf:
        # The forced suffix matches are exactly what the full walk would
        # do only when no suffix text reappears in either middle; with
        # such a repetition the walk may match a middle grapheme instead
        # and leave a suffix grapheme to be inserted/deleted.
        suffix_texts = set(old_texts[n - suf:])  # == new_texts[m - suf:]
        if (not suffix_texts.isdisjoint(old_texts[pre:n - suf])
                or not suffix_texts.isdisjoint(new_texts[pre:m - suf])):
            suf = 0

    lo_n, lo_m = pre, pre
    hi_n, hi_m = n - suf, m - suf

    ops = [(MATCH, i, i) for i in range(pre)]
    ops.extend(_align_core(old_texts, new_texts, lo_n, hi_n, lo_m, hi_m))
    # Suffix matches pair each side's own tail indices; the two indices
    # differ whenever the bodies have different lengths.
    ops.extend((MATCH, hi_n + k, hi_m + k) for k in range(suf))
    return ops


def _align_core(a, b, lo_n, hi_n, lo_m, hi_m):
    """Exact minimum-edit alignment of ``a[lo_n:hi_n]`` / ``b[lo_m:hi_m]``.

    ``a`` and ``b`` are grapheme-*text* lists; returned indices are
    offsets into them (i.e. the real per-side cluster indices).

    The construction is a suffix-cost DP plus a greedy walk that takes,
    at every position, the first operation in the ``match > substitute >
    delete > insert`` order whose remainder realizes the optimum.  When
    the full ``n * m`` table fits the cell guard it is computed
    directly; otherwise the same DP is evaluated inside a diagonal band
    ``|(n - i) - (m - j)| <= w`` whose half-width is doubled from a
    cheap lower bound until the banded ``rest(0, 0)`` is provably exact.
    Every minimum-cost alignment stays inside a band of half-width k
    (the edit distance), and every banded cell whose true cost is at
    most w holds the exact value, so the walk -- and therefore the
    emitted stream -- is identical to the untrimmed full-table walk.
    Only the memory shape changes: near-identical long revisions align
    in O((n + m) * k) cells, while two genuinely divergent long texts
    still exceed the guard and raise :class:`InputError`.
    """
    n, m = hi_n - lo_n, hi_m - lo_m
    if n * m <= MAX_TABLE_CELLS:
        # Historical fast path: the full suffix table fits the guard, so
        # compute it in one exact pass (a band wide enough to be the
        # whole table).
        rows, j_lo = _banded_suffix_table(a, b, lo_n, lo_m, n, m,
                                          max(n, m))
    else:
        w = _distance_lower_bound(a, b, lo_n, hi_n, lo_m, hi_m)
        while True:
            if _band_cells(n, m, w) > MAX_TABLE_CELLS:
                raise InputError(
                    "revisions differ too much to align "
                    f"(edit distance over {n} x {m} graphemes needs a "
                    f"band beyond the {MAX_TABLE_CELLS}-cell limit)")
            rows, j_lo = _banded_suffix_table(a, b, lo_n, lo_m, n, m, w)
            if _cell(rows, j_lo, 0, 0) <= w:
                break  # the true distance is at most w: value is exact
            w *= 2
    return _walk(a, b, lo_n, lo_m, n, m, rows, j_lo)


def _distance_lower_bound(a, b, lo_n, hi_n, lo_m, hi_m):
    """Cheap exact lower bound (>= 1) for the edit distance.

    The length gap is one bound; the per-text grapheme counts give
    another: every edit operation changes the count imbalance of any
    grapheme text by at most one, so the distance is at least the
    largest imbalance.  Starting the band doubling at this bound never
    skips the true distance and rejects divergent inputs before any DP
    work.
    """
    counts = {}
    for i in range(lo_n, hi_n):
        text = a[i]
        counts[text] = counts.get(text, 0) + 1
    for j in range(lo_m, hi_m):
        text = b[j]
        counts[text] = counts.get(text, 0) - 1
    bound = abs((hi_n - lo_n) - (hi_m - lo_m))
    for delta in counts.values():
        if delta < 0:
            delta = -delta
        if delta > bound:
            bound = delta
    return max(1, bound)


def _band_cells(n, m, w):
    """Number of stored table entries for a half-width-``w`` band."""
    total = 0
    base = m - n
    for i in range(n + 1):
        lo = base + i - w
        if lo < 0:
            lo = 0
        hi = base + i + w
        if hi > m:
            hi = m
        total += hi - lo + 1
    return total


def _banded_suffix_table(a, b, lo_n, lo_m, n, m, w):
    """Banded suffix-cost table for ``a[lo_n:lo_n+n]``/``b[lo_m:lo_m+m]``.

    Returns ``(rows, j_lo)`` where ``rows[i]`` holds ``rest(i, j)`` --
    the minimum cost of aligning the two suffixes starting at ``i`` and
    ``j`` -- for the columns ``j_lo[i] <= j < j_lo[i] + len(rows[i])``,
    which is the band ``|(n - i) - (m - j)| <= w`` clipped to
    ``[0, m]``.  Cells outside the band read as :data:`_INF`.  Boundary
    cells (last row, last column) inside the band are exact, and every
    cell whose true cost is at most ``w`` gets the exact value (the
    recurrence can only reach neighbours of no larger cost), which is
    all the greedy walk ever reads.
    """
    base = m - n
    j_lo = []
    rows = []
    for i in range(n + 1):
        lo = base + i - w
        if lo < 0:
            lo = 0
        hi = base + i + w
        if hi > m:
            hi = m
        j_lo.append(lo)
        rows.append(array("i", [0]) * (hi - lo + 1))
    # Boundary: last row, and the last column where the band reaches it.
    last = rows[n]
    lo = j_lo[n]
    for j in range(lo, m + 1):
        last[j - lo] = m - j
    for i in range(n):
        if base + i + w >= m:
            rows[i][m - j_lo[i]] = n - i
    # Fill bottom-up, right-to-left with the unit-cost recurrence.
    for i in range(n - 1, -1, -1):
        row, nxt = rows[i], rows[i + 1]
        lo, nlo = j_lo[i], j_lo[i + 1]
        hi = lo + len(row) - 1
        nlen = len(nxt)
        ai = a[lo_n + i]
        jmax = hi if hi < m else m - 1  # column m is a boundary cell
        for j in range(jmax, lo - 1, -1):
            pair = 0 if ai == b[lo_m + j] else 1
            t = j + 1 - nlo
            diag = nxt[t] + pair if 0 <= t < nlen else _INF
            t = j - nlo
            dele = nxt[t] + 1 if 0 <= t < nlen else _INF
            t = j + 1 - lo
            ins = row[t] + 1 if t < len(row) else _INF
            row[j - lo] = min(diag, dele, ins)
    return rows, j_lo


def _cell(rows, j_lo, i, j):
    """Banded table lookup; cells outside the band read as :data:`_INF`."""
    t = j - j_lo[i]
    if 0 <= t < len(rows[i]):
        return rows[i][t]
    return _INF


def _walk(a, b, lo_n, lo_m, n, m, rows, j_lo):
    """Greedy walk over the suffix-cost table, tie-broken per position.

    Takes the first operation in the ``match > substitute > delete >
    insert`` order whose remainder realizes the optimal total, exactly
    as the full-table walk does; banded-out cells read as :data:`_INF`
    and are never chosen.
    """
    ops = []
    i = j = 0
    while i < n or j < m:
        candidates = []
        if i < n and j < m:
            op = MATCH if a[lo_n + i] == b[lo_m + j] else SUBSTITUTE
            candidates.append((op, i + 1, j + 1, i, j))
        if i < n:
            candidates.append((DELETE, i + 1, j, i, None))
        if j < m:
            candidates.append((INSERT, i, j + 1, None, j))
        total = _cell(rows, j_lo, i, j)
        chosen = None
        for opname, ni, nj, oi, nj2 in candidates:
            if _OP_COST[opname] + _cell(rows, j_lo, ni, nj) == total:
                chosen = (opname, oi, nj2)
                break
        assert chosen is not None
        opname, oi, ni = chosen
        ops.append((opname,
                    None if oi is None else lo_n + oi,
                    None if ni is None else lo_m + ni))
        if opname in (MATCH, SUBSTITUTE, DELETE):
            i += 1
        if opname in (MATCH, SUBSTITUTE, INSERT):
            j += 1
    return ops


# --------------------------------------------------------------------------
# Per-grapheme braille data from finalized pages
# --------------------------------------------------------------------------
def grapheme_slots(layout):
    """Map every grapheme index of a finalized layout to its emitted cells.

    Returns a list indexed by grapheme index; each entry is a list of
    ``(page, line, col, mask)`` tuples in reading order (pages are
    emitted in order and cells within a line are column-sorted).  Space
    atoms dropped at a line boundary simply contribute no cells.
    """
    slots = []

    def ensure(idx):
        while len(slots) <= idx:
            slots.append([])

    for pg in layout.get("pages", []):
        page_no = pg["page"]
        for ln in pg.get("lines", []):
            line_no = ln["line"]
            for cell in ln.get("cells", []):
                gs, ge = cell["grapheme_start"], cell["grapheme_end"]
                ensure(ge - 1)
                for g in range(gs, ge):
                    slots[g].append(
                        (page_no, line_no, cell["col"], cell["mask"]))
    return slots


def _masks(slots, g):
    return [t[3] for t in slots[g]] if g < len(slots) else []


def _positions(slots, g):
    return [(t[0], t[1], t[2]) for t in slots[g]] if g < len(slots) else []


def _pages(slots, g):
    return {t[0] for t in slots[g]} if g < len(slots) else set()


# --------------------------------------------------------------------------
# Mark construction and segment merging
# --------------------------------------------------------------------------
def _classify(op, oi, ni, old_slots, new_slots):
    if op == SUBSTITUTE:
        return BODY_SUBSTITUTED
    if op == DELETE:
        return BODY_DELETED
    if op == INSERT:
        return BODY_INSERTED
    # matched grapheme: compare braille, then placement
    if _masks(old_slots, oi) != _masks(new_slots, ni):
        return RETRANSLATED
    if _positions(old_slots, oi) != _positions(new_slots, ni):
        return PAGINATION_SHIFT
    return UNCHANGED


def _mark(kind, oi, ni, old_clusters, new_clusters,
          old_slots, new_slots):
    """One changed aligned position, with old/new ranges and page spans."""
    rec = {"kind": kind}
    if oi is not None:
        _, cs, ce = old_clusters[oi]
        rec["old"] = {
            "grapheme_index": oi, "char_start": cs, "char_end": ce,
            "text": old_clusters[oi][0],
            "pages": sorted(_pages(old_slots, oi)),
        }
    if ni is not None:
        _, cs, ce = new_clusters[ni]
        rec["new"] = {
            "grapheme_index": ni, "char_start": cs, "char_end": ce,
            "text": new_clusters[ni][0],
            "pages": sorted(_pages(new_slots, ni)),
        }
    return rec


def _page_range(pages):
    return {"start": min(pages), "end": max(pages)} if pages else None


def compare_finalizations(old_rev, new_rev):
    """Compare two stored, finalized revisions (read-only, no re-planning).

    Each argument is a revision dict as returned by ``Store.get_revision``
    with ``status == "ok"``.  Returns the impact report; raises
    :class:`InputError` for non-ok layouts or revisions too divergent to
    align within the table-size guard.
    """
    for label, rev in (("old", old_rev), ("new", new_rev)):
        if rev.get("status") != "ok" or rev.get("layout", {}).get(
                "status") != "ok":
            raise InputError(
                f"the {label} revision is not in a finalized 'ok' state")

    old_body, new_body = old_rev["request"]["body"], new_rev["request"]["body"]
    old_clusters = graphemes(old_body)
    new_clusters = graphemes(new_body)
    old_slots = grapheme_slots(old_rev["layout"])
    new_slots = grapheme_slots(new_rev["layout"])

    alignment = align_graphemes(old_clusters, new_clusters)
    edit_cost = sum(_OP_COST[op] for op, _, _ in alignment)

    # New-side neighbours of every aligned position.  A deletion owns no
    # new-side grapheme, but the gap it leaves still lands on a new-side
    # page: the page of the first new grapheme after the deletion point
    # (the content that pulled up into the gap), or of the last retained
    # grapheme when the deletion runs to the end of the body.
    prev_new = [None] * len(alignment)
    next_new = [None] * len(alignment)
    last = None
    for k, (_, _, ni) in enumerate(alignment):
        prev_new[k] = last
        if ni is not None:
            last = ni
    last = None
    for k in range(len(alignment) - 1, -1, -1):
        next_new[k] = last
        if alignment[k][2] is not None:
            last = alignment[k][2]

    def landing_pages(k):
        ni = next_new[k]
        if ni is None:
            ni = prev_new[k]
        return _pages(new_slots, ni) if ni is not None else set()

    counts = {BODY_SUBSTITUTED: 0, BODY_DELETED: 0, BODY_INSERTED: 0,
              RETRANSLATED: 0, PAGINATION_SHIFT: 0, UNCHANGED: 0}
    marks = []
    segments = []

    # Page impact sets; content changes win over chained reflow.
    old_content, new_content = set(), set()
    old_cascade, new_cascade = set(), set()

    cur = None  # open segment accumulator

    def note_pages(kind, oi, ni, landing=frozenset()):
        opages = _pages(old_slots, oi) if oi is not None else set()
        npages = (_pages(new_slots, ni) if ni is not None else set())
        npages = npages | landing
        if kind in _CONTENT_KINDS:
            old_content.update(opages)
            new_content.update(npages)
        else:  # pagination shift
            # Identical dots on different (or partly different) pages:
            # every touched page is only re-embossed due to chained flow.
            old_cascade.update(opages - npages)
            new_cascade.update(npages - opages)
            common = opages & npages
            old_cascade.update(common)
            new_cascade.update(common)

    def flush(seg):
        if seg is None:
            return
        old_pages, new_pages = seg.pop("_pages_old"), seg.pop("_pages_new")
        seg["old_page_range"] = _page_range(old_pages)
        seg["new_page_range"] = _page_range(new_pages)
        segments.append(seg)

    for k, (op, oi, ni) in enumerate(alignment):
        kind = _classify(op, oi, ni, old_slots, new_slots)
        counts[kind] += 1
        if kind == UNCHANGED:
            flush(cur)
            cur = None
            continue
        landing = landing_pages(k) if op == DELETE else set()
        note_pages(kind, oi, ni, landing)
        marks.append(_mark(kind, oi, ni, old_clusters, new_clusters,
                           old_slots, new_slots))
        if cur is None:
            cur = {"kinds": [], "mark_count": 0,
                   "old_char_start": None, "old_char_end": None,
                   "new_char_start": None, "new_char_end": None,
                   "_pages_old": set(), "_pages_new": set()}
        cur["kinds"].append(kind)
        cur["mark_count"] += 1
        if oi is not None:
            if cur["old_char_start"] is None:
                cur["old_char_start"] = old_clusters[oi][1]
            cur["old_char_end"] = old_clusters[oi][2]
            cur["_pages_old"].update(_pages(old_slots, oi))
        if ni is not None:
            if cur["new_char_start"] is None:
                cur["new_char_start"] = new_clusters[ni][1]
            cur["new_char_end"] = new_clusters[ni][2]
            cur["_pages_new"].update(_pages(new_slots, ni))
        # A pure deletion still lands on a new-side page (see above).
        cur["_pages_new"].update(landing)
    flush(cur)

    # Content pages are a superset of the cascade signal; keep lists
    # disjoint with content changes taking precedence.
    old_cascade -= old_content
    new_cascade -= new_content

    change_count = sum(counts[k] for k in _CHANGE_KINDS)
    old_page_count = old_rev["layout"].get("page_count", 0)
    new_page_count = new_rev["layout"].get("page_count", 0)
    return {
        "status": "compared",
        "identical": change_count == 0,
        "old_revision_no": old_rev["revision_no"],
        "new_revision_no": new_rev["revision_no"],
        "old_request_sha256": old_rev["request_sha256"],
        "new_request_sha256": new_rev["request_sha256"],
        "old_layout_sha256": old_rev["layout_sha256"],
        "new_layout_sha256": new_rev["layout_sha256"],
        "old_page_count": old_page_count,
        "new_page_count": new_page_count,
        "old_grapheme_count": len(old_clusters),
        "new_grapheme_count": len(new_clusters),
        "edit_cost": edit_cost,
        "counts": counts,
        "change_count": change_count,
        "segments": segments,
        "changes": marks,
        "old_affected_pages": {
            "content_changed": sorted(old_content),
            "cascade_reflow": sorted(old_cascade),
            "count": len(old_content) + len(old_cascade),
        },
        "new_reemboss_pages": {
            "content_changed": sorted(new_content),
            "cascade_reflow": sorted(new_cascade),
            "count": len(new_content) + len(new_cascade),
        },
    }
