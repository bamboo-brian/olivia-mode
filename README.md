# Olivia Mode

A Claude Code / Codex **skill** for interviewing you about a plan or design until
you reach a shared understanding — without dumping a hundred questions into the
chat.

The agent builds a **branching decision tree** of questions (each with 1–2
recommended answers and a rationale), then launches a tiny local web app you step
through at your own pace. As you answer, the agent watches the app's output and
reacts in real time: adding follow-up questions that your answers raise, and
resolving questions you've already answered elsewhere.

## How it works

1. The agent authors `./olivia-session.json` — the decision tree
   (see [`references/tree-schema.md`](references/tree-schema.md)).
2. It runs the server in the background:
   ```
   python3 ~/.claude/skills/olivia-mode/scripts/olivia_server.py --file ./olivia-session.json --port 0
   ```
3. You open the printed `http://127.0.0.1:PORT/` URL and answer questions one at
   a time — picking a recommended answer or writing your own, with an optional
   note on each.
4. The server is the **sole writer** of the JSON file while it runs. The agent
   makes changes through HTTP control endpoints (`/api/add`, `/api/resolve`,
   `/api/update`) so the two never race on the file.
5. Click **Done** to end the session and shut the server down. Your answers are
   in `olivia-session.json`.

## Resuming

Point the agent at an existing `olivia-session.json` to pick up where you left
off — only unanswered questions are offered.

## Requirements

Python 3 standard library only. No dependencies to install.

## Layout

```
SKILL.md                    agent instructions (the skill entry point)
scripts/olivia_server.py    the web server / runtime
references/tree-schema.md    session JSON format + worked example
```
