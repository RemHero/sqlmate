# 内网 LLM 适配总结

整理日期：2026-09-08。代码核对基线：`6fa839f`。

本文依据本地 `docs/change.md`、`docs/change.diff` 及当前实现，说明内网适配的原因、实现位置和仍需注意的边界。两个来源文件是本地对照材料，不随本文提交；本文不依赖读者取得原材料才能理解。

`change.md` 同时记录程序、配置和安装环境变化；`change.diff` 只覆盖 `app/` 下六个 Python 文件，而且引用的部分终端辅助函数没有完整贴出。因此不能把 diff 当成全部迁移工作，也不能只替换模型地址就认为完成适配。

## 1. 模型服务接入

| 适配点 | 内网情况及原因 | 当前项目的实现和边界 |
| --- | --- | --- |
| 网关地址、鉴权、模型名 | 从公网 API 转到内网 vLLM 服务，再迁到正式 HTTPS AI 网关；记录中的模型从 GLM-5.1 升到 GLM-5.2 | `ProviderConfig` 集中管理 `base_url`、`api_key`、`model`；示例 YAML 从环境变量取值。部署时使用网关实际暴露的模型标识和路径，不能仅凭模型名称推导 URL |
| 协议兼容 | 接口按 OpenAI-compatible 方式使用，但不代表全部接口和扩展参数都兼容 | 示例 provider 均为 `use_responses: false`，走 Chat Completions；不要仅因使用 OpenAI SDK 就切到 Responses |
| 节点绑定 | 一个 provider 可以服务多个规划、生成节点；不同 provider 可以使用同一网关而参数不同 | 当前示例的所有节点都绑定 `ark_primary`。仅修改 `ark_glm` 不会影响 CORE，必须一起检查 `node_bindings` |
| 推理开关 | 来源记录中，长 reasoning 导致 502 / incomplete read，因此增加 GLM 推理开关 | 配置经 `main._build_node()` 同时传给 `AgentNode` 与 `LLMNode`；两条调用路径都做注入 |
| 结构化输出 | 部分服务支持 `json_schema`，另一些只接受 `json_object`；模型仍可能产生非预期 JSON | 当前有 `supports_json_schema`、响应格式转换和本地解析恢复。网关实际能力需要验证，模型名不能保证支持 |
| TLS 证书 | 内网正式网关使用自签名证书，默认校验可能失败 | 原 diff 全部使用 `verify=False`；当前改为按 provider 的 `verify_ssl` 复用客户端，默认 `true`，避免一个内网配置影响所有服务 |
| 时间限制 | 原适配把 provider 和执行阶段超时放宽到 1800 秒；用户现确认内网单次思考不能超过 1200 秒 | 客户端等待 1800 秒不能改变服务端 1200 秒限制。需要拆小模型任务并保存进度，见 [CORE 新方案](CORE_STAGED_EXECUTION_DESIGN.md) |

对应源码：[配置模型](../app/config.py)、[ProviderRegistry](../app/services/providers.py)、[节点构建](../app/main.py)、[AgentNode](../app/nodes/agent.py)、[LLMNode](../app/nodes/llm.py)、[配置示例](../config/settings.example.yaml)。

### GLM thinking 参数的实际行为

当前代码在模型名转小写后包含 `glm` 时，构造下面的扩展请求体：

```json
{
  "chat_template_kwargs": {
    "enable_thinking": false
  }
}
```

生效优先级是：显式 `model_extra_body.chat_template_kwargs.enable_thinking` > provider 的 `enable_thinking` > 字段默认值 `false`。代码用 `setdefault` 保留显式覆盖。

这不是对所有模型通用的推理开关。当前 DeepSeek 分支有独立参数逻辑，provider 的 `enable_thinking: false` 不会自动关闭 DeepSeek thinking。如果内网网关把 GLM 注册成不包含 `glm` 的别名，当前自动注入分支也不会命中，需要显式配置扩展请求体，后续可考虑把模型能力独立配置。

当前 YAML 中 `ark_primary`、`ark_secondary` 关闭 thinking，`ark_glm` 开启；`.env.example` 没有 thinking 开关变量。只在 `.env` 添加一个未经 YAML 引用的变量不会生效。`ui.show_think_stream` 只控制界面展示，与模型是否思考无关。

关闭 thinking 是已经存在的运行选项，不是完成全量 CORE 用例的充分条件：即使不开 thinking，长输入、复杂 SQL 和长输出仍可能耗时过长。新的阶段方案应允许保留必要的推理能力。

### JSON 和 TLS 的兼容边界

`supports_json_schema` 通过上下文变量影响 Chat Completions 响应格式转换。当前 `AgentNode` 会在调用前设置它，`LLMNode` 尚未接收和设置这个配置；不能把“两个节点都支持 thinking 注入”理解为“两条路径的全部能力适配完全一致”。

来源中的自签名证书问题已用按 provider 的 `verify_ssl` 处理。若部署具备内部 CA，可进一步配置受信任 CA；当前没有独立的 `ca_bundle` 配置字段，不应在部署说明中将其当成已实现能力。`SQLMATE_PIP_TRUSTED_HOST` 只影响依赖安装，不影响模型网关 TLS。

## 2. Python SDK 依赖兼容

来源记录了两层问题：原来固定的 `openai==2.24.0` 与 `openai-agents==0.13.6` 的依赖要求冲突；放宽版本后，内网又遇到 usage 对象新增必填字段与 agents 构造参数不一致的问题。

当前 [requirements.txt](../requirements.txt) 使用 `openai-agents==0.13.6` 和 `openai>=2.26.0,<3`。[main.py](../app/main.py) 的 `_patch_input_tokens_details()` 检查 `InputTokensDetails.cache_write_tokens`：只有字段存在且必填时，才设默认值 `0` 并重建 Pydantic 模型。

这里需要区分三个事实：

- 来源将问题归因于 `openai>=2.46`；这是内网记录中的版本说明，本文没有重新验证这个版本边界。
- 当前补丁按字段形态判断，不依赖版本字符串，也不代表 SDK 之间所有兼容问题都已解决。
- 补丁在 `app.main` 加载期、主要应用模块导入前运行；直接导入其他模块的入口并不保证执行它。启动脚本另有一次导入依赖的预检查，也早于应用补丁。

迁移验证应覆盖安装解析、启动导入和一次实际模型调用。仅 `compileall` 成功只能说明 Python 语法可编译，不能证明依赖组合、请求格式或内网服务兼容。当前开放版本范围也不保证外网和内网每次安装得到完全相同版本；后续应保存通过内网验收的版本清单或约束文件。

## 3. 内网运行环境

| 适配点 | 当前方式 | 部署时需保留的条件 |
| --- | --- | --- |
| Python 版本和 SSL | 安装脚本探测 Python 3.10+ 且能 `import ssl`；可用已有解释器，也提供 Python 3.10 源码构建路径 | 有相应系统库、编译工具或预先准备好的可用 Python |
| 非系统 OpenSSL | `SQLMATE_OPENSSL_ROOT` 用于源码构建，`SQLMATE_OPENSSL_LIB_DIR` 加入运行时 `LD_LIBRARY_PATH` | 编译和运行使用相匹配的动态库；不能通过仅复制 Python 文件保证兼容 |
| Python 环境位置 | 启动优先级：`SQLMATE_PYTHON` → 项目 `.venv/bin/python` → 开发机 `/home/remhero/bin/.venv/bin/python` → `python3` | 内网建议准备项目 `.venv` 或指定现有解释器，不依赖外网用户的绝对路径 |
| 内部 PyPI | `SQLMATE_PIP_INDEX_URL`、`SQLMATE_PIP_TRUSTED_HOST` 配置安装源 | 镜像要有目标 Python、系统架构对应的依赖；当前脚本不是自带全部依赖的离线安装包 |
| 代理 | 当前 `install.sh` 会清除常见代理变量 | 这是安装步骤行为；模型请求路径没有同样的清理逻辑，需要按内网路由设置运行环境 |
| 知识目录 | 示例 YAML 从 `SQLMATE_DOC_KNOWLEDGE_DIR` 获取实际目录 | 业务知识文件需在内网另行准备；两个来源没有提供全部业务文档正文 |
| 知识检索 CLI | 默认启用 Claude CLI 知识索引，另有本地检索回退 | Python venv 不等于已安装并配置好 Claude CLI；使用它时需单独准备可访问的服务及配置 |

对应文件：[启动脚本](../sqlmate)、[安装脚本](../install.sh)、[知识服务](../app/services/knowledge.py)、[部署步骤](INTRANET_SETUP.md)。

`sqlmate` 先选择解释器并设置动态库路径，随后才启动 Python；`.env` 在 Python 内由 `load_dotenv()` 加载。因此 `SQLMATE_PYTHON`、`SQLMATE_OPENSSL_LIB_DIR` 等启动期变量需在 shell 或服务配置中导出，不能仅写进 `.env` 后期待影响之前的启动步骤。

来源对 OpenSSL 同时出现 1.1 共享库和 3.0.9 环境说明，不能据此得出一个通用二进制环境。应以目标 Python 的实际链接库和启动验证为准。当前安装脚本的源码构建分支也需要在目标内网主机验收，已有开发机环境能运行不能替代这项验证。

## 4. 终端输入与中断

这部分是在内网使用中暴露的 CLI 问题，并非 LLM 接口要求，但迁移时需要一并保留：

- 使用 `os.read`、`select` 和内部输入缓冲管理多行粘贴；当前实现还用增量 UTF-8 解码处理中文跨读取块的情况。
- 用一个 cbreak 会话处理多行请求，避免每行切换终端状态；跳过 CSI / SS3 转义序列，并处理 CR/LF 输入。
- 配合删除双 Ctrl-C 计数退出机制，应用收到中断后退出并返回 `130`，终端状态在输入会话结束时恢复。

对应 [console.py](../app/ui/console.py) 和 [main.py](../app/main.py)。`change.diff` 没贴全 `_read_raw_char`、`_skip_escape_sequence` 及动画输入相关实现，当前代码补充了这些逻辑。

来源把 `tty.setcbreak` 泛称 raw mode，并据此描述 Ctrl-C 一定作为字符返回；更准确地说，当前 cbreak 设置没有显式关闭 `ISIG`，Ctrl-C 仍可能走系统信号路径。中断适配应同时考虑信号和字符输入，不能只依赖 `\x03` 分支。

## 5. 外网开发、内网拉取的配置边界

通用源码、提示词、配置模板和依赖声明进入代码仓；内网 `.env`、虚拟环境、Python/OpenSSL 二进制和运行日志留在目标机器。当前 `.gitignore` 已覆盖 `.env`、`.venv/`、`.python310/`、`vendor/openssl/`、`logs/`、`output/`；来源提到的 `.openssl/` 不在当前忽略列表，若使用这个位置，需另外处理。

“内网拉取后能运行”的前提是首次准备已经完成：模型网关可达、运行时和依赖兼容、启动环境变量正确、知识目录存在，以及使用到的知识检索 CLI 可用。后续纯代码更新可复用这些条件；依赖声明变更后需要同步安装。不要把 Python 和 venv 的绝对路径链接作为跨机器部署机制。

## 6. 本次新增约束：单次推理上限 20 分钟

1200 秒来自用户对内网服务的说明，不是由本地日志测得。当前无法仅凭这两个文件判断它精确计量的是 reasoning、整个请求、首 token 等待还是流式连接存活时间；设计先以“每次完整模型请求不得占用超过 1200 秒”这个保守边界处理。

当前 CORE 仍是一次整阶段调用，携带全局计划、阶段计划、知识缓存和 DDL，并按提示词一次完成矩阵、用例列表、全部 SQL 和覆盖统计。长请求失败时，默认阶段 fallback 可能用通用 SQL 代替原任务；现有 checkpoint 只保存规划阶段，无法恢复 CORE 已完成的生成工作。

解决方案需要同时改变任务粒度、单次请求时间预算、进度持久化和完成判定。具体设计见 [CORE 分阶段生成方案 v2](CORE_STAGED_EXECUTION_DESIGN.md)。该方案尚未实现，本总结也不宣称当前程序已经解决 20 分钟断开问题。
