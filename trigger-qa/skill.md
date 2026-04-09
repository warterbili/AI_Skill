---
name: trigger-qa
description: "Trigger the Dash sourcing QA pipeline on AWS. Use when the user says: trigger QA, run QA, start QA pipeline, retrigger QA, QA for <platform> <country> <month>. Example: /trigger-qa LMN TH 202603"
disable-model-invocation: true
allowed-tools: Read, Bash, Glob, Grep
---

# Trigger QA Pipeline AWS

Use this skill when the user wants to start or retrigger one Dash sourcing QA run from a machine that already has AWS CLI access.

This skill is intentionally agent-agnostic. Any assistant can follow the same workflow: gather the scope, run the bundled script relative to the skill folder, and report the Lambda + EMR result.

## Required Inputs

Do not trigger the pipeline until these are known:

- `id_platform`
- `country`
- `refresh` in `YYYYMM`

You also need one of:

- `trigger_engineer_name`
- `trigger_engineer_id`

If any required value is missing, ask a short follow-up question before running anything.

## Defaults

Unless the user says otherwise, use:

- `env = dev`
- `layer = raw`
- `table_list = ["outlet_information", "outlet_meal", "meal_option", "option_relation"]`
- `load_raw_as_strings = 0`
- `region = eu-central-1`
- `function_name = arn:aws:lambda:eu-central-1:593453040104:function:dash-sourcing-pipeline-spark`

## Workflow

1. Gather `id_platform`, `country`, `refresh`, and either an engineer name or engineer id.
2. Verify local AWS access:
   `aws sts get-caller-identity --region eu-central-1`
3. Run the bundled script:
   `python3 C:/Users/admin/.claude/skills/trigger-qa/scripts/trigger_qa_pipeline.py`
5. Report:
   - Lambda invocation status
   - caller identity used for the trigger
   - cluster id, name, and state if found
   - EMR console link

## Script Usage

Run the bundled script with Python:

```bash
python3 C:/Users/admin/.claude/skills/trigger-qa/scripts/trigger_qa_pipeline.py \
  --platform EPL \
  --country US \
  --refresh 202602 \
  --engineer-name Cam
```

You can also pass a raw Slack id instead of a mapped engineer name:

```bash
python3 C:/Users/admin/.claude/skills/trigger-qa/scripts/trigger_qa_pipeline.py \
  --platform EPL \
  --country US \
  --refresh 202602 \
  --engineer-id U08HRFUBJ59
```

If your team uses different engineer-name mappings, provide a JSON file:

```bash
python3 C:/Users/admin/.claude/skills/trigger-qa/scripts/trigger_qa_pipeline.py \
  --platform EPL \
  --country US \
  --refresh 202602 \
  --engineer-name Alice \
  --engineer-map-file /path/to/engineer_map.json
```

Example `engineer_map.json`:

```json
{
  "Alice": "U12345678",
  "Bob": "U23456789"
}
```

## Notes

- The script uses the local `aws` CLI, so it does not require `boto3`.
- A Lambda response body of `null` is still acceptable if the EMR cluster is created successfully.
- If EMR verification times out, report that the trigger was sent and that the cluster was not observed within the verification window.
- Prefer reporting the exact cluster id and state instead of only saying "started".
