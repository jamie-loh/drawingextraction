#!/usr/bin/env python3
"""
Detect titled areas on a construction drawing PDF (detail views, tables, title
block) and export the text inside each one as structured entities (JSON).

Nothing in here matches drawing-specific words. Everything is found from how the
sheet is drawn:

  titles       text that has a thin filled bar (an underline) directly under it,
               same width as the text. Stacked underlined lines = one title.
  subtitle     the smaller line directly under a title (usually the scale note).
  tables       a closed grid of long horizontal + vertical ruling lines.
               Column names = the header text above each column (merged header
               cells are combined), rows = the bands between horizontal rules.
  detail view  a group of drawing items that touch / lie within --tol points of
               each other, matched to the title above or inside it.
  title block  everything below the page-wide rule near the bottom. It is split
               into the cells formed by its ruling lines. In each cell the label
               is the line ending in ':' (otherwise the top line), the value is
               the rest of the cell.

Usage:
    python detect_areas.py drawing.pdf
    python detect_areas.py drawing.pdf --json out.json --annotated out.pdf
    python detect_areas.py drawing.pdf --bbox        # also write boxes into the JSON
    python detect_areas.py drawing.pdf --tol 14      # tuning

Requires: pip install pymupdf

Assumptions (the script warns when they do not hold): titles are underlined with
a filled bar, tables are drawn as ruled grids, and the sheet has a page-wide
rule above the title block. Distances are PDF points (1 pt = 1/72 inch).
"""
import argparse
import json
import os
import re
import sys

import pymupdf


# ------------------------------------------------------------------- helpers
def cx(r):
    return (r.x0 + r.x1) / 2


def cy(r):
    return (r.y0 + r.y1) / 2


def norm(s):
    return re.sub(r"\s+", " ", s).strip()

#create safe id 
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

#create safe key name
def snake(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

#avoid duplicate keys
#diff table with remarks-remarks, remarks_2
def unique_key(key, taken):
    k, n = key, 2
    while k in taken:
        k, n = f"{key}_{n}", n + 1
    taken.add(k)
    return k

#read from top to bottom and left to right
def reading_order(lines):
    return sorted(lines, key=lambda l: (round(l["rect"].y0), l["rect"].x0))


def merge_paragraphs(lines, max_gap=0.5, align_tol=3.0):
    """Join wrapped text into one string per paragraph.

    Two horizontal lines belong to the same paragraph when the PDF stores them in the
    same text block (one text object), they have the same font size, are stacked tightly (gap <= max_gap x line height) and share an alignment:
    same left edge, same right edge or same centre (within align_tol pt).
    Rotated lines are never merged. Returns a list of strings in reading order."""

    # how it decides?
    # same text block, same font size, close underneath, lined up 

    # Go through the lines in reading order (top to bottom).
    # If a line was already used in an earlier paragraph, skip it.
    # Otherwise, start a new paragraph with this line.
    # If the line is rotated (sideways text), don’t merge anything. It stays alone.
    # If it’s normal horizontal text, keep looking for the next matching line below and add it, again and again, until none is found.
    # Join all the lines’ text with spaces and clean it up.


    out, used = [], set()
    for l in reading_order(lines):
        if id(l) in used:
            continue
        para = [l]
        used.add(id(l))
        if l["horizontal"]:
            while True:
                last = para[-1]["rect"]
                nxt = None
                for m in lines:
                    if (id(m) in used or not m["horizontal"] or m["block"] != l["block"]
                            or abs(m["size"] - l["size"]) > 0.5):
                        continue
                    r = m["rect"]
                    gap = r.y0 - last.y1
                    aligned = (abs(r.x0 - last.x0) <= align_tol or abs(r.x1 - last.x1) <= align_tol
                               or abs(cx(r) - cx(last)) <= align_tol)
                    if -2 <= gap <= max_gap * (last.y1 - last.y0) and aligned:
                        if nxt is None or gap < nxt[0]:
                            nxt = (gap, m)
                if nxt is None:
                    break
                para.append(nxt[1])
                used.add(id(nxt[1]))
        out.append(norm(" ".join(p["text"] for p in para)))
    return out

# block (a group of text) -> line(one row of text inside a block) -> span(a piece of a line that has the same font and size) 
def get_lines(page):
    out = []
    for bi, b in enumerate(page.get_text("dict")["blocks"]):
        for ln in b.get("lines", []):
            text = "".join(s["text"] for s in ln["spans"]).strip()
            if not text:
                continue
            out.append({
                "block": bi,  # lines typed as one text object share a block
                "text": text,
                "rect": pymupdf.Rect(ln["bbox"]),
                "size": round(max(s["size"] for s in ln["spans"]), 1),
                "horizontal": abs(ln["dir"][0]) > 0.99,
            })
    return out


# ------------------------------------------------------------------ geometry
def collect_geometry(page):
    """items (for grouping), thin filled bars (underlines), horizontal and
    vertical rule segments, and the y of the page-wide rule above the title block."""
    W, H = page.rect.width, page.rect.height
    items, thin, hs, vs = [], [], [], []
    for d in page.get_drawings():
        filled = d.get("fill") is not None
        for it in d["items"]:
            kind = it[0]
            if kind == "l":
                a, b = it[1], it[2]
                r = pymupdf.Rect(a, b)
                r.normalize()
                if r.height <= 0.6:
                    hs.append((r.x0, r.x1, (a.y + b.y) / 2))
                elif r.width <= 0.6:
                    vs.append((r.y0, r.y1, (a.x + b.x) / 2))
            elif kind == "re":
                r = pymupdf.Rect(it[1])
                r.normalize()
                if r.height <= 3 and r.width >= 30 and filled:
                    thin.append(pymupdf.Rect(r))
                if r.height <= 3:
                    hs.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
                elif r.width <= 3:
                    vs.append((r.y0, r.y1, (r.x0 + r.x1) / 2))
                elif not filled:  # stroked box: its four edges are rules
                    hs += [(r.x0, r.x1, r.y0), (r.x0, r.x1, r.y1)]
                    vs += [(r.y0, r.y1, r.x0), (r.y0, r.y1, r.x1)]
            elif kind == "qu":
                r = it[1].rect
            else:  # curve
                r = pymupdf.Rect(it[1], it[-1])
                r.normalize()
            if r.width > 0.5 * W or r.height > 0.5 * H:
                continue  # sheet frame / page-wide rules are not part of any view
            items.append((pymupdf.Rect(r.x0 - .5, r.y0 - .5, r.x1 + .5, r.y1 + .5), None))
    wide = [y for (x0, x1, y) in hs if x1 - x0 >= 0.9 * W and y > 0.7 * H]
    frame_y = min(wide) if wide else None
    return items, thin, hs, vs, frame_y


def merge_collinear(segs, tol=1.5):
    """segs = [(lo, hi, pos)] -> overlapping / touching segments on the same line merged."""
    # start, end, height
    #1. put pieces at the same height next to each other and go left to right
    #2. pieces at almost the same height (difference of 0.6 or less ) are on the same line, so they go in the same team
    #3. if it overlaps, glue it on and make the line longer
    #   if theres a big gap, stop. finished piece is saved and the next piece starts a new line

    #(0, 10)   (9, 20)   (25, 30) -> (0,20), (25,30)
    #tol=1.5 => how small a gap along the line can be and still count as touching
    #0.6 => how close in height to count as the same line 
    segs = sorted(segs, key=lambda s: (s[2], s[0]))
    groups = []
    for s in segs:
        if groups and abs(s[2] - groups[-1][-1][2]) <= 0.6:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        pos = sum(s[2] for s in g) / len(g)
        g.sort(key=lambda s: s[0])
        lo, hi = g[0][0], g[0][1]
        for s in g[1:]:
            if s[0] <= hi + tol:
                hi = max(hi, s[1])
            else:
                out.append((lo, hi, pos))
                lo, hi = s[0], s[1]
        out.append((lo, hi, pos))
    return out

# group numbers(positions of lines on a page) that are close tgt and gives one number per group
# 1. Sort the numbers from smallest to largest.
# 2. Go through them one by one:
#         If the number is close to the last one in the current group (difference of 1.5 or less), add it to that group.
#         Otherwise, start a new group.
# 3. At the end, replace each group with its average.

def cluster_positions(vals, tol=1.5):
    vals = sorted(vals)
    out = []
    for v in vals:
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


# -------------------------------------------------------------------- tables
def find_tables(Hm, Vm, W, H, tol=1.5, span_h=0.9, span_v=0.7, min_h=4, min_v=3):
    """A table = connected rules forming a closed grid: >= min_h horizontal rules
    spanning >= span_h of its width and >= min_v vertical rules spanning >= span_v of
    its height, including all four outer edges. span_v is lower because a merged
    header cell makes some column rules start part-way down."""
    Hs = [h for h in Hm if h[1] - h[0] < 0.9 * W]  # (x0, x1, y)
    Vs = [v for v in Vm if v[1] - v[0] < 0.9 * H]  # (y0, y1, x)
    n = len(Hs) + len(Vs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (x0, x1, y) in enumerate(Hs):
        for j, (y0, y1, x) in enumerate(Vs):
            if x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol:
                a, b = find(i), find(len(Hs) + j)
                if a != b:
                    parent[a] = b
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    tables = []
    for ids in groups.values():
        hh = [Hs[i] for i in ids if i < len(Hs)]
        vv = [Vs[i - len(Hs)] for i in ids if i >= len(Hs)]
        if len(hh) < min_h or len(vv) < min_v:
            continue
        x0 = min(min(h[0] for h in hh), min(v[2] for v in vv))
        x1 = max(max(h[1] for h in hh), max(v[2] for v in vv))
        y0 = min(min(h[2] for h in hh), min(v[0] for v in vv))
        y1 = max(max(h[2] for h in hh), max(v[1] for v in vv))
        full_h = [h for h in hh if h[1] - h[0] >= span_h * (x1 - x0)]
        full_v = [v for v in vv if v[1] - v[0] >= span_v * (y1 - y0)]
        if len(full_h) < min_h or len(full_v) < min_v:
            continue
        ys = cluster_positions([h[2] for h in full_h], tol)
        xs = cluster_positions([v[2] for v in full_v], tol)
        if abs(ys[0] - y0) > tol or abs(ys[-1] - y1) > tol or abs(xs[0] - x0) > tol or abs(xs[-1] - x1) > tol:
            continue  # not closed on all four sides: not a table
        # header ends at the first full rule below where the last column rule starts
        # (a merged header cell makes one column rule start lower than the others)
        last_start = max(v[0] for v in full_v)
        hb = next((y for y in ys if y > max(last_start, ys[0]) + tol), None)
        if hb is None:
            continue
        tables.append({"rect": pymupdf.Rect(x0, y0, x1, y1), "xs": xs, "ys": ys, "hb": hb})
    return tables


def table_entity(tb, lines):
    xs, ys, hb = tb["xs"], tb["ys"], tb["hb"]
    inside = [l for l in lines if tb["rect"].contains(pymupdf.Point(cx(l["rect"]), cy(l["rect"])))]
    header = reading_order([l for l in inside if cy(l["rect"]) < hb])
    body = [l for l in inside if cy(l["rect"]) >= hb]

    # column names: every header line that overlaps a column band (>15% of its width)
    taken, columns = set(), []
    for c in range(len(xs) - 1):
        parts = []
        for l in header:
            r = l["rect"]
            ov = min(r.x1, xs[c + 1]) - max(r.x0, xs[c])
            if ov > 0 and ov >= 0.15 * r.width:
                parts.append(l["text"])
        name = norm(" ".join(parts))
        columns.append(unique_key(snake(name) or f"col_{c + 1}", taken))

    rows = []
    bands = [(ys[i], ys[i + 1]) for i in range(len(ys) - 1) if ys[i] >= hb - 0.5]
    for top, bottom in bands:
        row = {}
        for c, key in enumerate(columns):
            cell = [l for l in body
                    if top <= cy(l["rect"]) < bottom and xs[c] <= cx(l["rect"]) < xs[c + 1]]
            text = norm(" ".join(l["text"] for l in reading_order(cell)))
            row[key] = text or None
        if any(v is not None for v in row.values()):
            rows.append({"row": len(rows) + 1, **row})
    return {"columns": columns, "rows": rows}, inside


# -------------------------------------------------------------------- titles
def underlined_lines(lines, thin):
    out = []
    for l in lines:
        r = l["rect"]
        tol = 3 + 0.05 * r.width
        for u in thin:
            if abs(u.x0 - r.x0) <= tol and abs(u.x1 - r.x1) <= tol and r.y1 - 5 <= u.y0 <= r.y1 + 4:
                out.append(l)
                break
    return out

#ul is a list of lines that have an underline
#"chain" is a stack of lines that form one title
# 1.build chains, check last line of each chain, roughly centered add to a chain, if not start a new one
# 2.turn chains into titles
def chain_titles(ul, max_gap=14, x_tol=160, size_tol=0.7):
    """Stack underlined lines of the same size into one title."""
    chains = []
    for l in sorted(ul, key=lambda l: (l["rect"].y0, l["rect"].x0)):
        for ch in chains:
            last = ch[-1]
            gap = l["rect"].y0 - last["rect"].y1
            if (-2 <= gap <= max_gap and abs(l["size"] - last["size"]) <= size_tol
                    and abs(cx(l["rect"]) - cx(last["rect"])) <= x_tol):
                ch.append(l)
                break
        else:
            chains.append([l])
    titles = []
    for ch in chains:
        rect = pymupdf.Rect(ch[0]["rect"])
        for l in ch:
            rect |= l["rect"]
        titles.append({"text": norm(" ".join(l["text"] for l in ch)), "lines": ch,
                       "rect": rect, "size": ch[-1]["size"]})
    return titles


def find_subtitle(title, lines, max_gap=14):
    """Smaller line directly under the title that overlaps it horizontally."""
    last = title["lines"][-1]["rect"]
    best = None
    for l in lines:
        if l in title["lines"] or l["size"] >= title["size"]:
            continue
        gap = l["rect"].y0 - last.y1
        ov = min(l["rect"].x1, title["rect"].x1) - max(l["rect"].x0, title["rect"].x0)
        if -2 <= gap <= max_gap and ov > 0 and (best is None or gap < best[0]):
            best = (gap, l)
    return best[1] if best else None


# --------------------------------------------------------------- components
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
        q = pymupdf.Rect(r.x0 - tol, r.y0 - tol, r.x1 + tol, r.y1 + tol)
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
        u = pymupdf.Rect(items[ids[0]][0])
        cl = []
        for i in ids:
            u |= items[i][0]
            if items[i][1] is not None:
                owner[id(items[i][1])] = len(comps)
                cl.append(items[i][1])
        comps.append({"rect": u, "n": len(ids), "used": False, "lines": cl})
    return comps, owner


# --------------------------------------------------------------- title block
def prune_dangling(Hs, Vs, tol=1.5):
    """Keep only rules whose both ends meet a perpendicular rule. Cell dividers do;
    lines drawn inside a stamp or logo end in free space."""
    def touches_v(x, y):
        return any(abs(v[2] - x) <= tol and v[0] - tol <= y <= v[1] + tol for v in Vs)

    def touches_h(x, y):
        return any(abs(h[2] - y) <= tol and h[0] - tol <= x <= h[1] + tol for h in Hs)

    return ([h for h in Hs if touches_v(h[0], h[2]) and touches_v(h[1], h[2])],
            [v for v in Vs if touches_h(v[2], v[0]) and touches_h(v[2], v[1])])


def title_block(lines, Hm, Vm, frame_y, page_rect):
    region = [l for l in lines if cy(l["rect"]) >= frame_y - 1]
    if not region:
        return None
    Hr, Vr = prune_dangling([h for h in Hm if h[2] >= frame_y - 1],   # (x0, x1, y)
                            [v for v in Vm if v[1] > frame_y])       # (y0, y1, x)
    cells = {}
    for l in region:
        x, y = cx(l["rect"]), cy(l["rect"])
        left = max([v[2] for v in Vr if v[2] <= x and v[0] - 1 <= y <= v[1] + 1], default=page_rect.x0)
        right = min([v[2] for v in Vr if v[2] >= x and v[0] - 1 <= y <= v[1] + 1], default=page_rect.x1)
        top = max([h[2] for h in Hr if h[2] <= y and h[0] - 1 <= x <= h[1] + 1], default=frame_y)
        bottom = min([h[2] for h in Hr if h[2] >= y and h[0] - 1 <= x <= h[1] + 1], default=page_rect.y1)
        cells.setdefault((round(left), round(top), round(right), round(bottom)), []).append(l)

    fields, other, taken = {}, [], set()
    for ls in cells.values():
        # letters set one by one around a stamp: 5+ tiny same-size fragments in one cell
        sizes = [l["size"] for l in ls if len(l["text"]) <= 3]
        ring = {s for s in sizes if sizes.count(s) >= 5}
        ls = reading_order([l for l in ls if not (len(l["text"]) <= 3 and l["size"] in ring)])
        if not ls:
            continue
        label_line = next((l for l in ls if l["text"].rstrip().endswith(":")), None)
        if label_line is None and len(ls) >= 2:
            label_line = ls[0]
        if label_line is not None:
            label = norm(label_line["text"]).rstrip(":").strip()
            value = norm(" ".join(l["text"] for l in ls if l is not label_line)) or None
            fields[unique_key(snake(label) or "field", taken)] = value
            continue
        m = re.match(r"^([^:]*[A-Za-z][^:]*?)\s*:\s*(\S.*)$", ls[0]["text"])  # 'label: value' on one line
        if m:
            fields[unique_key(snake(m.group(1)) or "field", taken)] = norm(m.group(2))
        else:
            other.append(ls[0])
    other = [norm(l["text"]) for l in sorted(other, key=lambda l: (round(cy(l["rect"]) / 4), l["rect"].x0))]

    rect = pymupdf.Rect(region[0]["rect"])
    for l in region:
        rect |= l["rect"]
    for (y0, y1, x) in Vr:
        rect |= pymupdf.Rect(x, max(y0, frame_y), x, y1)
    for (x0, x1, y) in Hr:
        rect |= pymupdf.Rect(x0, y, x1, y)
    return {"rect": rect, "fields": fields, "other_text": other, "lines": region}


# ------------------------------------------------------------------ analysis
def detect(page, tol=10.0, pad=6.0, min_items=20, max_title_gap=60.0):
    W, H = page.rect.width, page.rect.height
    lines = get_lines(page)
    items, thin, hs, vs, frame_y = collect_geometry(page)
    Hm, Vm = merge_collinear(hs), merge_collinear(vs)
    warnings, areas, claimed = [], [], set()

    # 1. tables
    tables = find_tables(Hm, Vm, W, H)

    # 2. titles (underlined text outside tables' cells)
    def in_table(l, margin=0.0):
        p = pymupdf.Point(cx(l["rect"]), cy(l["rect"]))
        return any((tb["rect"] + (-margin, -margin, margin, margin)).contains(p) for tb in tables)

    titles = chain_titles(underlined_lines([l for l in lines if not in_table(l)], thin))
    if not titles:
        warnings.append("no underlined titles found: areas will be untitled")

    # 3. tables -> nearest title above
    free = list(titles)
    for tb in tables:
        best = None
        for t in free:
            gap = tb["rect"].y0 - t["rect"].y1
            ov = min(tb["rect"].x1, t["rect"].x1) - max(tb["rect"].x0, t["rect"].x0)
            if -3 <= gap <= max_title_gap and ov >= 0.5 * t["rect"].width and (best is None or gap < best[0]):
                best = (gap, t)
        tb["title"] = best[1] if best else None
        if best:
            free.remove(best[1])
        payload, inside = table_entity(tb, lines)
        rect = pymupdf.Rect(tb["rect"])
        text_lines = list(inside)
        if tb["title"]:
            rect |= tb["title"]["rect"]
            text_lines += tb["title"]["lines"]
        claimed |= {id(l) for l in text_lines}
        areas.append({"type": "table", "title": tb["title"]["text"] if tb["title"] else None,
                      "rect": rect, "payload": payload})

    # 4. detail views: groups of items matched to the remaining titles
    table_rects = [tb["rect"] + (-3, -3, 3, 3) for tb in tables]
    table_title_rects = [a["rect"] for a in areas]
    limit = frame_y if frame_y else H

    def keep(r, line):
        if r.y1 >= limit - 2:
            return False
        c = pymupdf.Point(cx(r), cy(r))
        return not any(tr.contains(c) for tr in table_rects) and not (
            line is not None and id(line) in claimed)

    items = items + [(pymupdf.Rect(l["rect"]), l) for l in lines]
    body = [(r, l) for r, l in items if keep(r, l)]
    comps, owner = connected_components(body, tol)

    # markup drawn over / beside a table (revision clouds, tick marks) belongs to that table
    for c in comps:
        if any(c["rect"].intersects(tb["rect"] + (-3 * tol, -3 * tol, 3 * tol, 3 * tol)) for tb in tables):
            c["used"] = True

    def comp_for(t):
        ci = owner.get(id(t["lines"][0]))
        if ci is not None and comps[ci]["n"] >= min_items:
            return ci
        best = None
        for k, c in enumerate(comps):
            if c["n"] < min_items:
                continue
            gap = t["rect"].y0 - c["rect"].y1
            ov = min(t["rect"].x1, c["rect"].x1) - max(t["rect"].x0, c["rect"].x0)
            if -5 <= gap <= max_title_gap and ov >= 0.5 * t["rect"].width:
                if best is None or gap < best[0]:
                    best = (gap, k)
        return best[1] if best else None

    pool = [l for _, l in body if l is not None]
    for t in free:
        ci = comp_for(t)
        rect = pymupdf.Rect(t["rect"])
        own = list(t["lines"])
        content = []
        if ci is None:
            warnings.append(f'no drawing found for title "{t["text"]}"')
        else:
            comps[ci]["used"] = True
            rect |= comps[ci]["rect"]
            content = [l for l in comps[ci]["lines"] if l not in own]
        sub = find_subtitle(t, pool)  # the line under a title often sits in its own small group
        if sub is not None:
            own.append(sub)
            content = [l for l in content if l is not sub]
            rect |= sub["rect"]
        claimed |= {id(l) for l in own + content}
        payload = {"text": merge_paragraphs(content)}
        areas.append({"type": "detail_view", "title": t["text"],
                      "subtitle": norm(sub["text"]) if sub else None,
                      "rect": rect, "payload": payload, "line_rects": [pymupdf.Rect(l["rect"]) for l in own + content]})

    for c in comps:
        if not c["used"] and c["n"] >= min_items:
            rect = pymupdf.Rect(c["rect"])
            claimed |= {id(l) for l in c["lines"]}
            warnings.append("untitled drawing area: " + str([round(v) for v in rect]))
            areas.append({"type": "detail_view", "title": None, "subtitle": None, "rect": rect,
                          "payload": {"text": merge_paragraphs(c["lines"])},"line_rects": [pymupdf.Rect(l["rect"]) for l in c["lines"]]})

    # 5. title block
    drawing = {}
    if frame_y:
        tbk = title_block(lines, Hm, Vm, frame_y, page.rect)
        if tbk:
            claimed |= {id(l) for l in tbk["lines"]}
            drawing = tbk["fields"]
            areas.append({"type": "title_block", "title": "title block", "rect": tbk["rect"],
                          "payload": {"fields": tbk["fields"], "other_text": tbk["other_text"]}})
    else:
        warnings.append("no page-wide rule near the bottom: title block not detected")

    for a in areas:
        a["rect"] = pymupdf.Rect(a["rect"]) + (-pad, -pad, pad, pad)
        a["rect"] &= page.rect
    areas.sort(key=lambda a: (round(a["rect"].y0 / 100), a["rect"].x0))
    unclaimed = [norm(l["text"]) for l in reading_order([l for l in lines if id(l) not in claimed])]
    return areas, drawing, unclaimed, warnings


# -------------------------------------------------------------------- output
def export_detail_views(doc, page, areas, entities, out_dir, dpi=300):
    """For each detail view write a cropped vector PDF (master) and a PNG next to it.
    Content outside the view is removed from the PDF (not just hidden), and so is
    text that belongs to a neighbouring view whose box overlaps this one.
    Adds entity["files"] = {"pdf": ..., "png": ...} (paths as written).
    Call it BEFORE annotate(), which draws boxes on the page."""
    os.makedirs(out_dir, exist_ok=True)
    for a, ent in zip(areas, entities):
        if a["type"] != "detail_view":
            continue
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", ent["entity_id"])
        pdf_path = os.path.join(out_dir, name + ".pdf")
        png_path = os.path.join(out_dir, name + ".png")
        clip = pymupdf.Rect(a["rect"])

        out = pymupdf.open()
        out.insert_pdf(doc, from_page=page.number, to_page=page.number)
        crop = out[0]
        P = crop.rect

        # pass 1: everything outside the view (text and line art)
        for r in (pymupdf.Rect(P.x0, P.y0, P.x1, clip.y0), pymupdf.Rect(P.x0, clip.y1, P.x1, P.y1),
                  pymupdf.Rect(P.x0, clip.y0, clip.x0, clip.y1), pymupdf.Rect(clip.x1, clip.y0, P.x1, clip.y1)):
            if not r.is_empty:
                crop.add_redact_annot(r)
        crop.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)

        # pass 2 (text only): lines cut by the edge, and other views' lines inside this box
        for blk in crop.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                lr = pymupdf.Rect(ln["bbox"])
                if lr.intersects(clip) and not clip.contains(lr):
                    crop.add_redact_annot(lr)
        for b in areas:
            if b is not a and b["type"] == "detail_view":
                for lr in b.get("line_rects", []):
                    if lr.intersects(clip):
                        crop.add_redact_annot(lr)
        crop.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)

        crop.set_cropbox(clip)                    # page = just the view
        out.save(pdf_path, garbage=4, deflate=True, clean=True)
        crop.get_pixmap(dpi=dpi).save(png_path)   # render from the crop so PNG == PDF
        out.close()
        ent["files"] = {"pdf": pdf_path, "png": png_path}

COLORS = {"detail_view": (1, 0, 0), "table": (0, 0.4, 1), "title_block": (0, 0.6, 0.2)}


def annotate(doc, page, areas, out_path):
    for i, a in enumerate(areas, 1):
        col = COLORS[a["type"]]
        page.draw_rect(a["rect"], color=col, width=3)
        page.insert_text((a["rect"].x0 + 4, max(a["rect"].y0 - 6, 14)),
                         f'{i}. {a["title"] or "untitled"}', fontsize=16, color=col)
    doc.save(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, default=0, help="0-based page index")
    ap.add_argument("--json", default="areas.json")
    ap.add_argument("--annotated", default="areas_annotated.pdf")
    ap.add_argument("--tol", type=float, default=10.0,
                    help="items closer than this (pt) join one view; raise if views split, lower if they merge")
    ap.add_argument("--pad", type=float, default=6.0, help="padding around each annotated box (pt)")
    ap.add_argument("--bbox", action="store_true", help="also write each area's bounding box into the JSON")
    ap.add_argument("--crops-dir", default="crops", help="folder for the per-detail-view PDF + PNG")
    ap.add_argument("--dpi", type=int, default=300, help="PNG resolution (200-300 is typical)")
    args = ap.parse_args()

    # replace the outputs of the previous run
    for path in (args.json, args.annotated):
        if os.path.abspath(path) == os.path.abspath(args.pdf):
            sys.exit(f"{path} is the input drawing; choose a different output name.")
        if os.path.exists(path):
            try:
                os.remove(path)
            except PermissionError:
                sys.exit(f"Cannot replace {path}: it is open in another program. Close it and run again.")

    doc = pymupdf.open(args.pdf)
    page = doc[args.page]
    areas, drawing, unclaimed, warnings = detect(page, tol=args.tol, pad=args.pad)

    stem = os.path.splitext(os.path.basename(args.pdf))[0]
    seen, entities = set(), []
    for i, a in enumerate(areas, 1):
        base = f'{stem}#{slug(a["title"]) if a["title"] else "untitled"}'
        eid, n = base, 2
        while eid in seen:  # same title twice: number it
            eid, n = f"{base}-{n}", n + 1
        seen.add(eid)
        ent = {"id": i, "entity_id": eid, "type": a["type"], "title": a["title"]}
        if a.get("subtitle"):
            ent["subtitle"] = a["subtitle"]
        if args.bbox:
            ent["bbox"] = [round(v, 1) for v in a["rect"]]  # x0, y0, x1, y1
        ent.update(a["payload"])
        entities.append(ent)

    export_detail_views(doc, page, areas, entities, args.crops_dir, args.dpi)

    payload = {
        "source": os.path.basename(args.pdf),
        "page": args.page,
        "page_size_pt": [page.rect.width, page.rect.height],
        "drawing": drawing,
        "areas": entities,
        "unclaimed_text": unclaimed,
        "warnings": warnings,
    }
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    annotate(doc, page, areas, args.annotated)

    for e in entities:
        size = (f'{len(e["rows"])} rows' if "rows" in e else
                f'{len(e["text"])} text' if "text" in e else f'{len(e["fields"])} fields')
        print(f'{e["id"]:>2}. [{e["type"]}] {e["title"]}  ({size})')
    for w in warnings:
        print("WARNING:", w)
    print(f"\nWrote {args.json} and {args.annotated}")


if __name__ == "__main__":
    main()