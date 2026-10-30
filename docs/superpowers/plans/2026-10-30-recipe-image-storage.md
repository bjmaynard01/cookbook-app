# Task 11: Recipe Image Storage Implementation Plan

**Design:** `docs/superpowers/specs/2026-10-30-recipe-image-storage-design.md`
**Branch:** permanent `dev`; no task branch/worktree

## Steps

1. Add Pillow as a runtime dependency and implement `recipes/images.py` with format verification, byte-limit enforcement, UUID relative paths, atomic writes, safe path resolution, serving metadata, and best-effort cleanup. Add unit tests for supported images, corrupt/mislabeled files, size limits, path traversal, and cleanup. Run them red then green.
2. Add caller-transaction-managed image-path update/remove operations to the recipe service layer. Add isolated MariaDB tests for existing/missing recipes, rollback behavior, and preservation of other recipe fields. Run the focused service tests.
3. Add an upload request-size guard before auth middleware reads unsafe request bodies. Add tests for declared oversize and missing length on the image endpoint, plus unaffected ordinary editor posts.
4. Add an authenticated `POST /recipes/{id}/image` route and public `GET /media/{path}` route. Verify role, CSRF, MIME, cache, nosniff, not-found, and failure cleanup behavior with route tests.
5. Add current-image preview, multipart upload/replace, and remove controls to the recipe edit form. Surface success/failure messages and keep image controls independent from metadata saves.
6. Ensure successful recipe deletion removes its image after commit; alias-protected or failed deletion keeps the file. Add route/service regression coverage.
7. Run focused tests after each step, then the full suite, compile check, and `git diff --check`. Update `docs/BUILD-ROADMAP.md`, `docs/PROJECT-STATUS.md`, and `docs/DESKTOP-HANDOFF.md` to mark Task 11 complete. Commit and push task work on `dev`; merge the tested milestone into `main`.

## Constraints

- Use disposable MariaDB for DB tests; never read `.env` or touch the running development database.
- The size guard must run before `AuthMiddleware` reads/parses the body. Uploaded file size validation alone is too late to bound request buffering.
- Database transactions remain caller-managed. New files are removed if the DB update fails; old files are removed only after commit.
- Preserve the uploaded original bytes. Supported decoded formats: JPEG, PNG, WebP.
