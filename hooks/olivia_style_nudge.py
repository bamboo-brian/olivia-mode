#!/usr/bin/env python3
"""SessionStart nudge: suggest the Planning Facilitator style in gated projects.

Whatever this prints to stdout is added to Claude's context at session start.
It prints a short note asking Claude to offer enabling the output style, and
only when all of these hold:

  - the project is opted into the write gate (per `olivia_server.py gate`,
    the same single source of truth the PreToolUse gate uses), and
  - the Planning Facilitator style is not already selected in the project's
    or user's settings, and the project has not explicitly chosen a
    different style (an explicit choice is respected, not nagged), and
  - the session is a fresh context (startup or /clear) -- resumed and
    compacted sessions already carry the earlier nudge.

Everything here is best-effort: any failure exits silently rather than
disturbing session start.
"""

import json
import os
import subprocess
import sys

STYLE_NAME = "Planning Facilitator"


def _server() -> "str | None":
    env = os.environ.get("OLIVIA_MODE_SERVER")
    if env and os.path.isfile(os.path.expanduser(env)):
        return os.path.expanduser(env)
    here = os.path.dirname(os.path.abspath(__file__))
    sibling = os.path.normpath(os.path.join(here, "..", "scripts", "olivia_server.py"))
    return sibling if os.path.isfile(sibling) else None


def _output_style(path: str) -> "str | None":
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("outputStyle")
    except (OSError, ValueError):
        return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}

    if data.get("source") not in (None, "startup", "clear"):
        return

    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()

    project_styles = [
        _output_style(os.path.join(project, ".claude", "settings.local.json")),
        _output_style(os.path.join(project, ".claude", "settings.json")),
    ]
    user_style = _output_style(os.path.expanduser("~/.claude/settings.json"))
    if STYLE_NAME in project_styles or user_style == STYLE_NAME:
        return
    if any(project_styles):  # a different style, chosen explicitly -- respect it
        return

    server = _server()
    if server is None:
        return
    try:
        out = subprocess.run(
            ["python3", server, "gate", "--cwd", project],
            capture_output=True, text=True, timeout=15,
        )
        if not json.loads(out.stdout).get("gated"):
            return
    except (subprocess.SubprocessError, ValueError):
        return

    print(
        "This project is opted into the olivia-mode write gate: creating a "
        "markdown document requires a completed Olivia interview that "
        "declares it as a deliverable. The '%s' output style is built for "
        "that workflow but is not currently selected. Early in the session, "
        "briefly let the user know and offer to enable it by setting "
        '"outputStyle": "%s" in this project\'s .claude/settings.local.json '
        "(they can also pick it via /config -> Output style; either way it "
        "takes effect after /clear or a new session). If they decline, drop "
        "the subject for the rest of the session." % (STYLE_NAME, STYLE_NAME)
    )


if __name__ == "__main__":
    main()
