#!/usr/bin/env python3
"""PreToolUse gate: *creating* a deliverable must be authorized by olivia-mode.

Only document creation is gated. Writes to a file that already exists on disk
are always allowed -- revising an existing document never needs an interview,
and Edit/MultiEdit only operate on existing files anyway (which is why
hooks.json registers this gate for the Write tool alone).

All policy lives in the plugin. This hook only asks the server:

    olivia_server.py authorize --cwd <cwd> --file <path>

Expected stdout (JSON): {"authorized": bool, "reason": str, ...}

Exit 0 = allow. Exit 2 = block; stderr is fed back to Claude as the reason.

The gate is opt-in per project: it fires only when CLAUDE_PROJECT_DIR (falling
back to the hook input's cwd) is listed in the plugin's `gated_projects` user
config -- the values Claude Code prompts for when the plugin is enabled
(reconfigurable later via /plugin). This hook holds no policy of its own; it
asks the server `gate --cwd <project>` whether the project is gated, then
`authorize` whether a completed interview covers the file. Which interview
authorizes which file is decided by the sessions' `deliverables`.

Registered automatically by the plugin via hooks/hooks.json; no Claude Code
settings.json entry is needed. The script ships inside the plugin, so the
server is found right next to it. Overrides:

    OLIVIA_MODE_SERVER   explicit path to olivia_server.py
    OLIVIA_GATE_PATTERN  regex replacing the default gated-file pattern (\\.md$)
"""

import glob
import json
import os
import re
import subprocess
import sys

# Files that require interview authorization inside a gated project.
GATED = re.compile(os.environ.get("OLIVIA_GATE_PATTERN") or r"\.md$",
                   re.IGNORECASE)

SERVER_ENV = "OLIVIA_MODE_SERVER"
SERVER_GLOBS = [
    # Fallbacks for a copy of this script running outside the plugin tree.
    os.path.expanduser("~/.claude/plugins/**/olivia-mode/scripts/olivia_server.py"),
]


def _sibling_server() -> str:
    """olivia_server.py as shipped alongside this hook in the plugin."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "scripts", "olivia_server.py"))


def deny(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def find_server() -> str | None:
    env = os.environ.get(SERVER_ENV)
    if env and os.path.isfile(os.path.expanduser(env)):
        return os.path.expanduser(env)
    sibling = _sibling_server()
    if os.path.isfile(sibling):
        return sibling
    for pattern in SERVER_GLOBS:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return hits[0]
    return None


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()
    path = data.get("tool_input", {}).get("file_path", "")
    if not GATED.search(path.replace(os.sep, "/")):
        sys.exit(0)

    # Only creation is gated: revising a document that already exists never
    # needs an interview.
    abspath = path if os.path.isabs(path) else os.path.join(cwd, path)
    if os.path.exists(abspath):
        sys.exit(0)

    server = find_server()
    if server is None:
        deny(
            "Blocked: olivia_server.py not found; cannot verify interview "
            f"authorization. Set {SERVER_ENV} in the hook environment, or "
            "tell the user the gate is misconfigured. Do not bypass."
        )

    # Opt-in check: only projects listed in the plugin's gated_projects user
    # config are gated at all. If this check itself breaks, fail open --
    # blocking markdown creation in projects that never opted in is worse
    # than missing one write in a project that did.
    project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    try:
        out = subprocess.run(
            ["python3", server, "gate", "--cwd", project],
            capture_output=True, text=True, timeout=15,
        )
        if not json.loads(out.stdout).get("gated"):
            sys.exit(0)
    except (subprocess.SubprocessError, ValueError):
        sys.exit(0)

    try:
        out = subprocess.run(
            ["python3", server, "authorize", "--cwd", cwd, "--file", path],
            capture_output=True, text=True, timeout=15,
        )
        verdict = json.loads(out.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        deny(
            f"Blocked: authorization check failed ({exc}). Ask the user how "
            "to proceed; do not bypass this gate."
        )

    if not verdict.get("authorized"):
        deny("Blocked: " + verdict.get(
            "reason",
            "no completed Olivia interview authorizes this document. Run "
            "the olivia-mode interview whose tree declares this path in "
            "its deliverables, then retry.",
        ))

    sys.exit(0)


if __name__ == "__main__":
    main()
