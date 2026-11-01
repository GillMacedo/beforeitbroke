#!/usr/bin/env python3
"""List what changed around an incident time, from git, GitHub and Kubernetes. Read-only."""

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

EXIT_OK, EXIT_PARTIAL, EXIT_USAGE = 0, 1, 2
COMMAND_TIMEOUT = 60  # seconds; a slow API server shouldn't hang an incident
PAGE_SIZE = 100  # the most GitHub returns per page
TEXT_LIMIT = 160  # keeps one change on one readable line
SUMMARY_LIMIT = 300  # a rollout can touch several containers
FILES_SHOWN = 3
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
DURATION = re.compile(r"^(\d+)([smhd])$")
DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class SourceError(Exception):
    """A source couldn't be read. Reported in coverage, never hidden."""


@dataclass
class Change:
    time: datetime
    source: str
    kind: str
    summary: str
    ref: str = ""


@dataclass
class Coverage:
    source: str
    status: str = "ok"
    changes: int = 0
    notes: list = field(default_factory=list)


def parse_time(text):
    if text == "now":
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{text} has no timezone. Add one, like 2026-11-01T14:32:00Z or 11:32:00-03:00")
    return parsed.astimezone(timezone.utc)


def parse_duration(text):
    match = DURATION.match(text)
    if not match:
        raise ValueError(f"{text} isn't a duration. Use a form like 90s, 30m, 2h or 1d")
    return timedelta(seconds=int(match.group(1)) * DURATION_SECONDS[match.group(2)])


def clean(text, limit=TEXT_LIMIT):
    """Flatten text from outside so it can't fake extra lines or terminal escapes."""
    text = CONTROL_CHARS.sub(" ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def run(command):
    if shutil.which(command[0]) is None:
        raise SourceError(f"{command[0]} isn't installed")
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as error:
        raise SourceError(f"{command[0]} timed out after {COMMAND_TIMEOUT}s") from error
    if done.returncode != 0:
        raise SourceError(clean(done.stderr or f"{command[0]} exited {done.returncode}"))
    return done.stdout


def run_json(command):
    try:
        return json.loads(run(command))
    except json.JSONDecodeError as error:
        raise SourceError(f"{command[0]} returned something other than JSON") from error


def read_time(text):
    return parse_time(text) if text else None


# --- git ---


def git_changes(repo, start, end):
    source = f"git:{repo}"
    base = ["git", "-C", repo]
    log = run(base + ["log", f"--since={start.isoformat()}", f"--until={end.isoformat()}",
                      "--date=iso-strict", "--name-only", "--format=%x01%H%x00%cI%x00%an%x00%s"])
    changes = []
    for entry in log.split("\x01")[1:]:
        header, _, files = entry.partition("\n")
        sha, when, author, subject = header.split("\x00", 3)
        names = [name for name in files.splitlines() if name]
        shown = ", ".join(clean(name, 60) for name in names[:FILES_SHOWN])
        more = f" +{len(names) - FILES_SHOWN} more" if len(names) > FILES_SHOWN else ""
        where = f" [{shown}{more}]" if names else ""
        changes.append(Change(parse_time(when), source, "commit", f"{clean(subject)} ({clean(author, 40)}){where}", sha[:12]))
    tags = run(base + ["for-each-ref", "refs/tags", "--format=%(creatordate:iso-strict)%00%(refname:short)"])
    for line in tags.splitlines():
        when, _, name = line.partition("\x00")
        if when and start <= parse_time(when) <= end:
            changes.append(Change(parse_time(when), source, "tag", clean(name)))
    return changes, ["Commit times show when code was committed, not when it was deployed."]


# --- GitHub ---


def github_list(repo, path, notes, time_field, start):
    """Read one page, newest first, and warn if the window could continue past it."""
    separator = "&" if "?" in path else "?"
    items = run_json(["gh", "api", f"repos/{repo}/{path}{separator}per_page={PAGE_SIZE}"])
    oldest = read_time(items[-1].get(time_field)) if len(items) == PAGE_SIZE else None
    if oldest and oldest >= start:
        notes.append(f"{path.split('?')[0]}: read only the newest {PAGE_SIZE}; older items in the window may be missing.")
    return items


def github_changes(repo, start, end):
    source = f"github:{repo}"
    changes, notes = [], []

    deployments = github_list(repo, "deployments", notes, "created_at", start)
    for item in deployments:
        when = read_time(item.get("created_at"))
        if when is None or not start <= when <= end:
            continue
        statuses = run_json(["gh", "api", f"repos/{repo}/deployments/{item['id']}/statuses?per_page=1"])
        state = statuses[0].get("state", "unknown") if statuses else "no status"
        summary = f"{clean(item.get('environment', '?'), 40)}: {clean(item.get('ref', '?'), 60)} ({state})"
        changes.append(Change(when, source, "deployment", summary, str(item.get("sha", ""))[:12]))

    releases = github_list(repo, "releases", notes, "published_at", start)
    for item in releases:
        when = read_time(item.get("published_at"))
        if when and start <= when <= end:
            changes.append(Change(when, source, "release", clean(item.get("name") or item.get("tag_name")), clean(item.get("tag_name", ""), 40)))

    pulls = github_list(repo, "pulls?state=closed&sort=updated&direction=desc", notes, "updated_at", start)
    for item in pulls:
        when = read_time(item.get("merged_at"))
        if when and start <= when <= end:
            author = (item.get("user") or {}).get("login", "?")
            changes.append(Change(when, source, "merged-pr", f"#{item['number']} {clean(item.get('title', ''))} ({clean(author, 40)})", str(item.get("merge_commit_sha") or "")[:12]))
    return changes, notes


# --- Kubernetes ---


def pod_spec_diff(old, new):
    """Say what changed between two pod specs. Env values are compared but never shown."""
    old_containers = {c.get("name"): c for c in old.get("containers", []) + old.get("initContainers", [])}
    new_containers = {c.get("name"): c for c in new.get("containers", []) + new.get("initContainers", [])}
    parts = []
    for name in sorted(set(old_containers) | set(new_containers)):
        before, after = old_containers.get(name), new_containers.get(name)
        if before is None:
            parts.append(f"{name}: added ({after.get('image')})")
        elif after is None:
            parts.append(f"{name}: removed")
        else:
            parts.extend(_container_diff(name, before, after))
    return "; ".join(parts) or "pod template changed, no image, env, resource or command difference found"


def _container_diff(name, before, after):
    parts = []
    if before.get("image") != after.get("image"):
        parts.append(f"{name}: image {before.get('image')} -> {after.get('image')}")
    old_env = {item.get("name"): item for item in before.get("env", [])}
    new_env = {item.get("name"): item for item in after.get("env", [])}
    changed = sorted(key for key in set(old_env) | set(new_env) if old_env.get(key) != new_env.get(key))
    if changed:
        parts.append(f"{name}: env changed: {', '.join(changed)} (values hidden)")
    if before.get("resources") != after.get("resources"):
        parts.append(f"{name}: resources changed")
    if before.get("command") != after.get("command") or before.get("args") != after.get("args"):
        parts.append(f"{name}: command or args changed")
    return parts


def images(spec):
    return ", ".join(f"{c.get('name')}={c.get('image')}" for c in spec.get("containers", []))


def owner_of(item, kind):
    for owner in item.get("metadata", {}).get("ownerReferences", []):
        if owner.get("kind") == kind:
            return owner.get("name")
    return None


def rollout_changes(source, items, kind, revision_of, spec_of, start, end, progress_times=None):
    """Find revisions created in the window, and rollbacks, which reuse an old revision.

    A rollback gives an old object the highest revision number without changing its
    creation time, so its real time comes from progress_times when the caller has one.
    """
    groups = {}
    for item in items:
        name = owner_of(item, kind)
        if name:
            groups.setdefault((item["metadata"].get("namespace", ""), name), []).append(item)
    changes, seen = [], set()
    for (namespace, name), revisions in groups.items():
        by_created = sorted(revisions, key=lambda item: (item["metadata"].get("creationTimestamp", ""), revision_of(item)))
        for index, item in enumerate(by_created):
            created = read_time(item["metadata"].get("creationTimestamp"))
            if created is None or not start <= created <= end:
                continue
            spec = spec_of(item)
            what = pod_spec_diff(spec_of(by_created[index - 1]), spec) if index else f"oldest revision on record ({images(spec)})"
            summary = clean(f"{kind} {namespace}/{name}: {what}", SUMMARY_LIMIT)
            changes.append(Change(created, source, "rollout", summary, f"revision {revision_of(item)}"))
            seen.add((namespace, name))
        by_revision = sorted(revisions, key=revision_of)
        current = by_revision[-1]
        when = (progress_times or {}).get((namespace, name))
        if current is not by_created[-1] and when and start <= when <= end:
            what = pod_spec_diff(spec_of(by_revision[-2]), spec_of(current))
            summary = clean(f"{kind} {namespace}/{name}: rolled back to an earlier revision: {what}", SUMMARY_LIMIT)
            changes.append(Change(when, source, "rollback", summary, f"revision {revision_of(current)}"))
            seen.add((namespace, name))
    return changes, seen


def _rs_revision(item):
    value = item["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0")
    return int(value) if value.isdigit() else 0


def _rs_spec(item):
    return item.get("spec", {}).get("template", {}).get("spec", {})


def _cr_spec(item):
    return item.get("data", {}).get("spec", {}).get("template", {}).get("spec", {})


def kube_changes(args, start, end):
    base = ["kubectl"] + (["--context", args.context] if args.context else [])
    context = args.context or run(base + ["config", "current-context"]).strip()
    source = f"kubernetes:{context}"
    scopes = [["-n", name] for name in args.namespace] or [["-A"]]
    notes = [f"Cluster context: {clean(context, 80)}.",
             "Secrets aren't read, so a secret change won't appear.",
             "Scale changes aren't tracked. StatefulSet and DaemonSet rollbacks may not appear, because they reuse old revisions and record no time.",
             "Events usually expire after about an hour, and old ReplicaSets are removed at the revision history limit."]
    changes, failures = [], []

    def fetch(resource, *flags):
        items = []
        for scope in scopes:
            try:
                items.extend(run_json(base + ["get", resource, "-o", "json", *flags, *scope]).get("items", []))
            except SourceError as error:
                failures.append(f"{resource}: {error}")
        return items

    deployments = fetch("deployments")
    progress_times = {}
    for item in deployments:
        for condition in item.get("status", {}).get("conditions", []):
            if condition.get("type") == "Progressing" and condition.get("lastUpdateTime"):
                progress_times[(item["metadata"].get("namespace", ""), item["metadata"]["name"])] = read_time(condition["lastUpdateTime"])
    found, moved = rollout_changes(source, fetch("replicasets"), "Deployment", _rs_revision, _rs_spec, start, end, progress_times)
    changes.extend(found)
    for item in deployments:
        metadata = item["metadata"]
        key = (metadata.get("namespace", ""), metadata["name"])
        when = progress_times.get(key)
        if key not in moved and when and start <= when <= end:
            changes.append(Change(when, source, "rollout-activity", clean(f"Deployment {key[0]}/{key[1]} updated with no new ReplicaSet: possibly a restart or a rollback. Running {images(_rs_spec(item))}", SUMMARY_LIMIT)))

    revisions = fetch("controllerrevisions")
    for kind in ("StatefulSet", "DaemonSet"):
        found, _ = rollout_changes(source, revisions, kind, lambda item: item.get("revision", 0), _cr_spec, start, end)
        changes.extend(found)

    for item in fetch("configmaps", "--show-managed-fields"):
        metadata = item["metadata"]
        if metadata["name"] == "kube-root-ca.crt":
            continue
        for managed in metadata.get("managedFields", []):
            when = read_time(managed.get("time"))
            if when and start <= when <= end:
                changes.append(Change(when, source, "configmap", f"{metadata.get('namespace')}/{metadata['name']} written by {clean(managed.get('manager', '?'), 40)} (values hidden)"))

    groups = {}
    for item in fetch("events"):
        if item.get("type") != "Warning":
            continue
        involved = item.get("involvedObject", {})
        when = read_time(item.get("lastTimestamp") or item.get("eventTime") or item["metadata"].get("creationTimestamp"))
        if when is None or not start <= when <= end:
            continue
        key = (involved.get("namespace"), involved.get("kind"), involved.get("name"), item.get("reason"))
        count = item.get("count") or 1
        earliest, total, message = groups.get(key, (when, 0, item.get("message", "")))
        groups[key] = (min(earliest, when), total + count, message)
    for (namespace, kind, name, reason), (when, count, message) in groups.items():
        changes.append(Change(when, source, "warning", clean(f"{kind} {namespace}/{name} {reason} x{count}: {clean(message)}", SUMMARY_LIMIT)))

    notes.extend(f"Couldn't read {clean(failure, 200)}" for failure in failures)
    return changes, notes, bool(failures), source


# --- output ---


def offset(when, at):
    seconds = int((when - at).total_seconds())
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    if seconds < 60:
        return f"{sign}{seconds}s"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{sign}{hours}h{minutes:02d}m" if hours else f"{sign}{minutes}m"


def stamp(when):
    return when.strftime("%Y-%m-%d %H:%M:%S UTC")


def render_text(report, at, start, end, changes, shown):
    lines = [f"Changes around {stamp(at)} (window {stamp(start)} to {stamp(end)})", "", "Coverage:"]
    for item in report:
        lines.append(f"  {item.source}: {item.status}, {item.changes} found")
        lines.extend(f"    {note}" for note in item.notes)
    lines += ["", "Timeline:" if shown else "Timeline: nothing found in the sources that could be read."]
    for change in shown:
        ref = f" {change.ref}" if change.ref else ""
        lines.append(f"  {offset(change.time, at):>8}  {change.time.strftime('%H:%M:%S')}  {change.kind:<16} {change.summary}{ref}")
    if len(shown) < len(changes):
        lines.append(f"  ...{len(changes) - len(shown)} more outside --max, the furthest from the incident time.")
    lines += ["", "A change close to the incident is a lead to check, not proof of the cause."]
    return "\n".join(lines)


def render_markdown(report, at, start, end, changes, shown):
    lines = [f"## Timeline around {stamp(at)}", "", f"Window: {stamp(start)} to {stamp(end)}", "", "| Offset | Time (UTC) | Source | Kind | Change |", "|---|---|---|---|---|"]
    for change in shown:
        summary = change.summary.replace("|", "\\|")
        ref = f" `{change.ref}`" if change.ref else ""
        lines.append(f"| {offset(change.time, at)} | {change.time.strftime('%H:%M:%S')} | {change.source} | {change.kind} | {summary}{ref} |")
    if len(shown) < len(changes):
        lines += ["", f"{len(changes) - len(shown)} more changes left out by `--max`."]
    lines += ["", "### What this covers", ""]
    for item in report:
        notes = " ".join(item.notes)
        lines.append(f"- **{item.source}:** {item.status}, {item.changes} found. {notes}".rstrip())
    return "\n".join(lines)


def render_json(report, at, start, end, changes, shown):
    rows = [dict(asdict(change), time=change.time.isoformat(), offset=offset(change.time, at)) for change in shown]
    return json.dumps({"at": at.isoformat(), "start": start.isoformat(), "end": end.isoformat(),
                       "coverage": [asdict(item) for item in report], "changes": rows,
                       "left_out": len(changes) - len(shown)}, indent=2)


RENDERERS = {"text": render_text, "markdown": render_markdown, "json": render_json}


def collect(args, start, end):
    changes, report = [], []

    def add(label, read):
        entry = Coverage(label)
        report.append(entry)
        try:
            found, notes = read()
        except (SourceError, KeyError, TypeError, ValueError, AttributeError) as error:
            entry.status, entry.notes = "FAILED", [clean(error, 240)]
            return
        entry.changes, entry.notes = len(found), notes
        changes.extend(found)

    for repo in args.repo:
        add(f"git:{repo}", lambda repo=repo: git_changes(repo, start, end))
    for repo in args.github:
        add(f"github:{repo}", lambda repo=repo: github_changes(repo, start, end))
    if args.kube:
        def read_kube():
            found, notes, partial, source = kube_changes(args, start, end)
            report[-1].source = source
            if partial:
                report[-1].status = "PARTIAL"
            return found, notes
        add("kubernetes", read_kube)
    return changes, report


def build_parser():
    parser = argparse.ArgumentParser(description="List what changed around an incident time. Read-only.")
    parser.add_argument("--at", required=True, help="incident time with a timezone, such as 2026-11-01T14:32:00Z, or 'now'")
    parser.add_argument("--before", default="2h", help="how far back to look (default 2h)")
    parser.add_argument("--after", default="15m", help="how far past the incident time to look (default 15m)")
    parser.add_argument("--repo", action="append", default=[], metavar="PATH", help="local git repo; repeatable")
    parser.add_argument("--github", action="append", default=[], metavar="OWNER/REPO", help="GitHub repo, read with gh; repeatable")
    parser.add_argument("--kube", action="store_true", help="read the cluster with kubectl")
    parser.add_argument("--context", help="kubectl context (default: the current one)")
    parser.add_argument("--namespace", action="append", default=[], help="limit Kubernetes to a namespace; repeatable")
    parser.add_argument("--format", choices=sorted(RENDERERS), default="text")
    parser.add_argument("--max", type=int, default=100, help="most changes to show, closest to the incident time (default 100)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.repo or args.github or args.kube):
        parser.error("choose at least one source: --repo, --github or --kube")
    if args.max < 1:
        parser.error("--max must be at least 1")
    try:
        at = parse_time(args.at)
        start, end = at - parse_duration(args.before), at + parse_duration(args.after)
    except ValueError as error:
        parser.error(str(error))
    changes, report = collect(args, start, end)
    closest = sorted(changes, key=lambda change: abs((change.time - at).total_seconds()))[: args.max]
    shown = sorted(closest, key=lambda change: change.time)
    print(RENDERERS[args.format](report, at, start, end, changes, shown))
    return EXIT_PARTIAL if any(item.status != "ok" for item in report) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
