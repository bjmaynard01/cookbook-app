# Task 18: Search and result filtering

## Goal

Implement public recipe search at `/search?q=` with MariaDB full-text indexes, ranked results, practical substring fallback, and composable category, tag, and total-time filters. Explain empty and zero-result states and offer useful category/tag suggestions.

## Design decisions

- Use MariaDB `FULLTEXT` indexes and natural-language `MATCH ... AGAINST` as the baseline, with explicit score tiers: title, then category/tag/ingredient, then description/instructions.
- Add case-insensitive escaped substring matching for partial terms and words omitted by the MariaDB parser. This is an OR-style token fallback; it keeps short/prefix searches useful without replacing relevance ranking.
- Keep category and tag refinement semantic and time refinement as `Recipe.total_minutes <= selected_limit`, excluding recipes without a known total.
- Limit the public query to 200 characters, ignore punctuation-only / one-letter alphabetic tokens in fallback matching, and make all filters ordinary GET parameters.
- Add FULLTEXT indexes for recipe title/description, ingredient/step text, category names, and tag names. Keep indexes single-column so each `MATCH` expression aligns with its index.

## Steps

1. Add route-level tests for relevance ordering, prefix/short-term matching, combined refinements, and empty/no-result guidance.
2. Add the MariaDB FULLTEXT indexes to SQLAlchemy metadata and an Alembic migration; verify migration upgrade/downgrade behavior.
3. Implement the search query and result data in a focused service, reusing recipe-card presentation data.
4. Add the `/search` route and a public search template with persistent category, time, and tag refinements.
5. Run focused tests and the full suite against a disposable MariaDB, build the container, and update the roadmap/spec status.

## Outcome

Task 18 is implemented. MariaDB 11 accepted all five FULLTEXT indexes and the search integration query; both migration upgrade and downgrade were verified. The focused search test passed, the full suite passed (**558 passed**) with `DEBUG=false`, and the Docker image built.

## Real-corpus tokenizer benchmark (2026-11-06)

The benchmark imported all 391 published WXR recipes into a fresh disposable MariaDB 11 database. Image fetching was disabled for this text-only run. It exercised the existing `search_recipes` path, which combines natural-language FULLTEXT ranking with case-insensitive substring fallback. MariaDB InnoDB FULLTEXT excludes words shorter than three characters; the fallback is what keeps short and prefix queries useful ([MariaDB FULLTEXT overview](https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/optimization-and-indexes/full-text-indexes/full-text-index-overview)).

Representative results were: `chicken` 77 recipes, `pie` 106, `mac` 14, `bbq` 8, `pinto beans` 46, and `crock pot` 112. Short/prefix searches also worked (`pi` returned 266). One-character alphabetic `x` returned no results as designed. Matching queries took 44–67 ms in this local disposable setup; the one-character no-result query took 5.2 ms. Numeric-only `2` returned 370 of 391 recipes, showing that quantity searches are very broad; consider separate search-quality tuning if that becomes a user concern.

No tokenizer change is indicated: the current word parser plus partial-match fallback preserves expected common, short, and prefix searches on the imported corpus. No schema or search-code change was made from this benchmark.
