"""Ask the shoot a question in English and let the model work out the SQL.

This is the query path, and it is the reason ClickHouse is here rather than a JSON file.
The model is handed the official ClickHouse MCP server as a tool and plans its own work:
inspect the schema, decide what to select, run it, read the rows, answer. Nothing here
contains a hardcoded query for the demo's question. If it did, the demo would be a puppet
show.

The MCP server runs read-only by default, which is the correct posture for a tool a language
model drives. The worst outcome of a confused query is a wrong answer, not a dropped table.

Every SQL statement the model issues is captured and returned alongside the answer. A
continuity report that cannot be audited is not usable evidence, and "show me the query you
ran" is the first thing anyone technical will ask.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.store import load_env  # noqa: E402

logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("mcp-clickhouse").setLevel(logging.ERROR)
logging.getLogger("docket.worker").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

DEFAULT_MODEL = os.environ.get("DAILIES_MODEL", "gemini-3.6-flash")
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash"]

# Enough turns to inspect the schema, run a query, notice it asked the wrong thing and try
# again. Bounded so a model that gets stuck in a loop costs one slow request, not a bill.
MAX_TOOL_TURNS = 6

# Tries per model before falling through to the next one. A question that needs several tool
# turns makes several model calls, so it meets several chances to catch a transient 503; with
# a single try each, the whole chain could exhaust in seconds and report every model down
# when nothing was actually wrong.
ATTEMPTS_PER_MODEL = 3
RETRY_BACKOFF_S = 2.0

SYSTEM_PROMPT = """\
You answer questions about a film shoot, using a ClickHouse database of what was observed in
each take.

Two tables, both in the `default` database:

  observations(scene_id, take_id, entity, category, position_h, depth, state, relative_to,
               moved_during_take, confidence, via, seen_at_timestamp, where_in_frame)
  takes(scene_id, take_id, scene_summary, duration_s, frames_used, model)

One row per object per take. `entity` is a normalized lowercase noun such as 'coffee mug'.
`position_h` is one of left, center, right, offscreen. `depth` is one of foreground,
midground, background, unknown.

How to work:

- Use run_query to look at the data. Inspect the schema first if you are unsure.
- An object being ABSENT from a take is expressed as the absence of a row, not a NULL. To
  find what is missing from a take, compare against the set of entities in the scene.
- `via = 'presence_check'` marks an observation recovered by re-examining the frames after a
  first pass missed it. It is still a real observation. Mention it only if asked.
- Answer in plain language, the way you would tell a camera assistant standing next to you.
  Name the takes. Be short.
- If the data does not answer the question, say so. Do not guess, and do not invent takes or
  objects that are not in the tables.
"""


@dataclass
class AskResult:
    question: str
    answer: str
    queries: list[str] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "queries": self.queries,
            "model": self.model,
        }


def _mcp_env() -> dict:
    """Environment for the ClickHouse MCP server, from the same config the loader uses."""
    env = dict(os.environ)
    env.setdefault("CLICKHOUSE_SECURE", "true")
    env.setdefault("CLICKHOUSE_VERIFY", "true")
    env.setdefault("CLICKHOUSE_PORT", "8443")
    env.setdefault("CLICKHOUSE_DATABASE", "default")
    # Quieter subprocess: the server prints a banner and an update notice on every start,
    # which is noise in a web request log.
    env.setdefault("FASTMCP_DISABLE_BANNER", "1")
    return env


def _server_command() -> str:
    """The mcp-clickhouse entry point inside this venv, falling back to PATH."""
    local = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "mcp-clickhouse.exe" if os.name == "nt" else "mcp-clickhouse"
    )
    return str(local) if local.exists() else "mcp-clickhouse"


def _clean_schema(schema: dict) -> dict:
    """Strip JSON Schema keywords the Gemini function-calling API rejects.

    MCP servers publish full JSON Schema. Gemini accepts a subset, and passing the extras
    through is a 400 rather than a warning, so they are removed rather than hoped about.
    """
    drop = {"$schema", "additionalProperties", "$defs", "definitions", "title", "default"}
    if not isinstance(schema, dict):
        return {}
    out = {}
    for key, value in schema.items():
        if key in drop:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _clean_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _clean_schema(value)
        elif isinstance(value, dict):
            out[key] = _clean_schema(value)
        else:
            out[key] = value
    # Gemini requires an object schema with properties for a function's parameters.
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def _tool_schema(tool) -> dict:
    """The tool's parameter schema, whatever this version of `mcp` calls it.

    The field is `inputSchema` up to mcp 1.27 and `input_schema` after. Reading only the
    camelCase name took the deployed service down with

        AttributeError: 'Tool' object has no attribute 'inputSchema'

    while every local run passed, because the local environment had 1.27.1 and the container
    built `mcp>=1.29.0` fresh from an unpinned requirement. Nothing in the repo was wrong;
    the two environments were simply not the same one. Requirements are pinned now, and this
    reads whichever name is present so a future rename fails soft rather than at runtime.
    """
    for attr in ("inputSchema", "input_schema"):
        schema = getattr(tool, attr, None)
        if schema:
            return dict(schema)
    return {"type": "object"}


def _mcp_tools_to_genai(mcp_tools, types):
    """Translate the MCP server's advertised tools into Gemini function declarations."""
    declarations = []
    for tool in mcp_tools:
        declarations.append(
            types.FunctionDeclaration(
                name=tool.name,
                description=(tool.description or "")[:1024],
                parameters=_clean_schema(_tool_schema(tool)),
            )
        )
    return [types.Tool(function_declarations=declarations)]


def _result_to_text(result) -> str:
    """Flatten an MCP tool result into something a model can read."""
    chunks = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks) if chunks else "(no rows)"


async def ask_async(question: str, scene_id: str | None = None, model: str = DEFAULT_MODEL) -> AskResult:
    from google import genai
    from google.genai import types
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("No API key. Set GOOGLE_API_KEY. https://aistudio.google.com/apikey")
    if not os.environ.get("CLICKHOUSE_HOST"):
        raise RuntimeError("CLICKHOUSE_HOST is not set. Copy .env.example to .env.")

    prompt = question if not scene_id else f"For scene '{scene_id}': {question}"
    client = genai.Client(api_key=api_key)

    params = StdioServerParameters(command=_server_command(), args=[], env=_mcp_env())

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = _mcp_tools_to_genai(listed.tools, types)

            # The loop is written out rather than delegated to the SDK's automatic function
            # calling, which fails here with "cannot pickle _asyncio.Future". Explicit is
            # better anyway: every step the model takes is visible and capturable, and
            # "which queries did it decide to run" is answerable rather than opaque.
            async def one_attempt(candidate: str) -> AskResult:
                """Run the whole tool conversation once, on one model.

                A fresh conversation every time on purpose: a half-finished tool exchange
                cannot be resumed against a new call, and replaying the old turns would
                count each query twice in the result.
                """
                contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
                queries: list[str] = []

                for _ in range(MAX_TOOL_TURNS):
                    response = await client.aio.models.generate_content(
                        model=candidate,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            tools=tools,
                            temperature=0.0,
                        ),
                    )
                    candidate_parts = response.candidates[0].content.parts or []
                    calls = [p.function_call for p in candidate_parts if getattr(p, "function_call", None)]

                    if not calls:
                        answer = (response.text or "").strip()
                        try:
                            from pipeline.telemetry import Run, record
                            record(Run(
                                operation="ask",
                                model=candidate,
                                latency_ms=0,
                                outcome="ok",
                                scene_id=scene_id or "",
                                findings=len(queries),
                                queries=queries,
                                detail=question,
                            ))
                        except Exception:
                            pass
                        return AskResult(
                            question=question, answer=answer, queries=queries, model=candidate
                        )

                    contents.append(response.candidates[0].content)
                    reply_parts = []
                    for call in calls:
                        args = dict(call.args or {})
                        sql = args.get("query") or args.get("sql")
                        queries.append(
                            str(sql).strip() if sql else f"-- {call.name}({', '.join(args)})"
                        )
                        try:
                            result = await session.call_tool(call.name, args)
                            output = _result_to_text(result)
                        except Exception as exc:
                            # Hand the failure back so the model can correct its own SQL
                            # instead of the whole request dying on one bad guess.
                            output = f"ERROR: {exc}"
                        reply_parts.append(
                            types.Part.from_function_response(
                                name=call.name, response={"result": output[:20000]}
                            )
                        )
                    contents.append(types.Content(role="user", parts=reply_parts))

                return AskResult(
                    question=question,
                    answer="Ran out of tool turns before reaching an answer.",
                    queries=queries,
                    model=candidate,
                )

            # Each model gets more than one go before the chain moves on. A harder question
            # spends several turns in the tool loop, so it meets several more chances to hit a
            # transient 503 than a simple one does, and with a single try per model the whole
            # chain could fall through in seconds and report every model unavailable when
            # nothing was really down. Retrying the same model after a short wait is what
            # recovers: pipeline/measure.py hit the same 503s and cleared them on retry.
            for candidate in [model] + [m for m in FALLBACK_MODELS if m != model]:
                for attempt in range(ATTEMPTS_PER_MODEL):
                    try:
                        return await one_attempt(candidate)
                    except Exception as exc:
                        text = str(exc)
                        if "503" not in text and "429" not in text:
                            raise
                        if attempt < ATTEMPTS_PER_MODEL - 1:
                            await asyncio.sleep(RETRY_BACKOFF_S * (2 ** attempt))

            raise RuntimeError(
                "The model API was rate limited or unavailable on every attempt "
                f"({', '.join([model] + [m for m in FALLBACK_MODELS if m != model])}, "
                f"{ATTEMPTS_PER_MODEL} tries each). This is upstream capacity, not a fault "
                "in the query. Try again in a moment."
            )


def ask(question: str, scene_id: str | None = None, model: str = DEFAULT_MODEL) -> AskResult:
    load_env()
    return asyncio.run(ask_async(question, scene_id, model))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="e.g. 'which takes is the smartphone missing from?'")
    parser.add_argument("--scene", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = ask(args.question, args.scene, args.model)
    except (RuntimeError, ValueError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nQuery failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    print(f"\nQ: {result.question}")
    print(f"\n{result.answer}\n")
    if result.queries:
        print(f"-- {len(result.queries)} statement(s) the agent chose to run --")
        for sql in result.queries:
            print(f"   {' '.join(sql.split())[:210]}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
