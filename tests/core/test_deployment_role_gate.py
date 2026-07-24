"""
Unit tests for Deployment Role Fail-Closed Gates
"""

import pytest
from pathlib import Path
from core.deployment_role_gate import (
    assert_research_allowed,
    assert_broker_access_allowed,
    ResearchNotAllowedOnProductionError,
    BrokerAccessNotAllowedOnResearchHostError
)


def test_research_allowed_on_air4(tmp_path):
    target_file = tmp_path / ".deployment-target"
    target_file.write_text('{"deployment_id": "air4", "host_role": "offline_research"}')
    assert assert_research_allowed(tmp_path) == "air4"


def test_research_blocked_on_mini(tmp_path):
    target_file = tmp_path / ".deployment-target"
    target_file.write_text('{"deployment_id": "mini", "host_role": "production_trading"}')
    with pytest.raises(ResearchNotAllowedOnProductionError):
        assert_research_allowed(tmp_path)


def test_broker_blocked_on_air4(tmp_path):
    target_file = tmp_path / ".deployment-target"
    target_file.write_text('{"deployment_id": "air4", "host_role": "offline_research"}')
    with pytest.raises(BrokerAccessNotAllowedOnResearchHostError):
        assert_broker_access_allowed(tmp_path)


def test_broker_allowed_on_mini(tmp_path):
    target_file = tmp_path / ".deployment-target"
    target_file.write_text('{"deployment_id": "mini", "host_role": "production_trading"}')
    assert assert_broker_access_allowed(tmp_path) == "mini"
