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

## Validation limits

There is no migrated recipe corpus in this workspace yet, so a meaningful corpus benchmark cannot be run. Correctness and ranking were validated with representative fixture data; benchmark the default MariaDB tokenizer against the actual imported collection during Task 19 and adjust only if that evidence warrants it.
