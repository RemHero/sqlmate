from __future__ import annotations

import contextvars

from openai import AsyncOpenAI
from agents.models.openai_responses import OpenAIResponsesModel
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.chatcmpl_converter import Converter

from app.config import ProviderConfig

# 少数模型（如 GLM-5.1）原生支持 response_format {"type": "json_schema"}，
# 不需要降级为 json_object。此 ContextVar 由 AgentNode/LLMNode 在每次调用前设置。
_current_model_supports_json_schema: contextvars.ContextVar[bool] = (
    contextvars.ContextVar("current_model_supports_json_schema", default=False)
)


def set_current_json_schema_support(supported: bool) -> None:
    """由 AgentNode/LLMNode 在构建 Agent 之前调用，控制是否保留 json_schema。"""
    _current_model_supports_json_schema.set(supported)


# DeepSeek / 第三方厂商不支持 OpenAI 的 response_format
# {"type": "json_schema", ...}，只支持 {"type": "json_object"}。
# 注意：json_object 要求 prompt 中必须出现 "json" 关键词，
# 该关键词由 AgentNode/LLMNode 在运行时自动追加。
_original_convert = Converter.convert_response_format.__func__


@classmethod
def _patched_convert_response_format(cls, final_output_schema):
    result = _original_convert(cls, final_output_schema)
    if isinstance(result, dict) and result.get("type") == "json_schema":
        if not _current_model_supports_json_schema.get(False):
            return {"type": "json_object"}
    return result


Converter.convert_response_format = _patched_convert_response_format


class ProviderRegistry:
    """根据配置创建模型对象的注册表。

    每个节点不会直接关心 API key 或 base_url，
    只需要通过 provider 名称向这里索取对应 model。
    """

    def __init__(self, providers: dict[str, ProviderConfig]) -> None:
        self.providers = providers

    def get_model(self, provider_name: str):
        """为指定 provider 构建可供 Agents SDK 使用的模型实例。"""
        cfg = self.providers[provider_name]
        client = AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
            organization=cfg.organization,
            project=cfg.project,
        )
        if cfg.use_responses:
            return OpenAIResponsesModel(model=cfg.model, openai_client=client)
        return OpenAIChatCompletionsModel(model=cfg.model, openai_client=client)
