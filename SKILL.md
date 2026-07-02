---
name: olivia-mode
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "Olivia Mode".
---

# Olivia Mode

Interview the user relentlessly about every aspect of a plan or design until you
reach a shared understanding. Instead of dumping dozens of questions into the
chat, you build a **branching decision tree** of questions and serve them through
a small local web app that the user steps through at their own pace. You watch
the app's output and react in real time — adding follow-ups, pruning questions
the user has already answered elsewhere.

**If a question can be answered by exploring the codebase, explore the codebase
instead of asking it.**

## The pieces

- `scripts/olivia_server.py` — a stdlib-only web server (the whole runtime),
  plus a `sessions` discovery command.
- `references/tree-schema.md` — the session JSON format. **Read this before
  authoring a tree.**
- `~/.olivia-mode/sessions/` — the **known directory** where every session file
  lives (override the root with `OLIVIA_MODE_HOME`). Each file's name encodes the
  working directory it belongs to, and the exact directory is also stored in the
  JSON's `cwd` field — that pairing is what lets a *later* Claude/Codex session
  find and resume this interview. You never pick this path by hand; the `sessions`
  command hands you the right one.

Invoke the script by its **absolute skill path** — your CWD is the user's repo,
not the skill directory.

## Workflow

### 1. Understand & check for an existing session
Work out what plan or design you are interviewing about, from the user's request
plus codebase exploration. Answer anything the code can answer yourself.

Then ask whether an interview already exists for this repo:

```
python3 ~/.claude/skills/olivia-mode/scripts/olivia_server.py sessions --cwd "$PWD"
```

This prints JSON: `matches` (sessions whose stored `cwd` is this directory, each
with `path`, `title`, `answered`/`pending`/`complete` counts) and `newPath` (the
file to use for a fresh session).

- If a match is **not `complete`** and fits the current request, offer to
  **resume** it — skip to step 6 (Resume) with its `path`.
- Otherwise start fresh: use `newPath` as the file for the new session.

### 2. Build the tree (new session only)
Generate as many questions as it takes to reach full understanding. For each:
- Give it 1–2 **recommended answers**, each with a short rationale.
- Anticipate the **follow-up questions** each recommended answer would raise, and
  add them as branch children keyed to that recommendation (`parentId` +
  `parentAnswer`). This is what makes it a decision tree, not a flat list.
- As you write, collect a **`references`** glossary. Add an entry for **any
  identifier, term, or phrase** you use in a question or recommendation that the
  user might not have memorized — `R-18`, `S5`, `Decision #6`, a service/module
  name, a domain term. The web UI turns each occurrence into a hover tooltip, so
  the user never has to go hunting for what a shorthand means. Err on the side of
  adding one: a reference with no matching text is harmless.

Write the tree to the `newPath` from step 1, following `references/tree-schema.md`.
Include `"cwd": "<absolute working directory>"` at the top level so the session
stays tied to this repo, and a top-level `"references"` object with any glossary
entries.

### 3. Launch the server with the `Monitor` tool
The server is a long-running process that emits one `OLIVIA_EVENT` line per
user action. Stream those events with the **`Monitor` tool** — each stdout line
becomes a notification you receive while you keep working. Run the launch
command as the monitor's `command` with `persistent: true` (the watch ends by
itself when the user clicks Done and the server exits). Use the session's
absolute `path` (the `newPath` for a new session, or a match's `path` to resume):

- **command:** `python3 ~/.claude/skills/olivia-mode/scripts/olivia_server.py serve --file <path> --cwd "$PWD" --port 0 2>&1`
- **persistent:** `true`
- **description:** e.g. `olivia interview events`

The first event you get back is:

```
OLIVIA_EVENT {"type":"ready","url":"http://127.0.0.1:PORT/","file":"...","cwd":"...","questions":N}
```

Give the user the `url` and tell them to open it and start answering.

> **Do NOT wait with `sleep`.** Never poll for events with `sleep 45; tail ...`
> or chained sleeps — the harness blocks that. The `Monitor` tool delivers each
> event as a notification; just react when one arrives. (If you ever need to
> wait for a single condition instead, use a Bash `run_in_background` command
> that exits when the condition is true, e.g. `until grep -q done log; do sleep 1; done`.)

### 4. React to events
Notifications arrive on their own schedule (they are events, **not** user
replies). Each user action prints one line:

```
OLIVIA_EVENT {"type":"answer","qid":"q3","choiceId":"r1","custom":"","note":"...","next":"q4"}
```

On each `answer`, decide whether it:
- **raises new follow-ups** → add them, or
- **makes a pending question moot** (already answered here) → resolve it.

Apply every change **through the HTTP API with `curl`** — see below. Between
events you can do other work; the next notification will bring you back.

> **Race-avoidance rule:** while the server is running it is the *only* writer of
> the session file. **Never edit the file directly during a live session.**
> Direct edits are fine only before launch or after shutdown.

### 5. Finish
When you see `OLIVIA_EVENT {"type":"done"}` (the user clicked **Done**) the
server exits and the monitor ends. Read the final session file (the `file` path
from the `ready` event) and use the fully-answered tree to inform the work the
interview was for. Hand the answered tree back to the user.

### 6. Resume
To resume, **skip step 2** and relaunch `serve` on an existing session's `path`
(from the `sessions` discovery in step 1, or one the user names). The index shows
answered/pending status and offers only pending questions. Because sessions live
in the central `~/.olivia-mode/sessions/` directory keyed by `cwd`, this works
even in a brand-new Claude session with no memory of the earlier interview.

## Agent control endpoints (via `curl`)

Use `$URL` from the `ready` event. Bodies are JSON.

**Add questions** — `POST /api/add`. Send `{"questions": [ ... ]}` (or a single
question object). Omit `id` to let the server assign one. To make a follow-up,
set `parentId` + `parentAnswer` (a recommendation id, or `"*"` for any answer):

```bash
curl -s -X POST "$URL/api/add" -H 'Content-Type: application/json' -d '{
  "questions": [{
    "text": "Which Redis eviction policy?",
    "parentId": "q1", "parentAnswer": "r1",
    "recommendations": [{"id":"r1","label":"allkeys-lru","rationale":"good cache default"}]
  }]
}'
```

**Resolve a question** (answered by another) — `POST /api/resolve`:

```bash
curl -s -X POST "$URL/api/resolve" -H 'Content-Type: application/json' \
  -d '{"id":"q5","reason":"answered by q3"}'
```

**Update a question's text/recommendations** — `POST /api/update`:

```bash
curl -s -X POST "$URL/api/update" -H 'Content-Type: application/json' \
  -d '{"id":"q2","text":"revised text","recommendations":[...]}'
```

**Add references** — `POST /api/references`. Merge new glossary entries (e.g.
when a follow-up you just added introduces a fresh term). Send `{"references":
{...}}` mapping identifier → description; existing entries are kept:

```bash
curl -s -X POST "$URL/api/references" -H 'Content-Type: application/json' \
  -d '{"references":{"S5":"Snapshot stage 5","Decision #6":"Whether runs fan out per-client"}}'
```

**Read current state** — `GET /api/state` returns the full tree JSON.

## Event reference

| Event     | Meaning                                             |
| --------- | --------------------------------------------------- |
| `ready`   | Server up; carries `url`, `file`, `cwd`, `questions`.|
| `answer`  | User answered `qid`; carries choice, note, `next`.  |
| `added`   | You added questions; carries new `ids`.             |
| `references` | You added glossary entries; carries the `ids`.   |
| `resolve` | A question was resolved.                            |
| `updated` | A question was edited.                              |
| `done`    | Session over; server is shutting down.              |
