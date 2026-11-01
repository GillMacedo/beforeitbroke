# Architecture

    git_changes, github_changes, kube_changes --> changes + coverage notes
    collect --> keeps going when a source fails, and records why
    main --> keeps the changes closest to --at, sorts by time, renders

- **Sources** only read. Each returns `Change` rows and notes about what it can't see.
- **Rollouts** compare each ReplicaSet or ControllerRevision with the one created before it.
- **Rollbacks** reuse an old ReplicaSet. The highest revision that isn't the newest object is a rollback, timed from the Deployment's `Progressing` condition.
- **The skill** tells Claude to report coverage first and call changes leads, not causes.

Key choice: a skill with one standard-library script, not an MCP server. See `decisions/`.
