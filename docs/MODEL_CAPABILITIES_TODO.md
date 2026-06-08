# MODEL_CAPABILITIES — 模型能力配置化 TODO

## 现状问题

当前模型差异逻辑通过 `if is_deepseek` / `if is_doubao` 硬编码散落在 `agent.py` 中。
每新增一个模型提供商，就需要在多处添加 if-else 分支，维护成本指数级增长。

## 目标

将所有模型差异抽象为 `ProviderConfig` 中的配置项，运行时由配置驱动，消除硬编码分支。

---

## 需要配置化的差异点

### 1. Thinking 模式配置

**现状**（`agent.py:247-253`）：
```python
if is_deepseek:
    extra_body.setdefault("thinking", {"type": "enabled"})
if not is_doubao:
    extra_body.setdefault("reasoning_effort", "max")
```

**方案**：在 `ProviderConfig` 中增加 `thinking_config` 字段：

```yaml
providers:
  ark_primary:
    thinking:
      mode: extra_body          # extra_body | reasoning_effort | none
      extra_body_key: thinking
      extra_body_value: {"type": "enabled"}
      disable_for_tool_choice: true   # tool_choice="required" 时是否需禁用
      disable_value: {"type": "disabled"}
  ark_secondary:
    thinking:
      mode: none                # 豆包不支持 thinking 相关参数
```

### 2. tool_choice 兼容性

**现状**（`agent.py:260-268`）：
```python
if is_deepseek:
    tool_choice = None          # thinking 模式不兼容
elif self.force_first_tool and self.tools:
    tool_choice = "required"
```

**方案**：
```yaml
providers:
  ark_primary:
    tool_choice:
      supports_required: false  # thinking 模式下设为 false
      force_first_tool_fallback: none  # required 不可用时的替代值
  ark_secondary:
    tool_choice:
      supports_required: true
      force_first_tool_fallback: required
```

### 3. 工具调用后强制检查

**现状**（`agent.py:286-307`）：DeepSeek 特有逻辑——工具未被调用则强制检索后重新生成。

**方案**：
```yaml
providers:
  ark_primary:
    tool_behavior:
      check_tool_called: true
      force_retrieval_on_miss: true
  ark_secondary:
    tool_behavior:
      check_tool_called: false   # tool_choice="required" 保证必定调用
```

### 4. Max Turns 限制策略

**现状**（`agent.py:274-275`）：
```python
if is_deepseek and self.force_first_tool and self.tools:
    effective_max_turns = self.max_tool_rounds + 2
```

**方案**：
```yaml
providers:
  ark_primary:
    turn_limit:
      limit_tool_rounds: true
      max_turns_formula: "max_tool_rounds + 2"
```

### 5. response_format 支持

**现状**（`providers.py:11-26`）：全局 monkey-patch 将 `json_schema` 转换为 `json_object`。

**方案**：改为每个 provider 的配置项，去掉全局补丁：

```yaml
providers:
  ark_primary:
    response_format:
      supported_types: [json_object]   # 不支持 json_schema
      fallback: json_object
      requires_json_keyword_in_prompt: true
```

### 6. API 类型选择

**现状**（`config.py:114`、`providers.py:50-52`）：已有 `use_responses` 配置，无需改动。

---

## 目标配置结构

```yaml
providers:
  ark_primary:                           # DeepSeek
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_API_KEY}
    model: deepseek-v4-pro
    use_responses: false
    capabilities:
      thinking_enabled: true
      thinking_mode: extra_body          # extra_body | reasoning_effort | none
      thinking_config:
        key: thinking
        value: {"type": "enabled"}
        disable_for_tool_choice:
          key: thinking
          value: {"type": "disabled"}
      tool_choice_required: false        # thinking 模式不兼容
      check_tool_called: true            # 工具未调用时需重试
      limit_tool_rounds: true
      response_formats: ["json_object"]  # 不支持 json_schema
      max_output_tokens: 32768

  ark_secondary:                         # 豆包
    base_url: https://ark.cn-beijing.volces.com/api/v3
    api_key: ${ARK_API_KEY}
    model: doubao-seed-2-0-pro-260215
    use_responses: false
    capabilities:
      thinking_enabled: false
      thinking_mode: none
      tool_choice_required: true
      check_tool_called: false
      limit_tool_rounds: false
      response_formats: ["json_object"]
      max_output_tokens: 16384
```

---

## 执行建议

1. **Phase 1**：在 `ProviderConfig` 中新增 `capabilities` 字段，保持向后兼容（所有字段设默认值 = 当前 DeepSeek 行为）
2. **Phase 2**：将 `agent.py` 中的 `is_deepseek` / `is_doubao` 判断替换为读 `capabilities.*` 配置
3. **Phase 3**：将 `providers.py` 中的全局 monkey-patch 替换为 per-provider 行为
4. **Phase 4**：清理 `_model_name()` 检测逻辑，改为纯配置驱动

每一步独立可测，逐步迁移，避免大爆炸式重构。
