# Desktop handoff — 2026-10-30

## Current repository state

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- `main` and `origin/main` are both at `cfb19b8` (`feat(recipes): add shared draft and save validation`).
- `main` is the only local and remote branch.
- Tasks 7.4–7.9 and Task 8 are consolidated on `main`.
- The Task 8 focused suite passed: **23 tests**. The last full isolated suite, during Task 7.9, passed **401 tests**; the full suite has not been rerun after Task 8.

## What is implemented

The repository has the full Task 6 data model, authentication and account protections through Task 7.9, and Task 8's shared Pydantic recipe draft and save-readiness validation. Task 8 accepts partial drafts, keeps ordered recipe sections and raw source text, validates title/category/ingredient requirements, reports missing steps as a warning, and resolves total time. See `docs/superpowers/plans/2026-10-30-recipe-draft.md` and `docs/superpowers/specs/2026-09-20-cookbook-design.md`.

Recipe CRUD persistence and routes, recipe forms, images, public cookbook pages, search, and import are not implemented yet. The next work is Task 9: recipe CRUD services, slug generation and rename behavior. Start with a detailed Task 9 plan. Slugs are generated from the title on initial creation and remain editable afterward; collision suffixing and old-slug aliases belong to Task 9.

## Working agreements

- Use a fresh OpenCode/Qwen session for each small implementation subtask. The owner approved the configured `ollama-remote/qwen3.8:27b-128k` endpoint at `https://ollama.maynardfolks.com/v1` for bounded source and prompt transfer.
- Do not create progress, handoff, or checkpoint notes. Flag meaningful decision points to the owner.
- Do not commit or push future task work without the owner's authorization.
- Keep database tests on a disposable isolated MariaDB. Do not migrate or test against the running development database while a task is unmerged. Never read or commit `.env`.

See `docs/PROJECT-STATUS.md` for project context and local Windows data cautions, and `README.md` for setup.
