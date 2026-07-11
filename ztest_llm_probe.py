"""Probe the REAL agent code path: aask_tool (native tool-use, streaming).

The agent's think loop calls llm.aask_tool(req, system_msgs=..., tools=...) which
goes through _achat_completion_stream_tool -> aclient.messages.stream(...). That
is a different path from a plain acompletion, and the one that can hang on SSE
pings. This probe exercises exactly that path, bounded by a short wall-clock.

Not a pytest; run directly.
"""
import asyncio
import time

from mote.common.config.loader import load_config
from mote.router.llm.llm_provider_registry import create_llm_instance, resolve_api_type

# A minimal but real native tool spec (OpenAI-function shape; the provider
# converts it). Mirrors what ToolExecutor.get_native_tool_specs emits.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
            },
        },
    }
]

SYSTEM = "You are a helpful assistant. Use tools when appropriate."


async def run(label, coro_factory, bound=60):
    t0 = time.time()
    try:
        rsp = await asyncio.wait_for(coro_factory(), timeout=bound)
        dt = time.time() - t0
        content = getattr(rsp, "content", rsp)
        calls = getattr(rsp, "tool_calls", None)
        print(f"[{label}] OK in {dt:.2f}s content={content!r} tool_calls={calls}")
    except asyncio.TimeoutError:
        dt = time.time() - t0
        print(f"[{label}] TIMEOUT after {dt:.2f}s (HUNG — reproduces the stuck call)")
    except Exception as e:  # noqa: BLE001
        dt = time.time() - t0
        import traceback

        print(f"[{label}] ERROR after {dt:.2f}s: {type(e).__name__}: {e}")
        traceback.print_exc()


async def main():
    cfg = load_config()
    llm_cfg = cfg.llm
    print(
        f"provider={resolve_api_type(llm_cfg)} base_url={llm_cfg.base_url} "
        f"model={llm_cfg.model} proxy={llm_cfg.proxy!r}"
    )
    llm = create_llm_instance(llm_cfg)

    # Path A: plain blocking completion (already known to work) — baseline.
    await run("acompletion", lambda: llm.acompletion([{"role": "user", "content": "reply with the single word: pong"}]))

    # Path B: aask_tool WITH tools, streaming (the agent's real think path).
    await run(
        "aask_tool.stream",
        lambda: llm.aask_tool(
            "What's the weather in Beijing? Use the tool.", system_msgs=[SYSTEM], tools=TOOLS, stream=True
        ),
    )

    # Path C: aask_tool WITH tools, non-streaming (isolates streaming as the cause).
    await run(
        "aask_tool.blocking",
        lambda: llm.aask_tool(
            "What's the weather in Beijing? Use the tool.", system_msgs=[SYSTEM], tools=TOOLS, stream=False
        ),
    )

    # Path D: plain aask streaming text (no tools) — the other think channel.
    await run("aask.stream", lambda: llm.aask("reply with the single word: pong", system_msgs=[SYSTEM], stream=True))


if __name__ == "__main__":
    asyncio.run(main())
