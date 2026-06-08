# Known Issues

This document records product and workflow issues that are intentionally left for later design work.

## Knowledge Retrieval Timing

Current behavior:

- `PLANNER_DRAFT`, `SETUP_PLAN_REFINER`, `DDL_PLAN_REFINER`, and `CORE_PLAN_REFINER` are `AgentNode` instances with access to `retrieve_database_knowledge`.
- These nodes currently use `force_first_tool=True`, so their first model action is forced to be a knowledge retrieval tool call.
- The model still decides the retrieval payload: topics, search goal, knowledge source selection, and required evidence.
- After the first tool call, the model may decide whether additional retrieval calls are needed.

Why this exists:

- The first implementation prioritizes proving that the knowledge-index tool is actually connected and used by all planning nodes.
- It also prevents a common failure mode where the model writes a plan without calling the tool even though the prompt says evidence is required.

Concern:

- A forced first retrieval can happen before the model has completed an initial requirement decomposition.
- This means the first retrieval may be broad or incomplete, because the model may not yet know the exact knowledge gaps.
- The more precise target behavior is: draft a small initial decomposition, identify knowledge gaps, call the tool for those gaps, then produce the formal structured plan.

Future direction:

- Replace `force_first_tool=True` with a two-step planning loop.
- Step 1: model produces a lightweight `KnowledgeNeed` or `PlanDraftSkeleton`.
- Step 2: code invokes `retrieve_database_knowledge` for those needs, or allows the same agent to call the tool after explicitly listing gaps.
- Step 3: model produces the final `PlannerDraft` or `PhasePlan` using retrieved evidence.
- Add a guardrail: if a planning node produces a final plan with target-specific syntax claims but no knowledge retrieval event, reject or retry.

Open questions:

- Should the knowledge-need skeleton be a separate schema, or embedded into the existing planner prompt?
- Should retrieval be done by the same planning agent or by a dedicated retrieval node?
- How should the workflow balance latency against repeated retrieval rounds for complex requests?
