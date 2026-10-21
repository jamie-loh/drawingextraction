#!/usr/bin/env python3
"""
Detect titled areas on a construction drawing PDF (detail views, schedule
tables, title block) and export their bounding boxes.

Usage:
    python detect_areas.py drawing.pdf
    python detect_areas.py drawing.pdf --page 0 --json areas.json --annotated areas_annotated.pdf
    python detect_areas.py drawing.pdf --tol 14 --pad 8      # tuning knobs

Requires: pip install pymupdf

How it works
  1. Every individual line segment / rectangle and every text line on the sheet
     becomes an "item" (compound paths are split, because one PDF path can
     span several views and its bbox is useless).
  2. Items that touch or lie within --tol points of each other are joined into
     connected components. Each view / table is one component.
  3. Titles are found from text:
       - detail views: the same-size text lines directly above an "N T S" note
       - schedules:    all-caps lines ending in "SCHEDULE"
     Each title is matched to its component. If the title sits slightly apart
     from its drawing (gap > tol), it is joined to the drawing just above it.
  4. Title block: bottom-right region starting at "Qualified Person Endorsement".

All boxes are in PDF points (origin top-left, y grows downward). Defaults are
tuned for an A1 sheet (2384 x 1684 pt); scale --tol / --pad for other sizes.
"""
import argparse
import json
import re

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz


# ---------------------------------------------------------------- text helpers
def get_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            text = "".join(s["text"] for s in ln["spans"]).strip()
            if not text:
                continue
            out.append({
                "text": text,
                "rect": fitz.Rect(ln["bbox"]),
                "size": round(max(s["size"] for s in ln["spans"]), 1),
            })
    return out


def cx(r):
    return (r.x0 + r.x1) / 2


# ------------------------------------------------------------- title finders
def find_detail_titles(lines, max_gap=14, x_tol=160, size_tol=0.7):
    """Title = same-size text lines stacked directly above an 'N T S' note.
    x_tol is generous because titles are often left-aligned over the NTS."""
    nts_re = re.compile(r"^N\s*T\s*S$", re.I)
    results = []
    for nts in (l for l in lines if nts_re.match(l["text"])):
        title, ref, size = [], nts, None
        while True:
            cands = []
            for l in lines:
                if l is ref or l is nts or l in title:
                    continue
                gap = ref["rect"].y0 - l["rect"].y1
                if -2 <= gap <= max_gap and abs(cx(l["rect"]) - cx(nts["rect"])) <= x_tol:
                    if size is None or abs(l["size"] - size) <= size_tol:
                        cands.append((gap, l))
            if not cands:
                break
            _, best = min(cands, key=lambda c: c[0])
            title.insert(0, best)
            size = best["size"]
            ref = best
        if title:
            rect = fitz.Rect(title[0]["rect"])
            for l in title + [nts]:
                rect |= l["rect"]
            results.append({
                "title": re.sub(r"\s+", " ", " ".join(l["text"] for l in title)),
                "title_rect": rect,
                "anchor": title[0],
                "scale": re.sub(r"\s+", "", nts["text"]).upper(),
            })
    return results


def find_schedule_titles(lines):
    out = []
    for l in lines:
        t = l["text"]
        if t.isupper() and re.search(r"SCHEDULE$", t) and len(t.split()) >= 3:
            out.append({"title": t, "title_rect": fitz.Rect(l["rect"]), "anchor": l})
    return out


# ------------------------------------------------------------ item collection
def collect_items(page, lines):
    """Return (items, frame_y). items = [(rect, line_or_None)]."""
    W, H = page.rect.width, page.rect.height
    items, long_h = [], []
    for d in page.get_drawings():
        for it in d["items"]:
            kind = it[0]
            if kind == "l":
                r = fitz.Rect(it[1], it[2])
            elif kind == "re":
                r = fitz.Rect(it[1])
            elif kind == "qu":
                r = it[1].rect
            else:  # curve
                r = fitz.Rect(it[1], it[-1])
            r.normalize()
            if r.width > 0.5 * W or r.height > 0.5 * H:
                # sheet frame / full-width rules: not part of any view
                if r.width > 0.5 * W and r.height < 2 and r.y0 > 0.7 * H:
                    long_h.append(r.y0)
                continue
            items.append((fitz.Rect(r.x0 - .5, r.y0 - .5, r.x1 + .5, r.y1 + .5), None))
    for l in lines:
        items.append((fitz.Rect(l["rect"]), l))
    # title block sits under the lowest long horizontal rule near the bottom
    frame_y = min(long_h) if long_h else H
    return items, frame_y


def connected_components(items, tol):
    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = max(40.0, tol * 4)
    grid = {}
    for i, (r, _) in enumerate(items):
        q = fitz.Rect(r.x0 - tol, r.y0 - tol, r.x1 + tol, r.y1 + tol)
        for gx in range(int(q.x0 // cell), int(q.x1 // cell) + 1):
            for gy in range(int(q.y0 // cell), int(q.y1 // cell) + 1):
                for j in grid.get((gx, gy), ()):
                    r2 = items[j][0]
                    if q.x0 <= r2.x1 and q.x1 >= r2.x0 and q.y0 <= r2.y1 and q.y1 >= r2.y0:
                        a, b = find(i), find(j)
                        if a != b:
                            parent[a] = b
                grid.setdefault((gx, gy), []).append(i)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    comps, owner = [], {}
    for ids in groups.values():
        u = fitz.Rect(items[ids[0]][0])
        for i in ids:
            u |= items[i][0]
            if items[i][1] is not None:
                owner[id(items[i][1])] = len(comps)
        comps.append({"rect": u, "n": len(ids), "used": False})
    return comps, owner


# --------------------------------------------------------------- title block
def find_title_block(lines):
    anchor = next((l for l in lines if l["text"].lower().startswith("qualified person")), None)
    if not anchor:
        return None
    y0 = anchor["rect"].y0 - 2
    rect = fitz.Rect(anchor["rect"].x0 - 2, y0, anchor["rect"].x1, anchor["rect"].y1)
    for l in lines:
        if l["rect"].y0 >= y0 and l["rect"].x0 >= rect.x0 - 2:
            rect |= l["rect"]
    return rect


# ------------------------------------------------------------------ main logic
def detect(page, tol=10.0, pad=6.0, min_items=20, max_title_gap=60.0):
    lines = get_lines(page)
    items, frame_y = collect_items(page, lines)
    body = [it for it in items if it[0].y1 < frame_y - 2]
    comps, owner = connected_components(body, tol)

    details = find_detail_titles(lines)
    schedules = find_schedule_titles(lines)
    warnings = []

    areas = []

    def comp_for_title(t):
        """Component for a title: the one holding it, else the drawing just above."""
        ci = owner.get(id(t["anchor"]))
        if ci is not None and comps[ci]["n"] >= min_items:
            return ci
        tr = t["title_rect"]
        best = None
        for k, c in enumerate(comps):
            if c["n"] < min_items:
                continue
            gap = tr.y0 - c["rect"].y1
            ov = min(tr.x1, c["rect"].x1) - max(tr.x0, c["rect"].x0)
            if -5 <= gap <= max_title_gap and ov >= 0.5 * tr.width:
                if best is None or gap < best[0]:
                    best = (gap, k)
        return best[1] if best else None

    for t in details:
        ci = comp_for_title(t)
        rect = fitz.Rect(t["title_rect"])
        if ci is None:
            warnings.append(f'no drawing found for detail title "{t["title"]}"')
        else:
            rect |= comps[ci]["rect"]
            comps[ci]["used"] = True
        areas.append({"type": "detail_view", "title": t["title"], "scale": t["scale"], "rect": rect})

    for t in schedules:
        ci = owner.get(id(t["anchor"]))
        rect = fitz.Rect(t["title_rect"])
        if ci is None or comps[ci]["n"] < min_items:
            warnings.append(f'no table found for schedule title "{t["title"]}"')
        else:
            rect |= comps[ci]["rect"]
            comps[ci]["used"] = True
        areas.append({"type": "schedule_table", "title": t["title"], "rect": rect})

    tblock = find_title_block(lines)
    if tblock:
        areas.append({"type": "title_block", "title": "TITLE BLOCK", "rect": tblock})

    for c in comps:
        if not c["used"] and c["n"] >= min_items:
            warnings.append("untitled drawing component: " + str([round(v) for v in c["rect"]]))

    for a in areas:
        a["rect"] = fitz.Rect(a["rect"]) + (-pad, -pad, pad, pad)
        a["rect"] &= page.rect

    areas.sort(key=lambda a: (round(a["rect"].y0 / 100), a["rect"].x0))
    return areas, warnings


COLORS = {"detail_view": (1, 0, 0), "schedule_table": (0, 0.4, 1), "title_block": (0, 0.6, 0.2)}


def annotate(doc, page, areas, out_path):
    for i, a in enumerate(areas, 1):
        col = COLORS[a["type"]]
        page.draw_rect(a["rect"], color=col, width=3)
        page.insert_text((a["rect"].x0 + 4, max(a["rect"].y0 - 6, 14)), f'{i}. {a["title"]}',
                         fontsize=16, color=col)
    doc.save(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, default=0, help="0-based page index")
    ap.add_argument("--json", default="areas.json")
    ap.add_argument("--annotated", default="areas_annotated.pdf")
    ap.add_argument("--tol", type=float, default=10.0,
                    help="items closer than this (pt) join one view; raise if views split, lower if they merge")
    ap.add_argument("--pad", type=float, default=6.0, help="padding added around each box (pt)")
    args = ap.parse_args()

    doc = fitz.open(args.pdf)
    page = doc[args.page]
    areas, warnings = detect(page, tol=args.tol, pad=args.pad)

    payload = {
        "source": args.pdf,
        "page": args.page,
        "page_size_pt": [page.rect.width, page.rect.height],
        "areas": [
            {
                "id": i,
                "type": a["type"],
                "title": a["title"],
                **({"scale": a["scale"]} if "scale" in a else {}),
                "bbox": [round(v, 1) for v in a["rect"]],  # x0, y0, x1, y1
            }
            for i, a in enumerate(areas, 1)
        ],
        "warnings": warnings,
    }
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    annotate(doc, page, areas, args.annotated)

    for a in payload["areas"]:
        print(f'{a["id"]:>2}. [{a["type"]}] {a["title"]}  {a["bbox"]}')
    for w in warnings:
        print("WARNING:", w)
    print(f"\nWrote {args.json} and {args.annotated}")


if __name__ == "__main__":
    main()