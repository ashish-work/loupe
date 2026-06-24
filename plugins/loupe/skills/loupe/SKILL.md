---
name: loupe
description: >-
  Run an interactive, human-in-the-loop pull request review. Use this whenever
  the user asks to review a PR, review a pull request, "look at PR #N", review a
  diff or branch before merging, or wants to curate/approve review comments
  before they're posted. loupe opens the agent's findings in a rich browser UI
  where the human accepts, edits, dismisses, and adds line comments, then sends
  the curated review back. Prefer this over dumping review comments into chat
  whenever the user wants to actually act on the review (post to GitHub, request
  changes) or wants control over what gets said. Trigger even if the user does
  not say the word "loupe".
version: 1.0.0
license: Apache-2.0
author:
  name: Ashish Gupta
homepage: https://github.com/ashish-work/loupe
---

# loupe — interactive PR review

loupe turns a one-way "here are my review comments" dump into a feedback loop.
The agent does the analysis; the human curates it in a browser; the curated
review comes back as JSON for the agent to post. The agent's job is the
*intelligence* (read the diff, find real issues). loupe owns the *interface*
(render the diff, collect decisions).

The loop:

1. Fetch the PR's metadata + unified diff.
2. Read the diff and write **findings** — anchored, severity-tagged comments.
3. Assemble a `review.json` and run the loupe CLI. It serves a UI, opens the
   browser, and **blocks** until the human sends the review back.
4. Parse the returned JSON (kept / edited / dismissed findings + the human's own
   comments + an overall verdict).
5. Act on it — post a GitHub review, or summarize, per what the user wants.

## Prerequisites

- **Python 3.8+** (standard library only — nothing to install).
- **`gh` CLI**, authenticated, for fetching/posting GitHub PRs. Not strictly
  required: loupe accepts any unified diff, so a local `git diff` works too
  (e.g. reviewing a branch before it becomes a PR, or a non-GitHub remote).

## Step 1 — Fetch the PR

For a GitHub PR number `N`:

```bash
gh pr view N --json number,title,headRefName,baseRefName,author,url,body
gh pr diff N
```

For a local branch / pre-PR review, build the diff yourself:

```bash
git diff main...HEAD          # or any base...head range
```

Keep the raw diff text — loupe parses it. Do **not** hand-build a structured
representation of the diff; just pass the raw unified diff string.

## Step 2 — Write the findings

Read the diff carefully and identify real issues. Each finding anchors to one
line and carries a severity. **This is where the value is** — be specific,
correct, and grounded in the actual changed code. A few strong findings beat a
wall of nitpicks.

### Anchoring rule (important)

`line` is a line number **from the diff**, and `side` says which side:

- `side: "new"` (default) → `line` is the **new-file** line number. Use this for
  added (`+`) lines and unchanged context lines. This is the common case.
- `side: "old"` → `line` is the **old-file** line number. Use only when
  commenting on a removed (`-`) line.

Only anchor to lines that actually appear in the diff. Don't invent line
numbers — if a finding is about code outside the hunks, either widen the diff
(`gh pr diff N` shows full hunks) or put it in the overall `notes` instead.

### Severities

Pick the one that reflects real impact, not effort:

| severity   | use for |
|------------|---------|
| `critical` | bugs that will break prod, data loss, security holes |
| `high`     | correctness/robustness issues that should block merge |
| `medium`   | real problems worth fixing but not blockers |
| `low`      | minor improvements, smells |
| `nit`      | style/preference, non-blocking |
| `praise`   | call out genuinely good choices (use sparingly, it builds trust) |

### review.json schema

ALWAYS use this exact structure:

```json
{
  "pr": {
    "number": 482,
    "title": "Add retry with exponential backoff",
    "repo": "owner/repo",
    "author": "username",
    "branch": "feat/retry-backoff",
    "base": "main",
    "url": "https://github.com/owner/repo/pull/482"
  },
  "summary": "One short paragraph: what the PR does and your overall take.",
  "diff": "<the raw unified diff text>",
  "findings": [
    {
      "id": "f1",
      "file": "gateway/client.go",
      "line": 28,
      "side": "new",
      "severity": "high",
      "title": "Short headline for the issue",
      "body": "The actual review comment. Explain the problem and the fix.",
      "suggestion": "optional replacement code shown in a code block"
    }
  ],
  "structure": {
    "...": "optional — see Step 2b"
  },
  "diagrams": [
    { "id": "d1", "title": "...", "kind": "architecture", "mermaid": "flowchart LR\n  ..." }
  ]
}
```

Field notes: `id` must be unique per finding (`f1`, `f2`, …) — it's how
decisions map back. `repo` is `owner/repo` (needed later for posting). `summary`
and `suggestion` are optional but `summary` is worth writing. `structure` is
optional (Step 2b); `diagrams` is optional (Step 2c). Write `review.json` to a
temp path (e.g. `/tmp/loupe-review-N.json`).

## Step 2b — Model the structural changes (the UML view)

Whenever the PR changes the **shape** of the code — adds/removes a class, struct,
interface, enum, or service; adds/removes/changes the signature of a method or
field; or rewires relationships between types — include a `structure` block.
loupe renders it as a UML class diagram above the diff, coloring **whole classes
and individual members** by change status (added = green, modified = amber,
removed = red, struck). This is the "meta level" overview a line diff can't give:
the reviewer sees "a new `RetryPolicy` class appeared and `Client` grew a field
and changed `Do`" at a glance, can click any class to jump to its diff, and can
leave architecture comments on a class.

This is your judgment call — you understand the code semantics. Don't try to
reconstruct the whole codebase; model only the types the PR touches (plus any
unchanged type needed to make a relationship readable).

```json
"structure": {
  "diagram_title": "optional one-line caption",
  "classes": [
    {
      "name": "RetryPolicy",
      "file": "gateway/retry.go",
      "line": 10,
      "change": "added",
      "stereotype": "struct",
      "attributes": [
        {"name": "maxAttempts int", "change": "added"},
        {"name": "baseDelay time.Duration", "change": "added"}
      ],
      "methods": [
        {"name": "Backoff(attempt) Duration", "change": "added"},
        {"name": "ShouldRetry(resp, err) bool", "change": "added"}
      ]
    },
    {
      "name": "Client",
      "file": "gateway/client.go",
      "line": 8,
      "change": "modified",
      "stereotype": "struct",
      "attributes": [
        {"name": "transport http.RoundTripper"},
        {"name": "retry *RetryPolicy", "change": "added"}
      ],
      "methods": [
        {"name": "Do(req) (*Response, error)", "change": "modified"}
      ]
    }
  ],
  "relations": [
    {"from": "Client", "to": "RetryPolicy", "type": "composes"}
  ]
}
```

Rules that make the diagram useful:

- **`change` on a class** is one of `added` | `modified` | `removed` |
  `unchanged`. Use `modified` for an existing class whose members changed — and
  then mark the specific members. The whole point of the view is showing *what*
  changed inside an existing class, so don't just mark the class `modified` and
  leave its members unmarked.
- **`change` on a member** is the same four values; omit it (or use `unchanged`)
  for members that didn't change. `name` is a freeform signature string —
  language-agnostic, formatted however reads best (`maxAttempts int`,
  `+ getName(): String`, `def backoff(self, attempt)` …). Keep it short.
- **`file` + `line`** (optional) is the class's declaration site on the **new**
  side. If that line is in the diff, clicking the class scrolls to it; otherwise
  it scrolls to the file. Include it when you can — it's what links the meta view
  to the diff.
- **`stereotype`** (optional) is the `«…»` tag shown above the name: `class`,
  `struct`, `interface`, `enum`, `service`, `component`, etc.
- **`relations`** draw edges between classes by `name`. `type` is one of:
  `extends` (solid + hollow triangle), `implements` (dashed + hollow triangle),
  `uses`/`depends` (dashed + arrow), `composes` (solid + filled diamond),
  `aggregates` (solid + hollow diamond). Add a `"label"` to override the default
  edge label. Both endpoints must match a class `name` in `classes`.

## Step 2c — Offer overview diagrams (the meta view)

A UML class diagram (Step 2b) only fits PRs that change **types** — classes,
interfaces, signatures, relationships. Most PRs (feature work, integrations,
config, flows) don't, and the reviewer still wants to grasp the *idea* before the
line diff. For those, offer **Mermaid overview diagrams**: loupe renders them as
pictures at the top of the review (Mermaid is lazy-loaded from CDN and falls back
to showing the source offline, so a missing network is never fatal).

**Ask first — do NOT author diagrams unprompted.** Use the **AskUserQuestion**
tool to ask which (if any) the reviewer wants — multi-select — then author ONLY
the chosen ones, grounded in the actual diff. Offer these options:

- **Architecture overlay** — system boxes (frontend, API, DB, external services)
  with the nodes/edges this PR added or changed highlighted. Best for PRs that
  wire in services or move data. *(usually the most useful)*
- **Capability map** — the PR's changes grouped by user-facing capability, not by
  folder. Best as a "what's in this PR" index for a broad batch.
- **User-journey flow** — the affected flow end-to-end with a badge on each step
  the PR changed. Best when a PR changes a *flow* rather than adding features.
- **None** — skip diagrams (e.g. a pure refactor better served by the UML view).

Author each chosen diagram as a Mermaid graph and add it to `diagrams`:

```json
"diagrams": [
  {
    "id": "d1",
    "title": "Architecture — new external integrations",
    "kind": "architecture",
    "mermaid": "flowchart LR\n  user([User]) --> fe[Frontend] --> api[API]\n  api -->|new| sms[[SMS]]:::added\n  classDef added fill:#d7f5dd,stroke:#1a7f37\n  classDef changed fill:#fff3cd,stroke:#9a6700"
  }
]
```

Rules that make these useful:

- `mermaid` is a raw Mermaid graph string (`\n` between lines). Keep it small and
  readable — a handful of nodes, not the whole system. Ground every node and edge
  in real files/services from the diff; don't invent.
- Color what the PR touched with `classDef added`/`changed`/`removed` + `:::added`
  on nodes (green = added, amber = changed, red = removed) — that overlay is the
  whole point of the meta view.
- NO emojis in labels (words/icons only); keep labels short.
- Author **1–3** diagrams max. `diagrams` and `structure` are complementary: use
  `structure` (UML) for type-shape changes, `diagrams` for the system/feature
  idea. A feature/integration PR usually wants `diagrams`; a refactor wants
  `structure`; a big PR can have both.
- A broken graph degrades to showing its Mermaid source (never blocks the
  review), but sanity-check syntax — balanced `subgraph`/`end`, every `:::class`
  has a matching `classDef`, endpoints exist.

## Step 3 — Run loupe

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/loupe.py /tmp/loupe-review-N.json
```

It prints the UI URL to **stderr**, opens the browser, and blocks. Tell the user
something like: "Opened the review in your browser — accept, edit, or dismiss
each finding, add your own line comments, pick a verdict, and hit **Send review
to agent**." Then wait for the command to return.

Flags: `--no-open` (don't launch a browser — print the URL instead, useful on
remote/headless boxes; the user opens it manually or via a forwarded port),
`--port N` (preferred port, default 7842; auto-bumps if taken), `--timeout S`
(give up after S seconds; default `0` = wait indefinitely).

The curated review is printed to **stdout** as JSON. Exit codes:

- `0` — review submitted (stdout has the payload)
- `2` — timed out
- `3` — user cancelled
- `1` — bad input (e.g. malformed review.json)

## Step 4 — Read the returned review

stdout JSON shape:

```json
{
  "status": "submitted",
  "verdict": "request_changes",
  "notes": "overall reviewer notes for the PR",
  "findings": [
    {"id": "f1", "decision": "accept", "body": "final text"},
    {"id": "f2", "decision": "edit",   "body": "human-reworded text"},
    {"id": "f3", "decision": "dismiss"}
  ],
  "added_comments": [
    {"file": "gateway/client.go", "line": 35, "side": "new", "body": "human's own comment"}
  ],
  "structure_comments": [
    {"class": "RetryPolicy", "file": "gateway/retry.go", "line": 10, "body": "architecture note on this class"}
  ]
}
```

Build the final comment set from this: take every finding whose `decision` is
`accept` or `edit` (use its `body`), drop every `dismiss`, then append all
`added_comments`. `verdict` is the human's call on the PR; `notes` is the review
summary. `structure_comments` are architecture-level notes the reviewer left on
a class in the UML view — `line` may be `null` (the class declaration isn't
always in the diff), so treat these as design feedback, not necessarily inline
diff comments (see Step 5).

## Step 5 — Act on the curated review

Default to posting it as a GitHub review (this is usually the point). Map the
verdict to the review event and the comments to inline comments. `side` maps
`new → RIGHT`, `old → LEFT`.

Build the GitHub reviews-API payload and post with `gh api`:

```bash
# verdict -> event: approve->APPROVE, comment->COMMENT, request_changes->REQUEST_CHANGES
gh api repos/{owner}/{repo}/pulls/{N}/reviews \
  --method POST \
  --input - <<'JSON'
{
  "event": "REQUEST_CHANGES",
  "body": "<notes>",
  "comments": [
    {"path": "gateway/client.go", "line": 28, "side": "RIGHT", "body": "<final body>"},
    {"path": "gateway/client.go", "line": 35, "side": "RIGHT", "body": "<added comment>"}
  ]
}
JSON
```

Assemble that JSON programmatically from the loupe output (don't hand-type it).
If GitHub rejects an inline comment because the line isn't part of the diff,
fall back to including that comment in the review `body` instead of dropping it.

For `structure_comments`: if a comment has a `line` that's part of the diff,
post it inline like any other (path = its `file`, side = RIGHT). Otherwise, fold
it into the review `body` under a short "Architecture notes" heading, prefixed
with the class name — e.g. `**RetryPolicy:** fold this back into Client`. These
are design-level remarks, so the review body is usually the right home.

If the user isn't on GitHub or just wants the writeup, skip the API call and
present the curated review as a concise summary (verdict + notes + the kept
comments grouped by file, with architecture notes listed separately).

### Verdict → event mapping

**Example 1:** verdict `approve` → `"event": "APPROVE"`
**Example 2:** verdict `comment` → `"event": "COMMENT"`
**Example 3:** verdict `request_changes` → `"event": "REQUEST_CHANGES"`

## Iterating

loupe runs one round per invocation. If the human's notes ask you to dig deeper
or re-review after changes, regenerate `review.json` with updated findings and
run the CLI again — each run is a fresh round.

## Try it

A sample review ships with the skill. To see the UI with no PR:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/loupe.py ${CLAUDE_SKILL_DIR}/examples/example-review.json
```
