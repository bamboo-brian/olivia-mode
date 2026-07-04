# Olivia Mode

Named after the very best question asker I know.

A Claude Code **plugin** (a single skill) for interviewing you about a plan or
design until you reach a shared understanding — without dumping a hundred
questions into the chat.

The agent builds a **branching decision tree** of questions (each with 1–2
recommended answers and a rationale), then launches a tiny local web app you step
through at your own pace. As you answer, the agent watches the app's output and
reacts in real time: adding follow-up questions that your answers raise, and
resolving questions you've already answered elsewhere.

## How it works

1. The agent checks for an existing interview for your repo and, for a new one,
   authors the decision tree (see
   [`references/tree-schema.md`](references/tree-schema.md)) into a session file
   in the central store.
2. It runs the server in the background:
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/olivia_server.py" serve --file <path> --cwd "$PWD" --port 0
   ```
3. You open the printed `http://127.0.0.1:PORT/` URL and answer questions one at
   a time — picking a recommended answer or writing your own, with an optional
   note on each.
4. The server is the **sole writer** of the JSON file while it runs. The agent
   makes changes through HTTP control endpoints (`/api/add`, `/api/resolve`,
   `/api/update`) so the two never race on the file.
5. Click **Done** to end the session and shut the server down. Your answers are
   saved in the session file.

## Where sessions are stored

Sessions live in a central directory — `~/.olivia-mode/sessions/` by default
(override the root with the `OLIVIA_MODE_HOME` environment variable), **not** in
your repo. Each file is named after the working directory it belongs to and also
records that directory in a `cwd` field, so the interview for a given project can
be found again later.

## Resuming

Because sessions are keyed by working directory, a later run picks up where you
left off automatically — even in a brand-new Claude/Codex session. The agent
discovers existing interviews for the current directory with:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/olivia_server.py" sessions --cwd "$PWD"
```

and relaunches the server on the unfinished one. Only unanswered questions are
offered.

## Requirements

Python 3 standard library only. No dependencies to install.

## Layout

```
SKILL.md                    agent instructions (the skill entry point)
scripts/olivia_server.py    the web server / runtime + `sessions` discovery
references/tree-schema.md    session JSON format + worked example
```

Session files are written to `~/.olivia-mode/sessions/` (see
[Where sessions are stored](#where-sessions-are-stored)), outside this repo.
