# beforeitbroke

Claude Code skill and CLI that lists what changed around an incident time, from git, GitHub and Kubernetes.

## Architecture
`plugin/scripts/beforeitbroke.py` holds all the logic: each source returns changes and coverage notes, then a renderer prints them. `plugin/skills/finding-recent-changes/SKILL.md` tells Claude when and how to run it. See `docs/architecture.md`.

## Commands
- `make test`: unit tests and project checks
- `make lint`: manifest validation
- `make demo`: run on made-up data through `tests/fakes`

## Conventions
- Python 3.10+, standard library only.
- Commands must stay read-only. A test checks this.
- Never request Secrets or print env or ConfigMap values.
- A source that can't be read must show as FAILED or PARTIAL, never as empty.

## Do and don't
- Do pass outside text through `clean`.
- Don't add network calls of its own or telemetry.
- Don't claim a change caused an incident.

## Definition of done
`make test` and `make lint` pass, and `ROADMAP.md` and docs are updated when relevant.
