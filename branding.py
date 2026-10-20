"""Best-effort terminal identity for the operator; never part of run evidence."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from triglav_art import art_lines

RESET = "\x1b[0m"
GOLD = "\x1b[38;2;205;170;94m"
MUTED = "\x1b[38;2;160;163;166m"


def clean(value):
    """Keep untrusted Git/config text on one ASCII terminal line."""
    return re.sub(r"[^ -~]", "?", str(value))


def git_identity(repo):
    def query(*args):
        try:
            return clean(subprocess.run(
                ["git", "-C", str(repo), *args], capture_output=True,
                encoding="utf-8", errors="replace", timeout=1, check=True,
            ).stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return "unknown"
    branch = query("symbolic-ref", "--quiet", "--short", "HEAD")
    commit = query("rev-parse", "--short=7", "HEAD")
    return ("detached" if branch == "unknown" and commit != "unknown" else branch, commit)


def runtime_info(config, *, critic=False, reviewer=False):
    roles = config.get("roles", {})
    metadata = config.get("model_metadata", {})
    active = {"code": True, "critic": critic, "review": reviewer}
    names = {}
    for role, enabled in active.items():
        alias = roles.get(role)
        names[role] = clean(metadata.get(alias, {}).get("display_name", alias)) if enabled and alias else "OFF"
    count = sum(enabled and bool(roles.get(role)) for role, enabled in active.items())
    mode = {1: "SINGLE-MODEL", 2: "TWO-MODEL", 3: "THREE-MODEL"}.get(count, "UNCONFIGURED")
    return mode, names


def render(config, branch, commit, *, width, height, color, critic=False,
           reviewer=False, dry_run=False):
    """Fit within the viewport, reserving the final column and prompt row."""
    mode, names = runtime_info(config, critic=critic, reviewer=reviewer)
    status = "DRY RUN" if dry_run else "STARTING"
    columns, rows = max(0, width - 1), max(0, height - 1)
    if not columns or not rows:
        return ""
    if not color or columns < 77 or rows < 31:
        lines = [f"TRIGLAV | {mode} | {status}",
                 f"BRANCH {clean(branch)} | COMMIT {clean(commit)}",
                 f"EXECUTOR {names['code']}",
                 f"CRITIC {names['critic']} | REVIEWER {names['review']}"]
        return "\n".join(line[:columns] for line in lines[:rows]) + "\n"

    right_width = columns - 43
    def fit(text, limit=right_width):
        return text if len(text) <= limit else text[:limit - 3] + "..."
    def field(label, value):
        return MUTED + f"{label:<12}" + RESET + fit(clean(value), right_width - 12)
    info = [GOLD + "TRIGLAV" + RESET,
            GOLD + "THREE HEADS. ONE VERDICT." + RESET,
            MUTED + "Local AI Engineering Harness" + RESET, "",
            field("BRANCH", branch), field("COMMIT", commit), field("MODE", mode), "",
            field("EXECUTOR", names["code"]), field("CRITIC", names["critic"]),
            field("REVIEWER", names["review"]), "", field("STATUS", status)]
    # The supplied renderer resets both foreground and background on every row.
    return "\n".join(line + "   " + (info[i] if i < len(info) else "")
                     for i, line in enumerate(art_lines())) + RESET + "\n"


def truecolor_terminal(stream, env):
    if "NO_COLOR" in env or env.get("TERM") == "dumb":
        return False
    try:
        "\u2580".encode(stream.encoding or "ascii")
    except (LookupError, UnicodeError):
        return False
    return bool(env.get("WT_SESSION") or env.get("TERM_PROGRAM") == "vscode"
                or env.get("COLORTERM", "").lower() in {"truecolor", "24bit"})


def show_splash(repo, config, *, critic=False, reviewer=False, dry_run=False,
                quiet=False, stream=None):
    """One call per operator invocation; failure cannot affect controller status."""
    stream = sys.stderr if stream is None else stream
    try:
        if quiet or not stream.isatty():
            return
        try:
            size = os.get_terminal_size(stream.fileno())
        except (OSError, ValueError):
            size = os.terminal_size((80, 24))
        branch, commit = git_identity(repo)
        text = render(config, branch, commit, width=size.columns, height=size.lines,
                      color=truecolor_terminal(stream, os.environ), critic=critic,
                      reviewer=reviewer, dry_run=dry_run)
        stream.write(text)
        stream.flush()
    except Exception:
        # A broken terminal, malformed display metadata or missing Git is cosmetic.
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview the TRIGLAV operator splash (no models)")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--critic", action="store_true")
    parser.add_argument("--reviewer", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        # No config, Git or artwork work is needed for captured or quiet output.
        if not args.quiet and sys.stderr.isatty():
            config = json.loads(args.config.read_text(encoding="utf-8-sig"))
            show_splash(args.repo, config, critic=args.critic, reviewer=args.reviewer,
                        dry_run=args.dry_run, quiet=args.quiet)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
