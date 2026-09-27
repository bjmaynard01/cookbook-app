#!/usr/bin/env python3
"""Spike 001 FINAL consolidation (correct WXR element structures).

WXR 1.2 has TWO category structures:
  channel-level: <wp:category> with wp:term_id / wp:category_nicename / wp:cat_name (taxonomy)
  post-level:    <category> (UN-namespaced) with wp:category_nicename / wp:category_name children
Same for tags: channel <wp:tag> vs post <tag>.
"""
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

WXR = "/home/stephanie/Projects/cookbook-app/wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml"
W = "http://wordpress.org/export/1.2/"
CT = "http://purl.org/rss/1.0/modules/content/"

def q(t): return "{%s}%s" % (W, t)
def txt(el):
    return el.text if el is not None and el.text is not None else ""

ch = ET.parse(WXR).getroot().find("channel")
items = list(ch.findall("item"))
posts = [it for it in items if txt(it.find(q("post_type"))) == "post"]
pages = [it for it in items if txt(it.find(q("post_type"))) == "page"]
attachs = [it for it in items if txt(it.find(q("post_type"))) == "attachment"]
print(f"posts={len(posts)} pages={len(pages)} attachments={len(attachs)}")

# ---- channel taxonomy: categories & tags with term ids ----
print("\n== CHANNEL categories (term_id -> nicename = cat_name) ==")
cat_id2slug, slug2name = {}, {}
for c in ch.findall(q("category")):
    tid = txt(c.find(q("term_id")))
    slug = txt(c.find(q("category_nicename")))
    name = txt(c.find(q("cat_name")))
    parent = txt(c.find(q("category_parent")))
    cat_id2slug[tid] = slug
    slug2name[slug] = name
    print(f"   id={tid:>4} slug={slug!r:20} name={name!r:22} parent={parent!r}")

print("\n== CHANNEL tags (term_id -> nicename = tag_name) ==")
for c in ch.findall(q("tag")):
    tid = txt(c.find(q("term_id")))
    slug = txt(c.find(q("category_nicename"))) or txt(c.find(q("tag_name")))
    name = txt(c.find(q("cat_name"))) or txt(c.find(q("tag_name")))
    print(f"   id={tid:>4} slug={slug!r:20} name={name!r}")

# ---- per-post categories: <category domain="category" nicename="slug">Name</category>
def pcats(it):
    out = []
    for c in it.findall("category"):
        if c.get("domain") not in (None, "category"):
            continue
        out.append((c.get("nicename"), (c.text or "").strip()))
    return out

def ptags(it):
    out = []
    for c in it.findall("category"):
        if c.get("domain") != "post_tag":
            continue
        out.append((c.get("nicename"), (c.text or "").strip()))
    return out

# ---- page-level [catlist] shortcode -> canonical real8 category name ----
print("\n== category pages -> [catlist] name ==")
pagecat = {}
real8_by_slug = {}
for p in pages:
    body = txt(p.find("{%s}encoded" % CT))
    m = re.search(r"\[catlist[^>]*name=['\"]([^'\"]+)['\"]", body) or \
        re.search(r"\[catlist\s+((?:'[^']*'|\"[^\"]+\"|[^\s\[\]]+))\]", body)
    nm = (m.group(1) if m else None)
    if nm:
        nm = nm.lower()
    pagecat[txt(p.find(q("post_id")))] = (txt(p.find("title")), nm)
    if nm:
        real8_by_slug[nm] = txt(p.find("title"))
# The 8 canonical category NAMES (menu). Map slug->menu category by matching.
real8_names = {"Main Dishes", "Drinks", "Desserts", "Side Dishes", "Breakfast",
               "Appetizers", "Baking", "Fermentation"}
# Subcategories (parent is a real8 slug):
real8_slugs = {
    "main-dishes", "drinks", "desserts", "side-dishes", "breakfast",
    "appetizers", "baking", "fermentation",
    # subcategories all nested under the above
    "bread", "desserts-baking", "crockpot", "dressings", "sauces",
}
def is_recipe_slug(slug):
    return (slug or "").lower().strip() in real8_slugs

for pid in sorted(pagecat, key=lambda s: int(s)):
    print(f"   page {pid:>4} {pagecat[pid][0]!r:20} catlist={pagecat[pid][1]!r}")
print("   real8 (canonical menu):", sorted(real8_names))


# ---- recipe set = posts that carry at least one category whose SLUG is in real8_slugs ----
recipes, nonrecipes = [], []
for it in posts:
    in8 = any(is_recipe_slug(sl) for sl, _n in pcats(it))
    (recipes if in8 else nonrecipes).append(it)
print(f"\n== RECIPE SET: {len(recipes)} of {len(posts)} posts carry at least one real8/slug-category ==")

cnt = Counter()
for it in recipes:
    for sl, _n in pcats(it):
        if is_recipe_slug(sl):
            cnt[sl] += 1
print("   per-category-slug counts (a post may be in several):")
for k, v in cnt.most_common():
    print(f"     {v:3d}  {k}")

print(f"\n== NON-RECIPE posts: {len(nonrecipes)} ==")
for it in nonrecipes:
    print(f"   {txt(it.find(q('post_id'))):>5} {txt(it.find('title'))[:55]!r:58} "
          f"cats={[n for _,n in pcats(it)]} status={txt(it.find(q('status')))} slug={txt(it.find(q('post_name')))}")

# ---- draft / non-publish ----
print("\n== non-publish posts ==")
for it in posts:
    if txt(it.find(q("status"))) != "publish":
        print(f"   {txt(it.find(q('post_id'))):>5} status={txt(it.find(q('status')))!r} {txt(it.find('title'))!r} cats={[n for _,n in pcats(it)]}")

# ---- comments ----
print("\n== comments (on posts) ==")
cmap = []
for it in posts:
    for c in it.findall(q("comment")):
        cmap.append((txt(it.find(q("post_id"))),
                     txt(it.find("title")),
                     txt(c.find(q("comment_author"))),
                     txt(c.find(q("comment_author_email"))) or "(none)",
                     (txt(c.find(q("comment_content")))[:70]).replace("\n", " "),
                     txt(c.find(q("comment_date"))),
                     txt(c.find(q("comment_approved")))))
for pid, title, auth, mail, cont, date, app in sorted(cmap, key=lambda x: int(x[0])):
    print(f"   {pid:>5} {title[:32]!r} {auth!r:24} {date} app={app} {cont!r}")

# ---- attachments: orphan / no-parent ----
real_ids = {txt(it.find(q("post_id"))) for it in posts} | \
           {txt(p.find(q("post_id"))) for p in pages}
orphan = []
noorig = []
for a in attachs:
    parent = txt(a.find(q("post_parent")))
    has_orig = any(txt(m.find(q("meta_key"))) == "_wp_attached_file" for m in a.findall(q("postmeta")))
    if parent not in real_ids:
        orphan.append(a)
    if not has_orig:
        noorig.append(a)
print(f"\n== attachments: total={len(attachs)} orphan={len(orphan)} missing _wp_attached_file={len(noorig)}")
for a in orphan[:20]:
    print("   orphan att", txt(a.find(q("post_id"))), "parent=", repr(txt(a.find(q("post_parent")))))

# ---- slug collisions (post_name among posts+pages) ----
print("\n== duplicate slugs among posts (case-insensitive) ==")
sl = Counter()
sl2name = defaultdict(list)
for it in posts:
    s = txt(it.find(q("post_name"))).lower()
    if s:
        sl[s] += 1
        sl2name[s].append((txt(it.find(q("post_id"))), txt(it.find("title"))))
for s, n in sl.most_common(15):
    if n > 1:
        print(f"   {s!r} x{n}  {sl2name[s]}")
