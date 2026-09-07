# Interactive Review

## Purpose

The CLI pauses at these checkpoints:

1. Global planner output
2. `SETUP` subplan
3. `DDL` subplan
4. `CORE` subplan

At each checkpoint, the user can:

- approve
- provide feedback
- cancel the workflow

## Feedback Loop

When the user submits feedback:

1. The current plan is sent back to a revision node.
2. The revision node updates the structured plan.
3. The CLI shows the revised plan again.
4. The workflow only continues after approval.

## Checkpoint Persistence

After each approval, SqlMate automatically persists the current state
as a **checkpoint**. This enables resuming interrupted runs without
repeating expensive LLM calls.

### Checkpoint Stages

| Stage | After Approving | Saved Data |
|-------|----------------|------------|
| `planner` | Global plan | PlannerOutput (global_plan + outlines) |
| `setup_plan` | SETUP subplan | PlannerOutput (with all 3 PhasePlans) |
| `ddl_plan` | DDL subplan | PlannerOutput (with reviewed DDL) |
| `core_plan` | CORE subplan | PlannerOutput (fully reviewed) |

### Checkpoint Directory Structure

```
logs/{run_id}/
  checkpoint_manifest.json           # Tracks completed stages
  user_input.json                    # Original user request
  planner/planner_output.json        # Stage 1 checkpoint
  setup_plan/planner_output.json     # Stage 2 checkpoint
  ddl_plan/planner_output.json       # Stage 3 checkpoint
  core_plan/planner_output.json      # Stage 4 checkpoint
```

### Resume Usage

```bash
# Resume from the most advanced checkpoint
python -m app.main --config config/settings.example.yaml --resume 20260610_143021_123

# Force restart (clear existing checkpoints)
python -m app.main --config config/settings.example.yaml --resume 20260610_143021_123 --force
```

When resuming:

1. The tool loads the most advanced checkpoint's `PlannerOutput`
2. Already-completed planning stages are skipped
3. If all 4 planning stages are complete, it jumps directly to SQL generation
4. The original `user_input` is restored, so no re-prompting is needed

### Resume Behavior Matrix

| Last Completed | Skipped |
|----------------|---------|
| `planner` | `planner.run()` + global plan review |
| `setup_plan` | Above + all 3 PhasePlan builds + SETUP review |
| `ddl_plan` | Above + DDL review |
| `core_plan` | Above + CORE review → **direct to SQL generation** |
