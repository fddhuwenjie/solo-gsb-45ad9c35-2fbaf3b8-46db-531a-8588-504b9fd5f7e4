"""Finalization exports: per-page JSON, printable SVG, replay request."""
from .storage import canonical_json, sha256_text

# Geometry in millimeters for an A4-ish embosser grid.  Dot pitch follows
# common embosser spacing: 2.5mm horizontal per cell, 5mm per line.
DOT_R = 0.7
CELL_W = 2.54
LINE_H = 10.0
MARGIN = 12.0


def pages_json_export(rev, layout, request_obj):
    """The deterministic per-page JSON artifact."""
    pages = layout.get("pages", [])
    return {
        "format": "braille-pages/v1",
        "document_id": rev["doc_id"],
        "revision_no": rev["revision_no"],
        "request_sha256": rev["request_sha256"],
        "layout_sha256": rev["layout_sha256"],
        "page_count": layout.get("page_count", len(pages)),
        "binding": layout.get("binding", {}),
        "cross_references": layout.get("cross_references", []),
        "pages": pages,
    }


def replay_export(rev, request_obj):
    """A self-contained request that reproduces this revision exactly."""
    return {
        "format": "braille-replay/v1",
        "document_id": rev["doc_id"],
        "revision_no": rev["revision_no"],
        "request_sha256": rev["request_sha256"],
        "request": request_obj,
    }


# Dot centers inside a cell.  Braille dots numbered:
#   1 4
#   2 5
#   3 6
_DOT_POS = {
    1: (0, 0), 2: (0, 1), 3: (0, 2),
    4: (1, 0), 5: (1, 1), 6: (1, 2),
}
DOT_DY = 2.5
DOT_DX = 2.5


def _dots_for(mask):
    return [d for d in range(1, 7) if (mask >> (d - 1)) & 1]


def svg_export(rev, layout, request_obj):
    """Render all pages as printable SVG pages (one <svg> per page)."""
    pages = layout.get("pages", [])
    paper = request_obj.get("paper", {})
    cpl = paper.get("cells_per_line", 40)
    lpp = paper.get("lines_per_page", 25)
    gutter = layout.get("binding", {}).get("gutter_cells", 0)

    width_mm = MARGIN * 2 + (cpl + gutter) * CELL_W
    height_mm = MARGIN * 2 + (lpp + 1) * LINE_H
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
         f'viewBox="0 0 {width_mm:.2f} {height_mm * max(len(pages), 1) + 20:.2f}">'),
        '<style>.dot{fill:#000}.frame{fill:none;stroke:#999;'
        'stroke-width:0.15}.pnum{font:3px sans-serif;fill:#555;'
        'text-anchor:middle}.gutter{stroke:#ccc;stroke-width:0.2;'
        'stroke-dasharray:1,1}</style>',
    ]
    y_off = 0.0
    for i, pg in enumerate(pages):
        parts.append(
            f'<g transform="translate(0,{y_off:.2f})">'
        )
        parts.append(
            f'<rect class="frame" x="0.5" y="0.5" '
            f'width="{width_mm - 1:.2f}" height="{height_mm - 1:.2f}" '
            f'rx="1.5"/>'
        )
        # gutter line at the binding edge
        if gutter:
            gx = MARGIN + gutter * CELL_W
            parts.append(
                f'<line class="gutter" x1="{gx:.2f}" y1="{MARGIN:.2f}" '
                f'x2="{gx:.2f}" y2="{height_mm - MARGIN:.2f}"/>'
            )
        for ln in pg.get("lines", []):
            line_no = ln["line"]
            ly = MARGIN + (line_no - 1) * LINE_H
            for cell in ln.get("cells", []):
                if cell["mask"] == 0:
                    continue  # blank / space cells raise no dots
                # col already includes the planner's gutter offset
                col = cell["col"]
                cx = MARGIN + (col - 1) * CELL_W
                cy = ly
                for d in _dots_for(cell["mask"]):
                    dxn, dyn = _DOT_POS[d]
                    parts.append(
                        f'<circle class="dot" cx="{cx + dxn * DOT_DX:.2f}" '
                        f'cy="{cy + dyn * DOT_DY:.2f}" r="{DOT_R:.2f}"/>'
                    )
        parts.append(
            f'<text class="pnum" x="{width_mm / 2:.2f}" '
            f'y="{height_mm - 4:.2f}">{pg["page"]}</text>'
        )
        parts.append("</g>")
        y_off += height_mm + 4
    parts.append("</svg>")
    return "\n".join(parts)


def export_all(rev, layout, request_obj):
    doc = pages_json_export(rev, layout, request_obj)
    replay = replay_export(rev, request_obj)
    json_text = canonical_json(doc) + "\n"
    replay_text = canonical_json(replay) + "\n"
    svg_text = svg_export(rev, layout, request_obj)
    return {
        "json": json_text,
        "svg": svg_text,
        "replay": replay_text,
        "artifact_sha256": {
            "pages_json": sha256_text(json_text),
            "pages_svg": sha256_text(svg_text),
            "replay_json": sha256_text(replay_text),
        },
    }
