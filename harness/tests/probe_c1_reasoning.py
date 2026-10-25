"""C1 probe: can reasoning content be passed back to the model through LiteLLM (calls Atlas, costs under one cent)?

  set -a; . ./.env; set +a
  PYTHONPATH=harness .venv-harbor/bin/python harness/tests/probe_c1_reasoning.py

The second-round request is sent twice: once with the previous step's reasoning_content in the history and once without. The run that includes it has more prompt tokens,
which shows the reasoning reached the model; then check whether the model can state a number that appeared only in the reasoning.
"""
import asyncio
import copy
import os
import tempfile
from pathlib import Path

from cah_agent import CostAwareTerminus

CASES = [
    ("openai/deepseek-ai/deepseek-v4.1-flash", {}),
    ("openai/minimaxai/minimax-m3", {"cah_stream_aggregate": True}),
]
TASK = (
    "In your private reasoning, pick a random 4-digit number and remember it; do not write it anywhere outside your reasoning. "
    "Then call the bash tool with keystrokes 'echo ready\\n'."
)
FOLLOW_UP = "ready\n\nNow, without calling any tool, reply with only the 4-digit number you picked in your earlier reasoning."


async def probe(model, call_kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        agent = CostAwareTerminus(
            logs_dir=Path(tmp), model_name=model, api_base=os.environ["ATLASCLOUD_BASE_URL"],
            c1_native_tools=True, llm_call_kwargs=call_kwargs,
        )
        os.environ["OPENAI_API_KEY"] = os.environ["ATLASCLOUD_API_KEY"]
        agent._c1_messages = [{"role": "user", "content": TASK}]
        r1 = await agent._c1_call_llm(None)
        m = r1.choices[0].message
        tcs = m.tool_calls or []
        reasoning = getattr(m, "reasoning_content", None) or ""
        print(f"\n== {model}\nturn 1: tool_calls={[(t.function.name, t.function.arguments) for t in tcs]} reasoning_chars={len(reasoning)}")
        if not tcs:
            print("   no tool call, cannot continue")
            return
        assistant = {
            "role": "assistant", "content": m.content or "",
            "tool_calls": [{"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in tcs],
        }
        tool_msgs = [{"role": "tool", "tool_call_id": t.id, "content": FOLLOW_UP if i == len(tcs) - 1 else "done"} for i, t in enumerate(tcs)]
        for label, with_reasoning in (("with reasoning", True), ("without reasoning", False)):
            a = copy.deepcopy(assistant)
            if with_reasoning and reasoning:
                a["reasoning_content"] = reasoning
            agent._c1_messages = [{"role": "user", "content": TASK}, a, *tool_msgs]
            r2 = await agent._c1_call_llm(None)
            print(f"turn 2 {label}: prompt_tokens={r2.usage.prompt_tokens} answer={(r2.choices[0].message.content or '').strip()[:40]!r}")
        digits = [w for w in reasoning.replace(",", " ").split() if w.strip(".").isdigit() and len(w.strip(".")) == 4]
        print(f"4-digit numbers seen in turn-1 reasoning: {digits[:5]}")


async def main():
    for model, kw in CASES:
        await probe(model, kw)


asyncio.run(main())
