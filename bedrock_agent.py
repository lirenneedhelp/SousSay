"""Standalone MCP client for the running server (Streamable HTTP).

    python bedrock_agent.py --smoke            # no AWS needed: lists tools, calls a few over real HTTP
    python bedrock_agent.py "what can I cook?"  # full Bedrock tool-calling loop (needs AWS credentials)

The server must be running:  uvicorn main:app --port 8000
"""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from fastmcp import Client

load_dotenv()
MCP_URL = os.getenv("MCP_URL", "http://localhost:8000/mcp/")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
SESSION_ID = "cli-smoke-session"


def result_text(result) -> str:
    return "".join(c.text for c in result.content if hasattr(c, "text"))


def to_bedrock_spec(tool) -> dict:
    schema = dict(getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {})
    schema.setdefault("type", "object")
    props = {k: v for k, v in schema.get("properties", {}).items() if k != "session_id"}
    schema["properties"] = props
    schema["required"] = [r for r in schema.get("required", []) if r != "session_id"]
    return {"toolSpec": {"name": tool.name, "description": tool.description or "", "inputSchema": {"json": schema}}}


async def smoke() -> None:
    async with Client(MCP_URL) as client:
        tools = await client.list_tools()
        print(f"{len(tools)} tools over Streamable HTTP:", ", ".join(t.name for t in tools))
        for name, args in [
            ("search_recipes", {"ingredients": ["chicken", "rice", "lemon"]}),
            ("start_recipe", {"recipe": "lemon soy chicken"}),
            ("set_timer", {"label": "smoke test", "seconds": 5}),
            ("list_timers", {}),
        ]:
            res = await client.call_tool(name, {"session_id": SESSION_ID, **args}, raise_on_error=False)
            print(f"- {name}: error={res.is_error} {result_text(res)[:110]}")


async def agent(prompt: str) -> None:
    import boto3  # imported lazily so --smoke works without AWS

    bedrock = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    async with Client(MCP_URL) as client:
        specs = [to_bedrock_spec(t) for t in await client.list_tools()]
        messages = [{"role": "user", "content": [{"text": prompt}]}]
        for _ in range(6):
            out = bedrock.converse(modelId=MODEL_ID, messages=messages, toolConfig={"tools": specs})["output"]["message"]
            messages.append(out)
            uses = [b["toolUse"] for b in out["content"] if "toolUse" in b]
            if not uses:
                print("Chef:", " ".join(b["text"] for b in out["content"] if "text" in b))
                return
            results = []
            for u in uses:
                res = await client.call_tool(u["name"], {**u["input"], "session_id": SESSION_ID}, raise_on_error=False)
                print(f"-> {u['name']}({json.dumps(u['input'])})")
                results.append({"toolResult": {"toolUseId": u["toolUseId"], "status": "error" if res.is_error else "success",
                                               "content": [{"text": result_text(res)}]}})
            messages.append({"role": "user", "content": results})
        print("Stopped after too many tool rounds.")


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        asyncio.run(smoke())
    else:
        asyncio.run(agent(" ".join(sys.argv[1:]) or "I'm hungry, what can I make for dinner tonight?"))
