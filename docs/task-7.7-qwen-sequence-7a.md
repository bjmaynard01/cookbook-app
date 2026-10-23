# Tiny subtask: CLI credential inputs only

Create ONLY cli_inputs.py; read ONLY config.py CLISettings/read_password_file
and bootstrap.normalize_username signatures. No planning, other docs/source,
tests, environment reads via tools, git, Docker, deps. Source runtime naturally
reads config passed in, never web get_settings. Keep below100 lines.

account_credentials(args,cfg)->(username,password,restore_admin_bool).
Use import getpass and getpass.getpass at runtime (tests instrument it),
builtins input directly. normalize_username from auth.bootstrap.
If args.command==setup: force flag requires explicit --username BEFORE prompts;
noninteractive password source (explicit file/cfg.first_admin_password_file/
cfg.first_admin_password is not None) also requires explicit --username.
Interactive username input default owner. Normalize username.
Password precedence: args.password_file > cfg.first_admin_password_file >
cfg.first_admin_password > interactive. read_password_file from config preserves
spaces. Plain cfg env password chosen with cfg.debug false logs WARNING through
logger cookbook.cli mentioning FIRST_ADMIN_PASSWORD (never its value).
Interactive password+confirmation via getpass; mismatch ValueError fixed message.
When force: warning fixed local recovery message, typed input MUST equal
`yes/<normalized username>` even file mode; mismatch ValueError fixed message.
Return restore_admin True only for force. Never silently create named recovery.

If user reset-password: normalize args.username; args.password_file or
interactive+confirmation (parser requires a mode), no FIRST_ADMIN fallback;
return restore_admin=False. Services enforce shared min8 policy.
No DB access, print passwords, or raw passwords in exceptions/logs/args.
Compile source, report path and stop.
