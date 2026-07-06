# Olivia Mode

Named after the very best question asker I know.

A Claude Code **plugin** for interviewing you about a plan or design until you
reach a shared understanding — without dumping a hundred questions into the
chat. Alongside the interview skill it ships a **Planning Facilitator** output
style and a **PreToolUse write gate** that together keep an agent from drafting
deliverable documents it never earned the answers for.

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

## Deliverables and the write gate

Every interview tree declares, up front, the deliverable path(s) it authorizes
— a `deliverables` list of paths or globs relative to the repo, e.g.
`["docs/specs/payments-ledger.md"]` or `["docs/plans/auth-*.md"]`. Declaring it
at tree-authoring time is deliberate: the binding is part of the plan, not
something the agent retro-fits afterwards to satisfy the gate.

The plugin registers a **PreToolUse hook** (`hooks/olivia_gate.py`) on the
`Write` tool. The gate is **opt-in per project**: when you enable the plugin,
Claude Code prompts for a **Gated projects** list (the plugin's
`gated_projects` user config — change it any time via `/plugin`). Only
projects whose directory (`CLAUDE_PROJECT_DIR`) is on that list are gated;
that setting is the single source of truth, and you can check any directory
against it with:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/olivia_server.py" gate --cwd "$PWD"
```

The Planning Facilitator output style has the agent run that check when
planning work starts, and tell you if the current project isn't gated — so
discovery doesn't depend on reading this README.

Projects not on the list are never touched. Inside a listed project,
**creating any markdown file** is gated (override the file pattern with
`OLIVIA_GATE_PATTERN`). Modifying a document that already exists is always
allowed — revision never needs an interview, only bringing a new deliverable
into existence does. On each gated creation the hook asks the server:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/olivia_server.py" authorize --cwd "$PWD" --file <path>
```

which answers one question — does any **completed** session's `deliverables`
match this path?

```json
{"authorized": true, "session": "~/.olivia-mode/sessions/...json", "title": "Payments ledger spec"}
{"authorized": false, "reason": "matched 'Payments ledger spec' but 3 questions pending; ..."}
{"authorized": false, "reason": "no session declares this path; author a tree with a deliverables entry ..."}
```

An unauthorized write is blocked, and the `reason` is fed back to the agent as
its recovery instruction. If two completed sessions match one path, the most
recently completed wins (the others are listed as `alternates`).

The **Planning Facilitator** output style (`output-styles/planning-facilitator.md`)
is the other half: it directs the agent to treat Olivia interviews as its
primary elicitation mechanism and never to fill unstated requirements with
assumptions. Select it via `/config` → **Output style**, or set
`"outputStyle": "Planning Facilitator"` in the project's
`.claude/settings.local.json`. You don't have to remember to: a SessionStart
hook (`hooks/olivia_style_nudge.py`) notices when a session starts in a
gated project without the style selected and has the agent offer to enable
it — and stays silent everywhere else, or once the project has made an
explicit style choice.

The interview skill itself still works standalone — an interview run purely
for shared understanding needs no document at all and declares
`"deliverables": []`.

## Requirements

Python 3 standard library only. No dependencies to install.

## Layout

```
SKILL.md                                   agent instructions (the skill entry point)
scripts/olivia_server.py                   the web server / runtime + `sessions`, `authorize`, `gate`
references/tree-schema.md                  session JSON format + worked example
hooks/hooks.json                           registers the plugin's hooks
hooks/olivia_gate.py                       PreToolUse gate: blocks unauthorized document creation
hooks/olivia_style_nudge.py                SessionStart nudge: offers the output style in gated projects
output-styles/planning-facilitator.md      interview-first planning output style
```

Session files are written to `~/.olivia-mode/sessions/` (see
[Where sessions are stored](#where-sessions-are-stored)), outside this repo.
