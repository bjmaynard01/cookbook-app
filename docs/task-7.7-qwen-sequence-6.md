# Tiny subtask: CLI parser and init-secret only

Read ONLY pyproject.toml. Create cli.py and edit pyproject.toml/.gitignore/
.env.example only. No other source/docs/tests, planning, git, Docker, environment
inspection, dependencies. Keep cli.py below100 lines, stdlib argparse only.

main(argv=None)->int; module imports cli_config from .config for later use,
but init-secret NEVER calls it or any DB/SECRET_KEY code.
Parser: init-secret [--out PATH(default secret.key)] [--overwrite]; setup
[--username NAME] [--password-file PATH] [--force-owner-recreation]; user
reset-password USERNAME required mutually exclusive --interactive / --password-file
PATH. Parse Path file options. Required subcommands. No raw --password argument.

init-secret: secrets.token_hex(32), UTF8 write value+newline. No overwrite
unless --overwrite; default atomic O_CREAT|O_EXCL,0600. Best-effort chmod0600,
print only destination path (never key). OSError -> fixed stderr message,return1.
--overwrite deliberate rotation can use O_TRUNC. Don't create parent dirs.
Account commands: LAZY import `from .cli_accounts import run_account` and
return run_account(args). That module is next subtask; do not create stub or
implement it yet. `if __name__=="__main__": raise SystemExit(main())`.

Add [project.scripts] cookbook="cookbook.cli:main". Add /secret.key ignore.
.env.example append commented SECRET_KEY_FILE guidance, SESSION_TTL_HOURS=12,
FIRST_ADMIN_PASSWORD_FILE bootstrap-only and FIRST_ADMIN_PASSWORD plain dev
warning guidance. Existing proxy/cookie entries retained, don't duplicate.
Compile source only, report and stop.
