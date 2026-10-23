# Tiny subtask: CLI account dispatch and DB lifecycle

Create ONLY cli_accounts.py. Read ONLY cli.py parser, cli_inputs.py API,
bootstrap.py helper signatures and config.py CLISettings.database_url. No
planning, other source/docs, tests, environment/.env reads via tools, git,
Docker, dependencies. Keep below95 lines.

run_account(args)->int: cfg=cli_config(); (u,p,restore)=account_credentials(args,cfg).
No web get_settings/get_engine/get_session anywhere. asyncio.run(async operation):
engine=create_async_engine(cfg.database_url,pool_pre_ping=True); factory=
async_sessionmaker(engine,expire_on_commit=False). try/finally await engine.dispose()
on EVERY outcome. setup normal calls await bootstrap.create_first_admin(u,p,
factory=factory); setup force calls reset_password(u,p,restore_admin=True,
factory=factory); user reset calls reset_password(u,p,factory=factory).
Services own transactions. No account counts/guards duplicated in CLI.
Success print fixed message, return0; never password/hash/secret output.

Expected ValueError/AlreadyBootstrapped/UserNotFound/LockRowMissing/read-file
RuntimeError/OSError -> fixed helpful stderr message, return1. Password minimum
can say at least8/no complexity. Operational SQLAlchemyError -> fixed stderr
database failure,return1 (do NOT stringify it: parameters can expose hashes).
Do not blanket turn arbitrary programming failures into success. EOFError/
KeyboardInterrupt credential input -> cancellation nonzero without traceback.
No cookies/sessions issued for CLI setup; reset service wipes sessions.
No configuration caching/modification, no subprocess, no logging secrets.
Compile source, report and stop.
