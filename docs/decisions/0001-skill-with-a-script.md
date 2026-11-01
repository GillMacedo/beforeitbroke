# 0001: A skill with one standard-library script

**Decision:** ship a Claude Code skill and one Python script, using the user's `git`, `gh` and `kubectl`.

**Why:** installing it needs no server or package, and it inherits the access the user already has. A script also lines up times consistently, which is easy to get wrong when an agent runs the tools separately.

**Alternatives:** an MCP server would be easier to list in registries, but it adds an install and a process to run. Move to one only if real use shows the skill isn't enough.
