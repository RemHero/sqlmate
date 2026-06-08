from __future__ import annotations

import json as _json
from typing import Any

from pydantic import BaseModel, Field, model_validator


class KnowledgeItem(BaseModel):
    """单条知识项。"""

    topic: str
    content: str
    source: str
    evidence_type: str = ""
    confidence: str = ""


class KnowledgeRoundRecord(BaseModel):
    """一次知识检索回合的记录。"""

    stage: str
    requested_topics: list[str] = Field(default_factory=list)
    reason: str = ""
    found_topics: list[str] = Field(default_factory=list)
    missing_topics: list[str] = Field(default_factory=list)
    tool: str = "local"
    searched_sources: list[str] = Field(default_factory=list)


class KnowledgeBundle(BaseModel):
    """某个阶段累计得到的知识包。"""

    stage: str
    requested_topics: list[str] = Field(default_factory=list)
    found_items: list[KnowledgeItem] = Field(default_factory=list)
    missing_topics: list[str] = Field(default_factory=list)
    distilled_summary: str = ""
    related_topics: list[str] = Field(default_factory=list)
    retrieval_rounds: list[KnowledgeRoundRecord] = Field(default_factory=list)


def _dict_element_to_str(d: dict) -> str:
    """将 list 中的 dict 元素转换为可读字符串。

    优先提取常见键值（topic / guc_name / name），否则序列化整个 dict。
    """
    for key in ("topic", "guc_name", "name", "key", "id"):
        if key in d and d[key]:
            s = str(d[key])
            if "description" in d and d["description"]:
                s += f" | {d['description']}"
            elif "default" in d and d["default"]:
                s += f" | default={d['default']}"
            return s
    return _json.dumps(d, ensure_ascii=False)


def _heal_list_str_field(data: dict, field: str) -> None:
    """修复 list[str] 字段：dict → list，str → [str]，list-of-dicts → list-of-str。"""
    if field not in data:
        return
    val = data[field]
    if isinstance(val, dict):
        data[field] = [f"{k}: {v}".rstrip(": ") if v else k for k, v in val.items()]
    elif isinstance(val, str):
        data[field] = [val]
    elif isinstance(val, list):
        healed: list[str] = []
        for item in val:
            if isinstance(item, str):
                healed.append(item)
            elif isinstance(item, dict):
                healed.append(_dict_element_to_str(item))
            else:
                healed.append(str(item))
        data[field] = healed


def _heal_list_dict_str_field(data: dict, field: str) -> None:
    """修复 list[dict[str,str]] 字段中 dict value 为非字符串（list/dict）的情况。"""
    if field not in data:
        return
    val = data[field]
    if not isinstance(val, list):
        return
    healed: list[dict[str, str]] = []
    for item in val:
        if isinstance(item, dict):
            healed.append({
                str(k): _json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                for k, v in item.items()
            })
        else:
            healed.append(item)
    data[field] = healed



_LIST_STR_FIELDS_GLOBAL = [
    "must_cover",
    "validation_strategy",
    "planning_considerations",
    "risks",
]

_LIST_STR_FIELDS_OUTLINE = [
    "coverage_focus",
    "test_dimensions",
    "stage_boundaries",
    "shared_dependencies",
    "success_criteria",
    "deferred_planning_questions",
]


class GlobalPlan(BaseModel):
    """Planner 输出的全局总计划。"""

    task_goal: str
    db_dialect: str
    feature_under_test: str
    requirement_decomposition: list[dict[str, Any]] = Field(default_factory=list)
    coverage_matrix: list[dict[str, Any]] = Field(default_factory=list)
    must_cover: list[str] = Field(default_factory=list)
    required_setup_objects: list[dict[str, str]] = Field(default_factory=list)
    validation_strategy: list[str] = Field(default_factory=list)
    sql_contract: dict[str, Any] = Field(default_factory=dict)
    planning_considerations: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def heal(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for field in _LIST_STR_FIELDS_GLOBAL:
            _heal_list_str_field(data, field)
        _heal_list_dict_str_field(data, "required_setup_objects")
        if "sql_contract" in data and not isinstance(data["sql_contract"], dict):
            data["sql_contract"] = {"items": data["sql_contract"]}
        return data


class PhaseOutline(BaseModel):
    """Planner 阶段产出的高层阶段纲领。"""

    stage: str
    objective: str
    coverage_focus: list[str] = Field(default_factory=list)
    test_dimensions: list[str] = Field(default_factory=list)
    stage_boundaries: list[str] = Field(default_factory=list)
    shared_dependencies: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    deferred_planning_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def heal(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for field in _LIST_STR_FIELDS_OUTLINE:
            _heal_list_str_field(data, field)
        return data


class PhasePlan(BaseModel):
    """单个阶段的结构化子计划。"""

    stage: str
    objective: str
    test_focus: list[str] = Field(default_factory=list)
    input_contract: list[str] = Field(default_factory=list)
    output_contract: list[str] = Field(default_factory=list)
    required_topics: list[str] = Field(default_factory=list)
    optional_topics: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    knowledge_gaps: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def heal(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for field in [
            "test_focus", "input_contract", "output_contract",
            "required_topics", "optional_topics", "assumptions",
            "knowledge_gaps", "evidence_requirements",
        ]:
            _heal_list_str_field(data, field)
        return data


_OUTLINE_STAGE_MAP = {
    "setup_outline": "SETUP",
    "ddl_outline": "DDL",
    "core_outline": "CORE",
}


def _heal_outline_stage(data: dict, key: str, stage: str) -> None:
    """注入缺失的 stage 字段。"""
    if key in data and isinstance(data[key], dict):
        if "stage" not in data[key]:
            data[key] = dict(data[key])
            data[key]["stage"] = stage


def _heal_planner_notes_like(data: dict, field: str) -> None:
    """修复 planner_notes / debate_summary 等 list[str] 字段。"""
    if field not in data:
        return
    val = data[field]
    if isinstance(val, str):
        data[field] = [val]
    elif isinstance(val, dict):
        data[field] = [f"{k}: {v}".rstrip(": ") if v else k for k, v in val.items()]


class PlannerDraft(BaseModel):
    """Planner Draft 节点的输出结构。"""

    global_plan: GlobalPlan
    setup_outline: PhaseOutline
    ddl_outline: PhaseOutline
    core_outline: PhaseOutline
    planner_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def heal(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for key, stage in _OUTLINE_STAGE_MAP.items():
            _heal_outline_stage(data, key, stage)
        _heal_planner_notes_like(data, "planner_notes")
        return data


class PlannerReview(BaseModel):
    """Planner Critic 节点的输出结构。"""

    verdict: str
    challenges: list[str] = Field(default_factory=list)
    missing_coverage: list[str] = Field(default_factory=list)
    questionable_assumptions: list[str] = Field(default_factory=list)
    knowledge_concerns: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def heal(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for field in ["challenges", "missing_coverage", "questionable_assumptions", "knowledge_concerns"]:
            _heal_list_str_field(data, field)
        return data


class PlannerOutput(BaseModel):
    """最终正式版 planner 输出。"""

    global_plan: GlobalPlan
    setup_outline: PhaseOutline
    ddl_outline: PhaseOutline
    core_outline: PhaseOutline
    setup_plan: PhasePlan | None = None
    ddl_plan: PhasePlan | None = None
    core_plan: PhasePlan | None = None
    cached_knowledge: dict[str, KnowledgeBundle] = Field(default_factory=dict)
    debate_summary: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def heal(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for key, stage in _OUTLINE_STAGE_MAP.items():
            _heal_outline_stage(data, key, stage)
        _heal_planner_notes_like(data, "debate_summary")
        return data


class PhaseResult(BaseModel):
    """单个阶段节点最终产出的结果。"""

    stage: str
    plan_summary: str
    knowledge_used: KnowledgeBundle
    generated_sql: str
    validation_notes: list[str] = Field(default_factory=list)
    unresolved_risks: list[str] = Field(default_factory=list)
    evidence_checklist: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def heal(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for field in ["validation_notes", "unresolved_risks", "evidence_checklist"]:
            _heal_list_str_field(data, field)
        if "knowledge_used" in data:
            ku = data["knowledge_used"]
            if not isinstance(ku, dict):
                stage = str(data.get("stage", "")).strip().lower() or "unknown"
                if isinstance(ku, str):
                    data["knowledge_used"] = {"stage": stage, "distilled_summary": ku}
                else:
                    data["knowledge_used"] = {"stage": stage}
        return data


class UserReviewDecision(BaseModel):
    """CLI 用户审批结果。"""

    approved: bool
    feedback: str = ""
    reviewer: str = "user"


class ExecutionResult(BaseModel):
    """SQL 执行器输出的统一执行结果。"""

    success: bool
    executor_type: str
    message: str
    failed_stage: str | None = None
    errors: list[str] = Field(default_factory=list)


class FinalWorkflowOutput(BaseModel):
    """整个工作流完成后的总输出。"""

    planner_output: PlannerOutput
    setup_result: PhaseResult
    ddl_result: PhaseResult
    core_result: PhaseResult
    merged_sql: str
    execution: ExecutionResult
