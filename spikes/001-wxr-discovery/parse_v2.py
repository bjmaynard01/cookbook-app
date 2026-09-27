#!/usr/bin/env python3
"""Line-granularity splitter (v2).

Unit: a section = maximal run of non-blank lines (blank line / <p> / <br> split).
Classifier:
  ingredient section:  ≥3 lines AND ≥60% lines quantity-like AND no line starts with
                       an imperative verb AND no line >100 chars
  step section:        ≥1 line starts with an imperative verb, OR ≥50% lines >40 chars
  heading:             matches ^Ingredients/Directions/Instructions (case-insens) + optional colon
Description = leading section(s) BEFORE the first ingredient/step pair; trailing
section with no quantity lines and no imperative lines = tail notes (kept with description).

Always: raw_text preserved verbatim; the split is advisory (D4).
"""
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

WXR = "/home/stephanie/Projects/cookbook-app/wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml"
WP = "http://wordpress.org/export/1.2/"
CT = "http://purl.org/rss/1.0/modules/content/"
def q(t): return "{%s}%s" % (WP, t)
def text(el):
    return el.text if el is not None and el.text is not None else ""

class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.br = 0
        self.block = 0
    def handle_data(self, data):
        self.parts.append(data)
    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        elif tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"):
            self.parts.append("\n\n" if self.block else "\n")
            self.block = 1
    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"):
            self.parts.append("\n")
            self.block = 0
    def render(self):
        s = "".join(self.parts)
        s = s.replace("&amp;", "&").replace("&#8217;", "'").replace("\u00b7", "·")
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

def to_text(htmlsrc):
    p = TextHTML()
    p.feed(htmlsrc or "")
    return p.render()

IMPERATIVE = re.compile(
    r"^\s*(preheat|mix|stir|pour|add|heat|cook|bake|boil|blend|spread|pour"
    r"|place|put|put|set|whisk|fold|drain|chop|dice|slice|shred|grate"
    r"|season|combine|beat|whip|knead|roll|flatten|brush|top|garnish|serve"
    r"|remove|let|cool|cooling|refrigerat|stir|taste|adjust|heat through"
    r"|transfer|pour in|spread the|bake it|cook it|heat it|place \d|add \d|add the"
    r"|bring to|simmer|stir in|sift|scoop|drizzle|pour over) ", re.I)

QTY = re.compile(
    r"^\s*(\d+(\.\d+)?\s?\d*/\d*|\d+\s?\d/\d|1/\d+|\d+\s*\+\s*\d+|\d+\s*–\s*\d+|\d+\s*-\s*\d+|\d+\.?\d*)"
    r"|^\s*(one|two|three|four|five|six|seven|eight|nine|ten|a|an|some|no)\s"
    r"|^\s*[½¼¾²³]\s"
    r"|^\s*\d+\s?\d/\d+|\s*\d+\s*(c|oz|oz\.|tbsp|tsp|lb|lbs|g/kg|ml|l|cup|cups|cup\b|pinch)",
    re.I)

def is_qty(line):
    return bool(QTY.match(line))

def starts_imperative(line):
    return bool(IMPERATIVE.match(line))

def classify(lines):
    if len(lines) == 1:
        return "single"
    n = len(lines)
    qty = sum(1 for l in lines if is_qty(l))
    imp = sum(1 for l in lines if starts_imperative(l))
    long = sum(1 for l in lines if len(l) > 60)
    if imp and imp / n > 0.3:
        return "steps"
    if qty >= 3 and qty / n > 0.4 and long < 1 and imp < 2:
        return "ingredients"
    if long > n / 2:
        return "steps"
    return "prose"

def parse(body_html):
    t = to_text(body_html)
    sections, cur = [], []
    for line in t.split("\n"):
        if not line.strip():
            if cur:
                sections.append(cur)
                cur = []
        else:
            cur.append(line.strip())
    if cur:
        sections.append(cur)
    out = []
    for i, s in enumerate(sections):
        c = classify(s)
        first = s[0].lower()
        if re.match(r"^(ingredient|direction|instruction|method|notes?|substitution)\b\W*:?\s*$", first, re.I):
            c = "heading:" + first.split(":")[0].lower()
        out.append((c, s))
    return out

SAMPLES = ["23", "26", "1250", "604", "1787", "195", "955", "213", "324"]

def main():
    ch = ET.parse(WXR).getroot().find("channel")
    items = {text(it.find(q("post_id"))): it for it in ch.findall("item")}
    for pid in SAMPLES:
        it = items.get(pid)
        if it is None:
            continue
        title = text(it.find("title"))
        body = text(it.find("{%s}encoded" % CT))
        secs = parse(body)
        print("=" * 72)
        print(f"POST {pid} — {title}   ({len(secs)} sections)")
        for c, s in secs:
            preview = " / ".join(x[:48] for x in s[:3])
            print(f"   [{c:14}] ({len(s):2d} lines)  {preview[:150]}")

if __name__ == "__main__":
    main()
