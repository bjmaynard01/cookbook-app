# Spike 001 — WXR Discovery

**Question:** Can we deterministically migrate every recipe off the WordPress source
(`wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml`) into the cookbook schema,
and — if so — what exactly *is* a recipe, how does it map to the 8 category pages,
which fields exist, and is the free-text body parseable without an LLM for structure?

**Why it matters:** This is the design spec's gate (§5.1). Nothing about the ingest
pipeline, the category model, or the body/sub-section schema should be built until the
shape of the source is known. A wrong assumption here (e.g. "ingredients are a
`<ul>`", "yield is a meta field", "category pages list their recipes") would rework the
whole migration.

**Method:** Parsed the WXR with `xml.etree`; enumerated posts/pages/attachments, the full
category+tag taxonomy, and postmeta keys; derived the recipe set from **per-post** category
`nicename` membership (not the page, not the channel taxonomy — see findings §2); prototyped
a line-granularity ingredients/directions splitter and scored it on 9 sampled bodies incl.
edge cases; diffed 5 recipes against the live site; collected all edge cases (unmenued
posts, drafts, near-dup titles, broken slugs, orphaned images, spam, comments).

**Verdict: GREEN — proceed.** Source is well-formed and carries every field a cookbook
needs (title, slug, date, author, category, body, image). Category membership is
unambiguous. The only genuinely ambiguous part — splitting a free-text body into
ingredients vs directions — has a workable deterministic heuristic that got 9/9 sampled
bodies right (it is *reviewable*, and the raw text is always preserved). **No LLM is
required for structure.** Details, the field census, and the edge-case inventory are in
`findings.md`.

**Decisions still needed from the owner (don't block the build):**
1. Migrate all **388 published** recipes (auto-assign the 14 unmenued ones to a catch-all
   "Others") vs menued-only (374)? Recommendation: all 388, data preservation.
2. Confine the body split to the deterministic heuristic for v1 (no LLM)? Recommended: yes.
3. Model the body as an ordered `section[]` (heading?, kind, lines) so `Topping:`/
   `Filling:`/layer structure survives, rather than two fixed buckets? Recommended: yes.

**How to reproduce:**
```
python3 final2.py      # census: taxonomy, membership, drafts, comments, orphans, dup slugs
python3 parse_v2.py    # ingredients/directions splitter, 9 sample bodies
python3 dups.py        # near-dup titles, broken slugs, non-recipe list
python3 meta_keys.py   # postmeta key census + yield/time probes
```

**Throwaway debug scripts** (namespace discovery, raw dumps) are pruned.
