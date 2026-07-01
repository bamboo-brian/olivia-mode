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

- `scripts/olivia_server.py` — a stdlib-only web server (the whole runtime).
- `references/tree-schema.md` — the `olivia-session.json` format. **Read this
  before authoring a tree.**
- `./olivia-session.json` — the session file, written in the user's current
  working directory. Also the resume target.

Invoke the script by its **absolute skill path** — your CWD is the user's repo,
not the skill directory:

```
python3 ~/.claude/skills/olivia-mode/scripts/olivia_server.py --file ./olivia-session.json --port 0
```

## Workflow

### 1. Understand
Work out what plan or design you are interviewing about, from the user's request
plus codebase exploration. Answer anything the code can answer yourself.

### 2. Build the tree (new session only)
Generate as many questions as it takes to reach full understanding. For each:
- Give it 1–2 **recommended answers**, each with a short rationale.
- Anticipate the **follow-up questions** each recommended answer would raise, and
  add them as branch children keyed to that recommendation (`parentId` +
  `parentAnswer`). This is what makes it a decision tree, not a flat list.

Write the tree to `./olivia-session.json` following `references/tree-schema.md`.

### 3. Launch the server with the `Monitor` tool
The server is a long-running process that emits one `OLIVIA_EVENT` line per
user action. Stream those events with the **`Monitor` tool** — each stdout line
becomes a notification you receive while you keep working. Run the launch
command as the monitor's `command` with `persistent: true` (the watch ends by
itself when the user clicks Done and the server exits):

- **command:** `python3 ~/.claude/skills/olivia-mode/scripts/olivia_server.py --file ./olivia-session.json --port 0 2>&1`
- **persistent:** `true`
- **description:** e.g. `olivia interview events`

The first event you get back is:

```
OLIVIA_EVENT {"type":"ready","url":"http://127.0.0.1:PORT/","file":"...","questions":N}
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
> `olivia-session.json`. **Never edit the file directly during a live session.**
> Direct edits are fine only before launch or after shutdown.

### 5. Finish
When you see `OLIVIA_EVENT {"type":"done"}` (the user clicked **Done**) the
server exits and the monitor ends. Read the final
`./olivia-session.json` and use the fully-answered tree to inform the work the
interview was for. Hand the answered tree back to the user.

### 6. Resume
If the user points at an existing `olivia-session.json`, **skip step 2** and just
relaunch the server on that file. The index shows answered/pending status and
offers only pending questions.

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

**Read current state** — `GET /api/state` returns the full tree JSON.

## Event reference

| Event     | Meaning                                             |
| --------- | --------------------------------------------------- |
| `ready`   | Server up; carries `url`, `file`, `questions`.      |
| `answer`  | User answered `qid`; carries choice, note, `next`.  |
| `added`   | You added questions; carries new `ids`.             |
| `resolve` | A question was resolved.                            |
| `updated` | A question was edited.                              |
| `done`    | Session over; server is shutting down.              |
