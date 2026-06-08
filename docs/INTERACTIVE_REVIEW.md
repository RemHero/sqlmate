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

## Feedback loop

When the user submits feedback:

1. The current plan is sent back to a revision node.
2. The revision node updates the structured plan.
3. The CLI shows the revised plan again.
4. The workflow only continues after approval.

## Notes to fill later

- Review policy:
- Required approval criteria:
- Escalation rules:

