"""Tests for v0.6 inventory parser (YAML → WorkloadInventory)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cloudprice_mcp.inventory import (
    InventoryError,
    WorkloadInventory,
    parse_dict,
    parse_yaml,
    parse_yaml_file,
)

# --- Minimal happy paths ---


def test_parse_yaml_minimal():
    text = textwrap.dedent(
        """
        source_cloud: aws
        compute:
          - { name: api, vcpus: 4, memory_gb: 16, quantity: 6 }
        """
    )
    inv = parse_yaml(text)
    assert inv.source_cloud == "aws"
    assert inv.commitment == "none"  # default
    assert inv.multi_az is False  # default
    assert len(inv.compute) == 1
    assert inv.compute[0].name == "api"
    assert inv.compute[0].vcpus == 4
    assert inv.compute[0].memory_gb == 16.0
    assert inv.compute[0].quantity == 6
    assert inv.compute[0].snapshot_incremental_factor == 1.0  # default


def test_parse_yaml_empty_returns_empty_inventory():
    inv = parse_dict({})
    assert isinstance(inv, WorkloadInventory)
    assert inv.is_empty()


def test_is_empty_true_when_no_workload_items():
    inv = WorkloadInventory(source_cloud="aws", commitment="1yr_no_upfront")
    assert inv.is_empty()


def test_is_empty_false_when_any_item_present():
    inv = parse_dict({"compute": [{"name": "x", "vcpus": 1, "memory_gb": 1}]})
    assert not inv.is_empty()


# --- Full YAML round-trip ---


def test_parse_yaml_full_inventory():
    text = textwrap.dedent(
        """
        source_cloud: aws
        commitment: 1yr_no_upfront
        multi_az: true
        one_time:
          data_to_migrate_gb: 50000
        compute:
          - name: api
            vcpus: 4
            memory_gb: 16
            quantity: 6
            multi_az: true
            os_disk_gb: 100
            os_disk_type: ssd
            snapshot_count: 7
            snapshot_incremental_factor: 0.3
        storage:
          - { name: app-data, capacity_gb: 2000, disk_type: ssd, snapshot_count: 7 }
        object_storage:
          - { name: media, capacity_gb: 50000, tier: hot }
        databases:
          - { name: primary, engine: postgres, vcpus: 8, memory_gb: 32, storage_gb: 500 }
        egress:
          - { name: internet, gb_per_month: 5000 }
          - { name: replica, gb_per_month: 2000, direction: inter_region }
        """
    )
    inv = parse_yaml(text)

    assert inv.source_cloud == "aws"
    assert inv.commitment == "1yr_no_upfront"
    assert inv.multi_az is True
    assert inv.one_time.data_to_migrate_gb == 50000

    assert len(inv.compute) == 1
    c = inv.compute[0]
    assert c.os_disk_gb == 100
    assert c.snapshot_count == 7
    assert c.snapshot_incremental_factor == 0.3

    assert len(inv.storage) == 1
    assert inv.storage[0].capacity_gb == 2000
    assert len(inv.object_storage) == 1
    assert inv.object_storage[0].tier == "hot"
    assert len(inv.databases) == 1
    assert inv.databases[0].engine == "postgres"
    assert inv.databases[0].storage_gb == 500
    assert len(inv.egress) == 2
    assert inv.egress[0].direction == "out_to_internet"
    assert inv.egress[1].direction == "inter_region"


def test_to_dict_round_trips():
    inv = parse_yaml("compute:\n  - {name: x, vcpus: 1, memory_gb: 1}\n")
    d = inv.to_dict()
    assert d["compute"][0]["name"] == "x"
    assert "source_cloud" in d
    assert d["one_time"]["data_to_migrate_gb"] == 0


# --- File-based parsing ---


def test_parse_yaml_file(tmp_path: Path):
    p = tmp_path / "inv.yaml"
    p.write_text(
        "compute:\n  - {name: api, vcpus: 2, memory_gb: 4}\n", encoding="utf-8"
    )
    inv = parse_yaml_file(p)
    assert len(inv.compute) == 1
    assert inv.compute[0].vcpus == 2


def test_parse_yaml_file_missing_raises_clear_error(tmp_path: Path):
    with pytest.raises(InventoryError, match="not found"):
        parse_yaml_file(tmp_path / "does_not_exist.yaml")


# --- Validation: required fields ---


@pytest.mark.parametrize(
    "section,item,missing",
    [
        ("compute", {"vcpus": 1, "memory_gb": 1}, "name"),
        ("compute", {"name": "x", "memory_gb": 1}, "vcpus"),
        ("compute", {"name": "x", "vcpus": 1}, "memory_gb"),
        ("storage", {"capacity_gb": 100}, "name"),
        ("storage", {"name": "x"}, "capacity_gb"),
        ("egress", {"gb_per_month": 100}, "name"),
        ("databases", {"vcpus": 1, "memory_gb": 1}, "name"),
    ],
)
def test_missing_required_field_raises_clear_error(section, item, missing):
    with pytest.raises(InventoryError, match=missing):
        parse_dict({section: [item]})


# --- Validation: type / range checks ---


def test_invalid_cloud_rejected():
    with pytest.raises(InventoryError, match="aws / azure / gcp / oci"):
        parse_dict({"source_cloud": "ibm"})


def test_invalid_commitment_rejected():
    with pytest.raises(InventoryError, match="commitment"):
        parse_dict({"commitment": "lifetime"})


def test_negative_vcpus_rejected():
    with pytest.raises(InventoryError, match="positive integer"):
        parse_dict({"compute": [{"name": "x", "vcpus": 0, "memory_gb": 1}]})


def test_negative_memory_rejected():
    with pytest.raises(InventoryError, match="positive number"):
        parse_dict({"compute": [{"name": "x", "vcpus": 1, "memory_gb": -1}]})


def test_invalid_disk_type_rejected():
    with pytest.raises(InventoryError, match="disk_type"):
        parse_dict(
            {"storage": [{"name": "x", "capacity_gb": 100, "disk_type": "tape"}]}
        )


def test_invalid_object_tier_rejected():
    with pytest.raises(InventoryError, match="tier"):
        parse_dict(
            {"object_storage": [{"name": "x", "capacity_gb": 100, "tier": "warm"}]}
        )


def test_invalid_egress_direction_rejected():
    with pytest.raises(InventoryError, match="direction"):
        parse_dict(
            {"egress": [{"name": "x", "gb_per_month": 100, "direction": "outbound"}]}
        )


def test_snapshot_incremental_factor_out_of_range_rejected():
    with pytest.raises(InventoryError, match=r"\[0\.0, 1\.0\]"):
        parse_dict(
            {
                "compute": [
                    {
                        "name": "x",
                        "vcpus": 1,
                        "memory_gb": 1,
                        "snapshot_incremental_factor": 1.5,
                    }
                ]
            }
        )


def test_multi_az_must_be_bool_not_string():
    with pytest.raises(InventoryError, match="boolean"):
        parse_dict({"multi_az": "yes"})


# --- Validation: structural errors ---


def test_invalid_yaml_raises():
    with pytest.raises(InventoryError, match="Invalid YAML"):
        parse_yaml("compute:\n  - {name: x, vcpus: [unbalanced")


def test_empty_yaml_raises():
    with pytest.raises(InventoryError, match="empty"):
        parse_yaml("")


def test_root_must_be_mapping():
    with pytest.raises(InventoryError, match="mapping"):
        parse_yaml("- a\n- b\n")


def test_section_must_be_list():
    with pytest.raises(InventoryError, match="must be a list"):
        parse_dict({"compute": "not a list"})


def test_item_must_be_mapping():
    with pytest.raises(InventoryError, match="must be a mapping"):
        parse_dict({"compute": ["string-not-dict"]})


# --- Edge cases that often bite ---


def test_quantity_defaults_to_one():
    inv = parse_dict({"compute": [{"name": "x", "vcpus": 1, "memory_gb": 1}]})
    assert inv.compute[0].quantity == 1


def test_os_disk_gb_can_be_omitted():
    inv = parse_dict({"compute": [{"name": "x", "vcpus": 1, "memory_gb": 1}]})
    assert inv.compute[0].os_disk_gb is None


def test_egress_direction_defaults_to_internet():
    inv = parse_dict({"egress": [{"name": "x", "gb_per_month": 100}]})
    assert inv.egress[0].direction == "out_to_internet"


def test_database_engine_defaults_to_postgres():
    inv = parse_dict(
        {"databases": [{"name": "x", "vcpus": 1, "memory_gb": 1}]}
    )
    assert inv.databases[0].engine == "postgres"


def test_source_cloud_is_lowercased():
    inv = parse_dict({"source_cloud": "AWS"})
    assert inv.source_cloud == "aws"


def test_bool_for_int_field_rejected():
    """True being treated as 1 would be a footgun — explicit bool check."""
    with pytest.raises(InventoryError):
        parse_dict({"compute": [{"name": "x", "vcpus": True, "memory_gb": 1}]})
