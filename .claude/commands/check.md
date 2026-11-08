---
description: Run the full quality gate (lint + typecheck + tests) and summarise failures
---

Run `make check` and report the result.

- If everything passes, say so in one line and stop.
- If something fails, group the output by stage (ruff / mypy / pytest) and
  show only the first offending failure per stage with its file and line.
- Do not attempt to fix anything. Report, then wait for instructions.
- Never pass `--fix` or edit files as part of this command.
