import io, re
import xml.etree.ElementTree as ET
from collections import defaultdict
W = "http://wordpress.org/export/1.2/"
def qn(t): return "{%s}%s" % (W, t)
def txt(el): return el.text if el is not None and el.text is not None else ""
ch = ET.parse('/home/stephanie/Projects/cookbook-app/wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml').getroot().find("channel")
posts = [it for it in ch.findall("item") if txt(it.find(qn("post_type"))) == "post"]
REAL8 = {"appetizers","baking","breakfast","desserts","drinks","fermentation","main-dishes","side-dishes","bread","desserts-baking","crockpot","dressings","sauces"}
def pcats(it):
    return [(c.get("nicename"),(c.text or "").strip()) for c in it.findall("category") if c.get("domain") in (None,"category")]
def in8(it):
    return any((s or "").lower() in REAL8 for s,_ in pcats(it))
rec = [it for it in posts if in8(it)]
non = [it for it in posts if not in8(it)]

def normt(t): return re.sub(r"[^a-z0-9]+", "", (t or "").lower())
d = defaultdict(list)
for it in rec:
    d[normt(txt(it.find("title")))].append((txt(it.find(qn("post_id"))), txt(it.find("title")), txt(it.find(qn("post_name")))))
dup = {k:v for k,v in d.items() if len(v)>1}
print("== near-dup recipe titles (normalized) : %d groups ==" % len(dup))
for k,v in sorted(dup.items(), key=lambda x:-int(x[1][0][0])):
    for pid,t,sl in v:
        print("    ", pid, repr(t), "slug=", sl)

print("\n== recipe posts with numeric/trashed/empty slug ==")
for it in rec:
    sl = txt(it.find(qn("post_name")))
    if sl in ("0","") or sl.isdigit() or "__trashed" in sl:
        print("    ", txt(it.find(qn("post_id"))), "slug=", repr(sl), "title=", repr(txt(it.find("title"))))

print("\n== recipe set size ==", len(rec), "  non-recipe ==", len(non))
print("non-recipe list:")
for it in non:
    print("    ", txt(it.find(qn("post_id"))), repr(txt(it.find("title"))), "cats=", [s for s,_ in pcats(it)], "status=", txt(it.find(qn("status"))))
