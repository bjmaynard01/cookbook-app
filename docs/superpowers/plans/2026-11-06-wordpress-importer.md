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
- `--verify --run <id>` rechecks source hash/counts, audit-log parity, category and ingredient invariants, recipe and page aliases, image accounting, and a later all-skipped resume run. Image failures remain visible in the verification report for owner acceptance.
- Work directly on `dev`, per the standing branch workflow; use a disposable MariaDB for all database validation.

## Steps

1. [x] Add a minimal WXR fixture and parser tests for WXR namespaces, published/draft selection, hierarchical categories, post metadata, page aliases, and body preservation.
2. [x] Implement the pure parser and deterministic line-level section splitter; verify malformed/oversized exports fail before writes.
3. [x] Add dry-run/report tests for field presence, category evidence, slug allocation/collisions, and zero database mutation.
4. [x] Add apply/resume integration coverage for per-recipe audit, aliases, category creation, original image storage, image failure isolation, and second-run skipping.
5. [x] Add verification-gate and CLI tests; cover source counts, aliases, category/ingredient invariants, image accounting, and idempotency.
6. [x] Run the full suite and schema check against a disposable database, build the Docker image, and update the roadmap and project status.

## Pending source facts

The expected `wp-exports/maynardfamilycookbook.WordPress.2026-09-27.xml` is absent from this workspace. The owner directed fixture-first validation, with the real import deferred until the WXR is available and its dry-run report is reviewed. The owner approved retaining post 1950, “Pinto Bean Recipes,” as one flagged collection record.
