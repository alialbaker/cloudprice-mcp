"""Local proof that the Gateway handler dispatches correctly. No AWS required.

Run:  python gateway_lambda/test_local.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from handler import lambda_handler


class FakeClientContext:
    def __init__(self, tool):
        self.custom = {"bedrockAgentCoreToolName": f"cloudpricetools___{tool}"}


class FakeContext:
    def __init__(self, tool):
        self.client_context = FakeClientContext(tool)


CASES = [
    ("get_aws_price", {"instance_type": "t3.2xlarge"}),
    ("compare_clouds", {"vcpus": 8, "memory_gb": 32}),
    ("compare_egress", {"transfers": [
        {"name": "cdn", "gb_per_month": 51200, "direction": "out_to_internet"}
    ]}),
    ("does_not_exist", {}),
]

for tool, args in CASES:
    result = lambda_handler(args, FakeContext(tool))
    ok = "error" not in result
    label = "OK  " if ok else "ERR "
    summary = json.dumps(result)[:150]
    print(f"{label} {tool:<28} -> {summary}")

# A malformed invocation must return an error, not raise.
print("\nNo-context case:")
print("     ", json.dumps(lambda_handler({}, object()))[:150])
