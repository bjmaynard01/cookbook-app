# Task 19: WordPress migration importer

## Goal

Import published WordPress recipe posts from a WXR export into the cookbook with deterministic body parsing, original image transfer, legacy aliases, durable audit, resume behavior, and a verifiable report.

## Design

- Keep WXR parsing pure and bounded: stdlib XML parser, one input-size cap, explicit WXR namespaces, and a parsed import plan before any write.
- Default `cookbook import-wp --file ...` to a read-only dry run. Require `--apply` or `--resume` to write. Reject a report destination that resolves to the source WXR path.
- Treat the eight seeded menu categories as canonical. Resolve per-post category terms through WXR parent slugs; a term with no menu-category ancestor maps to the owner-approved `Others` category. Ignore WordPress post tags and comments per the approved v1 scope.
- Preserve source post IDs, publication dates, titles, original query permalink paths, and all body text. Split body lines deterministically into ordered ingredient/step/note sections; retain raw line text and emit warnings for uncertain classification. Do not invent servings or times.
- Generate slugs from titles with deterministic suffix allocation on collisions. Preserve WordPress IDs separately; the `/?p=<post_id>` alias always points to the resulting recipe UUID.
- Resolve each recipe's `_thumbnail_id` to its WXR attachment URL. Use the existing SSRF-checked, redirect-validated, byte-bounded fetcher, then the image validator/storage so original supported bytes are retained. Image failure does not block the recipe; it is counted and detailed in the audit.
- Create a run audit row, then commit each recipe, alias, and per-recipe log entry atomically so an interrupted process can resume by `(source_system, source_id)`. Record failures and continue other posts.
- `--verify --run <id>` rechecks source hash/counts, audit-log parity, category and ingredient invariants, recipe and page aliases, image accounting, and a later all-skipped resume run. Image failures remain visible in verification; accepted omissions are recorded on the post audit row and migration-run notes.
- Work directly on `dev`, per the standing branch workflow; use a disposable MariaDB for all database validation.

## Steps

1. [x] Add a minimal WXR fixture and parser tests for WXR namespaces, published/draft selection, hierarchical categories, post metadata, page aliases, and body preservation.
2. [x] Implement the pure parser and deterministic line-level section splitter; verify malformed/oversized exports fail before writes.
3. [x] Add dry-run/report tests for field presence, category evidence, slug allocation/collisions, and zero database mutation.
4. [x] Add apply/resume integration coverage for per-recipe audit, aliases, category creation, original image storage, image failure isolation, and second-run skipping.
5. [x] Add verification-gate and CLI tests; cover source counts, aliases, category/ingredient invariants, image accounting, and idempotency.
6. [x] Run the full suite and schema check against a disposable database, build the Docker image, and update the roadmap and project status.

## Corpus dry-run review (2026-11-06)

The supplied `maynardfamilycookbook.WordPress.2026-11-06.xml` parsed successfully in dry-run mode against a fresh disposable MariaDB. Its SHA-256 is `405b3b6372ccfd3baa9a163b305d67323d19e44086b8083f0aa273900bd98a1e`. The report counts 392 posts total (391 published, one draft excluded), all eight required category pages, 182 image references, and 12 duplicate-title slugs allocated with deterministic `-2` suffixes. Compared with the September discovery spike (389 total, 388 published), this newer export contains three additional published posts.

Seven published posts have incomplete recipe fields. Three contain preparation text but no separately classified ingredient lines, three have ingredients but no preparation steps, and one has no readable body. The owner approved retaining posts 210, 331, 342, and 870 without inventing ingredient data. The importer scopes this exception to those source IDs and titles, records each exception in the report and migration audit, and verifies the audit marker. Apply is blocked for any other post with no ingredient lines. The three ingredient-only posts remain represented with parser warnings. The owner previously approved retaining post 1950, “Pinto Bean Recipes,” as one flagged collection record.

The owner approved applying all 391 published posts with those four recorded exceptions. Import run `96797932-43a5-47af-9455-cb7e20b637d9` imported all 391 with zero recipe errors. Because the app container's split DNS resolved the image host to a private address, the safe fetcher initially rejected all 182 images. A temporary importer container using public DNS retried the same bounded, redirect-validated fetch: 181 images were validated and stored; post 1957's AVIF URL still redirects to an unsafe destination and remains absent. No safety check was bypassed. The owner accepted that missing image as an exception on 2026-11-06; the approval and fetch failure are recorded on the post's migration audit row and run notes. Resume run `ea253eff-9a07-4b64-abb1-4ecb75847c60` skipped all 391. Final verification passed the source hash/count, audit parity, categories, aliases, approved-exception markers, image accounting, stored-image, and resume checks, with the accepted image failure still visible.
