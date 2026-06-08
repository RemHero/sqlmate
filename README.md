# SqlMate

SqlMate uses the OpenAI Agents SDK to orchestrate a SQL test-case generation workflow for database feature validation.

## Features

- Runtime-loaded prompt files from `node_prompts/`
- Per-node model provider and API key binding
- Planner debate flow with structured outputs
- Interactive CLI review gates for planner and subplans
- Parallel `SETUP` / `DDL` / `CORE` execution
- On-demand multi-round knowledge retrieval with evidence-oriented tools
- Local run logs and JSON artifacts
- Pluggable SQL executor, with a mock executor enabled by default
- Sample case runner that persists inputs, outputs, and transcripts

## Quick Start

1. Create a config file from `config/settings.example.yaml`.
2. Export the required environment variables.
3. Run:

```bash
/home/remhero/bin/.venv/bin/python -m app.main \
  --config config/settings.example.yaml \
  --input-file tests/demo_input.json
```

For auto approval during scripted runs:

```bash
/home/remhero/bin/.venv/bin/python -m app.main \
  --config config/settings.example.yaml \
  --input-file tests/demo_input.json \
  --approval-mode auto
```

To generate mock sample outputs:

```bash
/home/remhero/bin/.venv/bin/python sample_cases/run_mock_samples.py
```

## Notes

- Prompts are loaded at runtime from `node_prompts/`.
- New prompt templates for revision and subplan review nodes are generated and ready to be filled in.
- Interactive workflow behavior is documented in `docs/`.
- If you want OpenAI-hosted trace export in addition to local logs, configure `observability.openai_trace_api_key`.
- By default the workflow validates with a mock executor, so the pipeline can be smoke-tested without a live database.
