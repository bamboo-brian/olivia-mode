# `olivia-session.json` schema

The interview is a **branching decision tree** stored as a flat list of
questions. Children point at their parent (adjacency), which keeps live-adding
branches and depth-first traversal simple.

## Top level

```json
{
  "title": "Short name of the plan under discussion",
  "cwd": "/Users/you/repos/project",
  "created": "2026-06-30T12:00:00+00:00",
  "updated": "2026-06-30T12:34:00+00:00",
  "questions": [ /* Question objects */ ]
}
```

| Field       | Type   | Notes                                                           |
| ----------- | ------ | --------------------------------------------------------------- |
| `title`     | string | Shown as the page heading.                                      |
| `cwd`       | string | Absolute working directory this session belongs to. Ties the session to its repo for discovery/resume; the server backfills it from `--cwd` if omitted. |
| `created`   | string | ISO-8601. The server fills this if omitted.                     |
| `updated`   | string | ISO-8601. The server re-stamps this on every write.             |
| `questions` | array  | Flat list; nesting is expressed via `parentId`.                 |

Sessions are stored in `~/.olivia-mode/sessions/` (root overridable via
`OLIVIA_MODE_HOME`), one file per interview, named after the `cwd`.

## Question object

```json
{
  "id": "q1",
  "text": "The question to ask",
  "parentId": null,
  "parentAnswer": null,
  "recommendations": [
    { "id": "r1", "label": "Recommended answer", "rationale": "why this" }
  ],
  "status": "pending",
  "answer": null
}
```

| Field             | Type          | Notes                                                                                   |
| ----------------- | ------------- | --------------------------------------------------------------------------------------- |
| `id`              | string        | Unique. Convention `q1`, `q2`, ... The server assigns one if omitted on `/api/add`.     |
| `text`            | string        | The question.                                                                           |
| `parentId`        | string / null | `null` = **root question** (shown on the index). Otherwise the `id` of the parent.      |
| `parentAnswer`    | string / null | Which parent answer **activates** this child: a recommendation `id`, or `"*"` for any.   |
| `recommendations` | array         | 1–2 recommended answers. Each has `id`, `label`, `rationale`.                            |
| `status`          | string        | `pending` \| `answered` \| `resolved` \| `skipped`.                                      |
| `answer`          | object / null | Populated on submit (see below).                                                        |

### Recommendation object

```json
{ "id": "r1", "label": "Use Postgres", "rationale": "Already the team standard" }
```

### Answer object (written by the server on submit)

```json
{ "choiceId": "r1", "customText": "", "note": "extra context from the user" }
```

- `choiceId` is a recommendation `id`, or the literal `"custom"` when the user
  wrote their own answer (then `customText` holds it).
- `note` is the free-text note attached to the chosen answer.

## Branching semantics

- **Roots** (`parentId: null`) appear on the index in array order.
- A child becomes **reachable only after its parent is answered** with a
  matching `parentAnswer` (`"*"` matches any answer). Children of unanswered
  questions, and children keyed to answers the user did not pick, stay hidden.
- **Next question** is computed by a DFS over roots (in order), descending into
  a question's activated children once it is answered. The user is sent to the
  first `pending` question after the one they just answered.

## Worked example

Two roots; the first has a branch that only appears if the user picks `r1`.

```json
{
  "title": "New caching layer",
  "cwd": "/Users/you/repos/project",
  "created": "2026-06-30T12:00:00+00:00",
  "questions": [
    {
      "id": "q1",
      "text": "Where should the cache live?",
      "parentId": null,
      "parentAnswer": null,
      "recommendations": [
        { "id": "r1", "label": "Redis (shared)", "rationale": "Survives restarts; shared across pods" },
        { "id": "r2", "label": "In-process LRU", "rationale": "Zero infra, but per-instance" }
      ],
      "status": "pending",
      "answer": null
    },
    {
      "id": "q2",
      "text": "Which Redis eviction policy?",
      "parentId": "q1",
      "parentAnswer": "r1",
      "recommendations": [
        { "id": "r1", "label": "allkeys-lru", "rationale": "Good default for a cache" }
      ],
      "status": "pending",
      "answer": null
    },
    {
      "id": "q3",
      "text": "What TTL should entries have?",
      "parentId": null,
      "parentAnswer": null,
      "recommendations": [
        { "id": "r1", "label": "5 minutes", "rationale": "Balances freshness and hit-rate" }
      ],
      "status": "pending",
      "answer": null
    }
  ]
}
```

Flow: answer `q1`. If the user picks `r1` (Redis), they are sent to `q2`, then
`q3`. If they pick `r2` (in-process), `q2` is skipped and they go straight to
`q3`.
