"""子進程中執行 Claude Agent SDK triage — 避免巢狀 session 問題

用法: python -m src._triage_subprocess <json_input_file> <json_output_file>
"""

import asyncio
import json
import os  # noqa: F401 — used below before other imports
import sys

# 清除巢狀標記 — 備援機制（主要由呼叫端透過 clean_env 處理）
for _key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
    os.environ.pop(_key, None)
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)


async def _triage_async(prompt: str, model: str) -> dict | None:
    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        permission_mode="bypassPermissions",
        allowed_tools=[],
    )

    collected_text = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage) and message.result:
            collected_text.append(message.result)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    collected_text.append(block.text)

    if not collected_text:
        return None

    raw = "\n".join(collected_text).strip()

    # 清理 markdown code block
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    start = raw.find("{")
    if start == -1:
        return None

    decoder = json.JSONDecoder()
    result, _ = decoder.raw_decode(raw, start)
    return result


def main():
    input_file = sys.argv[1]
    output_file = sys.argv[2]

    with open(input_file, encoding="utf-8") as f:
        params = json.load(f)

    prompt = params["prompt"]
    model = params.get("model", "claude-sonnet-4-20250514")

    try:
        result = asyncio.run(_triage_async(prompt, model))
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump({"ok": True, "result": result}, f, ensure_ascii=False)
    except Exception as e:
        import traceback

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(
                {"ok": False, "error": str(e), "traceback": traceback.format_exc()},
                f,
                ensure_ascii=False,
            )


if __name__ == "__main__":
    main()
