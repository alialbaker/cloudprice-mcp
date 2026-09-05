"""AWS Lambda target for Amazon Bedrock AgentCore Gateway.

Exposes the cloudprice-mcp tools to AgentCore Gateway, which fronts this
function as an MCP server. The same engine that backs the stdio MCP server
(`cloudprice-mcp` on PyPI) answers here — there is no second implementation.

Gateway's calling convention, per the AWS samples:
  * `event` is the tool's arguments, flat (not wrapped in a body).
  * The tool name arrives out of band, on the Lambda context:
    `context.client_context.custom["bedrockAgentCoreToolName"]`, prefixed
    with the gateway target name and a `___` separator.

The engine needs no credentials and makes no network calls — every price
lives in bundled JSON — so this function's execution role needs CloudWatch
Logs and nothing else. It calls `cloudprice_mcp.dispatch`, which carries no
MCP dependency, so the deployment package stays free of the MCP SDK and the
web stack underneath it.
"""

from __future__ import annotations

import logging
from typing import Any

from cloudprice_mcp import dispatch

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Gateway prefixes tool names as "<target>___<tool>".
_TOOL_NAME_SEPARATOR = "___"

def _resolve_tool_name(context: Any) -> str | None:
    """Pull the bare tool name off the Lambda context.

    Returns None rather than raising, so a malformed invocation is reported
    as a tool error the model can read instead of a 500 it cannot.
    """
    client_context = getattr(context, "client_context", None)
    if client_context is None:
        return None

    custom = getattr(client_context, "custom", None) or {}
    extended = custom.get("bedrockAgentCoreToolName")
    if not extended:
        return None

    # Take the last segment: the target prefix is not part of the tool name,
    # and a target that is configured without one still works.
    return extended.split(_TOOL_NAME_SEPARATOR)[-1]


def lambda_handler(event: dict, context: Any) -> dict:
    tool_name = _resolve_tool_name(context)

    if tool_name is None:
        logger.error("No bedrockAgentCoreToolName on the invocation context")
        return {
            "error": "Tool name missing from the request context. This "
                     "function is only callable through AgentCore Gateway."
        }

    if tool_name not in dispatch.tool_names():
        logger.error("Unknown tool: %s", tool_name)
        return {
            "error": f"Unknown tool: {tool_name}",
            "available_tools": dispatch.tool_names(),
        }

    logger.info("Invoking %s with %d argument(s)", tool_name, len(event or {}))

    try:
        return dispatch.call(tool_name, event or {})
    except Exception as exc:
        # Return the failure rather than raising it. A raised exception reaches
        # the model as an opaque error; a returned one lets the model say what
        # actually went wrong.
        logger.exception("Tool %s failed", tool_name)
        return {"error": f"{tool_name} failed: {exc}"}
