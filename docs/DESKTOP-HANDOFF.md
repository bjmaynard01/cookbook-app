# Desktop handoff — 2026-10-30

## Current state

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- Permanent integration workflow: `dev` is the active development branch; `main` is the stable integration branch.
- Tasks 10 and 11 are implemented and integrated on `main`; `dev` remains the active development branch.
- `docs/BUILD-ROADMAP.md` is the task sheet for the current v1 scope. Tasks 1–11 are complete; Task 12 (safe URL fetch and page extraction) is next.
- Task 11 full isolated suite: **469 passed**, with one existing Starlette/httpx deprecation warning. The Docker image also built successfully.
- Check live Git state before acting; this file is a maintained snapshot, not a replacement for Git.

## Working agreement

- Do all implementation and task commits on `dev`, then push `origin/dev`.
- Integrate reviewed, tested task milestones from `dev` into `main`; do not develop directly on `main`.
- Use a fresh OpenCode/Qwen session for each small implementation subtask when useful. Bounded source and prompts may be sent to the owner's trusted endpoint `https://ollama.maynardfolks.com/v1` using `ollama-remote/qwen3.8:27b-128k`.
- Do not create progress, handoff, or checkpoint notes. Flag meaningful decision points to the owner.
- Keep database tests on disposable isolated MariaDB. Never read or commit `.env`, and do not test or migrate against the running development database.

See `docs/PROJECT-STATUS.md` for the current product and environment notes, `docs/BUILD-ROADMAP.md` for the complete task list, and `README.md` for setup.
