Read-only Task 7.7 CLI review. No edits, planning, environment/.env access, git,
Docker, tests/subprocess runs or extra exploration. Read ONLY src/cookbook/
cli.py, cli_inputs.py, cli_accounts.py, config.py and tests/test_cli.py,
test_cli_inputs.py, test_cli_config.py. Shared bootstrap service is reviewed
separately; accepts factory=, creates first admin in fresh locked transaction,
or reset_password(...restore_admin=True) restores only existing named user and
wipes sessions atomically.
Requirements: init-secret32 random bytes hex, exclusive default, overwriteonly
flag, best-effort0600, outputpathonly, no config/DB/SECRET_KEY requirement.
AccountCLI DB-only config ignoring web keyfile, ownengine disposed SAMEloop
even failure; exact shared services, never own count/guard. Interactive hidden
password twice, username defaultowner (honor explicit), min8 via sharedservice.
Setup password precedence explicitfile>FIRST_ADMIN_PASSWORD_FILE>plainenv>
interactive, preserve spaces, warn plainenv when DEBUGfalse without value.
Reset requires interactive/file and ignores bootstrapfallback. Recoveryflag
requires explicitusername + typed yes/<normalized username> even filemode,
restores named existingonly, nevercreates. Expectedfailures nonzero and safe
stderr (no SQLparameters/passwords/hashes). Report concrete bugs with file:line,
severity, trigger/impact and smallest fix; separate optional suggestions.
If no blocking findings say so. Stop.
