"""Invoke the deployed AgentCore harness. Nothing runs locally but this script.

Compare with ask.py: there, the agent loop ran on your machine. Here the loop
runs in AWS - model, system prompt, tools and memory all come from the harness
config, so the caller sends only a question and a session id. That is what
makes it embeddable: a website calls this one API instead of reimplementing
the loop.

Usage:
    python gateway_lambda/ask_harness.py "your question"
    python gateway_lambda/ask_harness.py --session mysession1234567890abcdef "follow-up"
"""

import json
import sys
import uuid

import boto3


# Resolved at runtime rather than hardcoded: the harness ARN changes whenever
# the harness is replaced (for example, setting encryptionKeyArn on its memory
# requires recreation). Override with CLOUDPRICE_HARNESS_ARN if needed.
def _harness_arn():
    import os
    if os.environ.get("CLOUDPRICE_HARNESS_ARN"):
        return os.environ["CLOUDPRICE_HARNESS_ARN"]
    ctl = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
    for h in ctl.list_harnesses().get("harnesses", []):
        if h.get("harnessName", "").startswith("cloudprice_gateway_agent"):
            return h.get("harnessArn") or h.get("arn")
    raise SystemExit("No cloudprice harness found")


HARNESS = None  # set after boto3 import below

args = sys.argv[1:]
# Reusing a session id continues the conversation, which is what lets memory
# apply. Session ids must be at least 33 characters.
session = str(uuid.uuid4()) + "-cloudprice"
actor = None
if args and args[0] == "--actor":
    actor, args = args[1], args[2:]
if args and args[0] == "--session":
    session, args = args[1], args[2:]

question = " ".join(args) or "Cheapest cloud for 8 vCPU and 32 GB?"

HARNESS = _harness_arn()
client = boto3.client("bedrock-agentcore", region_name="us-east-1")

print("Q: " + question)
print("   session " + session[:24] + "...\n")

# actorId scopes memory. Without it every caller shares one memory pool,
# which on a public surface means one visitor's context leaks into another's
# answers. Each distinct caller MUST get a distinct actorId.
kwargs = {
    "harnessArn": HARNESS,
    "runtimeSessionId": session,
    "messages": [{"role": "user", "content": [{"text": question}]}],
}
if actor:
    kwargs["actorId"] = actor

resp = client.invoke_harness(**kwargs)


def iter_text(node):
    """Yield every 'text' string, wherever it sits in the event payload."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "text" and isinstance(value, str):
                yield value
            else:
                yield from iter_text(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_text(value)


# InvokeHarness returns an EventStream, not a single body.
for event in resp["stream"]:
    for payload in event.values():
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                print(payload, end="")
                continue

        blob = json.dumps(payload)
        if "toolUse" in blob:
            print("\n  [tool] " + blob[:200] + "\n")
        elif '"text"' in blob:
            for chunk in iter_text(payload):
                print(chunk, end="")
        elif "stopReason" in blob or "usage" in blob:
            print("\n  [" + blob[:200] + "]")

print()
