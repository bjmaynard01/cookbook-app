import io, re
import xml.etree.ElementTree as ET
from collections import Counter
W = "http://wordpress.org/export/1.2/"
ch = ET.parse('/home/stephanie/Projects/cookbook-app/wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml').getroot().find("channel")
def pt(it):
    el = it.find("{%s}post_type" % W)
    return (el.text if el is not None and el.text else "")
posts = [it for it in ch.findall("item") if pt(it) == "post"]
cnt = Counter()
for it in posts:
    for m in it.findall("{%s}postmeta" % W):
        k = m.find("{%s}meta_key" % W)
        cnt[(k.text if k is not None and k.text else "?")] += 1
print("== postmeta keys (posts only) ==")
for k, v in cnt.most_common():
    print("  %4d  %s" % (v, k))
d = io.open('/home/stephanie/Projects/cookbook-app/wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml', encoding='utf-8').read()
print("\n== raw-text probes for structured yield/time fields ==")
for probe in ['yield', 'prepTime', 'cookTime', 'totalTime', 'prep_time', 'cook_time', 'serves', 'servings', 'recipe_yield', 'serving']:
    hits = len(re.findall(probe, d, re.I))
    print("  %-16s %d" % (probe, hits))
