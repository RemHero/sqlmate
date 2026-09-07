from __future__ import annotations

import asyncio
import os
import threading
import time
import termios
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import ProviderConfig
from app.nodes.agent import AgentNode
from app.nodes.llm import LLMNode
from app.services.providers import ProviderRegistry
from app.ui.console import WorkflowConsole


class _PromptLoader:
    def load(self, node_name: str) -> str:
        return f"prompt for {node_name}"


class _RunLogger:
    def log(self, *args) -> None:
        return None


class _StreamResult:
    final_output = "ok"
    last_response_id = None

    async def stream_events(self):
        if False:
            yield None


class IntranetAdaptationTests(unittest.TestCase):
    def test_provider_http_clients_follow_ssl_policy_and_are_reused(self) -> None:
        registry = ProviderRegistry({})
        verified_client = object()
        insecure_client = object()
        with patch(
            "app.services.providers.httpx.AsyncClient",
            side_effect=[verified_client, insecure_client],
        ) as client_factory:
            self.assertIs(registry._get_http_client(True), verified_client)
            self.assertIs(registry._get_http_client(True), verified_client)
            self.assertIs(registry._get_http_client(False), insecure_client)
        self.assertEqual(
            [call.kwargs for call in client_factory.call_args_list],
            [{"verify": True}, {"verify": False}],
        )

    def test_glm_thinking_default_and_explicit_extra_body_override(self) -> None:
        async def exercise(model_extra_body: dict | None, configured: bool) -> dict:
            captured: dict = {}
            node = AgentNode(
                "OTHER",
                model=SimpleNamespace(model="GLM-5.2"),
                output_type=None,
                run_config=None,
                model_extra_body=model_extra_body,
                enable_thinking=configured,
            )

            async def fake_run(*args, **kwargs):
                captured.update(kwargs["extra_body"])
                return "ok", None, False

            node._single_agent_run = fake_run  # type: ignore[method-assign]
            ctx = SimpleNamespace(ui=None, prompt_loader=_PromptLoader())
            await node.run(ctx, {})
            return captured

        default_body = asyncio.run(exercise(None, False))
        self.assertFalse(default_body["chat_template_kwargs"]["enable_thinking"])

        override_body = asyncio.run(
            exercise({"chat_template_kwargs": {"enable_thinking": True}}, False)
        )
        self.assertTrue(override_body["chat_template_kwargs"]["enable_thinking"])

    def test_llm_node_injects_glm_thinking_setting(self) -> None:
        node = LLMNode(
            "TEST",
            model=SimpleNamespace(model="GLM-5.2"),
            output_type=str,
            run_config=None,
            enable_thinking=True,
        )
        ctx = SimpleNamespace(ui=None, prompt_loader=_PromptLoader(), run_logger=_RunLogger())
        with (
            patch("app.nodes.llm.Agent") as agent,
            patch("app.nodes.llm.Runner.run_streamed", return_value=_StreamResult()),
        ):
            asyncio.run(node.run(ctx, {}))
        settings = agent.call_args.kwargs["model_settings"]
        self.assertTrue(settings.extra_body["chat_template_kwargs"]["enable_thinking"])

    def test_raw_reader_preserves_split_utf8_and_buffered_characters(self) -> None:
        console = WorkflowConsole(show_banner=False)
        read_fd, write_fd = os.pipe()

        def write_split_character() -> None:
            os.write(write_fd, "中".encode("utf-8")[:1])
            time.sleep(0.03)
            os.write(write_fd, "中".encode("utf-8")[1:] + b"x")
            os.close(write_fd)

        writer = threading.Thread(target=write_split_character)
        writer.start()
        try:
            self.assertEqual(console._read_raw_char(read_fd), "中")
            self.assertEqual(console._read_raw_char(read_fd), "x")
        finally:
            os.close(read_fd)
            writer.join()

    def test_escape_sequence_does_not_consume_following_input(self) -> None:
        console = WorkflowConsole(show_banner=False)
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"[Aq")
        os.close(write_fd)
        try:
            console._skip_escape_sequence(read_fd)
            self.assertEqual(console._read_raw_char(read_fd), "q")
        finally:
            os.close(read_fd)

    def test_cbreak_preserves_crlf_for_application_level_handling(self) -> None:
        console = WorkflowConsole(show_banner=False)
        master_fd, slave_fd = os.openpty()
        original = termios.tcgetattr(slave_fd)
        try:
            console._set_cbreak(slave_fd)
            input_flags = termios.tcgetattr(slave_fd)[0]
            self.assertFalse(input_flags & termios.ICRNL)
            self.assertFalse(input_flags & termios.INLCR)
            self.assertFalse(input_flags & termios.IGNCR)
        finally:
            termios.tcsetattr(slave_fd, termios.TCSANOW, original)
            os.close(master_fd)
            os.close(slave_fd)

    def test_provider_defaults_are_secure_and_thinking_is_opt_in(self) -> None:
        config = ProviderConfig(base_url="https://example.com", api_key="x", model="GLM-5.2")
        self.assertTrue(config.verify_ssl)
        self.assertFalse(config.enable_thinking)


if __name__ == "__main__":
    unittest.main()
