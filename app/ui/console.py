from __future__ import annotations

import asyncio
import codecs
import json
import os
import select
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from typing import Any

from rich import cells
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from app.schemas import PhasePlan, PlannerOutput, UserReviewDecision


class WorkflowInterrupted(RuntimeError):
    """表示用户主动中断了当前工作流。"""


class WorkflowConsole:
    """SqlMate 的终端转录式 CLI。

    这个版本不再维护“仪表盘式”的固定布局，而是模仿 Linux shell：
    1. 顶部品牌与猫头像只打印一次
    2. 后续所有输入、计划、审批、结果都直接顺序追加到终端历史
    3. 用户向上滚动即可看到完整上下文，不再单独维护“历史面板”
    4. 输入提示使用灰底，便于和工具输出区分
    """

    def __init__(
        self,
        approval_mode: str = "interactive",
        show_banner: bool = True,
        show_raw_json: bool = False,
        show_think_stream: bool = False,
    ) -> None:
        self.approval_mode = approval_mode
        self.show_banner = show_banner
        self.show_raw_json = show_raw_json
        self.show_think_stream = show_think_stream
        self.console = Console()
        self._banner_rendered = False
        self._banner_animation_pending = True
        self._interrupted = False
        self._stream_mode: str | None = None
        self._stream_buffer = ""
        self._input_buffer = ""
        self._input_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._working_enabled = False
        self._working_visible = False
        self._working_label = "working"
        self._working_tick = 0
        self._last_activity_at = time.monotonic()
        self._working_stop = threading.Event()
        self._working_lock = threading.Lock()
        self._working_thread: threading.Thread | None = None

    def render_banner(self) -> None:
        """在会话开始时打印一次 SqlMate 标志和猫头像。"""
        if self._banner_rendered:
            return
        if not self.show_banner:
            self._banner_rendered = True
            return
        self._render_banner_frame(cat_shift=0, clear=True)
        self.console.print(Text(str(Path.cwd()), style="dim"))
        self.console.print()
        self._banner_rendered = True

    def start(self) -> None:
        """保留统一接口，转录式 CLI 无需额外启动动作。"""
        return

    def stop(self) -> None:
        """保留统一接口，转录式 CLI 无需额外关闭动作。"""
        self.stop_working()
        return

    def reset_for_new_run(self, request_text: str) -> None:
        """一轮新请求开始前插入一个轻量分隔，便于阅读。"""
        self.stop_working()
        self.console.print()
        self.console.print(Rule(style="grey39"))
        self.console.print(Text(f"SqlMate received: {request_text}", style="dim"))
        self.start_working("working")

    def info(self, message: str) -> None:
        """输出普通信息。"""
        self._before_output()
        self.console.print(Text(f"SqlMate {message}", style="cyan"))

    def success(self, message: str) -> None:
        """输出成功信息。"""
        self._before_output()
        self.console.print(Text(f"SqlMate {message}", style="green"))

    def warning(self, message: str) -> None:
        """输出警告信息。"""
        self._before_output()
        self.console.print(Text(f"SqlMate {message}", style="yellow"))

    def add_event(self, message: str, kind: str = "info") -> None:
        """兼容旧接口，把事件直接打印到转录流里。"""
        style = {
            "info": "cyan",
            "success": "green",
            "warning": "yellow",
            "error": "red",
            "system": "magenta",
            "control": "bright_blue",
        }.get(kind, "white")
        self._before_output()
        self.console.print(Text(f"SqlMate {message}", style=style))

    def note_output(self, message: str) -> None:
        """打印收尾摘要，例如输出文件和执行结果。"""
        self._before_output()
        self.console.print(Text(f"SqlMate {message}", style="bright_white"))

    def update_summary(self, title: str, lines: list[str] | str) -> None:
        """保留旧接口。

        终端转录模式下，不再维护独立摘要面板，因此这里不做任何事。
        """
        return

    def enter_node(self, step_name: str, stage: str, node_name: str, detail: str = "") -> None:
        """在长耗时阶段开始时打印最少必要提示。

        注意这里只提示用户真正需要知道的内容，不再暴露内部状态机。
        """
        message = self._node_message(node_name, detail)
        if message:
            self.start_working("working")
            self._before_output()
            self.console.print(Text(f"SqlMate {message}", style="dim"))

    def stream_begin(self, node_name: str) -> None:
        """开始打印某个节点的实时流式返回。"""
        self._before_output()
        self._close_stream_mode()
        self.console.print(Text(f" Live {node_name} ", style="black on rgb(92,76,120)"))

    def stream_reasoning_delta(self, delta: str) -> None:
        """打印 reasoning 增量，并用 `<think>` 包裹。"""
        if not delta:
            return
        if not self.show_think_stream:
            return
        if self._stream_mode != "think":
            self._close_stream_mode()
            self._stream_mode = "think"
            self._stream_buffer = "<think>"
        self._append_stream_delta(delta, mode="think")

    def stream_text_delta(self, delta: str) -> None:
        """打印正文增量。"""
        if not delta:
            return
        if not self.show_raw_json:
            return
        if self._stream_mode != "text":
            self._close_stream_mode()
            self._stream_mode = "text"
            self._stream_buffer = ""
        self._append_stream_delta(delta, mode="text")

    def stream_tool_event(self, text: str) -> None:
        """打印工具调用相关事件。"""
        self._before_output()
        self._close_stream_mode()
        if not self.show_think_stream and len(text) > 500:
            text = text[:500] + " ...[truncated]"
        self._print_full_bg_line(text, mode="tool")

    def stream_tool_output(self, text: str) -> None:
        """打印工具返回结果。

        默认模式下只展示部分关键信息；开启 `show_think_stream` 时打印完整返回。
        """
        self._before_output()
        self._close_stream_mode()
        rendered_lines = self._render_tool_output_lines(text)
        for line in rendered_lines:
            self._print_full_bg_line(line, mode="tool")

    def stream_final_block(self, title: str, content: str) -> None:
        """打印最终结构化结果块，使用单独背景色。"""
        if not self.show_raw_json:
            return
        self._before_output()
        self._close_stream_mode()
        self.console.print(Rule(title, style="green"))
        for line in content.splitlines() or [""]:
            self._print_full_bg_line(line, mode="final")

    def stream_end(self) -> None:
        """结束当前流式输出段落。"""
        self._close_stream_mode()

    def show_generated_sql(self, node_name: str, sql: str) -> None:
        """打印阶段生成的 SQL，默认始终展示（不受 show_raw_json 控制）。"""
        if not sql:
            return
        self._before_output()
        self._close_stream_mode()
        self.console.print(Rule(f"{node_name} SQL", style="cyan"))
        self.console.print(Text(sql.strip(), style="bright_white"))

    def leave_node(self, step_name: str, stage: str, node_name: str, detail: str = "") -> None:
        """保留旧接口。

        完成提示由更高层的计划输出和最终结果承担，因此这里默认静默。
        """
        return

    def fail_node(self, step_name: str, stage: str, node_name: str, detail: str = "") -> None:
        """在节点失败时输出一条可见警告。"""
        self._before_output()
        self.console.print(Text(f"SqlMate {detail or node_name}", style="red"))

    def mark_step(self, step_name: str, status: str, detail: str = "") -> None:
        """保留旧接口，转录模式不展示内部 step。"""
        return

    def render_planner_output(self, planner_output: PlannerOutput) -> None:
        """把总 planner 以 shell 输出的形式打印到终端。"""
        self._close_stream_mode()
        self.stop_working()
        self.console.print(Rule("Planner", style="bright_cyan"))
        for line in self._format_global_plan(planner_output).plain.splitlines():
            self._print_full_bg_line(line, mode="plan")

    def render_phase_plan(self, stage: str, plan: PhasePlan) -> None:
        """把单阶段计划以 shell 输出的形式打印到终端。"""
        self._close_stream_mode()
        self.stop_working()
        self.console.print(Rule(f"{stage} Plan", style="bright_cyan"))
        for line in self._format_phase_plan(plan).plain.splitlines():
            self._print_full_bg_line(line, mode="plan")

    def review_planner_output(self, planner_output: PlannerOutput) -> UserReviewDecision:
        """打印总计划，并在同一终端历史中收集审批意见。"""
        self.render_planner_output(planner_output)
        return self._ask_for_decision("planner")

    def review_phase_plan(self, stage: str, plan: PhasePlan) -> UserReviewDecision:
        """打印子计划，并在同一终端历史中收集审批意见。"""
        self.render_phase_plan(stage, plan)
        return self._ask_for_decision(f"{stage.lower()} plan")

    def save_transcript(self, path: Path, payload: dict[str, Any]) -> None:
        """保存 JSON transcript，便于后续调试或回放。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def prompt_user_request(self) -> dict[str, Any] | None:
        """采集一条新的用户请求。

        返回 `None` 表示用户希望结束整个 REPL。
        """
        self.stop_working()
        request = self._multiline_request_input().strip()
        if request.lower() in {"/exit", "/quit"}:
            return None
        if not request:
            request = "请生成功能回归 SQL 测试用例"
        return {
            "task_goal": request,
            "db_dialect": "unknown",
            "feature_under_test": request,
            "raw_request": request,
            "requirements": [],
            "constraints": [],
        }

    def prompt_continue(self) -> bool:
        """在一轮执行完成后询问是否继续下一轮。

        返回值：
        1. `True` 代表继续进入下一轮请求
        2. `False` 代表结束整个工具会话
        """
        self.stop_working()
        while True:
            answer = self._gray_input("❯ 本次已结束，按 c 继续下一次生成，输入 quit 退出工具: ").strip().lower()
            if answer in {"c", "continue", ""}:
                return True
            if answer in {"q", "quit", "exit"}:
                return False
            self.console.print(Text("Please enter c or quit.", style="yellow"))

    async def maybe_pause(self) -> None:
        """兼容旧接口，当前转录模式不支持后台暂停。"""
        return

    def raise_if_interrupted(self) -> None:
        """如果用户在审批阶段选择退出，则终止工作流。"""
        if self._interrupted:
            raise WorkflowInterrupted("Workflow interrupted by user.")

    def reset_interrupt_state(self) -> None:
        """在一次成功交互后清空中断标记。"""
        self._interrupted = False
        self._close_stream_mode()

    def refresh(self) -> None:
        """兼容旧接口，转录模式无需刷新。"""
        return

    def _ask_for_decision(self, target: str) -> UserReviewDecision:
        """在 shell 转录流中完成审批。

        设计上让审批动作本身也成为终端历史的一部分，用户回滚即可看到：
        1. 刚刚看到的计划长什么样
        2. 自己当时是 approve 还是反馈修订
        """
        if self.approval_mode == "auto":
            self._before_output()
            self.console.print(Text(f"❯ approve {target}", style="black on rgb(58,58,62)"))
            return UserReviewDecision(approved=True)

        while True:
            answer = self._gray_input(f"❯ approve {target}? [a/f/q] ").strip().lower() or "a"
            if answer in {"a", "f", "q"}:
                break
            self.console.print(Text("Please enter a, f, or q.", style="yellow"))
        if answer == "a":
            return UserReviewDecision(approved=True)
        if answer == "q":
            self._interrupted = True
            raise WorkflowInterrupted("Workflow cancelled by user.")
        self._before_output()
        self.console.print(Text("Enter feedback. Finish with a single line containing END.", style="yellow"))
        lines: list[str] = []
        while True:
            line = self._gray_input("… ")
            if line.strip() == "END":
                break
            lines.append(line)
        return UserReviewDecision(approved=False, feedback="\n".join(lines).strip())

    def _gray_input(self, prompt: str) -> str:
        """使用 raw terminal mode 读取单行，避免 ANSI 显示延迟和粘贴丢失。"""
        bg = "\x1b[48;2;58;58;62m\x1b[38;2;245;245;245m"
        reset = "\x1b[0m"

        if self._should_animate_banner_input():
            sys.stdout.write(bg + prompt)
            sys.stdout.flush()
            try:
                return self._animated_banner_input()
            finally:
                sys.stdout.write(reset)
                sys.stdout.flush()

        if not sys.stdin.isatty():
            sys.stdout.write(bg + prompt)
            sys.stdout.flush()
            try:
                return input()
            except EOFError:
                return "quit"
            finally:
                sys.stdout.write(reset)
                sys.stdout.flush()

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        chars: list[str] = []
        try:
            self._set_cbreak(fd)
            sys.stdout.write(bg + prompt)
            sys.stdout.flush()
            while True:
                char = self._read_raw_char(fd)
                if not char:
                    return "quit"
                if char in {"\n", "\r"}:
                    if char == "\r":
                        self._discard_buffered_lf()
                    sys.stdout.write(reset + "\n")
                    sys.stdout.flush()
                    return "".join(chars)
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\x04":
                    if not chars:
                        raise EOFError
                    continue
                if char in {"\x7f", "\b"}:
                    if chars:
                        chars.pop()
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                    continue
                if char == "\x1b":
                    self._skip_escape_sequence(fd)
                    continue
                if char.isprintable():
                    chars.append(char)
                    sys.stdout.write(char)
                    sys.stdout.flush()
        except EOFError:
            return "quit"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            sys.stdout.write(reset)
            sys.stdout.flush()

    def _multiline_request_input(self) -> str:
        """读取多行请求。

        规则：
        1. 首行使用 `❯ ` 提示
        2. 后续行使用 `… ` 提示
        3. 输入空行表示当前请求结束
        4. 支持用户直接粘贴多行文本
        """
        self.console.print(Text("输入请求，空行提交。可直接粘贴多行内容。", style="dim"))
        if self._should_animate_banner_input():
            first_line = self._gray_input("❯ ")
            if first_line.strip().lower() in {"/exit", "/quit"}:
                return first_line.strip()
            if not first_line:
                return ""
            return self._continue_multiline_raw([first_line])

        if not sys.stdin.isatty():
            try:
                return sys.stdin.read().strip()
            except EOFError:
                return "quit"

        return self._raw_multiline_session()

    def _raw_multiline_session(self) -> str:
        """在一次 raw-mode 会话中读取完整多行请求。"""
        return self._continue_multiline_raw([])

    def _continue_multiline_raw(self, lines: list[str]) -> str:
        """从已有行继续读取，空行提交；整个会话只切换一次终端模式。"""
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        prefix = "\x1b[48;2;58;58;62m\x1b[38;2;245;245;245m"
        suffix = "\x1b[0m"
        current: list[str] = []

        def show_prompt() -> None:
            prompt = "❯ " if not lines else "… "
            sys.stdout.write(prefix + prompt)
            sys.stdout.flush()

        show_prompt()
        try:
            self._set_cbreak(fd)
            while True:
                char = self._read_raw_char(fd)
                if not char:
                    return "\n".join(lines)
                if char in {"\n", "\r"}:
                    if char == "\r":
                        self._discard_buffered_lf()
                    line = "".join(current)
                    current.clear()
                    sys.stdout.write(suffix + "\n")
                    sys.stdout.flush()
                    if not lines and line.strip().lower() in {"/exit", "/quit"}:
                        return line.strip()
                    if line == "":
                        return "\n".join(lines)
                    lines.append(line)
                    show_prompt()
                    continue
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\x04":
                    if not current and not lines:
                        raise EOFError
                    continue
                if char in {"\x7f", "\b"}:
                    if current:
                        current.pop()
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                    continue
                if char == "\x1b":
                    self._skip_escape_sequence(fd)
                    continue
                if char.isprintable():
                    current.append(char)
                    sys.stdout.write(char)
                    sys.stdout.flush()
        except EOFError:
            return "/quit" if not lines else "\n".join(lines)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            sys.stdout.write(suffix)
            sys.stdout.flush()

    def _read_raw_char(self, fd: int) -> str:
        """绕过 Python stdin 缓冲读取字符，并保留快速粘贴的剩余内容。"""
        if self._input_buffer:
            char, self._input_buffer = self._input_buffer[0], self._input_buffer[1:]
            return char

        decoded = ""
        while not decoded:
            data = bytearray(os.read(fd, 4096))
            if not data:
                return ""
            while select.select([fd], [], [], 0.01)[0]:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                data.extend(chunk)
            # 保留跨两次底层读取的 UTF-8 半字符，避免中文被替换字符破坏。
            decoded = self._input_decoder.decode(bytes(data), final=False)
        self._input_buffer += decoded[1:]
        return decoded[0]

    @staticmethod
    def _set_cbreak(fd: int) -> None:
        """进入 cbreak，并保留原始 CR/LF 以正确识别 Windows 风格粘贴。"""
        tty.setcbreak(fd)
        attributes = termios.tcgetattr(fd)
        attributes[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
        termios.tcsetattr(fd, termios.TCSANOW, attributes)

    def _discard_buffered_lf(self) -> None:
        """粘贴 CRLF 文本时避免把同一个换行处理两次。"""
        if self._input_buffer.startswith("\n"):
            self._input_buffer = self._input_buffer[1:]

    def _skip_escape_sequence(self, fd: int) -> None:
        """吞掉方向键和功能键等终端转义序列。"""
        def next_char() -> str:
            if self._input_buffer:
                char, self._input_buffer = self._input_buffer[0], self._input_buffer[1:]
                return char
            ready, _, _ = select.select([fd], [], [], 0.01)
            if not ready:
                return ""
            return self._read_raw_char(fd)

        first = next_char()
        if first == "[":
            for _ in range(32):
                char = next_char()
                if not char or "@" <= char <= "~":
                    break
        elif first == "O":
            next_char()

    def _should_animate_banner_input(self) -> bool:
        """只在首个用户输入前启用猫猫动画。"""
        return self.show_banner and self._banner_rendered and self._banner_animation_pending and sys.stdin.isatty()

    def _animated_banner_input(self) -> str:
        """在等待首个输入前，让猫猫持续左右移动；一旦开始输入就停止。"""
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        buffer: list[str] = []
        move_started = False
        shift = 0
        step = 4
        max_shift = self._banner_max_shift()
        try:
            self._set_cbreak(fd)
            while True:
                has_buffered_input = bool(self._input_buffer)
                ready = has_buffered_input or bool(select.select([fd], [], [], 0.16)[0])
                if ready:
                    char = self._read_raw_char(fd)
                    if not char:
                        return "quit"
                    if char in {"\n", "\r"}:
                        if char == "\r":
                            self._discard_buffered_lf()
                        self._banner_animation_pending = False
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return "".join(buffer)
                    if char == "\x03":
                        raise KeyboardInterrupt
                    if char in {"\x7f", "\b"}:
                        if buffer:
                            buffer.pop()
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                        continue
                    if char == "\x1b":
                        self._skip_escape_sequence(fd)
                        continue
                    if char.isprintable():
                        if not move_started:
                            self._banner_animation_pending = False
                            move_started = True
                        buffer.append(char)
                        sys.stdout.write(char)
                        sys.stdout.flush()
                    continue

                if move_started:
                    continue
                if max_shift <= 0:
                    continue
                shift += step
                if shift >= max_shift:
                    shift = max_shift
                    step = -4
                elif shift <= 0:
                    shift = 0
                    step = 4
                self._redraw_banner_cat(shift)
        except EOFError:
            return "quit"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)

    def _close_stream_mode(self) -> None:
        """在切换流式区域时补齐结尾和换行。"""
        if self._stream_mode == "think":
            self._append_stream_delta("</think>", mode="think", finalize=True)
        elif self._stream_mode == "text":
            self._flush_stream_buffer(finalize=True, mode="text")
        self._stream_mode = None
        self._stream_buffer = ""

    def _append_stream_delta(self, delta: str, mode: str, finalize: bool = False) -> None:
        """把增量内容按行缓冲，并以整行背景色输出。

        这样做的目的不是只给文字着色，而是让当前整行都具备同一种背景色，
        便于用户一眼区分 `<think>` 区和正文区。
        """
        parts = delta.split("\n")
        for index, part in enumerate(parts):
            self._stream_buffer += part
            has_newline = index < len(parts) - 1
            if has_newline:
                self._flush_stream_buffer(finalize=True, mode=mode)
                self._stream_buffer = ""
            else:
                self._flush_stream_buffer(finalize=finalize, mode=mode)

    def _flush_stream_buffer(self, finalize: bool, mode: str) -> None:
        """把当前缓冲区按整行宽度输出。

        非 finalize 时，会原地重绘当前最后一行；
        finalize 时，会真正换行固化到终端历史中。
        """
        width = max(20, self.console.size.width)
        wrapped = cells.chop_cells(self._stream_buffer or "", width)
        if not wrapped:
            wrapped = [""]
        for completed in wrapped[:-1]:
            self._write_full_bg_line(completed, mode=mode, newline=True)
        tail = wrapped[-1]
        if len(wrapped) > 1:
            self._stream_buffer = tail
        if finalize:
            self._write_full_bg_line(tail, mode=mode, newline=True)
        else:
            self._write_full_bg_line(tail, mode=mode, newline=False)

    def _print_full_bg_line(self, text: str, mode: str) -> None:
        """打印一整行底色的静态内容。"""
        width = max(20, self.console.size.width)
        for line in cells.chop_cells(text or "", width) or [""]:
            self._write_full_bg_line(line, mode=mode, newline=True)

    def _write_full_bg_line(self, text: str, mode: str, newline: bool) -> None:
        """使用 ANSI 直接写终端，保证整行都铺满背景色。"""
        self._clear_working_line()
        self._last_activity_at = time.monotonic()
        width = max(20, self.console.size.width)
        padded = cells.set_cell_size(text, width)
        ansi = self._stream_ansi(mode)
        clear = "\r\x1b[2K"
        end = "\n" if newline else ""
        sys.stdout.write(f"{clear}{ansi}{padded}\x1b[0m{end}")
        sys.stdout.flush()

    def _stream_ansi(self, mode: str) -> str:
        """返回不同流式区域对应的整行背景色。"""
        mapping = {
            "think": "\x1b[48;2;120;120;150m\x1b[38;2;20;20;20m",
            "text": "\x1b[48;2;44;62;96m\x1b[38;2;245;245;245m",
            "tool": "\x1b[48;2;118;99;64m\x1b[38;2;20;20;20m",
            "final": "\x1b[48;2;164;214;184m\x1b[38;2;20;20;20m",
            "plan": "\x1b[48;2;184;232;202m\x1b[38;2;20;20;20m",
        }
        return mapping.get(mode, "\x1b[48;2;60;60;60m\x1b[38;2;245;245;245m")

    def start_working(self, label: str = "working") -> None:
        """启动运行中提示，在长时间无输出时展示动态 `working...`。"""
        with self._working_lock:
            self._working_label = label
            self._working_enabled = True
            self._last_activity_at = time.monotonic()
            self._working_stop.clear()
            if self._working_thread and self._working_thread.is_alive():
                return
            self._working_thread = threading.Thread(target=self._working_loop, daemon=True)
            self._working_thread.start()

    def stop_working(self) -> None:
        """停止运行中提示并清理当前行。"""
        with self._working_lock:
            self._working_enabled = False
            self._working_stop.set()
        self._clear_working_line()

    def _before_output(self) -> None:
        """在普通输出前清理 working 行，并记录最近活动时间。"""
        self._clear_working_line()
        self._last_activity_at = time.monotonic()

    def _working_loop(self) -> None:
        """在 CLI 静默期显示 `working...`。"""
        while not self._working_stop.is_set():
            time.sleep(0.25)
            with self._working_lock:
                enabled = self._working_enabled
            if not enabled:
                continue
            if self._stream_mode is not None:
                self._clear_working_line()
                continue
            if time.monotonic() - self._last_activity_at < 0.8:
                self._clear_working_line()
                continue
            dots = "." * ((self._working_tick % 3) + 1)
            self._working_tick += 1
            width = max(20, self.console.size.width)
            message = cells.set_cell_size(f"SqlMate {self._working_label}{dots}", width)
            sys.stdout.write(f"\r\x1b[2K\x1b[38;2;160;160;160m{message}\x1b[0m")
            sys.stdout.flush()
            self._working_visible = True

    def _clear_working_line(self) -> None:
        """清空当前 working 行，避免覆盖正式输出。"""
        if not self._working_visible:
            return
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()
        self._working_visible = False

    def _render_tool_output_lines(self, text: str) -> list[str]:
        """按当前展示模式渲染工具输出。"""
        if self.show_think_stream:
            return [line for line in str(text).splitlines()] or [""]

        try:
            payload = json.loads(text)
        except Exception:
            preview = text if len(text) <= 800 else text[:800] + " ...[truncated]"
            return [f"[tool output] {preview}"]

        if not isinstance(payload, dict):
            preview = text if len(text) <= 800 else text[:800] + " ...[truncated]"
            return [f"[tool output] {preview}"]

        if "found_items" in payload or "found_topics" in payload or "missing_topics" in payload:
            return self._summarize_knowledge_payload(payload)

        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        preview = pretty if len(pretty) <= 1200 else pretty[:1200] + "\n...[truncated]"
        return preview.splitlines()

    def _summarize_knowledge_payload(self, payload: dict[str, Any]) -> list[str]:
        """摘要展示知识检索结果。"""
        stage = str(payload.get("stage") or "unknown")
        found_topics = payload.get("found_topics") or []
        missing_topics = payload.get("missing_topics") or []
        found_items = payload.get("found_items") or []
        lines = [
            f"[tool output] knowledge stage={stage}",
            f"found_topics: {', '.join(map(str, found_topics[:6])) or 'none'}",
        ]
        if len(found_topics) > 6:
            lines.append(f"found_topics_more: +{len(found_topics) - 6}")
        if missing_topics:
            lines.append(f"missing_topics: {', '.join(map(str, missing_topics[:4]))}")
            if len(missing_topics) > 4:
                lines.append(f"missing_topics_more: +{len(missing_topics) - 4}")
        for index, item in enumerate(found_items[:3], start=1):
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic") or "unknown")
            source = str(item.get("source") or "unknown")
            content = str(item.get("content") or "").replace("\n", " ").strip()
            if len(content) > 220:
                content = content[:220] + " ..."
            lines.append(f"evidence[{index}]: {topic} | {source}")
            if content:
                lines.append(f"  {content}")
        if len(found_items) > 3:
            lines.append(f"more_evidence: +{len(found_items) - 3}")
        summary = str(payload.get("distilled_summary") or "").replace("\n", " ").strip()
        if summary:
            if len(summary) > 280:
                summary = summary[:280] + " ..."
            lines.append(f"summary: {summary}")
        return lines

    def _wordmark(self) -> list[Text]:
        """返回缩小后的 SQLMATE 字样。"""
        colors = [
            (255, 72, 72),
            (235, 64, 120),
            (210, 58, 170),
            (170, 72, 220),
            (132, 84, 255),
        ]
        return [
            self._gradient_ascii("  ██████   ██████  ██      ███    ███  █████  ████████ ███████ ", colors),
            self._gradient_ascii("  ██      ██    ██ ██      ████  ████ ██   ██    ██    ██      ", colors),
            self._gradient_ascii("  ██████  ██    ██ ██      ██ ████ ██ ███████    ██    █████   ", colors),
            self._gradient_ascii("      ██  ██ ▄▄ ██ ██      ██  ██  ██ ██   ██    ██    ██      ", colors),
            self._gradient_ascii("  ██████   ██████  ███████ ██      ██ ██   ██    ██    ███████ ", colors),
        ]

    def _render_banner_frame(self, cat_shift: int, clear: bool) -> None:
        """渲染一帧 banner，字标在上，猫位于下方并左右摆动。"""
        if clear:
            self.console.clear(home=True)
        wordmark = self._wordmark()
        cat = self._pixel_cat(shift=cat_shift)
        for line in wordmark:
            self.console.print(line)
        for line in cat:
            self.console.print(line)

    def _redraw_banner_cat(self, shift: int) -> None:
        """在不重绘字标的前提下，仅重绘猫猫区域。"""
        cat = self._pixel_cat(shift=shift)
        up_lines = len(cat) + 3
        sys.stdout.write("\x1b7")
        sys.stdout.write(f"\x1b[{up_lines}A")
        for index, line in enumerate(cat):
            sys.stdout.write("\r\x1b[2K")
            self.console.print(line, end="")
            if index < len(cat) - 1:
                sys.stdout.write("\n")
        sys.stdout.write("\x1b8")
        sys.stdout.flush()

    def _banner_max_shift(self) -> int:
        """根据终端宽度计算猫猫最大横向移动距离。"""
        cat_width = max(len(line.plain) for line in self._pixel_cat(shift=0))
        return max(0, self.console.size.width - cat_width - 1)

    def _gradient_ascii(self, line: str, colors: list[tuple[int, int, int]]) -> Text:
        """为 wordmark 生成从暖色过渡到紫色的渐变。"""
        text = Text()
        visible_positions = [index for index, char in enumerate(line) if char != " "]
        if not visible_positions:
            return Text(line)
        count = len(visible_positions)
        last = max(1, count - 1)
        visible_index = 0
        for char in line:
            if char == " ":
                text.append(char)
                continue
            ratio = visible_index / last
            r, g, b = self._interpolate_palette(colors, ratio)
            text.append(char, style=f"rgb({r},{g},{b})")
            visible_index += 1
        return text

    def _interpolate_palette(self, colors: list[tuple[int, int, int]], ratio: float) -> tuple[int, int, int]:
        """在多段调色板之间插值，得到单个字符颜色。"""
        if ratio <= 0:
            return colors[0]
        if ratio >= 1:
            return colors[-1]
        segments = len(colors) - 1
        scaled = ratio * segments
        index = min(int(scaled), segments - 1)
        local = scaled - index
        start = colors[index]
        end = colors[index + 1]
        return tuple(int(start[channel] + (end[channel] - start[channel]) * local) for channel in range(3))

    def _format_global_plan(self, planner_output: PlannerOutput) -> Text:
        """格式化总计划，确保用户能直接读懂 planner 的真实意图。"""
        plan = planner_output.global_plan
        lines = [
            f"Goal: {plan.task_goal}",
            f"Dialect: {plan.db_dialect}",
            f"Feature: {plan.feature_under_test}",
        ]
        if plan.requirement_decomposition:
            requirement_lines: list[str] = []
            for item in plan.requirement_decomposition:
                parts = [
                    item.get("id"),
                    item.get("level"),
                    item.get("requirement") or item.get("test_point") or item.get("description"),
                    item.get("stage_owner") or item.get("owner"),
                ]
                fixed_dimensions = item.get("fixed_dimensions")
                if fixed_dimensions:
                    parts.append(f"fixed={json.dumps(fixed_dimensions, ensure_ascii=False)}")
                deferred_detail = item.get("deferred_detail")
                if deferred_detail:
                    parts.append(f"deferred={deferred_detail}")
                requirement_lines.append("  - " + " | ".join(str(part) for part in parts if part))
            lines.extend(
                [
                    "",
                    "Requirement decomposition:",
                    *requirement_lines,
                ]
            )
        if plan.coverage_matrix:
            lines.extend(
                [
                    "",
                    "Coverage matrix:",
                    *[
                        "  - "
                        + " | ".join(
                            str(part)
                            for part in [
                                item.get("dimension"),
                                item.get("values"),
                                item.get("combination_rule"),
                                item.get("stage_owner") or item.get("deferred_to"),
                            ]
                            if part
                        )
                        for item in plan.coverage_matrix
                    ],
                ]
            )
        lines.extend(
            [
                "",
                "Must cover:",
                *[f"  - {item}" for item in plan.must_cover],
                "",
                "Required setup objects:",
                *[
                    f"  - {item.get('type', 'object')}: {item.get('name') or item.get('table_name') or item.get('description', '-')}"
                    for item in plan.required_setup_objects
                ],
                "",
                "Validation strategy:",
                *[f"  - {item}" for item in plan.validation_strategy],
            ]
        )
        if plan.planning_considerations:
            lines.extend(["", "Planning considerations:", *[f"  - {item}" for item in plan.planning_considerations]])
        if plan.risks:
            lines.extend(["", "Risks:", *[f"  - {item}" for item in plan.risks]])
        outlines = [
            ("SETUP", planner_output.setup_outline),
            ("DDL", planner_output.ddl_outline),
            ("CORE", planner_output.core_outline),
        ]
        for stage, outline in outlines:
            lines.extend(
                [
                    "",
                    f"{stage} outline:",
                    f"  Objective: {outline.objective}",
                    "  Coverage focus:",
                    *[f"    - {item}" for item in outline.coverage_focus],
                    "  Test dimensions:",
                    *[f"    - {item}" for item in outline.test_dimensions],
                ]
            )
        return Text("\n".join(lines), style="white")

    def _format_phase_plan(self, plan: PhasePlan) -> Text:
        """格式化单阶段计划，供用户审批。"""
        lines = [
            f"Objective: {plan.objective}",
            "",
            "Test focus:",
            *[f"  - {item}" for item in plan.test_focus],
            "",
            "Input contract:",
            *[f"  - {item}" for item in plan.input_contract],
            "",
            "Output contract:",
            *[f"  - {item}" for item in plan.output_contract],
            "",
            "Required topics:",
            *[f"  - {item}" for item in plan.required_topics],
            "",
            "Evidence requirements:",
            *[f"  - {item}" for item in plan.evidence_requirements],
        ]
        if plan.assumptions:
            lines.extend(["", "Assumptions:", *[f"  - {item}" for item in plan.assumptions]])
        if plan.knowledge_gaps:
            lines.extend(["", "Knowledge gaps:", *[f"  - {item}" for item in plan.knowledge_gaps]])
        return Text("\n".join(lines), style="white")

    def _node_message(self, node_name: str, detail: str) -> str:
        """把内部节点名转换成用户可理解的最小提示。"""
        mapping = {
            "PLANNER_DRAFT": "正在生成总计划，请稍候...",
            "PLANNER_REVISION": "正在根据你的反馈修订总计划...",
            "SETUP_PLAN_REFINER": "正在生成或修订 SETUP 计划...",
            "DDL_PLAN_REFINER": "正在生成或修订 DDL 计划...",
            "CORE_PLAN_REFINER": "正在生成或修订 CORE 计划...",
            "SETUP": "正在生成 setup SQL...",
            "DDL": "正在生成 ddl SQL...",
            "CORE": "正在生成 core SQL...",
        }
        return mapping.get(node_name, "")

    def _pixel_cat(self, shift: int = 0) -> list[Text]:
        """返回一个三花配色的像素猫头像，可在初始化时左右轻微摆动。"""
        pad = " " * shift
        ear_gray = "rgb(138,146,158)"
        white = "rgb(246,246,240)"
        orange = "rgb(242,166,90)"
        body_black = "rgb(36,36,40)"

        return [
            self._cat_text(
                (pad + "                 ", None),
                ("▐▛", ear_gray),
                ("  ", None),
                ("▜▌", ear_gray),
            ),
            self._cat_text(
                (pad + "                ", None),
                ("▐", ear_gray),
                ("██", orange),
                ("██", white),
                ("██", ear_gray),
                ("▌", ear_gray),
            ),
            self._cat_text(
                (pad + "               ", None),
                ("▝", orange),
                ("▜█▌", orange),
                ("██", white),
                ("▐█▛▘", ear_gray),
            ),
            self._cat_text(
                (pad + "                 ", None),
                ("▝", orange),
                ("██", orange),
                ("██", white),
                ("▘", orange),
            ),
            self._cat_text(
                (pad + "                  ", None),
                ("▘", orange),
                ("  ", None),
                ("▝", orange),
            ),
        ]

    def _cat_text(self, *segments: tuple[str, str | None]) -> Text:
        """组装一行带分区颜色的猫猫 ASCII。"""
        text = Text()
        for content, style in segments:
            if style:
                text.append(content, style=style)
            else:
                text.append(content)
        return text
