"""Export the MCP tool definitions as an AgentCore Gateway tool schema.

Run:  python gateway_lambda/export_tool_schema.py

Gateway sees a Lambda target as an opaque ARN, so it needs to be told which
tools live behind it. Those definitions already exist: server.list_tools()
returns the 25 MCP Tool objects with name, description and inputSchema.

Gateway expects a BARE JSON ARRAY for lambda-function-arn targets (the
{"tools": [...]} wrapper is the mcp-server target format).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cloudprice_mcp import server

OUT = Path(__file__).resolve().parent / "tool-schema.json"

# AgentCore accepts a strict subset of JSON Schema. Anything else makes
# CreateGatewayTarget fail with "does not match expected format".
ALLOWED = {"type", "description", "properties", "required", "items"}

# Dropped keywords still carry meaning the model should see, so fold them
# into the description rather than discarding them.
def _fold(node: dict) -> str:
    bits = []
    if "enum" in node:
        bits.append("one of: " + ", ".join(str(v) for v in node["enum"]))
    if "default" in node:
        bits.append(f"default {node['default']}")
    if "minimum" in node:
        bits.append(f"min {node['minimum']}")
    if "maximum" in node:
        bits.append(f"max {node['maximum']}")
    return " (" + "; ".join(bits) + ")" if bits else ""


def _scalar_type(value):
    """AgentCore wants a single type string, not JSON Schema's list form.

    ["string", "null"] becomes "string" - the nullability is expressed by the
    field simply being absent from `required`.
    """
    if isinstance(value, list):
        non_null = [v for v in value if v != "null"]
        return non_null[0] if non_null else "string"
    return value


def sanitize(node):
    """Reduce a JSON Schema to the subset AgentCore accepts.

    The keyword filter applies ONLY at schema-node level. Inside `properties`
    the keys are user-defined property NAMES, so filtering them against the
    keyword allow-list silently deletes every argument the tool takes - which
    leaves the model unable to call it at all.
    """
    if not isinstance(node, dict):
        return node

    out = {}
    extra = _fold(node)

    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            # Map of name -> schema. Keep every name; sanitise the values.
            out["properties"] = {name: sanitize(sub) for name, sub in value.items()}
        elif key == "items":
            # A schema, or a list of schemas.
            out["items"] = ([sanitize(v) for v in value]
                            if isinstance(value, list) else sanitize(value))
        elif key == "required" and isinstance(value, list):
            out["required"] = list(value)
        elif key == "type":
            out["type"] = _scalar_type(value)
        elif key == "description":
            out["description"] = value
        # anything else (enum, default, minimum, maximum, additionalProperties)
        # is dropped - AgentCore rejects it - but _fold keeps its meaning.

    if extra:
        out["description"] = (out.get("description", "") + extra).strip()
    return out


def main() -> int:
    tools = asyncio.run(server.list_tools())

    schema = [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": sanitize(t.inputSchema),
        }
        for t in tools
    ]

    OUT.write_text(json.dumps(schema, indent=2), encoding="utf-8")

    size_kb = OUT.stat().st_size / 1024
    print(f"Wrote {OUT}")
    print(f"  {len(schema)} tools, {size_kb:.1f} KB")
    print(f"  first: {schema[0]['name']}")
    print(f"  last:  {schema[-1]['name']}")

    missing = [t["name"] for t in schema if not t.get("description")]
    print(f"  tools missing a description: {missing or 'none'}")

    # Prove nothing unsupported survived. Walk only schema-node keywords:
    # descend into properties VALUES (the keys there are argument names, not
    # keywords) and into items.
    found = set()

    def scan(node):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for sub in value.values():
                    scan(sub)
            elif key == "items":
                for sub in (value if isinstance(value, list) else [value]):
                    scan(sub)
            elif key not in ALLOWED:
                found.add(key)

    for t in schema:
        scan(t["inputSchema"])
    print(f"  unsupported keywords remaining: {sorted(found) or 'none'}")

    # A tool that declares required args but no properties cannot be called.
    broken = [
        t["name"] for t in schema
        if t["inputSchema"].get("required") and not t["inputSchema"].get("properties")
    ]
    print(f"  tools with required args but no properties: {broken or 'none'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
