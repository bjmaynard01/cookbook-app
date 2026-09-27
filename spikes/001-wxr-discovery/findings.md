# Spike 001 — WXR Discovery (findings)

**Date:** 2026-09-27 (session) · **Input:** `wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml` (2.3 MB)
**Goal (spec §5.1):** Before writing any implementation code, establish the ground truth of the source data — what a "recipe" is, how it maps to the 8 category pages, what fields exist, and whether the body text is parseable deterministically. Record findings + a build/can't-build verdict.

**Verdict: GREEN — proceed.** The WXR is well-formed, self-consistent, and carries every field a cookbook needs (title, slug, date, author, category membership, body text, image). Category membership is **unambiguous** (per-post tags). The **only** ambiguous piece is splitting a free-text body into *ingredients* vs *directions*, and that is solvable with a documented, reviewable heuristic — **no LLM is required for structure**. See §6 for the one decision that needs the owner.

---

## 1. Source inventory (measured, not assumed)

| Thing | Count | Note |
|---|---|---|
| recipe posts (`post_type=post`) | **389** | 388 published + 1 draft |
| category/index pages (`post_type=page`) | **8** | the 8 menu pages, ids 8,10,12,14,16,18,20,1557 |
| attachments (images) | **343** | 31 orphaned (parent=0 / no real parent) |
| authors | **6** | admin, bryan, barbara, william, stephanie, marshall |
| WP version | **7.1.2** | WXR format 1.2 |

**The 8 category pages** (each is a `post_type=page` whose body is a `[catlist name="…"]` shortcode):

| page id | title | catlist slug |
|---|---|---|
| 8 | Main Dishes | main dishes |
| 10 | Drinks | drinks |
| 12 | Desserts | desserts |
| 14 | Side Dishes | side dishes |
| 16 | Breakfast | breakfast |
| 18 | Appetizers | appetizers |
| 20 | Baking | baking |
| 1557 | Fermentation | fermentation |

## 2. Category membership — the key question, resolved

**A recipe appears on a category page if and only if the post carries that category's tag.** That's the rule. It is stored per-post (not by the page linking out), via un-namespaced elements of the form:

```xml
<category domain="category" nicename="main-dishes">Main Dishes</category>
```

⚠️ **WXR gotcha (would silently break a naive parser):** there are *two* different category structures in the file that look alike:
- **Per-post** membership → `<category domain="category" nicename="slug">Name</category>` (un-namespaced, attribute-driven, one `<category>` element per tag, 562 such closers).
- **Channel-level taxonomy** → `<wp:category>` with child `<wp:term_id>/<wp:category_nicename>/<wp:cat_name>` (17 of these, the master list).

They must not be confused. The per-post `nicename` attribute is what drives membership.

### Category taxonomy (all 17 terms, with parents)

| slug | name | parent |
|---|---|---|
| main-dishes | Main Dishes | — |
| breakfast | Breakfast | — |
| side-dishes | Side Dishes | — |
| baking | Baking | — |
| desserts | Desserts | — |
| **desserts-baking** | **Desserts** | baking *(2nd category also named "Desserts")* |
| drinks | Drinks | — |
| appetizers | Appetizers | — |
| fermentation | Fermentation | — |
| bread | Bread | baking *(sub)* |
| **crockpot** | Crockpot | main-dishes *(sub)* |
| **sauces** | Sauces | side-dishes *(sub)* |
| **dressings** | Dressings | side-dishes *(sub)* |
| asian | Asian | — *(not in the 8)* |
| bbq | BBQ | — *(not in the 8)* |
| instant-pot | Instant Pot | — *(not in the 8)* |
| uncategorized | Uncategorized | — |

So beyond the 8 menu categories there are **5 subcategories** (bread, crockpot, sauces, dressings, desserts-baking) and **3 extra top-level** (asian, bbq, instant-pot).

### Recipe set size
- **374** posts carry at least one of the 8 menu-category slugs (some also sub/extra tags).
- **15** posts carry only `bbq` or `uncategorized` and would appear on **none** of the 8 category pages (reachable only by direct URL). List in §5.
- Per-category counts (a post may sit in several): main-dishes 162 · baking 93 · desserts 77 · side-dishes 60 · breakfast 47 · desserts-baking 22 · appetizers 19 · bread 10 · drinks 8 · crockpot 7 · sauces 4 · fermentation 4 · dressings 1.

## 3. Fields that actually exist (postmeta census)

| postmeta key | posts | meaning |
|---|---|---|
| `_edit_last` | 199 | last-editor user id (not the author) |
| `_thumbnail_id` | 180 | featured image id → the 343 attachments |
| `_vc_post_settings` | 66 | page-builder (Visual Composer) settings — not needed for a recipe |
| `_stcr@_…@…` | 5 | comment-spam artifact — drop |
| `footnotes` | 2 | — |
| `_wp_old_slug` / `_wp_old_date` | 2 each | slug/date change history |
| `force_ssl_children` | 1 | legacy |
| `_oembed_…` | 4 | embedded media cache — drop |
| `_wp_desired_post_slug` | 1 | requested slug |

**Author** = the post author (`dc:creator`), not `_edit_last`. **Yield / prep-time / cook-time / total-time: NOT PRESENT as structured fields.** Raw-text probes returned 0 for `prepTime`, `cookTime`, `totalTime`, `recipe_yield`; the words "yield/serves/servings" appear only *inside prose* (e.g. "Yield:6-8 servings" is written in a step in Slow Cooked Spaghetti Sauce). **This confirms the spec's expectation that yield/times start empty** — they are not in the source.

## 4. Body text — the one real parsing problem

**320 of 389 posts (~82%) have NO HTML structure at all** — a run of plain-text lines separated by newlines. Only 31 posts use `<p>`, 32 use `<li>`, 13 use `<br>`. So ingredients/directions **cannot** be split by HTML. The only reliable signals are:

1. **Headings** (when present) — `Ingredients:`, `Directions:`, `Instructions:`, `Preparation:`, `Method:`, and recipe-specific sub-headings like `Topping:`, `Filling:`, `Crust`, layer names (`First Layer`, `Second Layer`…).
2. **Heuristic fallback** — a maximal block of short lines that are mostly quantity-quantified → ingredients; a block of imperative-verb lines ("Preheat…", "Mix…", "Add…") → directions.

The line-granularity splitter (`parse_v2.py`) produced a correct ingredients/directions split on all 9 sampled bodies, including edge cases: plain-text 2014 recipes (Buttermilk Pie, Loaded Potato), `Ingredients:`-headed (Nut Roll, Taco Casserole), no-heading multi-attachment (Slow Cooked Spaghetti Sauce), and a 24-section stacked recipe (Sex in a Pan) where it preserved named sub-blocks (Crust/Filling/Layers) instead of forcing two buckets.

⚠️ So the schema needs a **sub-section** concept, not just two fixed buckets — some recipes legitimately have `Topping:` / `Filling:` / per-layer groupings inside the body.

## 5. Edge cases the migrator must handle (owner decisions where noted)

- **15 unmenued published posts** (only tagged `bbq` or `uncategorized`) — won't appear on any of the 8 category pages: 1054 Smoked Pork Ribs (bbq), 1191 Play Dough, 1281 Memphis Meathead Rub (bbq), 1651 Korean Braised Short Rib, 1833 Soft Pillowy Sandwich Rolls, 1896 Marry Me Chicken Tortellini Soup, 1899 Pineapple Cowboy Candy, 1942 Matthew McConaughey's Tuna Salad, 1950 "Pinto Bean Recipes" (a markdown-style *collection*, not one recipe), 1618 Chessman Banana Pudding, 1755 Earthquake Cake, 1852 Broccoli Salad, 1855 Smothered Fried Chicken Thighs, 1957 Easy 30-Minute Italian Meatball Soup.
  → **Decision needed (§6):** include all 388 published recipes, or only the 374 menued ones? Dropping the 14 would lose real family recipes.
- **1 draft** (1865, empty body, uncategorized) → exclude.
- **12 near-duplicate title pairs** (legitimately two versions, distinct slugs, e.g. `taco-soup` / `taco-soup-2`, `taco-casserole` / `604`, `chicken-cordon-bleu-casserole` / `…-2`, two "Ricotta Stuffed Shells", two "Mexican Rice", two "Taco Bake", two "Texas Roadhouse Rolls", two "Banana Pudding poke Cake", two "Chocolate Gravy", two "Cinnamon Roll Cake", two "Duke's Secret Salsa"). Faithful migration keeps both; dedupe is an owner call.
- **6 broken/numeric/trashed slugs** (66, 365, 604, 121, 154, and `__trashed-2` for 1787) → slug must be regenerated from the title.
- **31 orphaned attachments** (no real parent) → skip or archive during media migration.
- **Spam to drop:** 16 Korean massage-parlor tags (term ids 20–35) + 5 `_stcr@_…` comment-spam meta keys.
- **20 comments across 14 posts** (family/friends Q&A, all `approved=1`). v1 scope decision (open §12 item) unchanged: default = out; they're in the WXR if needed later.

## 6. Decisions needed before implementation

1. **Include the 14 unmenued + 1 draft?** Recommended: migrate **all 388 published recipes** (data preservation), auto-assign the 14 to a catch-all "Others" category so none are orphaned; exclude the 1 empty draft. *(Owner to confirm.)*
2. **LLM fallback for the ingredients/directions split (open §12 item):** the deterministic heuristic above handles the corpus well. An LLM is only needed if you want "smarter" free-text interpretation. **Not required for a correct migration.** *(Owner to confirm deterministic-only for v1.)*
3. **Sub-section granularity:** adopt `section[]` (ordered list of {heading?, kind: ingredients|steps|note, lines[]}) as the body model so `Topping:`/`Filling:`/layer structure is preserved rather than flattened. *(Recommended; confirm.)*

## 7. Artifacts in this spike

| file | purpose | keep |
|---|---|---|
| `final2.py` | full census: posts/pages/attachs, taxonomy, membership, drafts, comments, orphans, dup slugs | ✅ source of the numbers above |
| `parse_v2.py` | line-granularity ingredients/directions splitter (heading-first + heuristic) | ✅ the parsing approach |
| `dups.py` | near-dup titles, broken slugs, non-recipe list | ✅ |
| `meta_keys.py` | postmeta key census + yield/time probes | ✅ |
| other `*.py` | incremental debugging (namespace discovery, raw dumps) | prune on review |

**Repro:** `python3 final2.py` · `python3 parse_v2.py` · `python3 dups.py` · `python3 meta_keys.py`
