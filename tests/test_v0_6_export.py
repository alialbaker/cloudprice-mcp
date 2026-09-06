"""Tests for v0.6 export renderers (markdown / json / text)."""
from __future__ import annotations

import json

from cloudprice_mcp.export import to_json, to_markdown, to_text

# --- Sample tool outputs (representative of what FinOps tools will produce) ---


def _migration_result() -> dict:
    return {
        "kind": "migration_assessment",
        "title": "Migration Assessment: AWS",
        "headline": "OCI saves 47%, payback 2.3 months",
        "source_monthly_usd": 4280,
        "targets": {
            "oci": {
                "monthly_usd": 2270,
                "savings_vs_source_pct": 47,
                "payback_months": 2.3,
                "caveats": ["A1.Flex is ARM"],
            },
            "gcp": {
                "monthly_usd": 3760,
                "savings_vs_source_pct": 12,
                "payback_months": 14.0,
                "caveats": [],
            },
            "azure": {
                "monthly_usd": 4400,
                "savings_vs_source_pct": -3,
                "payback_months": None,
                "caveats": [],
            },
        },
        "one_time_exit_cost_usd": 4250,
        "honest_gaps": [
            "No workload discovery",
            "No license portability modeling",
        ],
    }


def _commitment_result() -> dict:
    return {
        "kind": "commitment_optimization",
        "title": "Commitment Optimization: 6× t3.2xlarge on AWS",
        "headline": "3yr partial upfront pays back in 7.3 months, saves $35K over 3 years",
        "scenarios": {
            "none": {
                "monthly_usd": 1457,
                "3yr_total_usd": 52475,
                "savings_vs_ondemand_pct": 0,
                "upfront_usd": 0,
                "payback_months": None,
            },
            "1yr_no_upfront": {
                "monthly_usd": 918,
                "3yr_total_usd": 33065,
                "savings_vs_ondemand_pct": 37,
                "upfront_usd": 0,
                "payback_months": 1,
            },
            "3yr_partial_upfront": {
                "monthly_usd": 244,
                "3yr_total_usd": 17601,
                "savings_vs_ondemand_pct": 66,
                "upfront_usd": 8802,
                "payback_months": 7.3,
            },
        },
        "recommended": "3yr_partial_upfront",
        "recommendation_reason": "$35K savings, ~7-month payback even with $8.8K upfront",
    }


# --- Markdown renderer ---


def test_markdown_renders_migration_targets_table():
    out = to_markdown(_migration_result())
    assert "## Migration Assessment: AWS" in out
    assert "**OCI saves 47%, payback 2.3 months**" in out
    assert "| Target | Monthly $" in out
    assert "| oci | $2,270 | +47% | 2.3 mo |" in out
    assert "A1.Flex is ARM" in out


def test_markdown_renders_commitment_scenarios_table():
    out = to_markdown(_commitment_result())
    assert "| Scenario |" in out
    assert "| 3yr_partial_upfront |" in out
    assert "$8,802" in out  # upfront
    assert "$17,601" in out  # 3yr total
    assert "7.3 mo" in out


def test_markdown_includes_recommendation():
    out = to_markdown(_commitment_result())
    assert "Recommended" in out
    assert "3yr_partial_upfront" in out


def test_markdown_includes_honest_gaps_as_notes():
    out = to_markdown(_migration_result())
    assert "Notes" in out
    assert "No workload discovery" in out


def test_markdown_handles_minimal_dict():
    out = to_markdown({"title": "Empty", "headline": "Nothing here"})
    assert "## Empty" in out
    assert "**Nothing here**" in out


def test_markdown_handles_missing_title_gracefully():
    out = to_markdown({"targets": {"x": {"monthly_usd": 100}}})
    # Should not crash; falls back to "FinOps result"
    assert "## FinOps result" in out
    assert "$100" in out


def test_markdown_no_table_when_no_targets_or_scenarios():
    out = to_markdown({"title": "Plain", "headline": "No tables"})
    assert "|" not in out  # no markdown table syntax


def test_markdown_payback_renders_na_when_null():
    """Azure in the migration sample has payback_months=None → should show n/a."""
    out = to_markdown(_migration_result())
    # Find the azure row
    azure_line = next(line for line in out.splitlines() if "azure" in line)
    assert "n/a" in azure_line


def test_markdown_savings_pct_shows_negative_with_sign():
    """Azure shows -3% (target is more expensive than source)."""
    out = to_markdown(_migration_result())
    azure_line = next(line for line in out.splitlines() if "azure" in line)
    assert "-3%" in azure_line


# --- Text (plain ASCII) renderer ---


def test_text_renders_migration_compact():
    out = to_text(_migration_result())
    assert "Migration Assessment: AWS" in out
    # Underline
    assert "===" in out
    # Each cloud on its own line with currency + savings + payback
    assert "oci" in out
    assert "$2,270" in out
    assert "+47%" in out
    assert "2.3 mo" in out
    # Should NOT contain markdown table chars
    assert "|" not in out


def test_text_renders_commitment_scenarios():
    out = to_text(_commitment_result())
    assert "3yr_partial_upfront" in out
    assert "Recommended: 3yr_partial_upfront" in out
    assert "->" in out  # recommendation_reason marker


def test_text_renders_notes():
    out = to_text(_migration_result())
    assert "Notes:" in out
    assert "No workload discovery" in out


# --- JSON renderer ---


def test_json_round_trips_migration():
    out = to_json(_migration_result())
    parsed = json.loads(out)
    assert parsed["kind"] == "migration_assessment"
    assert parsed["targets"]["oci"]["monthly_usd"] == 2270


def test_json_is_pretty_printed():
    out = to_json(_migration_result())
    assert "\n" in out
    assert "  " in out  # 2-space indent


def test_json_preserves_field_order_loosely():
    """sort_keys=False so original ordering is retained for human readability."""
    out = to_json({"z": 1, "a": 2})
    # 'z' should appear before 'a' (insertion order)
    z_pos = out.index('"z"')
    a_pos = out.index('"a"')
    assert z_pos < a_pos


def test_json_handles_set_via_default():
    """Sets aren't natively json-serializable — _json_default sorts them into a list."""
    out = to_json({"things": {3, 1, 2}})
    parsed = json.loads(out)
    assert parsed["things"] == [1, 2, 3]


# --- Currency / payback formatting helpers (smoke check via end-to-end) ---


def test_currency_under_100_uses_2_decimal_places():
    out = to_markdown({"targets": {"oci": {"monthly_usd": 12.50}}})
    assert "$12.50" in out


def test_currency_over_100_uses_no_decimal_places():
    out = to_markdown({"targets": {"oci": {"monthly_usd": 4280}}})
    assert "$4,280" in out


def test_payback_zero_renders_as_zero():
    out = to_text({"scenarios": {"x": {"payback_months": 0}}})
    assert "0.0 mo" in out
