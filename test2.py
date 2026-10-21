import argparse
import pymupdf
from collections import Counter
from datetime import datetime
from pathlib import Path

# PDF = "WT312T31-RFD2786A.pdf"
# PAGE = 0          # first page
# MIN_SIZE = 5      # ignore vector shapes smaller than this (points), change if it gets too noisy

ap = argparse.ArgumentParser(description="Annotate a PDF page and write a report.")
ap.add_argument("pdf", help="path to the PDF")
ap.add_argument("--page", type=int, default=1, help="page number, 1-based (default: 1)")
ap.add_argument("--min-size", type=float, default=5, help="ignore vector shapes smaller than this, in points (default: 5)")
args = ap.parse_args()
 
PDF = args.pdf
PAGE = args.page - 1
MIN_SIZE = 5
# MIN_SIZE = args.min_size

# each run gets its own folder, e.g. output/WT312T31-RFD2786A_p1_20261019-143005/
OUT_DIR = Path("output") / f"{Path(PDF).stem}_p{PAGE + 1}_{datetime.now():%Y%m%d-%H%M%S}"
OUT_DIR.mkdir(parents=True, exist_ok=True)

#red for text, blue for vector shapes, green for images
RED, BLUE, GREEN = (1, 0, 0), (0, 0, 1), (0, 0.6, 0)

doc = pymupdf.open(PDF)
page = doc[PAGE]

text_blocks = [b for b in page.get_text("blocks") if b[6] == 0]
drawings = page.get_drawings()
images = page.get_image_info()

# ---- print info ----
print(f"Page {PAGE + 1}: {page.rect.width:.0f} x {page.rect.height:.0f} pt, rotation {page.rotation}")
print(f"Text blocks: {len(text_blocks)} | Vector shapes: {len(drawings)} | Images: {len(images)}")
print("scanned" if not drawings and not text_blocks and images else "vector")

import pymupdf

def merge_regions(rects, gap=10):
    rects = [pymupdf.Rect(r.x0 - gap, r.y0 - gap, r.x1 + gap, r.y1 + gap) for r in rects]
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            for i, o in enumerate(out):
                if r.intersects(o):
                    out[i] = o | r
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return [pymupdf.Rect(r.x0 + gap, r.y0 + gap, r.x1 - gap, r.y1 - gap) for r in rects]


def inside(b, r, tol=1):
    return (b.x0 >= r.x0 - tol and b.y0 >= r.y0 - tol and
            b.x1 <= r.x1 + tol and b.y1 <= r.y1 + tol)


def split_big(regions, boxes, page_area, max_area_frac=0.25, gap=40, min_gap=10):
    out = []
    for r in regions:
        if r.get_area() > page_area * max_area_frac and gap > min_gap:
            contained = [b for b in boxes if inside(b, r)]
            out.extend(split_big(merge_regions(contained, gap - 10), contained,
                                 page_area, max_area_frac, gap - 10, min_gap))
        else:
            out.append(r)
    return out


# ---- text-only boxes ----
words = page.get_text("words")                       # (x0, y0, x1, y1, word, block, line, word_no)
boxes = [pymupdf.Rect(w[:4]) for w in words if w[4].strip()]

# Scale the gap to the text size so it works on any sheet size
heights = sorted(b.height for b in boxes)
median_h = heights[len(heights) // 2]
GAP = median_h * 4          # ~3 text-heights between words; adjust the 3 up or down
print(f"{len(boxes)} words, median height {median_h:.1f} pt, GAP {GAP:.1f} pt")

regions = split_big(merge_regions(boxes, GAP), boxes, page.rect.get_area(),
                    gap=GAP, min_gap=GAP / 4)

for i, r in enumerate(regions):
    txt = page.get_text("text", clip=r).strip().replace("\n", " ")
    page.draw_rect(r, color=(1, 0.5, 0), width=2)

page.get_pixmap(dpi=100).save("regions.png")

# ---- draw colored boxes ----
shape = page.new_shape()

# for d in drawings:
#     r = d["rect"]
#     if r.width >= MIN_SIZE or r.height >= MIN_SIZE:
#         shape.draw_rect(r)
# shape.finish(color=BLUE, width=0.3)



for i, b in enumerate(text_blocks):
    r = pymupdf.Rect(b[:4])
    shape.draw_rect(r)
    shape.finish(color=RED, width=0.6)
    shape.insert_text(r.tl + (0, -1), f"T{i}", fontsize=6, color=RED)

for i, im in enumerate(images):
    r = pymupdf.Rect(im["bbox"])
    shape.draw_rect(r)
    shape.finish(color=GREEN, width=1.2)
    shape.insert_text(r.tl + (0, 8), f"IMG{i} {im['width']}x{im['height']}px", fontsize=8, color=GREEN)

shape.commit()

page.get_pixmap(dpi=150).save(OUT_DIR / "annotated.png")
print(f"\nSaved {OUT_DIR / 'annotated.png'}")

# ---- full report to file ----

 
with open(OUT_DIR / "report.txt", "w", encoding="utf-8") as f:
    print(f"File: {PDF} | Page {PAGE + 1}", file=f)
    print(f"Size: {page.rect.width:.0f} x {page.rect.height:.0f} pt, rotation {page.rotation}", file=f)
    print(f"Metadata: {doc.metadata}", file=f)
    print(f"Layers: {doc.layer_ui_configs()}", file=f)
    print(f"Links: {page.get_links()}", file=f)
    print(f"Annotations: {[a.type for a in page.annots()]}", file=f)
 
    print("\n=== TEXT BLOCKS ===", file=f)
    for i, b in enumerate(text_blocks):
        print(f"T{i}: bbox=({b[0]:.0f},{b[1]:.0f},{b[2]:.0f},{b[3]:.0f}) text={b[4].strip()!r}", file=f)
 
    # print("\n=== VECTOR SUMMARY (by width, color, layer) ===", file=f)
    # summary = Counter((d.get("width"), d.get("color"), d.get("layer")) for d in drawings)
    # for (w, c, layer), n in summary.most_common():
    #     print(f"width={w} color={c} layer={layer}: {n} shapes", file=f)
 
    # print("\n=== VECTOR SHAPES ===", file=f)
    # for i, d in enumerate(drawings):
    #     r = d["rect"]
    #     ops = [item[0] for item in d["items"]]  # l=line, re=rectangle, c=curve, qu=quad
    #     #type: "s"=stroke only, "f"=fill only, "fs"=both filled and outlined
    #     print(f"V{i}: bbox=({r.x0:.0f},{r.y0:.0f},{r.x1:.0f},{r.y1:.0f}) "
    #           f"type={d.get('type')} width={d.get('width')} color={d.get('color')} "
    #           f"fill={d.get('fill')} layer={d.get('layer')} ops={ops}", file=f)
 
    print("\n=== IMAGES ===", file=f)
    for i, im in enumerate(images):
        print(f"IMG{i}: bbox={tuple(round(v) for v in im['bbox'])} "
              f"size={im['width']}x{im['height']}px colorspace={im.get('colorspace')} bpc={im.get('bpc')}", file=f)
 
print(f"Saved {OUT_DIR / 'report.txt'}")