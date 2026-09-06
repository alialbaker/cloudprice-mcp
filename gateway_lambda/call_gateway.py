"""Call the AgentCore Gateway over MCP, signed with SigV4.

The gateway uses the AWS_IAM authorizer, so requests are signed with your
AWS credentials rather than carrying a bearer token. MCP itself is plain
JSON-RPC over HTTP here.

Run:  python gateway_lambda/call_gateway.py
"""
import json
import sys

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

URL = "https://cloudprice-gateway-xprwhtvo85.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
REGION = "us-east-1"
SERVICE = "bedrock-agentcore"

session = boto3.Session()
creds = session.get_credentials().get_frozen_credentials()


def rpc(method, params=None, rpc_id=1):
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method,
                       "params": params or {}})
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    req = AWSRequest(method="POST", url=URL, data=body, headers=headers)
    SigV4Auth(creds, SERVICE, REGION).add_auth(req)

    r = httpx.post(URL, content=body, headers=dict(req.headers), timeout=60)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:300]}")
        return None

    text = r.text
    # Streamable HTTP may answer as SSE; take the data line if so.
    if text.startswith("event:") or text.startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    return json.loads(text)


print("1. tools/list")
res = rpc("tools/list")
if not res:
    sys.exit(1)
tools = res.get("result", {}).get("tools", [])
print(f"   Gateway advertises {len(tools)} tools")
for t in tools[:3]:
    print(f"     - {t['name']}")
if len(tools) > 3:
    print(f"     ... and {len(tools) - 3} more")

print("\n2. tools/call -> get_aws_price(t3.2xlarge)")
name = next((t["name"] for t in tools if t["name"].endswith("get_aws_price")), None)
res = rpc("tools/call", {"name": name, "arguments": {"instance_type": "t3.2xlarge"}}, 2)
print("   ", json.dumps(res.get("result", res))[:400] if res else "no response")
