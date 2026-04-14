# AI Skills

A collection of Claude Code skills that automate the **end-to-end sourcing
pipeline** — from parsing a new food-delivery platform's APIs all the way to
running monthly crawls on Fargate and triggering QA.

Each skill is **self-contained** (a directory with `SKILL.md` + supporting
scripts) and **chains into the next** through a shared state-file convention,
so a complete migration doesn't require remembering 30 commands.

---

## The 7 Skills (in execution order)

| # | Skill | Purpose | Key Files |
|--:|---|---|---|
| 1 | [parse-workflow](parse-workflow/) | Analyze a reverse-engineered crawler, write `finder_parse.py` + `detail_parse.py` for a new food-delivery platform. Outputs `handoff.json` for the next step. | `SKILL.md`, `workflow.md`, `scripts/validate_handoff.py`, `validate_output.py` |
| 2 | [conso-migrate](conso-migrate/) | Take any crawler (Scrapy, Postman, Bruno, JS, Go…) and migrate it to the ConSo standard — spider code, Dockerfile, CI/CD, MySQL/Redis setup. Reads `handoff.json` if present. Includes shared diagnostic scripts for finder health monitoring. | `SKILL.md`, 13 templates, `check_mysql.py`, `check_spiderkeeper.py`, `check_redis.py`, `manage_spiderkeeper.py` |
| 3 | [grid-gen](grid-gen/) | Generate hexagonal (H3) grid points for finder spiders. Covers scope definition, S3 upload, Redis push, CASS update, and `push_grids.py` sync. Dual mode: FULL (spider project) or GENERATE-ONLY (pre-migration). | `SKILL.md`, `scripts/generate_grid.py` |
| 4 | [conso-deploy](conso-deploy/) | Deploy a migrated ConSo project to AWS — ECR repo, GitHub Actions, CloudWatch logs, EventBridge cron rules, CASS activation. | `SKILL.md` |
| 5 | [run-detail](run-detail/) | Trigger ad-hoc detail spider runs on Fargate. Smart entry, multi-instance, OOM auto-recovery, post-run S3/MySQL data verification. | `SKILL.md` |
| 6 | [id-refresh](id-refresh/) | Re-crawl a specific list of outlet IDs (from CSV). Dual mode: local Python or Fargate. Race-checked Redis push, ID-level landing verification. | `SKILL.md`, `scripts/id_refresh.py` |
| 7 | [trigger-qa](trigger-qa/) | Trigger the sourcing QA pipeline (Lambda → EMR). Smart entry detects duplicate triggers, optional `--wait-for-completion` polls cluster through terminal state. | `SKILL.md`, `scripts/trigger_qa_pipeline.py` |

---

## How They Chain Together

```
                  ┌────────────────────────────────────┐
                  │  parse-workflow                    │
                  │  • analyze reverse-engineered code │
                  │  • write parse demos               │
                  │  • generate handoff.json           │
                  └─────────────────┬──────────────────┘
                                    │ handoff.json
                                    ▼
                  ┌────────────────────────────────────┐
                  │  conso-migrate                     │
                  │  • reads handoff.json (Phase 0.01) │
                  │  • generates spider + Dockerfile   │
                  │  • migrates MySQL/Redis            │
                  └──────────┬─────────────────────────┘
                             │ ~/.claude/state/
               ┌─────────────┤
               ▼             ▼
  ┌──────────────────┐  ┌────────────────────────────────────┐
  │  grid-gen        │  │  conso-deploy                      │
  │  • H3 hex grid   │  │  • ECR + GitHub Actions            │
  │  • S3 + Redis    │  │  • EventBridge monthly cron        │
  │  • CASS update   │  │  • Phase 5.5 deploy-time smoke test│
  └──────────────────┘  └─────────────────┬──────────────────┘
    ▲ also usable                         │
    │ pre-migration         ┌─────────────┼───────────────┐
    │ (GENERATE-ONLY)       ▼             ▼               ▼
    │               ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
    │               │  run-detail   │ │  id-refresh   │ │  trigger-qa   │
    │               │  full crawls  │ │  targeted fix │ │  QA validate  │
    │               └───────────────┘ └───────────┬───┘ └───────▲───────┘
    │                                             │             │
    │                                             └─────────────┘
    │                                             chain to QA after data fix
    │
    └── grid-gen can also run standalone before conso-migrate
        (GENERATE-ONLY mode: grid + S3 upload, no Redis/CASS)
```

Each arrow represents a **state-file handoff**:
`~/.claude/state/{skill}/{platform}.json` — written by the upstream skill,
auto-discovered by the downstream skill's Proactive Preflight.

---

## Common Patterns Across All Skills

Every skill in this repo follows the same "smart Claude" patterns so behaviour
is predictable and skills compose cleanly:

| Pattern | What it does |
|---|---|
| **Frontmatter** | `name`, `description`, `disable-model-invocation: true`, `allowed-tools` whitelist |
| **Top-level rules** | Narrate every step • Auto-fix bounded (max 3 rounds) • Mode-switch • Leave-no-trace |
| **Proactive Preflight** | Auto-detect everything possible (git remote, AWS account, sibling state) BEFORE asking the user |
| **Phase 0 variable resolution** | Single source-of-truth table — every variable defined once, referenced everywhere |
| **Cross-phase Invariants** | Explicit table of "things that MUST stay coupled or production breaks silently" |
| **Pre-launch Gate (STOP-style)** | 4-6 hard checks before any production-touching action; any FAIL = STOP |
| **Inline error decision tables** | At each phase, "if you see X, do Y" — Claude doesn't fish through the bottom of the doc |
| **Verdict-driven Next Steps** | Final phase recommends specific next slash-command based on outcome |
| **State files** | `~/.claude/state/{skill}/{key}.json` for cross-session resume + cross-skill awareness |
| **JSON stdout / stderr narration** | Bundled scripts emit JSON on stdout, narration on stderr — pipeable through `jq` |

| **Cascading diagnostic chain** | When something looks wrong, Claude doesn't just report it — it runs a multi-layer decision tree (MySQL → SpiderKeeper → Redis), where each layer's result determines whether to run the next, and the final step is an actionable fix (not just a diagnosis) |

These patterns mean that **once you're familiar with one skill, the rest feel
identical** — same conventions, same assumptions, same rescue mechanisms.

---

## Shared Diagnostic Tools

These scripts live in `conso-migrate/` but are used by multiple skills.
They form a **cascading diagnostic chain** — each tool's output determines
whether Claude runs the next:

```
Layer 0: check_mysql.py          "Is finder writing to MySQL?"
    │
    ├─ ✅ fresh → done (proceed silently)
    ├─ ⚠️ stale → trigger Layer 1
    └─ ❌ empty → trigger Layer 1
         │
Layer 1: check_spiderkeeper.py   "Is the finder process alive?"
    │
    ├─ RUNNING + stalled → trigger Layer 2
    ├─ RUNNING + zombie → offer to stop+restart (manage_spiderkeeper.py)
    └─ NOT RUNNING → offer to start (manage_spiderkeeper.py)
         │
Layer 2: check_redis.py          "Are there grids to process?"
    │
    ├─ key exists (ZCARD≥0) → grids OK, issue is elsewhere (proxy/API)
    └─ key missing → grids never pushed, run push_grids.py first
```

| Script | What it checks | Used by |
|---|---|---|
| `check_mysql.py` | MySQL row count, last_refresh, growth in last N minutes | conso-migrate (Phase 12, 13.5), run-detail (Phase 0.6) |
| `check_spiderkeeper.py` | SpiderKeeper RUNNING finder jobs + MySQL cross-check | conso-migrate (Phase 13.5), run-detail (Phase 0.6) |
| `check_redis.py` | Redis grid counts via SSM → Docker exec | conso-migrate (Phase 12), run-detail (Phase 0.6) |
| `manage_spiderkeeper.py` | Stop / start / deploy finder jobs | conso-migrate (Phase 13), run-detail (Phase 0.6 auto-fix) |

All scripts are standalone (no scrapy.cfg dependency), support `--json` for
programmatic use, and auto-discover platform configs from AWS.

---

## Layout

```
AI_Skills/
├── README.md
├── .gitignore
├── <skill-name>/
│   ├── SKILL.md          # Skill definition (frontmatter + workflow)
│   ├── scripts/          # Supporting Python scripts (optional)
│   └── *.template        # Code templates (optional)
└── ...
```

---

## Anatomy of a New Skill

To add a new skill that fits this repo's style, copy the patterns from any
existing one (id-refresh and trigger-qa are good "small reference" examples):

1. **Create the directory:** `<skill-name>/` (use kebab-case, not snake_case).

2. **Add `SKILL.md`** with full frontmatter:
   ```yaml
   ---
   name: <skill-name>
   description: "<one sentence on when to use it>. Trigger words: ..."
   disable-model-invocation: true
   allowed-tools: Read, Write, Edit, Bash, Glob, Grep
   ---
   ```

3. **Apply the standard skeleton:**
   - Top-level rules (Narrate / Auto-fix bounded / Leave-no-trace)
   - Proactive Preflight section (silent auto-detection BEFORE Startup)
   - Startup self-introduction with Required Inputs
   - Phase 0 variable resolution + state-file helpers
   - Cross-phase Invariants table (4-7 invariants)
   - Workflow steps with inline error decision tables
   - Pre-launch Gate before any production-touching step
   - Final phase: persist state file + verdict-driven Next Steps

4. **Bundled scripts** (in `scripts/`) should:
   - Validate inputs strictly (regex, type checks)
   - Use parameterised SQL (never f-string interpolation of user input)
   - Print structured JSON to stdout, narration to stderr
   - Exit 0 on success, 1 on hard failure
   - Avoid hardcoded AWS account IDs / secret names — discover at runtime

5. **Update this README** — add a row to the Skills table and (if relevant)
   adjust the chain diagram.

---

## Usage

Place the skill in Claude Code's skills directory:

- **Windows:** `C:\Users\<user>\.claude\skills\<skill-name>\`
- **macOS/Linux:** `~/.claude/skills/<skill-name>/`

Then invoke with `/<skill-name>` in Claude Code. Skills with
`disable-model-invocation: true` will not be auto-suggested by Claude — they
must be explicitly invoked. This is intentional for production-touching
operations.

To keep the local copy in sync with this repo, after a `git pull`:

```bash
for skill in conso-deploy conso-migrate grid-gen id-refresh parse-workflow run-detail trigger-qa; do
    rm -rf ~/.claude/skills/$skill
    cp -r $(pwd)/$skill ~/.claude/skills/
done
```

---

## State Files

Each skill that completes meaningful work writes a state file to:

```
~/.claude/state/<skill-name>/<platform-or-key>.json
```

These are NOT cleaned by the leave-no-trace rule (deliberately persistent).
They serve two purposes:

1. **Cross-session resume** — invoking the same skill again with the same
   key surfaces the previous attempt and offers options (retry, view, abort).
2. **Cross-skill awareness** — downstream skills' Proactive Preflight reads
   upstream state to pre-fill inputs.

To inspect or clear:

```bash
ls ~/.claude/state/                              # which skills have state
ls ~/.claude/state/conso-migrate/                # which platforms have state
cat ~/.claude/state/conso-migrate/EPL.json       # see what was recorded
rm ~/.claude/state/conso-migrate/EPL.json        # force fresh start next time
```

---

## Contributing

This is a personal collection but PRs are welcome. Ground rules:

- New skills should follow the patterns documented above (not optional —
  consistency is what makes the chain work).
- Don't hardcode user-specific paths (`C:\Users\<name>\...`), AWS account IDs,
  or personal tokens. Use dynamic discovery.
- Bundled scripts must be runnable on both Windows (Git Bash) and macOS/Linux —
  use `MSYS_NO_PATHCONV=1` for `aws` calls that take `/`-prefixed args on Windows.
- If you change a skill's contract (e.g. the JSON schema downstream skills
  depend on), bump the relevant validator and update both producer + consumer
  in the same PR.
