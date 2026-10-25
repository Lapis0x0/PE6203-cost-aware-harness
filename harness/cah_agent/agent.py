"""CostAwareTerminus: five independently switchable components on top of Terminus-2 in harbor 0.6.3.

See README.md and docs/failure-analysis.md.
With all five switches off, Terminus-2 behaviour is unchanged; the baseline still uses stock terminus-2.

  C1 c1_native_tools   native tool calls (bash / write_file / task_complete), self-maintained message history, reasoning continuous across the whole task
  C2 c2_verify_gate    pre-submit verification gate: back up Office files at the start, auto-check artifacts on completion and require item-by-item review
  C3 c3_stall_budget   time budget hints, repeated-action hints, auto Ctrl+C and shell restart when the terminal hangs
  C4 c4_domain_notes   append a generic section of environment and tool hints to the prompt
  C5 c5_obs_denoise    observation denoising: drop browser, dbus, font and other system noise lines, collapse consecutive repeated lines

C2 and C3 notes are appended only to the end of the current step's new observation; existing history is untouched, so prompt cache hits are unaffected.
"""

import asyncio
import base64
import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

import litellm
from litellm.exceptions import AuthenticationError as LiteLLMAuthenticationError
from litellm.exceptions import ContextWindowExceededError as LiteLLMContextWindowExceededError

from harbor.agents.terminus_2.terminus_2 import Command, Terminus2
from harbor.llms.base import ContextLengthExceededError, LLMResponse
from harbor.llms.utils import add_anthropic_caching

try:  # provided by runner patch harbor-0.6.3-stream-aggregate.patch, used for MiniMax M3 on Atlas
    from harbor.llms.lite_llm import _cah_acompletion
except ImportError:  # pragma: no cover
    _cah_acompletion = None

from cah_agent.notes import DOMAIN_NOTES, VERIFY_CHECKLIST

HERE = Path(__file__).parent
TERMINAL_ANCHOR = "\n\nCurrent terminal state:\n{terminal_state}"
WRITE_CHUNK = 60000  # base64 characters, a multiple of 4; a single command-line argument must not exceed 128KB


def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@dataclass
class WriteFile(Command):
    """C1 write_file action. Subclasses Command so the stock loop can still read keystrokes when recording the trajectory."""

    path: str = ""
    content: str = ""
    call_id: str = ""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Type into the interactive terminal (tmux) and wait before reading its output. "
            "By default Enter is pressed afterwards, so a command runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keystrokes": {
                        "type": "string",
                        "description": "A command, text, or a single tmux key name such as C-c, Escape, Up. Empty string to just wait.",
                    },
                    "duration": {
                        "type": "number",
                        "description": "Seconds to wait before reading output (0.1 for instant commands, 1.0 default, at most 60).",
                    },
                    "press_enter": {
                        "type": "boolean",
                        "description": "Press Enter after the keystrokes (default true). Set false to type without submitting, e.g. inside an editor. Ignored for single key names.",
                    },
                },
                "required": ["keystrokes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file exactly as given (creates parent directories, overwrites existing files). "
            "Prefer absolute paths; relative paths are resolved from the task's working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Declare the task finished. You will be asked to confirm once before grading.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# C1: single tmux key names; no Enter is appended
TMUX_KEY_RE = re.compile(
    r"^\s*((C|M|S)-\S+|Escape|Enter|Tab|BTab|BSpace|Space|Up|Down|Left|Right|Home|End|"
    r"PageUp|PageDown|PgUp|PgDn|NPage|PPage|DC|IC|F\d{1,2})\s*$"
)



def _c1_keys(args: dict) -> str:
    """Map bash tool arguments to actual keystrokes."""
    keys = str(args.get("keystrokes", ""))
    if keys.endswith("\\n") and "\n" not in keys:
        # Argument check: the model wrote the trailing newline as a literal backslash + n, so the command would not run; turn it back into a real newline
        keys = keys[:-2] + "\n"
    if keys and not keys.endswith("\n") and _flag(args.get("press_enter", True)) and not TMUX_KEY_RE.match(keys):
        # Smoke runs showed 40-60% of M3 commands lack a newline and are typed but not executed; the tool presses Enter by default
        keys += "\n"
    return keys


# C5: these lines are system noise and carry no task information
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
NOISE_RES = [
    re.compile(p)
    for p in (
        r"^\s*\[\d+:\d+:\d+/\d+\.\d+:(ERROR|WARNING|INFO|VERBOSE\d*):",  # Chrome / Chromium log lines
        r"Fontconfig (error|warning)",
        r"^\s*ALSA lib ",
        r"^\s*libva (error|info)",
        r"Gtk-(WARNING|CRITICAL|Message)",
        r"^\s*\(process:\d+\): \S+-(WARNING|CRITICAL)",
        r"Failed to connect to (the )?(session |system )?bus",
        r"dbus-daemon\[\d+\]",
    )
]

# C3: these observation lines count as error signals, used to detect "the same error repeating"
ERROR_LINE_RE = re.compile(
    r"error|traceback|not found|no such file|permission denied|failed|cannot|unable to", re.IGNORECASE
)


class CostAwareTerminus(Terminus2):
    def __init__(
        self,
        *args,
        c1_native_tools=False,
        c2_verify_gate=False,
        c3_stall_budget=False,
        c4_domain_notes=False,
        c5_obs_denoise=False,
        budget_sec=2400,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._c1 = _flag(c1_native_tools)
        self._c2 = _flag(c2_verify_gate)
        self._c3 = _flag(c3_stall_budget)
        self._c4 = _flag(c4_domain_notes)
        self._c5 = _flag(c5_obs_denoise)
        self._budget_sec = float(budget_sec)

        if self._c1:
            self._prompt_template = (HERE / "templates" / "tools.txt").read_text()
        if self._c4:
            if TERMINAL_ANCHOR not in self._prompt_template:
                raise RuntimeError("prompt template has no terminal-state anchor for C4")
            self._prompt_template = self._prompt_template.replace(
                TERMINAL_ANCHOR, DOMAIN_NOTES.rstrip("\n") + TERMINAL_ANCHOR, 1
            )

        self._cah_env = None
        self._cah_t0 = 0.0

        # C1 state
        self._c1_messages: list[dict] = []
        self._c1_pending_calls: list[dict] = []

        # C2 state
        self._c2_max_gates = 2
        self._c2_gates = 0
        self._c2_run_check = False
        self._c2_refusing = False
        self._c2_gate_active = False
        self._c2_report = ""
        self._c2_last_problem = False

        # C3 state
        self._c3_milestones_done: set[float] = set()
        self._c3_cmd_hist: list[str] = []
        self._c3_err_hist: list[tuple] = []
        self._c3_step = 0
        self._c3_last_rep_warn = -100
        self._c3_buf_hash: str | None = None
        self._c3_stuck = 0

    # ------------------------------------------------------------------ entry point

    async def run(self, instruction, environment, context) -> None:
        self._cah_env = environment
        self._cah_t0 = time.time()
        if self._c2:
            await self._c2_exec("backup")
        await super().run(instruction, environment, context)

    def _get_error_response_type(self) -> str:
        if self._c1:
            return "tool call"
        return super()._get_error_response_type()

    async def _handle_llm_interaction(
        self, chat, prompt, logging_paths, original_instruction="", session=None
    ):
        if self._c1:
            result = await self._c1_interaction(chat, prompt, logging_paths)
        else:
            result = await super()._handle_llm_interaction(
                chat, prompt, logging_paths, original_instruction, session
            )
        commands, done, feedback, analysis, plan, llm_response = result

        if self._c2 and done and not (feedback and "ERROR:" in feedback):
            if not self._pending_completion:
                # First completion claim: open the verification gate if the limit is not exceeded
                if self._c2_gates < self._c2_max_gates:
                    self._c2_gates += 1
                    self._c2_run_check = True
                    self._c2_gate_active = True
            elif self._c2_last_problem and self._c2_gates < self._c2_max_gates:
                # Completion confirmed but the auto-check still reports problems: block once and return to working
                done = False
                self._c2_run_check = True
                self._c2_refusing = True

        return commands, done, feedback, analysis, plan, llm_response

    async def _execute_commands(self, commands, session):
        if self._c1:
            timeout_occurred, output = await self._c1_execute(commands, session)
        else:
            timeout_occurred, output = await super()._execute_commands(commands, session)

        notes: list[str] = []
        if self._c3:
            notes += await self._c3_notes(commands, session, output)
        if self._c2 and self._c2_run_check:
            self._c2_run_check = False
            self._c2_report = await self._c2_exec("check")
            self._c2_last_problem = "PROBLEM" in self._c2_report
            if self._c2_refusing:
                self._c2_refusing = False
                notes.append(
                    "[harness] You confirmed completion, but the automatic file check still reports problems:\n"
                    f"{self._c2_report}\n"
                    "Fix them, or if a reported problem is exactly what the task requires, say so explicitly. "
                    "Then mark the task complete again."
                )
        if notes:
            output = output + "\n\n" + "\n".join(notes)
        return timeout_occurred, output

    def _get_completion_confirmation_message(self, terminal_output: str) -> str:
        again = (
            "call task_complete again"
            if self._c1
            else 'include "task_complete": true in your JSON response again'
        )
        if self._c2 and self._c2_gate_active:
            self._c2_gate_active = False
            report = ""
            if self._c2_report and not self._c2_report.startswith("OK: 0 "):
                report = (
                    "[harness] Automatic check of office files created or changed during this task:\n"
                    f"{self._c2_report}\n\n"
                )
            return (
                f"Current terminal state:\n{terminal_output}\n\n"
                f"{report}{VERIFY_CHECKLIST} To mark it complete, {again}."
            )
        if self._c1:
            return (
                f"Current terminal state:\n{terminal_output}\n\n"
                "Are you sure you want to mark the task as complete? "
                "This will trigger your solution to be graded and you won't be able to "
                f"make any further corrections. If so, {again}."
            )
        return super()._get_completion_confirmation_message(terminal_output)

    def _limit_output_length(self, output: str, max_bytes: int = 10000) -> str:
        if self._c5:
            output = self._c5_denoise(output)
        return super()._limit_output_length(output, max_bytes)

    # ------------------------------------------------------------------ in-container execution

    def _exec_user(self):
        if self._session is not None:
            return getattr(self._session, "_user", None)
        return getattr(self._cah_env, "default_user", None)

    async def _env_exec(self, command: str, timeout_sec: int = 120):
        return await self._cah_env.exec(command=command, user=self._exec_user(), timeout_sec=timeout_sec)

    # ------------------------------------------------------------------ C1

    async def _c1_interaction(self, chat, prompt, logging_paths):
        logging_path, prompt_path, response_path = logging_paths
        if prompt_path is not None:
            prompt_path.write_text(prompt)

        # Feed the previous observation back into the history: if a tool_call is pending, send it as a tool message rather than a user message, so the reasoning chain continues
        if self._c1_pending_calls:
            last = len(self._c1_pending_calls) - 1
            for i, call in enumerate(self._c1_pending_calls):
                status = call.get("status")
                if i == last:
                    content = f"{status}\n\n{prompt}" if status else prompt
                else:
                    content = status or "Executed. The terminal output is in the last tool result of this turn."
                self._c1_messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
            self._c1_pending_calls = []
        else:
            self._c1_messages.append({"role": "user", "content": prompt})

        response = await self._c1_call_llm(logging_path)

        usage = self._llm._extract_usage_info(response)
        if usage is not None:
            chat._cumulative_input_tokens += usage.prompt_tokens
            chat._cumulative_output_tokens += usage.completion_tokens
            chat._cumulative_cache_tokens += usage.cache_tokens
            chat._cumulative_cost += usage.cost_usd

        choice = response.choices[0]
        msg = choice.message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None)
        tool_calls = list(getattr(msg, "tool_calls", None) or [])

        assistant: dict = {"role": "assistant", "content": content}
        if tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                }
                for tc in tool_calls
            ]
        if reasoning:
            assistant["reasoning_content"] = reasoning
        thinking_blocks = getattr(msg, "thinking_blocks", None)
        if thinking_blocks:
            assistant["thinking_blocks"] = thinking_blocks
        self._c1_messages.append(assistant)

        commands: list[Command] = []
        done = False
        pending: list[dict] = []
        plan_parts: list[str] = []
        for tc in tool_calls:
            name = tc.function.name
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as e:
                pending.append(
                    {"id": tc.id, "status": f"ERROR: arguments are not valid JSON ({e}); this call was not executed."}
                )
                continue
            if name == "bash":
                try:
                    duration = float(args.get("duration", 1.0) or 1.0)
                except (TypeError, ValueError):
                    duration = 1.0
                keys = _c1_keys(args)
                commands.append(Command(keystrokes=keys, duration_sec=min(max(duration, 0.0), 60.0)))
                pending.append({"id": tc.id, "status": None})
                plan_parts.append(f"bash: {keys.strip()[:200]}")
            elif name == "write_file":
                path = str(args.get("path", ""))
                body = str(args.get("content", ""))
                commands.append(
                    WriteFile(
                        keystrokes=f"[write_file {path} ({len(body)} chars)]",
                        duration_sec=0.0,
                        path=path,
                        content=body,
                        call_id=tc.id,
                    )
                )
                pending.append({"id": tc.id, "status": None})
                plan_parts.append(f"write_file: {path}")
            elif name == "task_complete":
                done = True
                pending.append({"id": tc.id, "status": None})
                plan_parts.append("task_complete")
            else:
                pending.append({"id": tc.id, "status": f"ERROR: unknown tool {name!r}."})
        self._c1_pending_calls = pending

        feedback = ""
        if not tool_calls:
            if choice.finish_reason == "length":
                feedback = "ERROR: Your response hit the output length limit before any tool call. Use shorter steps."
            else:
                feedback = "ERROR: No tool call found. Every turn must call at least one tool (bash, write_file or task_complete)."

        if response_path is not None:
            response_path.write_text(
                content + "\n\n" + json.dumps(assistant.get("tool_calls", []), ensure_ascii=False, indent=1)
            )

        llm_response = LLMResponse(
            content=content,
            reasoning_content=reasoning,
            model_name=getattr(response, "model", None),
            usage=usage,
        )
        return commands, done, feedback, content, "\n".join(plan_parts), llm_response

    async def _c1_call_llm(self, logging_path):
        call_kwargs = dict(self._llm_call_kwargs)
        stream = bool(call_kwargs.pop("cah_stream_aggregate", False))
        request = {
            **self._llm._build_base_kwargs(logging_path),
            "messages": add_anthropic_caching(self._c1_messages, self._model_name),
            "tools": TOOLS,
            "temperature": self._llm._temperature,
            "reasoning_effort": self._llm._reasoning_effort,
            **call_kwargs,
        }
        for attempt in range(3):
            try:
                if _cah_acompletion is not None:
                    return await _cah_acompletion(request, stream)
                return await litellm.acompletion(**request)
            except (LiteLLMContextWindowExceededError, ContextLengthExceededError):
                raise ContextLengthExceededError
            except LiteLLMAuthenticationError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:  # retry at most 3 times, same as harbor's LiteLLM.call
                if attempt == 2:
                    self._llm._handle_litellm_error(e)
                await asyncio.sleep(4 * 2**attempt)
        raise RuntimeError("unreachable")

    async def _c1_execute(self, commands, session):
        for command in commands:
            if isinstance(command, WriteFile):
                status = await self._c1_write_file(command)
                for call in self._c1_pending_calls:
                    if call["id"] == command.call_id:
                        call["status"] = status
                continue
            try:
                await session.send_keys(command.keystrokes, block=False, min_timeout_sec=command.duration_sec)
            except TimeoutError:
                return True, self._timeout_template.format(
                    timeout_sec=command.duration_sec,
                    command=command.keystrokes,
                    terminal_state=self._limit_output_length(await session.get_incremental_output()),
                )
        return False, self._limit_output_length(await session.get_incremental_output())

    async def _c1_write_file(self, wf: WriteFile) -> str:
        if not wf.path:
            return "ERROR: write_file needs a path."
        data = wf.content.encode("utf-8")
        b64 = base64.b64encode(data).decode()
        target = shlex.quote(wf.path)
        try:
            r = await self._env_exec(f'mkdir -p "$(dirname {target})" && : > {target}')
            if r.return_code != 0:
                return f"ERROR writing {wf.path}: {(r.stderr or r.stdout or '').strip()[:500]}"
            for i in range(0, len(b64), WRITE_CHUNK):
                r = await self._env_exec(f"echo {b64[i:i + WRITE_CHUNK]} | base64 -d >> {target}")
                if r.return_code != 0:
                    return f"ERROR writing {wf.path}: {(r.stderr or r.stdout or '').strip()[:500]}"
        except Exception as e:
            return f"ERROR writing {wf.path}: {type(e).__name__}: {e}"
        return f"Wrote {len(data)} bytes to {wf.path}."

    # ------------------------------------------------------------------ C2

    async def _c2_exec(self, mode: str) -> str:
        script = base64.b64encode((HERE / "artifact_check.py").read_bytes()).decode()
        cmd = f"echo {script} | base64 -d > /tmp/.cah_check.py && python3 /tmp/.cah_check.py {mode}"
        try:
            r = await self._env_exec(cmd, timeout_sec=180)
            out = (r.stdout or "").strip() or (r.stderr or "").strip()
        except Exception as e:
            out = f"artifact check skipped: {type(e).__name__}: {e}"
        return out[:3000]

    # ------------------------------------------------------------------ C3

    async def _c3_notes(self, commands, session, output: str) -> list[str]:
        self._c3_step += 1
        notes: list[str] = []

        # Time budget: one hint each at 50%, 75% and 90% used (only the highest threshold crossed is reported)
        elapsed = time.time() - self._cah_t0
        frac = elapsed / self._budget_sec if self._budget_sec > 0 else 0
        crossed = [m for m in (0.5, 0.75, 0.9) if frac >= m and m not in self._c3_milestones_done]
        if crossed:
            self._c3_milestones_done.update(crossed)
            m = max(crossed)
            left = max(0, int((self._budget_sec - elapsed) // 60))
            text = (
                f"[harness] Time check: about {int(elapsed // 60)} of {int(self._budget_sec // 60)} minutes used, "
                f"roughly {left} minutes left."
            )
            if m == 0.5:
                text += " If the required result does not exist yet, produce a working version now and refine it afterwards."
            elif m >= 0.9:
                text += " Make sure the required result is saved in its final location now."
            notes.append(text)

        # Repeated actions: the same batch of commands 3 times in the last 8 steps, or the same error line for 3 consecutive steps
        key = "\n".join(c.keystrokes.strip() for c in commands if c.keystrokes.strip())
        repeated = 0
        if key:
            self._c3_cmd_hist.append(key)
            repeated = self._c3_cmd_hist[-8:].count(key)
        err_sig = tuple(sorted({ln.strip()[:160] for ln in output.splitlines() if ERROR_LINE_RE.search(ln)}))[:6]
        self._c3_err_hist.append(err_sig)
        same_errors = len(self._c3_err_hist) >= 3 and err_sig and all(e == err_sig for e in self._c3_err_hist[-3:])
        if (repeated >= 3 or same_errors) and self._c3_step - self._c3_last_rep_warn >= 5:
            self._c3_last_rep_warn = self._c3_step
            notes.append(
                "[harness] You have tried essentially the same thing several times with the same result. "
                "Step back: re-read the task, check the relevant tool's --help and what already exists in the environment, "
                "and try a different approach."
            )

        # Terminal hang: keys were sent but the whole pane buffer stayed unchanged
        sent = any(c.keystrokes.strip() for c in commands if not isinstance(c, WriteFile))
        try:
            buf = await session.capture_pane(capture_entire=True)
        except Exception:
            buf = None
        if buf is not None:
            h = hashlib.sha1(buf.encode("utf-8", "ignore")).hexdigest()
            if sent and h == self._c3_buf_hash:
                self._c3_stuck += 1
            elif sent:
                self._c3_stuck = 0
            self._c3_buf_hash = h
        if self._c3_stuck == 3:
            try:
                await session.send_keys(["C-c"], block=False, min_timeout_sec=1.0)
            except Exception:
                pass
            notes.append(
                "[harness] The terminal has shown no change for 3 steps although keys were sent; it looks stuck. "
                "The harness sent Ctrl+C."
            )
        elif self._c3_stuck >= 5:
            self._c3_stuck = 0
            name = getattr(session, "_session_name", self.name())
            try:
                await self._env_exec(f"tmux respawn-pane -k -t {shlex.quote(name)} 'bash --login'")
                session._previous_buffer = None
                notes.append(
                    "[harness] The terminal was still unresponsive, so the harness restarted the shell. "
                    "The previous shell state (current directory, variables, running programs) is gone."
                )
            except Exception as e:
                notes.append(f"[harness] The terminal looks stuck and restarting the shell failed: {e}")
        return notes

    # ------------------------------------------------------------------ C5

    def _c5_denoise(self, output: str) -> str:
        lines = ANSI_RE.sub("", output).split("\n")
        kept: list[str] = []
        removed = 0
        for ln in lines:
            if any(r.search(ln) for r in NOISE_RES):
                removed += 1
                continue
            kept.append(ln)
        collapsed: list[str] = []
        i = 0
        while i < len(kept):
            j = i
            while j + 1 < len(kept) and kept[j + 1] == kept[i]:
                j += 1
            run = j - i + 1
            if run >= 3 and kept[i].strip():
                collapsed.append(kept[i])
                collapsed.append(f"[previous line repeated {run - 1} more times]")
            else:
                collapsed.extend(kept[i : j + 1])
            i = j + 1
        text = "\n".join(collapsed)
        if removed:
            text += f"\n[harness: {removed} system noise lines (browser, dbus, font, audio warnings) removed]"
        return text
