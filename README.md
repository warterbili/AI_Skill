# AI Skills

Personal collection of AI-assisted automation skills for sourcing data pipelines.

Each skill is a self-contained directory with a `skill.md` (or `SKILL.md`) definition and any supporting scripts or templates. Skills are designed to be used with Claude Code or similar AI coding assistants.

## Skills

| Skill | Description | Key Files |
|---|---|---|
| [conso-migrate](conso-migrate/) | Migrate a platform to the ConSo (Consolidated Sourcing) architecture. Generates spider code, Dockerfile, CI workflows, and handles MySQL/Redis setup. | `SKILL.md`, templates |
| [id-refresh](id-refresh/) | Re-crawl specific outlet IDs — push IDs from CSV to Redis, run the detail spider locally, verify S3 output. | `SKILL.md`, `scripts/id_refresh.py` |
| [parse-workflow](parse-workflow/) | Parse and validate sourcing workflow definitions against schema conventions. | `SKILL.md`, `validate_output.py` |
| [run-detail](run-detail/) | Launch ad-hoc detail spider runs on AWS Fargate with image validation, multi-instance support, OOM auto-recovery, and Loki log analysis. | `skill.md` |
| [trigger-qa](trigger-qa/) | Trigger the sourcing QA pipeline on AWS by invoking Lambda and verifying EMR cluster creation. | `skill.md`, `scripts/trigger_qa_pipeline.py` |

## Structure

```
AI_Skills/
├── README.md
├── <skill-name>/
│   ├── skill.md          # Skill definition (frontmatter + workflow)
│   ├── scripts/          # Supporting scripts (optional)
│   └── ...               # Templates, configs, etc. (optional)
└── <another-skill>/
    └── ...
```

## Adding a New Skill

1. Create a new directory: `<skill-name>/`
2. Add a `skill.md` with frontmatter:
   ```yaml
   ---
   name: <skill-name>
   description: "<when to use this skill>"
   ---
   ```
3. Add supporting scripts or templates as needed
4. Update this README's Skills table

## Usage

These skills are loaded by placing them in the Claude Code skills directory:

- **Windows:** `C:\Users\<user>\.claude\skills\<skill-name>\`
- **macOS/Linux:** `~/.claude/skills/<skill-name>/`

Once placed, invoke a skill with `/<skill-name>` in Claude Code.
