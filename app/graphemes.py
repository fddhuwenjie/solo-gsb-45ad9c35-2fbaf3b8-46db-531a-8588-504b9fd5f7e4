"""Grapheme-cluster splitting using only the Python standard library.

Python's ``regex`` module (which supports ``\\X``) is third party, so the
standard library cannot give a perfect UAX #29 grapheme boundary algorithm.
This implementation implements the common core:

* CR LF is one cluster;
* each other LF / CR / control character is its own cluster;
* a base character absorbs following combining marks (Unicode canonical
  category ``Mn`` / ``Me``) and variation selectors;
* regional indicator symbols pair up;
* ZWJ emoji sequences are held together (Extend / ZWJ chaining);
* hangul syllables compose by the UAX #29 L/V/T LV/LVT rules.

Sufficient for body text in Latin/CJK/simple-emoji documents; exotic
indic conjuncts may split more eagerly than a full UAX #29 engine.
Cluster byte offsets are counted in Unicode codepoints against the
NFC-normalized source, which is exactly the offset space used by the API.
"""
import unicodedata

ZWJ = "‌"
CR = "\r"
LF = "\n"


def _is_control(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat in ("Cc", "Cf") and ch not in (ZWJ,) or ch in (CR, LF)


def _is_extend(ch: str) -> bool:
    return unicodedata.category(ch) in ("Mn", "Me") or _is_vs(ch)


def _is_vs(ch: str) -> bool:
    # Variation selectors U+FE00..FE0F, U+E0100..E01EF
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF


def _is_ri(ch: str) -> bool:
    cp = ord(ch)
    return 0x1F1E6 <= cp <= 0x1F1FF


def _hangul_kind(ch: str) -> int:
    cp = ord(ch)
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return 1  # L
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return 2  # V
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return 3  # T
    if 0xAC00 <= cp <= 0xD7A3:
        return 5 if (cp - 0xAC00) % 28 else 4  # LVT or LV
    return 0


def graphemes(text: str):
    """Return a list of (cluster, start, end) codepoint-offset triples."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        start = i
        # CR LF
        if ch == CR and i + 1 < n and text[i + 1] == LF:
            i += 2
        elif ch in (CR, LF) or unicodedata.category(ch) in ("Cc",):
            i += 1
        else:
            i += 1
            kind = _hangul_kind(ch)
            ri_count = 1 if _is_ri(ch) else 0
            while i < n:
                nxt = text[i]
                if nxt == ZWJ and i + 1 < n:
                    # absorb ZWJ + following base/emoji
                    i += 2
                    while i < n and (_is_extend(text[i]) or text[i] == ZWJ):
                        if text[i] == ZWJ and i + 1 < n:
                            i += 2
                        else:
                            i += 1
                    continue
                if _is_extend(nxt):
                    i += 1
                    continue
                if ri_count and _is_ri(nxt) and ri_count == 1:
                    i += 1
                    ri_count += 1
                    continue
                nkind = _hangul_kind(nxt)
                if (
                    (kind == 1 and nkind in (1, 2, 4))
                    or (kind == 2 and nkind in (2, 3))
                    or (kind == 4 and nkind == 3)
                    or (kind == 3 and nkind == 3)
                ):
                    i += 1
                    kind = nkind if nkind in (2, 3, 5) else kind
                    continue
                break
        out.append((text[start:i], start, i))
    return out
