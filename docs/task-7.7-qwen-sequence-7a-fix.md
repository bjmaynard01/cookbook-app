Correct ONLY src/cookbook/cli_inputs.py. Read cli.py parser and cli_inputs.py.
No planning, tests, config/environment reads, git, Docker, dependencies.
Acceptance failures traced to mismatched argparse names: setup flag attribute
is force_owner_recreation (NOT force); reset has args.command == 'user' and
args.user_command == 'reset-password'. Fix both to match real parser.
Also interactive setup currently ignores supplied --username when no password
source: honor supplied username, prompt default owner only if none supplied.
Recovery warning should say restore named existing account as active admin and
reset password/sessions; confirmation prompt must show expected yes/<normalized
username> token so user knows what to type. Never print password/hash.
Compile source and stop. No other changes.
