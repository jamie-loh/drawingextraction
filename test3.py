import pymupdf   # pip install pymupdf

doc = pymupdf.open("WT312T31-RFD2786A.pdf")

svg = doc[0].get_svg_image()

with open("page1.svg", "w", encoding="utf-8") as f:
    f.write(svg)