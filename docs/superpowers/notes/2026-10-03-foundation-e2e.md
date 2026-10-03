# Foundation e2e gate — verification record (2026-10-03)

Task 5 of `docs/superpowers/plans/2026-09-27-foundation.md`
("Docker e2e — build, boot migrations, health, negative readyz — the gate"),
run on branch `task/5-foundation-e2e` (cut from `main` @ `576a306`). All
commands ran on the owner's box. **Preservation:** the dev stack (`app` +
`db`) was left running and healthy; `/mnt/container/db` and
`/mnt/container/app` were never removed; the only host path touched was the
media bind mount's permission bits (temporarily modified for the negative
readyz probe, then restored exactly). The unrelated container policy was
respected; no other containers were stopped and no images were removed.

## 1. Fresh image built from this repo (the real gate, not the retained image)

```
docker compose build
```

Build succeeded end-to-end. Key evidence from the build output:
- builder stage ran `uv sync --frozen --python /usr/local/bin/python` with
  `UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy` — proves `uv.lock` is
  complete and reproducible (Task 5 Step 2).
- `#28 [runtime 11/11] RUN chmod +x ./bin/docker-entrypoint.sh && chown -R
  cookbook:cookbook /app` DONE 0.3s.
- `Image cookbook:dev Built`.

Recorded digests (from build output + `docker inspect`):
- manifest list (repo tag `cookbook:dev`):
  `sha256:6893ea78210b0e6fd669ba4b2f4a7ae9d9c18880acf1e9557340e88216f134ed`
- per-arch manifest: `sha256:55d343ec4ac57d8ab0df670f12c369e1c45f0a30c165b8171d1120dcdcaf650c`
- image config: `sha256:b76d9b81d293181af1a8519cd35df4a3fe55382fb2370bb2e103a9fc33c74d64`
- local image ID: `6893ea78210b`; created 2026-10-03 15:43 -0400.

The previously retained image (`sha256:1a273aa35a57…`, 2026-09-28) was
superseded — as the plan anticipates, the build digest differs unless inputs
are byte-identical. Build, not retained image, is the gate.

## 2. Boot the dev stack on the fresh image

Pre-state: `app` (old image 1a273aa35a57) and `db` (`mariadb:11`) both
`Up … (healthy)` from an earlier session; `0.0.0.0:8000` published, `3306`
not published (db is on the compose network only).

```
docker compose up -d
```
```
 Container db Running
 Container app Recreate
 Container app Recreated
 Container db Waiting
 Container db Healthy
 Container app Starting
 Container app Started
```

Confirmation the running `app` is the fresh build:
```
docker inspect app --format '{{.Config.Image}}'   # cookbook:dev
docker inspect <app.ImageId> --format '{{.Id}}'   # sha256:6893ea78210b…
```
`app` reached `healthy` (its healthcheck is a `/readyz` 200 probe).

## 3. Entrypoint applied migrations (D23)

App log, filtered:
```
app | [entrypoint] applying database migrations (alembic upgrade head)
app | INFO  [alembic.runtime.migration] Context impl MySQLImpl.
app | INFO  [alembic.runtime.migration] Will assume non-transactional DDL.
app | [entrypoint] migrations OK; exec: uvicorn cookbook.app:create_app --factory --host 0.0.0.0 --port 8000
```

Schema state in the dev MariaDB (queried via the `cookbook` DB user, not root):
```
docker exec -i db mariadb -ucookbook -p<devpw> cookbook \
  -e "SHOW TABLES; SELECT version_num FROM alembic_version;"
```
```
Tables_in_cookbook
alembic_version
users
version_num
6d22befb6ec9
```
- `users` and `alembic_version` both present.
- `version_num` = `6d22befb6ec9` — the v0 users migration revision, as
  specified. (Alembic `upgrade head` was a no-op apply against already-current
  data under `/mnt/container/db`; the entrypoint path was still exercised and
  logged.)

## 4. Positive health — exact 200 shapes

```
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/healthz
```
```
{"status":"ok","version":"0.1.0","uptime_seconds":44}
HTTP 200
```
```
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/readyz
```
```
{"status":"ready","version":"0.1.0","checks":{"db":"ok","media":"ok"}}
HTTP 200
```
Both match the spec §9.5 / Task 3 expected shapes exactly.

## 5. Negative readyz — media not writable (spec §9.5: 503 + loud log, no self-shutdown)

App runs as `uid=1000(cookbook)`, which is the **owner** of `/media`
(host `/mnt/container/app`, `drwxr-xr-x 1000:1000`). Removing the *other*
write bit would NOT affect an owner, so the probe had to remove the **owner**
write bit — `chmod u-w` — to actually cause the `PermissionError`. (Recorded
as a deviation from the plan's `chmod o-w` example; the plan's own assertion
is `status == not_ready`, `checks.db == ok`, `checks.media` starts with
`error:` — all satisfied.)

Before: `stat /mnt/container/app` → `mode=755 stephanie:stephanie`.

```
chmod u-w /mnt/container/app          # 755 -> 555
curl -s -w '\nHTTP %{http_code}\n' http://127.0.0.1:8000/readyz
```
```
{"status":"not_ready","version":"0.1.0","checks":{"db":"ok","media":"error: PermissionError"}}
HTTP 503
```
- HTTP `503` as required.
- `checks.db == "ok"` (per-check probe; DB unaffected), `checks.media ==
  "error: PermissionError"` — matches the 2026-09-27 verified negative shape
  exactly.
- Loud log line (from `src/cookbook/app.py`, `log.warning("readyz: NOT
  READY %s", body)`):
  ```
  readyz: NOT READY {'status': 'not_ready', 'version': '0.1.0', 'checks': {'db': 'ok', 'media': 'error: PermissionError'}}
  ```
- No self-shutdown (D24): while media was read-only the app stayed
  `state=running, running=true` and kept serving other requests;
  `docker ps` → `app Up … (healthy)`, `db Up … (healthy)`.
- Restore + recovery:
  ```
  chmod u+w /mnt/container/app        # 555 -> 755 (exact original mode restored)
  stat /mnt/container/app             # mode=755 stephanie:stephanie
  curl -s -o /tmp/r -w '%{http_code}\n' http://127.0.0.1:8000/readyz   # 200
  {"status":"ready","version":"0.1.0","checks":{"db":"ok","media":"ok"}}
  ```

## 6. Complete test suite in the app container

The fresh image does not ship the tests, and `docker exec` runs as the
non-root `cookbook` user (no passwordless sudo), so tests were synced to
`/app/tests/` (writable by that user) — this does not modify the repo or the
image:
```
docker exec app sh -c 'mkdir -p /app/tests'
docker cp tests/test_{config,app,models}.py app:/app/tests/
```
Confirmed the fixed `test_app.py` (Task 4 teardown: `Iterator[TestClient]`,
`yield TestClient(app)`, 3 `cache_clear()` calls pre+post) is the file under
test.

Results (`/opt/venv/bin/python -m pytest`, in the `app` container, against the
dev `db` host):
- Task 4 alone — `test_models.py`: **2 passed**
  (`test_users_table_shape`, `test_user_roundtrip`).
- All three together, `config -> app -> models`: **12 passed**
  (the ordering that previously failed 10 passed / 2 failed).
- `models -> app -> config`: **12 passed**
- `app -> models -> config`: **12 passed**

The Task 4 fix holds in the freshly built image too — order-independent
passing across three combined orderings.

## 7. DB_HOST=db — what this run proves (vs. prior evidence)

This run proves, from the fresh image, that the dev `app` reaches the compose
`db` service by name:
- `docker exec app printenv` → `DB_HOST=db`, `DB_PORT=3306`, `DB_NAME=cookbook`,
  `MEDIA_DIR=/media`.
- `docker compose config` → `app` service `environment.DB_HOST: db` (this
  explicit `environment:` entry overrides the `.env` value `DB_HOST=127.0.0.1`
  from `env_file`, so the app is unambiguously pointed at the `db` service).
- `/readyz` `checks.db == "ok"` (a real `SELECT 1` round-trip) and the
  test_models DB tests pass — all through that hostname.

This is *new* proof at the dev-compose-hostname level. The earlier
2026-09-27 cross-container proof — the same single code path pointed at an
**external** MariaDB via `DB_HOST=<other host>` (the prod mechanism) —
remains **prior evidence**, recorded in
`docs/superpowers/SESSION-HANDOFF-2026-09-27.md`; it is not re-asserted as a
fresh result here.

## 8. State and limitations

Final state (preserved):
- `docker compose ps`: `app (cookbook:dev) Up healthy`, `db (mariadb:11)
  Up healthy`.
- `/mnt/container/app` restored to `755 stephanie:stephanie`; `/mnt/container/db`
  untouched.
- Repo working tree clean; only this record file was added.

Limitations:
- The negative-readyz probe required `chmod u-w` (owner write bit) rather than
  the plan's `chmod o-w` example, because the app runs as `uid=1000`, the
  owner of the media mount; the *other* bit alone would not produce the
  `PermissionError`. The observed shape (503, `db: ok`, `media:
  error: PermissionError`, loud log, container stays up, recover to 200)
  matches the plan's stated assertion.
- `/mnt/container/db` held pre-existing dev data; re-running `alembic upgrade
  head` against it was a no-op apply (schema already at head), so the
  entrypoint's apply path was exercised/logged but not a cold-migration run.
  This is expected for a preserved dev DB and does not weaken the gate.
- Tests were synced to the *container's* `/app/tests/` for execution; the repo
  `tests/` tree and the built image were not modified by this run.