# Desktop handoff — 2026-10-30

## Current state

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- Permanent integration workflow: `dev` is the active development branch; `main` is the stable integration branch.
- Task 10 (editor dashboard and recipe CRUD) is implemented and pushed to `main`. Its implementation and the initial roadmap are in commit `331a5ec`.
- `docs/BUILD-ROADMAP.md` is the task sheet for the current v1 scope. Tasks 1–10 are complete; Task 11 (recipe image storage) is next.
- The last full isolated test run passed **456 tests**, with one existing Starlette/httpx deprecation warning.
- Check live Git state before acting; this file is a maintained snapshot, not a replacement for Git.

## Working agreement

- Do all implementation and task commits on `dev`, then push `origin/dev`.
- Integrate reviewed, tested task milestones from `dev` into `main`; do not develop directly on `main`.
- Use a fresh OpenCode/Qwen session for each small implementation subtask when useful. Bounded source and prompts may be sent to the owner's trusted endpoint `https://ollama.maynardfolks.com/v1` using `ollama-remote/qwen3.8:27b-128k`.
- Do not create progress, handoff, or checkpoint notes. Flag meaningful decision points to the owner.
- Keep database tests on disposable isolated MariaDB. Never read or commit `.env`, and do not test or migrate against the running development database.

See `docs/PROJECT-STATUS.md` for the current product and environment notes, `docs/BUILD-ROADMAP.md` for the complete task list, and `README.md` for setup.
