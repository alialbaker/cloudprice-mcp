"""Unit tests for v0.7 price-refresh fetchers.

Each fetcher hits a public pricing API in production; in tests we mock the HTTP
layer (httpx) with pytest-httpx so the suite stays fast + hermetic. AWS uses
boto3 which we don't have mocked at the network level — instead we substitute
the boto3 client with a fake. boto3 is a SCRIPT-ONLY dep (not in package deps,
not in dev deps), so we inject a fake `boto3` module into sys.modules before
the AWS fetcher's lazy import runs.
"""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock

import pytest

from scripts.fetchers import azure, oci
from scripts.fetchers.base import MissingPriceError

# --- Azure fetcher ---


def _azure_response(unit_price: float, *, meter_name: str = "D2s v5", product_name: str = "Virtual Machines DSv5 Series") -> dict:
    return {
        "Items": [
            {
                "armSkuName": "Standard_D2s_v5",
                "armRegionName": "eastus",
                "meterName": meter_name,
                "productName": product_name,
                "priceType": "Consumption",
                "unitPrice": unit_price,
            }
        ]
    }


def _no_spot_response() -> dict:
    """v0.8.1: the fetcher makes a second call to look up the spot price.
    Tests that don't care about spot can use this to satisfy the call."""
    return {"Items": []}


def test_azure_refreshes_known_sku(httpx_mock):
    httpx_mock.add_response(json=_azure_response(0.0961))
    httpx_mock.add_response(json=_no_spot_response())  # spot lookup
    result = azure.fetch_instance_prices([
        {"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}
    ])
    assert len(result) == 1
    assert result[0]["sku"] == "D2s_v5"
    assert result[0]["hourly_usd"] == pytest.approx(0.0961)
    assert "spot_hourly_usd" not in result[0]


def test_azure_populates_spot_when_available(httpx_mock):
    httpx_mock.add_response(json=_azure_response(0.0961))
    httpx_mock.add_response(json={
        "Items": [
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5 Spot",
                "productName": "Virtual Machines DSv5 Series",
                "unitPrice": 0.0192,  # ~80% off on-demand
            }
        ]
    })
    result = azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])
    assert result[0]["hourly_usd"] == pytest.approx(0.0961)
    assert result[0]["spot_hourly_usd"] == pytest.approx(0.0192)


def test_azure_skips_spot_meters(httpx_mock):
    httpx_mock.add_response(json={
        "Items": [
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5 Spot",
                "productName": "Virtual Machines DSv5 Series",
                "unitPrice": 0.01,  # spot — should be ignored for ON-DEMAND lookup
            },
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5",
                "productName": "Virtual Machines DSv5 Series",
                "unitPrice": 0.0961,  # on-demand — should win
            },
        ]
    })
    httpx_mock.add_response(json=_no_spot_response())  # spot lookup
    result = azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])
    assert result[0]["hourly_usd"] == pytest.approx(0.0961)


def test_azure_skips_windows_variants(httpx_mock):
    httpx_mock.add_response(json={
        "Items": [
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5",
                "productName": "Virtual Machines DSv5 Series Windows",  # Win SKU
                "unitPrice": 0.30,
            },
            {
                "armSkuName": "Standard_D2s_v5",
                "meterName": "D2s v5",
                "productName": "Virtual Machines DSv5 Series",  # Linux
                "unitPrice": 0.0961,
            },
        ]
    })
    httpx_mock.add_response(json=_no_spot_response())  # spot lookup
    result = azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])
    assert result[0]["hourly_usd"] == pytest.approx(0.0961)


def test_azure_raises_when_sku_missing(httpx_mock):
    httpx_mock.add_response(json={"Items": []})
    with pytest.raises(MissingPriceError):
        azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])


def test_azure_preserves_catalog_on_http_error(httpx_mock, monkeypatch):
    """v0.11.1: a single 500 on a SKU lookup preserves the catalog value
    rather than crashing the whole cloud's refresh. The error surfaces via
    the orchestrator's per-cloud summary, not a process-killing exception."""
    monkeypatch.setattr(azure, "_INTER_CALL_DELAY_SECONDS", 0)
    httpx_mock.add_response(status_code=500)
    result = azure.fetch_instance_prices([
        {"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8, "hourly_usd": 0.0961}
    ])
    assert result[0]["hourly_usd"] == pytest.approx(0.0961)


def test_azure_retries_on_429_then_succeeds(httpx_mock, monkeypatch):
    """v0.11.1: a single 429 followed by a 200 should yield the 200 result
    (retry succeeded). monkeypatch removes the inter-call delay so the test
    stays fast."""
    monkeypatch.setattr(azure, "_INTER_CALL_DELAY_SECONDS", 0)
    monkeypatch.setattr(azure, "_RETRY_INITIAL_BACKOFF_SECONDS", 0)
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json=_azure_response(0.0961))
    httpx_mock.add_response(json=_no_spot_response())  # spot lookup, no retry
    result = azure.fetch_instance_prices([{"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8}])
    assert result[0]["hourly_usd"] == pytest.approx(0.0961)


def test_azure_skip_persists_catalog_when_all_retries_exhaust(httpx_mock, monkeypatch):
    """v0.11.1: if Azure 429s for every retry, the SKU keeps its existing
    catalog price instead of breaking the whole cloud's refresh."""
    monkeypatch.setattr(azure, "_INTER_CALL_DELAY_SECONDS", 0)
    monkeypatch.setattr(azure, "_RETRY_INITIAL_BACKOFF_SECONDS", 0)
    # 3 retries × 429 for the on-demand lookup
    for _ in range(3):
        httpx_mock.add_response(status_code=429)
    result = azure.fetch_instance_prices([
        {"sku": "D2s_v5", "vcpus": 2, "memory_gb": 8, "hourly_usd": 0.0961}
    ])
    # Existing catalog price preserved — refresh didn't crash
    assert result[0]["hourly_usd"] == pytest.approx(0.0961)


# --- OCI fetcher ---


def _oci_response() -> dict:
    """Minimal OCI catalog with E5 + A2 + A1 family rates."""
    def item(name: str, value: float) -> dict:
        return {
            "displayName": name,
            "currencyCodeLocalizations": [{"prices": [{"value": value}]}],
        }

    return {
        "items": [
            item("Compute - Standard - E5 - OCPU", 0.03),
            item("Compute - Standard - E5 - Memory", 0.002),
            item("Compute - Standard - A2 OCPU", 0.014),
            item("Compute - Standard - A2 Memory", 0.002),
            item("Compute - Standard - A1 - OCPU", 0.0),
            item("Compute - Standard - A1 - Memory", 0.0),
        ]
    }


def test_oci_computes_e5_price_from_family_rates(httpx_mock):
    httpx_mock.add_response(json=_oci_response())
    result = oci.fetch_instance_prices([
        {"sku": "VM.Standard.E5.Flex.1OCPU", "vcpus": 2, "memory_gb": 8},
    ])
    # 1 OCPU * $0.03 + 8 GB * $0.002 = $0.046
    assert result[0]["hourly_usd"] == pytest.approx(0.046)


def test_oci_handles_always_free(httpx_mock):
    httpx_mock.add_response(json=_oci_response())
    result = oci.fetch_instance_prices([
        {"sku": "VM.Standard.A1.Flex.AlwaysFree", "vcpus": 4, "memory_gb": 24},
    ])
    assert result[0]["hourly_usd"] == pytest.approx(0.0)


def test_oci_excludes_cloud_at_customer_variants(httpx_mock):
    # Cloud@Customer variants must not be picked up as the public-cloud price.
    httpx_mock.add_response(json={
        "items": [
            {
                "displayName": "Oracle Compute Cloud@Customer - Compute - Standard - E5 - Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.0004}]}],
            },
            {
                "displayName": "Compute - Standard - E5 - OCPU",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.03}]}],
            },
            {
                "displayName": "Compute - Standard - E5 - Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.002}]}],
            },
            {
                "displayName": "Compute - Standard - A2 OCPU",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.014}]}],
            },
            {
                "displayName": "Compute - Standard - A2 Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.002}]}],
            },
            {
                "displayName": "Compute - Standard - A1 - OCPU",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.0}]}],
            },
            {
                "displayName": "Compute - Standard - A1 - Memory",
                "currencyCodeLocalizations": [{"prices": [{"value": 0.0}]}],
            },
        ]
    })
    result = oci.fetch_instance_prices([
        {"sku": "VM.Standard.E5.Flex.1OCPU", "vcpus": 2, "memory_gb": 8},
    ])
    # 1 * 0.03 + 8 * 0.002 = 0.046 (NOT the Cloud@Customer memory rate)
    assert result[0]["hourly_usd"] == pytest.approx(0.046)


def test_oci_raises_for_unknown_family(httpx_mock):
    httpx_mock.add_response(json=_oci_response())
    # An unrecognized FAMILY (Z9) in the Flex pattern is now preserved instead
    # of raising — see v0.11.1 hardening: refresh shouldn't break on unknown
    # shapes. The catalog value is kept as-is.
    result = oci.fetch_instance_prices([
        {"sku": "VM.Standard.Z9.Flex.1OCPU", "vcpus": 2, "memory_gb": 8, "hourly_usd": 0.123},
    ])
    assert result[0]["hourly_usd"] == pytest.approx(0.123)


def test_oci_preserves_gpu_skus_without_refresh(httpx_mock):
    """v0.11.1: GPU SKUs (VM.GPU.* / BM.GPU*) are bundled in the catalog with
    manually-curated prices because Oracle's public pricing API doesn't expose
    them in the OCPU+memory model. The OCI fetcher must preserve them."""
    httpx_mock.add_response(json=_oci_response())
    result = oci.fetch_instance_prices([
        {"sku": "VM.GPU.A10.1", "vcpus": 15, "memory_gb": 240, "hourly_usd": 2.00,
         "gpu_type": "A10", "gpu_count": 1, "gpu_memory_gb_each": 24},
        {"sku": "BM.GPU.H100.8", "vcpus": 112, "memory_gb": 2048, "hourly_usd": 80.00,
         "gpu_type": "H100", "gpu_count": 8, "gpu_memory_gb_each": 80},
    ])
    # Prices preserved exactly — no refresh, no error
    assert result[0]["hourly_usd"] == pytest.approx(2.00)
    assert result[1]["hourly_usd"] == pytest.approx(80.00)
    # GPU metadata preserved
    assert result[0]["gpu_type"] == "A10"
    assert result[1]["gpu_count"] == 8


# --- AWS fetcher ---
#
# boto3 isn't available in CI (script-only dep). We inject a fake `boto3` module
# into sys.modules so the AWS fetcher's `import boto3` succeeds and returns our
# stand-in client. The fixture cleans up sys.modules after each test.


def _aws_product(price: float) -> dict:
    return {
        "terms": {
            "OnDemand": {
                "T1": {
                    "priceDimensions": {
                        "P1": {
                            "unit": "Hrs",
                            "pricePerUnit": {"USD": f"{price:.10f}"},
                        }
                    }
                }
            }
        }
    }


@pytest.fixture
def fake_boto3(monkeypatch):
    """Install a fake `boto3` module in sys.modules for the duration of a test.

    Use it as `fake_boto3(returns_price=0.192)` to control what `get_products`
    returns. boto3's kwargs (ServiceCode, Filters, MaxResults) intentionally
    use PascalCase to match the real API surface — the fake accepts **kwargs
    so it doesn't care what they're called.
    """
    def factory(*, returns_price: float | None = None, returns_products: list[dict] | None = None):
        if returns_products is None and returns_price is not None:
            returns_products = [_aws_product(returns_price)]
        elif returns_products is None:
            returns_products = []

        client = MagicMock()
        client.get_products.return_value = {
            "PriceList": [json.dumps(p) for p in returns_products],
        }

        boto3_mod = types.ModuleType("boto3")
        boto3_mod.client = MagicMock(return_value=client)
        monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
        return client

    return factory


def test_aws_parses_hourly_usd_from_nested_response(fake_boto3):
    fake_boto3(returns_price=0.192)
    # Re-import so the fetcher picks up the patched sys.modules state.
    from scripts.fetchers import aws

    result = aws.fetch_instance_prices([
        {"sku": "m5.xlarge", "vcpus": 4, "memory_gb": 16}
    ])
    assert result[0]["hourly_usd"] == pytest.approx(0.192)


def test_aws_skips_zero_priced_promo_rows(fake_boto3):
    fake_boto3(returns_products=[_aws_product(0.0), _aws_product(0.0104)])
    from scripts.fetchers import aws

    result = aws.fetch_instance_prices([
        {"sku": "t3.micro", "vcpus": 2, "memory_gb": 1}
    ])
    assert result[0]["hourly_usd"] == pytest.approx(0.0104)


def test_aws_raises_when_no_products_returned(fake_boto3):
    fake_boto3(returns_products=[])
    from scripts.fetchers import aws

    with pytest.raises(MissingPriceError):
        aws.fetch_instance_prices([
            {"sku": "nonexistent.xlarge", "vcpus": 4, "memory_gb": 16}
        ])
