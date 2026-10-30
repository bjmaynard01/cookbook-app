# Recipe Image Storage Design

**Date:** 2026-10-30
**Status:** Implemented and verified
**Roadmap:** Task 11 in `docs/BUILD-ROADMAP.md`

## Goal

Let editors attach, replace, and remove recipe images, persist them on the configured media volume, and serve them safely to cookbook pages and later ingestion/import features.

## User-visible behavior

- An editor can upload an image from an existing recipe's edit form, replace its current image, or remove it. A newly created recipe can receive an image after its first save and redirect to its edit form.
- Supported formats are JPEG, PNG, and WebP, limited to still images of at most 40 megapixels. The server verifies the decoded image format from file content; filename and submitted MIME type are not trusted. SVG and other formats are rejected.
- Uploads are capped by `UPLOAD_MAX_BYTES` (10 MiB by default), stored byte-for-byte under generated UUID filenames inside `MEDIA_DIR`, and recorded in the existing `Recipe.image_path` field as a relative path. There is no schema migration.
- Public image responses use a content type derived from the validated extension, `X-Content-Type-Options: nosniff`, and immutable caching because stored filenames are unique and never overwritten.
- Upload, replace, and remove are separate from recipe metadata saves. An image storage or validation failure leaves the existing recipe and image intact. Replacing/deleting a database reference commits before the old file is removed; a failed new write is cleaned up.
- The public media route only serves paths contained under the configured media directory and supported image extensions. Missing or unsafe paths return 404.

## Boundaries and implementation choices

- Use Pillow to decode/verify supported formats and reject malformed images, while preserving original bytes and metadata. No resize, recompression, or image transformation.
- Use the existing editor role and CSRF protection on all image mutations. Reject oversized requests before the existing auth middleware buffers/parses the body, and also enforce the per-file configured byte limit.
- Keep `MEDIA_DIR` as the only storage root. Write to a temporary file in the target directory and atomically rename to a random final path.
- Add a focused image persistence service that changes only `image_path` in the caller-owned database transaction. It does not commit.
- Do not add external image URLs, thumbnails, remote storage, SVG support, or schema changes.

## Acceptance

- Editors/admins can upload, replace, and remove images; anonymous and non-editor users cannot mutate image state.
- Missing/invalid CSRF is rejected. Oversized, unsupported, mislabeled, and corrupt images do not alter the stored image or leave a final file behind.
- Valid JPEG, PNG, and WebP files round-trip unchanged and are served with safe headers. Traversal attempts and missing files are rejected.
- Recipe deletion cleans up its stored image only after a successful database deletion; alias-protected deletion keeps the image.
- The full suite passes against disposable MariaDB. No running development DB or `.env` is accessed.
