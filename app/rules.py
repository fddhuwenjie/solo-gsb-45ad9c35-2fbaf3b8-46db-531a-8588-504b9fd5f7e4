"""Braille rule table compilation, contraction matching and ambiguity checks.

A rule maps a literal Unicode *pattern* to one or more 6-dot cells.  Cells
are encoded as strings of dot numbers, e.g. ``"124"`` (dots 1-2-4).  The
blank cell (space) is encoded as ``""`` only internally; rule patterns may
not contain whitespace.

Determinism contract
--------------------
* rules are ordered by ``(-priority, -pattern_length, id)``;
* at a match position the longest rule with the highest priority wins;
* when two *distinct* expansions are equally best (same priority, same
  length, different cells) the rule table is ambiguous and no revision may
  be finalized -- the first offending body position is reported.
"""
from .errors import InputError
from .graphemes import graphemes

# Dot mask helpers ---------------------------------------------------------
DOTS = "123456"
BLANK = 0
ALL_DOTS = 0b111111  # 63, used for unmappable characters
NUMBER_SIGN = 0b111100  # dots 3-4-5-6
CAPITAL_SIGN = 0b100000  # dot 6


def parse_cell(raw: str) -> int:
    if not isinstance(raw, str) or any(c not in DOTS for c in raw):
        raise InputError(f"invalid braille cell {raw!r}: use digits 1-6")
    if len(set(raw)) != len(raw):
        raise InputError(f"invalid braille cell {raw!r}: repeated dot")
    mask = 0
    for c in raw:
        mask |= 1 << (int(c) - 1)
    return mask


def cell_unicode(mask: int) -> str:
    return chr(0x2800 | mask)


# Grade-1 English braille alphabet -----------------------------------------
_LETTER_DOTS = {
    "a": "1", "b": "12", "c": "14", "d": "145", "e": "15",
    "f": "124", "g": "1245", "h": "125", "i": "24", "j": "245",
    "k": "13", "l": "123", "m": "134", "n": "1345", "o": "135",
    "p": "1234", "q": "12345", "r": "1235", "s": "234", "t": "2345",
    "u": "136", "v": "1236", "w": "2456", "x": "1346", "y": "13456",
    "z": "1356",
}
_PUNCT_DOTS = {
    ",": "2", ";": "23", ":": "25", ".": "256", "!": "235",
    "?": "236", "'": "3", "-": "36", "(": "236", ")": "356",
    "/": "34",
}
# digit n (n != 0) uses the letters a..i, 0 uses j.
_DIGIT_DOTS = {str(d): _LETTER_DOTS[chr(ord("a") + d - 1)] for d in range(1, 10)}
_DIGIT_DOTS["0"] = _LETTER_DOTS["j"]


class RuleTable:
    def __init__(self, rules, *, casefold=True, unknown="warn"):
        if unknown not in ("warn", "reject", "keep"):
            raise InputError("unknown_char must be one of: warn, reject, keep")
        self.unknown = unknown
        self.casefold = casefold
        self.rules = rules
        # index: first cluster (casefolded) -> list of rules sorted
        self.by_first = {}
        seen = {}
        entries_all = []
        for r in rules:
            pat = r["pattern"]
            if not isinstance(pat, str) or not pat:
                raise InputError("every rule needs a non-empty pattern")
            if any(ch.isspace() for ch in pat):
                raise InputError(
                    f"rule {r.get('id')!r}: contraction patterns may not "
                    "contain whitespace"
                )
            pat_clusters = [g for g, _, _ in graphemes(pat)]
            cells = tuple(parse_cell(c) for c in r["cells"])
            if not cells:
                raise InputError(f"rule {r.get('id')!r}: cells must be non-empty")
            key = pat.casefold() if casefold else pat
            priority = int(r.get("priority", 100))
            if key in seen:
                prev = seen[key]
                # Distinct expansions of the same pattern are statically
                # ambiguous only when priorities do not order them.
                if prev["cells"] != cells and prev["priority"] == priority:
                    self.ambiguity = {
                        "kind": "rule_ambiguity",
                        "reason": "duplicate_pattern",
                        "pattern": pat,
                        "rule_ids": sorted([prev["id"], r["id"]]),
                        "segment": None,
                    }
                    self.by_first = {}
                    return
                # otherwise the higher-priority definition shadows it
                if priority > prev["priority"]:
                    seen[key] = {"id": r["id"], "cells": cells,
                                 "priority": priority}
            else:
                seen[key] = {"id": r["id"], "cells": cells,
                             "priority": priority}
            entries_all.append({
                "id": r["id"],
                "pattern": pat,
                "key": [
                    (c.casefold() if casefold else c) for c in pat_clusters
                ],
                "cells": cells,
                "priority": priority,
                "_match_key": key,
            })
        # Only the surviving definition of each (folded) pattern may match;
        # lower-priority shadowed duplicates are not word-DP options.
        winners = {k: v["id"] for k, v in seen.items()}
        self.by_first = {}
        for entry in entries_all:
            if winners[entry["_match_key"]] != entry["id"]:
                continue
            del entry["_match_key"]
            first = entry["key"][0]
            self.by_first.setdefault(first, []).append(entry)
        for entries in self.by_first.values():
            entries.sort(key=lambda e: (-e["priority"], -len(e["key"]), e["id"]))
        self.ambiguity = None
        self._char_cache = {}

    # -- per-character grade-1 fallback ------------------------------------
    def char_cells(self, cluster: str):
        """Return (cells, kind) for a single grapheme cluster.

        kinds: 'letter', 'digit', 'punct', 'unknown', 'space'.  Capital
        letters and digit runs get their prefix signs from the word
        builder, not here.
        """
        cached = self._char_cache.get(cluster)
        if cached is not None:
            return cached
        ch = cluster
        low = ch.casefold() if self.casefold else ch
        if low in _LETTER_DOTS and len(cluster) == 1:
            cells = [parse_cell(_LETTER_DOTS[low])]
            if ch.isupper():
                cells = [CAPITAL_SIGN] + cells
            kind = "letter"
        elif cluster in _DIGIT_DOTS:
            cells, kind = [parse_cell(_DIGIT_DOTS[cluster])], "digit"
        elif cluster in _PUNCT_DOTS:
            cells, kind = [parse_cell(_PUNCT_DOTS[cluster])], "punct"
        elif cluster.isspace():
            cells, kind = [BLANK], "space"
        else:
            if self.unknown == "reject":
                raise InputError(f"no braille rule for character {cluster!r}")
            cells, kind = [ALL_DOTS], "unknown"
        result = (cells, kind)
        self._char_cache[cluster] = result
        return result

    def matches_at(self, clusters, pos: int):
        """All rules matching at ``pos`` (longest/highest-priority first)."""
        if pos >= len(clusters):
            return []
        first = clusters[pos]
        first = first.casefold() if self.casefold else first
        out = []
        for e in self.by_first.get(first, ()):  # literal patterns only
            plen = len(e["key"])
            if pos + plen > len(clusters):
                continue
            ok = True
            for k in range(plen):
                c = clusters[pos + k]
                c = c.casefold() if self.casefold else c
                if c != e["key"][k]:
                    ok = False
                    break
            if ok:
                out.append(e)
        return out



def compile_rules(payload, body: str):
    """Build the RuleTable from a request; inject default grade-1 alphabet.

    A request may pass ``"rules": {"include_defaults": false, "rules": [...],
    "casefold": true, "unknown_char": "warn"}``.  Custom rules *extend* the
    built-in letter/digit/punctuation handling unless defaults are disabled.
    """
    payload = payload or {}
    custom = payload.get("rules", [])
    if not isinstance(custom, list):
        raise InputError("rules.rules must be a list")
    norm = []
    for i, r in enumerate(custom):
        if not isinstance(r, dict):
            raise InputError("each rule must be an object")
        rid = r.get("id") or f"rule-{i+1}"
        if not isinstance(rid, str):
            raise InputError("rule id must be a string")
        cells = r.get("cells")
        if not isinstance(cells, list) or not cells:
            raise InputError(f"rule {rid!r}: cells must be a non-empty list")
        priority = r.get("priority", 100)
        if not isinstance(priority, int):
            raise InputError(f"rule {rid!r}: priority must be an integer")
        norm.append({
            "id": rid,
            "pattern": r["pattern"],
            "cells": cells,
            "priority": priority,
        })
    ids = [r["id"] for r in norm]
    if len(set(ids)) != len(ids):
        raise InputError("rule ids must be unique")
    table = RuleTable(
        norm,
        casefold=bool(payload.get("casefold", True)),
        unknown=payload.get("unknown_char", "warn"),
    )
    if table.ambiguity is not None:
        return table
    # Body scan: detect dynamic ties (equal priority + equal pattern length
    # but different expansion) at any match position in actual body words.
    clusters = [g for g, _, _ in graphemes(body)]
    word_runs = []
    j = 0
    while j < len(clusters):
        if clusters[j].isspace():
            j += 1
            continue
        k = j
        while k < len(clusters) and not clusters[k].isspace():
            k += 1
        word_runs.append((j, k))
        j = k
    for lo, hi in word_runs:
        p = lo
        while p < hi:
            ms = table.matches_at(clusters, p)
            if len(ms) >= 2:
                best = ms[0]
                for m in ms[1:]:
                    if (
                        m["priority"] == best["priority"]
                        and len(m["key"]) == len(best["key"])
                        and m["cells"] != best["cells"]
                    ):
                        # offsets are codepoint offsets (graphemes share them)
                        offs = _offsets(body)
                        table.ambiguity = {
                            "kind": "rule_ambiguity",
                            "reason": "equal_best_match",
                            "pattern": best["pattern"],
                            "rule_ids": sorted([best["id"], m["id"]]),
                            "segment": {
                                "grapheme_start": p,
                                "grapheme_end": p + len(best["key"]),
                                "char_start": offs[p],
                                "char_end": offs[min(p + len(best["key"]),
                                                     len(offs) - 1)],
                            },
                        }
                        return table
            p += 1
    return table


def _offsets(text: str):
    return [lo for _, lo, _ in graphemes(text)] + [len(text)]
