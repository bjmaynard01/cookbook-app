# Tiny subtask: CLI-only configuration and bootstrap password fields

Read ONLY config.py and tests/test_cli_config.py. Edit config.py only.
No planning/todos, git, tests, Docker, environment/.env reads, deps or other docs.

Add CLISettings(BaseSettings), env_file=.env extra=ignore with aliases:
DEBUG false, DB_HOST127.0.0.1, DB_PORT3306, DB_NAME/DB_USER cookbook,
DB_PASSWORD optional str repr=False, DB_PASSWORD_FILE optional Path,
MEDIA_DIR /media, FIRST_ADMIN_PASSWORD optional str repr=False,
FIRST_ADMIN_PASSWORD_FILE optional Path. NO SECRET_KEY fields/requirements.
database_url property uses sqlalchemy.engine.URL.create("mysql+asyncmy", ...,
query={"charset":"utf8mb4"}).render_as_string(hide_password=False) so passwords
with @ / : ? # work. cli_config()->CLISettings builds config and resolves
ONLY DB_PASSWORD_FILE file-first; bootstrap file path kept LAZY so explicit
CLI --password-file can override even an unreadable fallback file.

Add read_password_file(path)->str helper: UTF8 first line, strip only line
endings (preserve password spaces), empty file -> ""; unreadable -> RuntimeError
fixed message without file contents. Reuse for CLI DB file and bootstrap file.

Add FIRST_ADMIN_PASSWORD/FIRST_ADMIN_PASSWORD_FILE fields to web Settings;
get_settings() resolves that file-first via read_password_file if configured.
Keep existing SECRET_KEY fail-fast and existing DB/proxy/cookie behavior.
No CLI module or DB factory changes in this subtask. Below100 new lines.
Compileall config only, report and stop.
