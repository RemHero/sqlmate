from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shlex
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import Any, Awaitable, Callable

from app.schemas import KnowledgeBundle, KnowledgeItem, KnowledgeRoundRecord


class KnowledgeService:
    """阶段知识服务。

    这是当前项目中对 KV Store 的本地抽象，负责：
    1. 从磁盘加载阶段知识
    2. 按 topic 做模糊匹配
    3. 记录每一轮知识检索结果
    4. 合并多轮检索产物
    """

    def __init__(
        self,
        root_dir: Path,
        max_matches_per_topic: int = 3,
        *,
        enable_claude_index: bool = True,
        claude_command: str = "claude",
        claude_permission_mode: str = "bypassPermissions",
        claude_use_add_dir: bool = True,
        claude_timeout_seconds: int = 300,
        claude_max_output_chars: int = 40000,
        max_tool_found_items: int = 5,
        max_tool_item_content_chars: int = 3000,
        doc_knowledge_dir: str | Path = "/home/remhero/shared/ai/SqlMate/worflow/txzn",
        doc_knowledge_dirs: list[str | Path] | None = None,
        sql_examples_dir: str | Path = "knowledge/sql_examples_empty",
    ) -> None:
        self.root_dir = root_dir
        self.max_matches_per_topic = max_matches_per_topic
        self.enable_claude_index = enable_claude_index
        self.claude_command = claude_command
        self.claude_permission_mode = claude_permission_mode
        self.claude_use_add_dir = claude_use_add_dir
        self.claude_timeout_seconds = claude_timeout_seconds
        self.claude_max_output_chars = claude_max_output_chars
        self.max_tool_found_items = max_tool_found_items
        self.max_tool_item_content_chars = max_tool_item_content_chars
        self.doc_knowledge_dir = Path(doc_knowledge_dir)
        configured_doc_dirs = doc_knowledge_dirs or [doc_knowledge_dir]
        self.doc_knowledge_dirs = self._dedupe_paths(Path(path) for path in configured_doc_dirs)
        self.sql_examples_dir = Path(sql_examples_dir)
        self._cache: dict[str, dict[str, str]] = {}

    def _load_stage_knowledge(self, stage: str) -> dict[str, str]:
        """加载某个阶段的基础知识，并缓存到内存中。"""
        stage_key = stage.lower()
        if stage_key in self._cache:
            return self._cache[stage_key]
        path = self.root_dir / stage_key / "base_knowledge.json"
        if not path.exists():
            self._cache[stage_key] = {}
            return {}
        content = json.loads(path.read_text(encoding="utf-8"))
        normalized = {str(k).strip().lower(): str(v) for k, v in content.items()}
        self._cache[stage_key] = normalized
        return normalized

    def list_topics(self, stage: str) -> list[str]:
        """列出指定阶段当前已知的所有知识主题。"""
        stage_knowledge = self._load_stage_knowledge(stage)
        return sorted(stage_knowledge.keys())

    def retrieve(self, stage: str, topics: list[str], reason: str = "") -> KnowledgeBundle:
        """按照给定 topics 检索知识，并返回带检索记录的 bundle。"""
        stage_key = stage.lower()
        stage_knowledge = self._load_stage_knowledge(stage_key)
        found_items: list[KnowledgeItem] = []
        missing_topics: list[str] = []
        related_topics: set[str] = set()
        seen_topics: set[str] = set()

        for topic in topics:
            normalized = topic.strip().lower()
            matches = self._match_topics(stage_knowledge, normalized)
            if not matches:
                missing_topics.append(topic)
                continue

            for matched_topic in matches:
                related_topics.add(matched_topic)
                if matched_topic in seen_topics:
                    continue
                seen_topics.add(matched_topic)
                found_items.append(
                    KnowledgeItem(
                        topic=matched_topic,
                        content=stage_knowledge[matched_topic],
                        source=f"{stage_key}/base_knowledge.json",
                    )
                )

        summary = self.distill(stage_key, topics, found_items)
        round_record = KnowledgeRoundRecord(
            stage=stage_key,
            requested_topics=topics,
            reason=reason,
            found_topics=[item.topic for item in found_items],
            missing_topics=missing_topics,
            tool="local",
            searched_sources=[f"{stage_key}/base_knowledge.json"],
        )
        return KnowledgeBundle(
            stage=stage_key,
            requested_topics=topics,
            found_items=found_items,
            missing_topics=missing_topics,
            distilled_summary=summary,
            related_topics=sorted(related_topics),
            retrieval_rounds=[round_record],
        )

    def merge_bundles(self, stage: str, bundles: list[KnowledgeBundle]) -> KnowledgeBundle:
        """将多轮知识检索结果合并为一个统一上下文。"""
        stage_key = stage.lower()
        found_map: dict[str, KnowledgeItem] = {}
        related_topics: set[str] = set()
        requested_topics: list[str] = []
        missing_topics: list[str] = []
        rounds: list[KnowledgeRoundRecord] = []

        for bundle in bundles:
            requested_topics.extend(bundle.requested_topics)
            missing_topics.extend(bundle.missing_topics)
            related_topics.update(bundle.related_topics)
            rounds.extend(bundle.retrieval_rounds)
            for item in bundle.found_items:
                found_map[item.topic] = item

        found_items = list(found_map.values())
        distilled_summary = self.distill(stage_key, requested_topics, found_items)
        return KnowledgeBundle(
            stage=stage_key,
            requested_topics=requested_topics,
            found_items=found_items,
            missing_topics=sorted(set(missing_topics)),
            distilled_summary=distilled_summary,
            related_topics=sorted(related_topics),
            retrieval_rounds=rounds,
        )

    async def retrieve_external(
        self,
        *,
        stage: str,
        topics: list[str],
        search_goal: str,
        knowledge_sources: list[str],
        required_evidence: str,
        prompt_text: str,
        debug_callback: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> KnowledgeBundle:
        """通过 Claude Code CLI 检索资料库和 SQL 用例库。

        该方法是 Agents SDK tool 的后端实现。无论 Claude 是否成功返回，
        都会生成标准 KnowledgeBundle，便于后续节点把不确定性继续传递到 SQL 注释中。
        """
        stage_key = stage.strip().lower() or "shared"
        requested_topics = [topic.strip() for topic in topics if topic.strip()]
        normalized_sources = self._normalize_sources(knowledge_sources)
        if not self.enable_claude_index:
            return self._external_missing_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                message="Claude knowledge index is disabled by configuration.",
            )

        request_payload = {
            "stage": stage_key,
            "topics": requested_topics,
            "search_goal": search_goal,
            "knowledge_sources": normalized_sources,
            "required_evidence": required_evidence,
            "doc_knowledge_dir": str(self.doc_knowledge_dir),
            "doc_knowledge_dirs": [str(path) for path in self.doc_knowledge_dirs],
            "sql_examples_dir": str(self.sql_examples_dir),
            "candidate_sources": self._build_candidate_source_hints(
                topics=requested_topics,
                knowledge_sources=normalized_sources,
            ),
            "output_contract": {
                "format": "json",
                "fields": [
                    "found_items",
                    "missing_topics",
                    "distilled_summary",
                    "related_topics",
                    "notes",
                ],
            },
        }
        claude_prompt = (
            prompt_text.strip()
            + "\n\n<KnowledgeIndexRequest>\n"
            + json.dumps(request_payload, ensure_ascii=False, indent=2)
            + "\n</KnowledgeIndexRequest>\n"
        )
        command = self._build_claude_command(
            normalized_sources=normalized_sources,
            claude_prompt=claude_prompt,
        )
        if debug_callback:
            await self._emit_debug(
                debug_callback,
                "status",
                (
                    f"Launching Claude knowledge index. stage={stage_key} "
                    f"timeout={self.claude_timeout_seconds}s command={self._format_command_for_debug(command)}"
                ),
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            stdout_task = asyncio.create_task(
                self._read_stream(proc.stdout, "stdout", stdout_chunks, debug_callback)
            )
            stderr_task = asyncio.create_task(
                self._read_stream(proc.stderr, "stderr", stderr_chunks, debug_callback)
            )
            await asyncio.wait_for(proc.wait(), timeout=self.claude_timeout_seconds)
            await stdout_task
            await stderr_task
            stdout = "".join(stdout_chunks).strip()
            stderr = "".join(stderr_chunks).strip()
        except asyncio.CancelledError:
            partial_stdout = "".join(locals().get("stdout_chunks", [])).strip()
            partial_stderr = "".join(locals().get("stderr_chunks", [])).strip()
            logger.error(
                "Claude knowledge index cancelled. "
                "stage=%s topics=%s partial_stdout=%s partial_stderr=%s",
                stage_key, requested_topics,
                partial_stdout[:500] if partial_stdout else "(empty)",
                partial_stderr[:500] if partial_stderr else "(empty)",
            )
            for task_name in ("stdout_task", "stderr_task"):
                task = locals().get(task_name)
                if task:
                    task.cancel()
                    with contextlib.suppress(Exception):
                        await task
            if "proc" in locals():
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            if debug_callback:
                await self._emit_debug(
                    debug_callback,
                    "status",
                    "Claude knowledge index cancelled by user interrupt.",
                )
            raise
        except asyncio.TimeoutError:
            partial_stdout = "".join(locals().get("stdout_chunks", [])).strip()
            partial_stderr = "".join(locals().get("stderr_chunks", [])).strip()
            logger.error(
                "Claude knowledge index TIMED OUT after %ss. "
                "stage=%s topics=%s partial_stdout=%s partial_stderr=%s\n%s",
                self.claude_timeout_seconds,
                stage_key, requested_topics,
                partial_stdout[:500] if partial_stdout else "(empty)",
                partial_stderr[:500] if partial_stderr else "(empty)",
                traceback.format_exc(),
            )
            for task_name in ("stdout_task", "stderr_task"):
                task = locals().get(task_name)
                if task:
                    task.cancel()
                    with contextlib.suppress(Exception):
                        await task
            if "proc" in locals():
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            if debug_callback:
                await self._emit_debug(
                    debug_callback,
                    "status",
                    f"Claude knowledge index timed out after {self.claude_timeout_seconds}s.",
                )
            partial_bundle = self._build_raw_external_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                raw_text=partial_stdout or partial_stderr,
                topic="claude_timeout_result",
                evidence_type="raw_external_output",
                summary_prefix=f"Claude knowledge index timed out after {self.claude_timeout_seconds}s.",
            )
            if partial_bundle is not None:
                return partial_bundle
            return self._external_missing_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                message=f"Claude knowledge index timed out after {self.claude_timeout_seconds}s.",
            )
        except Exception as exc:
            partial_stdout = "".join(locals().get("stdout_chunks", [])).strip()
            partial_stderr = "".join(locals().get("stderr_chunks", [])).strip()
            exc_type = type(exc).__name__
            logger.error(
                "Claude knowledge index failed. "
                "stage=%s topics=%s exception_type=%s exception=%s "
                "partial_stdout=%s partial_stderr=%s\n%s",
                stage_key, requested_topics, exc_type, exc,
                partial_stdout[:500] if partial_stdout else "(empty)",
                partial_stderr[:500] if partial_stderr else "(empty)",
                traceback.format_exc(),
            )
            for task_name in ("stdout_task", "stderr_task"):
                task = locals().get(task_name)
                if task:
                    task.cancel()
                    with contextlib.suppress(Exception):
                        await task
            if "proc" in locals():
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            if debug_callback:
                await self._emit_debug(
                    debug_callback,
                    "status",
                    f"Claude knowledge index failed before completion: {type(exc).__name__}: {exc}",
                )
            partial_bundle = self._build_raw_external_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                raw_text=partial_stdout or partial_stderr,
                topic="claude_partial_result",
                evidence_type="raw_external_output",
                summary_prefix=f"Claude knowledge index failed before completion: {type(exc).__name__}: {exc}",
            )
            if partial_bundle is not None:
                return partial_bundle
            return self._external_missing_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                message=f"Claude knowledge index failed before completion: {type(exc).__name__}: {exc}",
            )
        if debug_callback:
            await self._emit_debug(
                debug_callback,
                "status",
                f"Claude knowledge index finished with exit_code={proc.returncode}.",
            )
        if proc.returncode != 0:
            message = stderr or stdout or f"claude exited with code {proc.returncode}"
            if debug_callback:
                await self._emit_debug(
                    debug_callback,
                    "status",
                    f"Claude knowledge index returned non-zero exit code {proc.returncode}: {self._truncate_for_note(message)}",
                )
            raw_bundle = self._build_raw_external_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                raw_text=stdout or stderr,
                topic="claude_nonzero_exit_output",
                evidence_type="raw_external_output",
                summary_prefix=f"Claude knowledge index returned non-zero exit code {proc.returncode}: {self._truncate_for_note(message)}",
            )
            if raw_bundle is not None:
                return raw_bundle
            return self._external_missing_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                message=f"Claude knowledge index returned non-zero exit code {proc.returncode}: {message}",
            )

        parsed = self._parse_external_json(stdout)
        if not parsed:
            if debug_callback:
                await self._emit_debug(
                    debug_callback,
                    "status",
                    "Claude returned output, but SqlMate could not parse it as the required JSON contract.",
                )
            raw_bundle = self._build_raw_external_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                raw_text=stdout,
                topic="claude_raw_result",
                evidence_type="raw_external_output",
                summary_prefix="Claude returned output, but it did not match the required JSON contract.",
            )
            if raw_bundle is not None:
                return raw_bundle
            return self._external_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                found_items=[],
                missing_topics=requested_topics,
                related_topics=[],
                summary="Claude returned no parseable knowledge.",
            )

        found_items = []
        for raw_item in parsed.get("found_items") or []:
            if not isinstance(raw_item, dict):
                continue
            topic = str(raw_item.get("topic") or raw_item.get("title") or "external_knowledge").strip()
            content = str(raw_item.get("content") or raw_item.get("summary") or "").strip()
            source = str(raw_item.get("source") or raw_item.get("file") or "claude_code_cli").strip()
            if not content:
                continue
            found_items.append(
                KnowledgeItem(
                    topic=topic,
                    content=self._truncate(content),
                    source=source,
                    evidence_type=str(raw_item.get("evidence_type") or raw_item.get("type") or "external").strip(),
                    confidence=str(raw_item.get("confidence") or "unknown").strip(),
                )
            )
        missing_topics = [str(item) for item in parsed.get("missing_topics") or [] if str(item).strip()]
        related_topics = [str(item) for item in parsed.get("related_topics") or [] if str(item).strip()]
        summary = str(parsed.get("distilled_summary") or parsed.get("summary") or "").strip()
        notes = [str(item).strip() for item in parsed.get("notes") or [] if str(item).strip()]
        if not summary:
            summary = self.distill(stage_key, requested_topics, found_items)
        if not found_items:
            salvage_parts = []
            if summary:
                salvage_parts.append(f"summary:\n{summary}")
            if notes:
                salvage_parts.append("notes:\n" + "\n".join(notes))
            salvage_parts.append("raw_json:\n" + json.dumps(parsed, ensure_ascii=False, indent=2))
            raw_bundle = self._build_raw_external_bundle(
                stage=stage_key,
                topics=requested_topics,
                reason=search_goal,
                searched_sources=normalized_sources,
                raw_text="\n\n".join(part for part in salvage_parts if part.strip()),
                topic="claude_schema_mismatch_result",
                evidence_type="raw_external_output",
                summary_prefix="Claude returned structured content, but no valid found_items could be extracted.",
            )
            if raw_bundle is not None:
                return raw_bundle
        if not found_items and not missing_topics:
            missing_topics = requested_topics
        return self._external_bundle(
            stage=stage_key,
            topics=requested_topics,
            reason=search_goal,
            searched_sources=normalized_sources,
            found_items=found_items,
            missing_topics=missing_topics,
            related_topics=related_topics,
            summary=self._truncate(summary),
        )

    def _build_raw_external_bundle(
        self,
        *,
        stage: str,
        topics: list[str],
        reason: str,
        searched_sources: list[str],
        raw_text: str,
        topic: str,
        evidence_type: str,
        summary_prefix: str,
    ) -> KnowledgeBundle | None:
        content = self._truncate((raw_text or "").strip())
        if not content:
            return None
        return self._external_bundle(
            stage=stage,
            topics=topics,
            reason=reason,
            searched_sources=searched_sources,
            found_items=[
                KnowledgeItem(
                    topic=topic,
                    content=content,
                    source="claude_code_cli",
                    evidence_type=evidence_type,
                    confidence="unknown",
                )
            ],
            missing_topics=[],
            related_topics=[],
            summary=f"{summary_prefix}\n{self._truncate_for_note(content)}",
        )

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        channel: str,
        chunks: list[str],
        debug_callback: Callable[[str, str], Awaitable[None] | None] | None,
    ) -> None:
        if stream is None:
            return
        while True:
            raw = await stream.read(2048)
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace")
            chunks.append(text)
            if debug_callback:
                await self._emit_debug(debug_callback, channel, text)

    async def _emit_debug(
        self,
        callback: Callable[[str, str], Awaitable[None] | None],
        channel: str,
        message: str,
    ) -> None:
        result = callback(channel, message)
        if asyncio.iscoroutine(result):
            await result

    def _format_command_for_debug(self, command: list[str]) -> str:
        rendered: list[str] = []
        skip_next_prompt = False
        for index, part in enumerate(command):
            if skip_next_prompt:
                skip_next_prompt = False
                rendered.append("<prompt omitted>")
                continue
            if part == "-p" and index + 1 < len(command):
                rendered.append("-p")
                skip_next_prompt = True
                continue
            rendered.append(part)
        return shlex.join(rendered)

    def _build_claude_command(self, *, normalized_sources: list[str], claude_prompt: str) -> list[str]:
        command = [*shlex.split(self.claude_command)]
        if self.claude_permission_mode:
            command.extend(["--permission-mode", self.claude_permission_mode])
        if self.claude_use_add_dir:
            add_dirs: list[Path] = []
            if "docs" in normalized_sources:
                add_dirs.extend(path for path in self.doc_knowledge_dirs if path.exists())
            if "sql_examples" in normalized_sources and self.sql_examples_dir.exists():
                add_dirs.append(self.sql_examples_dir)
            seen_dirs: set[str] = set()
            for path in add_dirs:
                resolved = str(path.resolve())
                if resolved in seen_dirs:
                    continue
                seen_dirs.add(resolved)
                command.extend(["--add-dir", resolved])
        command.extend(["-p", claude_prompt])
        return command

    def _dedupe_paths(self, paths: Any) -> list[Path]:
        result: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            path_obj = Path(path)
            key = str(path_obj)
            if key in seen:
                continue
            seen.add(key)
            result.append(path_obj)
        return result

    def distill(self, stage: str, topics: list[str], found_items: list[KnowledgeItem]) -> str:
        """把检索到的知识条目压缩成一段可直接给模型消费的摘要。"""
        if not found_items:
            topic_text = ", ".join(topics) if topics else "none"
            return f"No cached {stage} knowledge matched the requested topics: {topic_text}."
        lines = [f"{item.topic}: {item.content}" for item in found_items]
        return "\n".join(lines)

    def _match_topics(self, stage_knowledge: dict[str, str], normalized_topic: str) -> list[str]:
        """对单个 topic 做精确匹配、子串匹配和 token 匹配。"""
        if normalized_topic in stage_knowledge:
            return [normalized_topic]

        partial_matches = [
            topic
            for topic in stage_knowledge
            if normalized_topic in topic or topic in normalized_topic
        ]
        if partial_matches:
            return partial_matches[: self.max_matches_per_topic]

        topic_tokens = [token for token in normalized_topic.replace("_", " ").split() if token]
        token_matches: list[str] = []
        for topic in stage_knowledge:
            score = sum(1 for token in topic_tokens if token in topic)
            if score:
                token_matches.append(topic)
        return token_matches[: self.max_matches_per_topic]

    def retrieve_local_fallback(
        self,
        *,
        stage: str,
        topics: list[str],
        search_goal: str,
        knowledge_sources: list[str],
    ) -> KnowledgeBundle | None:
        found_items: list[KnowledgeItem] = []
        missing_topics: list[str] = []
        searched_sources: list[str] = []
        for source in knowledge_sources:
            if source == "docs":
                for doc_dir in self.doc_knowledge_dirs:
                    searched_sources.append(str(doc_dir))
                    found_items.extend(self._scan_dir_for_topics(doc_dir, topics, "docs"))
            elif source == "sql_examples":
                searched_sources.append(str(self.sql_examples_dir))
                found_items.extend(self._scan_dir_for_topics(self.sql_examples_dir, topics, "sql_examples"))

        found_topics = {item.topic for item in found_items}
        for topic in topics:
            if not any(self._topic_matches_item(topic, item) for item in found_items):
                missing_topics.append(topic)

        if not found_items:
            return None

        summary_lines = [
            "Local fallback scan returned evidence from configured directories.",
            *[f"{item.topic}: {item.source}" for item in found_items[:6]],
        ]
        return self._external_bundle(
            stage=stage,
            topics=topics,
            reason=search_goal,
            searched_sources=searched_sources,
            found_items=found_items[:12],
            missing_topics=missing_topics,
            related_topics=sorted(found_topics),
            summary="\n".join(summary_lines),
        )

    def _scan_dir_for_topics(self, root: Path, topics: list[str], source_kind: str) -> list[KnowledgeItem]:
        if not root.exists() or not root.is_dir():
            return []
        extensions = {".txt", ".md", ".sql", ".json", ".yaml", ".yml"}
        candidates: list[tuple[int, KnowledgeItem]] = []
        topic_tokens = self._expand_topic_tokens(topics)
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            name_text = path.name.lower()
            name_score = self._score_text_for_tokens(name_text, topic_tokens)
            content = ""
            content_score = 0
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lower_content = content.lower()
            content_score = self._score_text_for_tokens(lower_content, topic_tokens)
            if not name_score and not content_score:
                continue
            snippet = self._extract_snippet(content, topic_tokens)
            score = name_score * 10 + content_score
            candidates.append(
                (
                    score,
                    KnowledgeItem(
                        topic=path.stem,
                        content=snippet,
                        source=str(path),
                        evidence_type="sql_example" if source_kind == "sql_examples" else "feature_spec",
                        confidence="medium" if name_score else "low",
                    ),
                )
            )
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in candidates[:12]]

    def _expand_topic_tokens(self, topics: list[str]) -> list[str]:
        stopwords = {
            "syntax",
            "feature",
            "features",
            "parameter",
            "parameters",
            "supported",
            "object",
            "objects",
            "type",
            "types",
            "boundary",
            "case",
            "cases",
            "sql",
            "guc",
            "docs",
            "examples",
        }
        tokens: set[str] = set()
        for topic in topics:
            normalized = topic.lower().replace("_", " ").replace("-", " ")
            for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized):
                if len(token) >= 3 and token not in stopwords:
                    tokens.add(token)
        return sorted(tokens, key=len, reverse=True)

    def _build_candidate_source_hints(
        self,
        *,
        topics: list[str],
        knowledge_sources: list[str],
    ) -> dict[str, list[str]]:
        """根据 topic 先做一轮文件名级别候选筛选，帮助外部检索优先命中正确章节。"""
        hints: dict[str, list[str]] = {}
        topic_tokens = self._expand_topic_tokens(topics)
        for source in knowledge_sources:
            if source == "docs":
                roots = self.doc_knowledge_dirs
            elif source == "sql_examples":
                roots = [self.sql_examples_dir]
            else:
                continue
            scored: list[tuple[int, str]] = []
            for root in roots:
                if not root.exists() or not root.is_dir():
                    continue
                for path in root.rglob("*"):
                    if not path.is_file():
                        continue
                    name_text = path.name.lower()
                    score = self._score_text_for_tokens(name_text, topic_tokens) * 10
                    if score == 0:
                        try:
                            preview = path.read_text(encoding="utf-8", errors="replace")[:4000].lower()
                        except OSError:
                            preview = ""
                        score = self._score_text_for_tokens(preview, topic_tokens)
                    if score:
                        scored.append((score, str(path)))
            scored.sort(key=lambda item: (-item[0], item[1]))
            hints[source] = [path for _, path in scored[:12]]
        return hints

    def _score_text_for_tokens(self, text: str, tokens: list[str]) -> int:
        score = sum(1 for token in tokens if token and token in text)
        words = re.findall(r"[a-z0-9]+", text.lower())
        if words:
            initials = "".join(word[0] for word in words if word)
            score += sum(3 for token in tokens if self._looks_like_acronym(token) and token in initials)
        return score

    def _looks_like_acronym(self, token: str) -> bool:
        return bool(re.fullmatch(r"[a-z][a-z0-9]{1,5}", token))

    def _extract_snippet(self, content: str, tokens: list[str]) -> str:
        lower_content = content.lower()
        hit_positions = [lower_content.find(token) for token in tokens if token and lower_content.find(token) >= 0]
        if hit_positions:
            start = max(min(hit_positions) - 800, 0)
        else:
            start = 0
        snippet = content[start : start + 3200].strip()
        return self._truncate(snippet)

    def _topic_matches_item(self, topic: str, item: KnowledgeItem) -> bool:
        topic_tokens = self._expand_topic_tokens([topic])
        haystack = f"{item.topic}\n{item.content}\n{item.source}".lower()
        return self._score_text_for_tokens(haystack, topic_tokens) > 0

    def _external_missing_bundle(
        self,
        *,
        stage: str,
        topics: list[str],
        reason: str,
        searched_sources: list[str],
        message: str,
    ) -> KnowledgeBundle:
        return self._external_bundle(
            stage=stage,
            topics=topics,
            reason=reason,
            searched_sources=searched_sources,
            found_items=[],
            missing_topics=topics or ["external_knowledge"],
            related_topics=[],
            summary=self._truncate(message),
        )

    def _external_bundle(
        self,
        *,
        stage: str,
        topics: list[str],
        reason: str,
        searched_sources: list[str],
        found_items: list[KnowledgeItem],
        missing_topics: list[str],
        related_topics: list[str],
        summary: str,
    ) -> KnowledgeBundle:
        round_record = KnowledgeRoundRecord(
            stage=stage,
            requested_topics=topics,
            reason=reason,
            found_topics=[item.topic for item in found_items],
            missing_topics=missing_topics,
            tool="claude_code_cli",
            searched_sources=searched_sources,
        )
        return KnowledgeBundle(
            stage=stage,
            requested_topics=topics,
            found_items=found_items,
            missing_topics=missing_topics,
            distilled_summary=summary,
            related_topics=related_topics,
            retrieval_rounds=[round_record],
        )

    def _normalize_sources(self, sources: list[str]) -> list[str]:
        allowed = {"docs", "sql_examples"}
        normalized = [source.strip().lower() for source in sources if source.strip()]
        selected = [source for source in normalized if source in allowed]
        return selected or ["docs"]

    def _parse_external_json(self, text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def _truncate(self, text: str) -> str:
        if len(text) <= self.claude_max_output_chars:
            return text
        return text[: self.claude_max_output_chars] + "\n...[truncated]"

    def _truncate_for_note(self, text: str) -> str:
        if len(text) <= 1000:
            return text
        return text[:1000] + "...[truncated]"

    def _summarize_failure(self, text: str) -> str:
        if "InvalidSubscription" in text:
            return "InvalidSubscription from Claude Code CLI."
        if "timed out" in text.lower():
            return "Claude Code CLI timed out."
        return self._truncate_for_note(text)

    def summarize_external_issue(self, text: str) -> str:
        if not text:
            return ""
        return self._summarize_failure(text)
