"""Ask a question in plain English; the model answers using your gateway tools.

Runs locally, but every part of the deployed stack is exercised:

    you -> Bedrock (orchestrator inference profile)
             |  decides which tool to call
             v
        Gateway (SigV4, MCP) -> Cedar (LOG_ONLY) -> Lambda -> cloudprice_mcp

Because it invokes through the application inference profile, this is also
what makes the per-role CloudWatch metrics and the dashboard light up.

Usage:
    python gateway_lambda/ask.py "what is the cheapest cloud for 8 vcpu 32gb?"
"""
import json
import sys

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = "us-east-1"
GATEWAY = "https://cloudprice-gateway-xprwhtvo85.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
# The tagged profile, not a bare model id - that is what gives per-role cost
# and usage attribution.
PROFILE = "arn:aws:bedrock:us-east-1:891376971780:application-inference-profile/9kxwed2urtar"

session = boto3.Session(region_name=REGION)
creds = session.get_credentials().get_frozen_credentials()
bedrock = session.client("bedrock-runtime")


def mcp(method, params=None, rpc_id=1):
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}})
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    req = AWSRequest(method="POST", url=GATEWAY, data=body, headers=headers)
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(req)
    r = httpx.post(GATEWAY, content=body, headers=dict(req.headers), timeout=90)
    text = r.text
    if text.startswith(("event:", "data:")):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    return json.loads(text)


question = " ".join(sys.argv[1:]) or "Cheapest cloud for 8 vCPU and 32 GB RAM?"

print("Fetching tools from the gateway...")
tools = mcp("tools/list")["result"]["tools"]
tool_config = {"tools": [
    {"toolSpec": {
        "name": t["name"],
        "description": (t.get("description") or t["name"])[:1000],
        "inputSchema": {"json": t.get("inputSchema") or {"type": "object", "properties": {}}},
    }} for t in tools
]}
print(f"  {len(tools)} tools available\n")

messages = [{"role": "user", "content": [{"text": question}]}]
print(f"Q: {question}\n")

for turn in range(6):
    resp = bedrock.converse(
        modelId=PROFILE,
        messages=messages,
        toolConfig=tool_config,
        system=[{"text": "You are a FinOps assistant. Use the tools for any pricing "
                         "question; never guess numbers. Be concise and cite the figures."}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0},
    )
    out = resp["output"]["message"]
    messages.append(out)

    uses = [c["toolUse"] for c in out["content"] if "toolUse" in c]
    for c in out["content"]:
        if "text" in c and c["text"].strip():
            print(c["text"].strip(), "\n")

    if not uses:
        u = resp.get("usage", {})
        print(f"[tokens in={u.get('inputTokens')} out={u.get('outputTokens')}]")
        break

    results = []
    for use in uses:
        print(f"  -> calling {use['name']}({json.dumps(use['input'])[:90]})")
        res = mcp("tools/call", {"name": use["name"], "arguments": use["input"]}, turn + 2)
        payload = res.get("result", {}).get("content", [{}])[0].get("text", "{}")
        results.append({"toolResult": {"toolUseId": use["toolUseId"],
                                       "content": [{"text": payload[:6000]}]}})
    messages.append({"role": "user", "content": results})
