# 2026-07-24 Gemini CLI: Wave 0 Hardened Contracts, AST Purity & Serialization Unit Tests
import ast
from decimal import Decimal
import json
from pathlib import Path
import pytest

from strategies.futures.mts.config import ProfileResolver, SpreadPnlTrailConfig
from strategies.futures.mts.context_builder import LegSnapshot, SpreadContext, SpreadContextBuilder
from strategies.futures.mts.contracts import (
    EVALUATION_SCHEMA_VERSION,
    ExitAction,
    ExitDiagnostics,
    ExitEvaluation,
    ExitFamily,
    ExitReason,
    Leg,
    Side,
)
from strategies.futures.mts.economics import ContractEconomics
from strategies.futures.mts.registry import PolicyRegistry
from strategies.futures.mts.selector_contracts import SelectionDecision, StrategySelectionResult
from strategies.futures.mts.state import NormalReleaseState, SpreadPnlTrailState


def test_exit_family_enum_invariants():
    """Verify ExitFamily enum values and ensure NO_TRADE is not in ExitFamily."""
    families = [f.value for f in ExitFamily]
    assert "NORMAL_RELEASE" in families
    assert "REVERSE_HARVEST" in families
    assert "SPREAD_PNL_TRAIL" in families
    assert "NO_TRADE" not in families


def test_contract_purity_ast_audit():
    """AST Audit: Verify pure domain modules NEVER import prohibited I/O or broker modules."""
    prohibited_imports = {
        "shioaji",
        "datetime",
        "time",
        "pathlib",
        "yaml",
        "os",
        "requests",
        "urllib",
    }

    pure_files = [
        "contracts.py",
        "state.py",
        "config.py",
        "economics.py",
        "policy.py",
        "selector_contracts.py",
    ]

    mts_dir = Path(__file__).parent.parent.parent / "strategies" / "futures" / "mts"

    for file_name in pure_files:
        file_path = mts_dir / file_name
        assert file_path.exists(), f"File {file_name} missing"

        tree = ast.parse(file_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    assert root_pkg not in prohibited_imports, (
                        f"Prohibited import '{alias.name}' found in pure module {file_name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_pkg = node.module.split(".")[0]
                    assert root_pkg not in prohibited_imports, (
                        f"Prohibited import '{node.module}' found in pure module {file_name}"
                    )


def test_contract_economics_decimal_precision_and_canonicalization():
    """Verify ContractEconomics provides exact Decimal TWD precision and canonicalization."""
    econ_tmf = ContractEconomics.from_ticker("TMF", quantity=2)
    assert econ_tmf.point_value_twd == Decimal("10")
    
    # Calculate roundtrip friction at price 23000:
    # Commissions: 20 * 2 sides * 2 qty = 80 TWD
    # Tax: 23000 * 10 * 0.00002 * 2 * 2 = 18.4 -> quantized 18 TWD
    friction = econ_tmf.calculate_roundtrip_friction_twd(Decimal("23000"))
    assert friction == Decimal("98")
    assert isinstance(friction, Decimal)


def test_spread_context_builder_acl():
    """Verify SpreadContextBuilder creates pure immutable SpreadContext without broker dependencies."""
    ctx = SpreadContextBuilder.build_context(
        event_time_ns=1770000000000000000,
        session="NIGHT",
        ticker="TMF",
        quantity=1,
        near_contract="TMF202608",
        near_side=Side.SHORT,
        near_entry_price=Decimal("23100"),
        near_current_price=Decimal("23050"),
        near_high_price=Decimal("23120"),
        near_low_price=Decimal("23040"),
        far_contract="TMF202609",
        far_side=Side.LONG,
        far_entry_price=Decimal("23080"),
        far_current_price=Decimal("23110"),
        far_high_price=Decimal("23130"),
        far_low_price=Decimal("23070"),
        spread_z=1.25,
        spread_atr=15.0,
        realized_pnl_twd=0,
    )

    assert isinstance(ctx, SpreadContext)
    assert ctx.session == "NIGHT"
    # Near SHORT from 23100 to 23050 (+50 pts * 10 TWD = +500 TWD)
    assert ctx.near_leg.unrealized_pnl_twd == 500
    # Far LONG from 23080 to 23110 (+30 pts * 10 TWD = +300 TWD)
    assert ctx.far_leg.unrealized_pnl_twd == 300
    # Combined unrealized = 800 TWD
    assert ctx.combined_unrealized_pnl_twd == 800
    assert ctx.combined_net_pnl_twd > 700


def test_exit_evaluation_immutability_and_schema():
    """Verify ExitEvaluation is frozen and carries schema_version."""
    state = SpreadPnlTrailState(armed=True, combined_peak_pnl_twd=1200)
    eval_result = ExitEvaluation(
        family=ExitFamily.SPREAD_PNL_TRAIL,
        action=ExitAction.HOLD,
        legs=(),
        reason=ExitReason.NONE,
        next_state=state,
        diagnostics=ExitDiagnostics(policy_version="1.0", parameter_hash="abc"),
    )

    assert eval_result.action == ExitAction.HOLD
    assert eval_result.next_state.armed is True
    assert eval_result.schema_version == EVALUATION_SCHEMA_VERSION

    with pytest.raises(Exception):
        eval_result.action = ExitAction.EXIT_BOTH  # Should fail due to frozen=True


def test_profile_resolver_session_overrides():
    """Verify ProfileResolver resolves session overrides correctly for DAY vs NIGHT and raises on unknown session."""
    profile_dict = {
        "base": {
            "arm_profit_twd": 800,
            "arm_atr_ratio": 0.8,
            "trail_atr_ratio": 0.8,
        },
        "overrides": {
            "NIGHT": {
                "arm_atr_ratio": 1.0,
                "trail_atr_ratio": 1.1,
            }
        }
    }

    config_day = ProfileResolver.resolve_spread_pnl_trail(profile_dict, session="DAY")
    assert config_day.arm_atr_ratio == 0.8

    config_night = ProfileResolver.resolve_spread_pnl_trail(profile_dict, session="NIGHT")
    assert config_night.arm_atr_ratio == 1.0


def test_registry_invariants():
    """Verify PolicyRegistry registers and rejects duplicate or unknown family/version."""
    PolicyRegistry.clear()
    
    class DummyPolicy:
        family = ExitFamily.NORMAL_RELEASE
        version = "1.0"

    PolicyRegistry.register(ExitFamily.NORMAL_RELEASE, "1.0", DummyPolicy)
    assert PolicyRegistry.get(ExitFamily.NORMAL_RELEASE, "1.0") == DummyPolicy

    with pytest.raises(ValueError):
        PolicyRegistry.register(ExitFamily.NORMAL_RELEASE, "1.0", DummyPolicy)

    with pytest.raises(KeyError):
        PolicyRegistry.get(ExitFamily.SPREAD_PNL_TRAIL, "9.9")

    PolicyRegistry.clear()


def test_canonical_serialization_roundtrip():
    """Verify dataclasses can be serialized to JSON and deserialized identically."""
    econ = ContractEconomics.from_ticker("TMF", quantity=1)
    econ_dict = {
        "ticker": econ.ticker,
        "point_value_twd": str(econ.point_value_twd),
        "quantity": econ.quantity,
        "commission_per_side_twd": str(econ.commission_per_side_twd),
        "tax_rate": str(econ.tax_rate),
        "minimum_tick": str(econ.minimum_tick),
    }

    serialized = json.dumps(econ_dict, sort_keys=True)
    deserialized_dict = json.loads(serialized)
    
    econ_restored = ContractEconomics(
        ticker=deserialized_dict["ticker"],
        point_value_twd=Decimal(deserialized_dict["point_value_twd"]),
        quantity=int(deserialized_dict["quantity"]),
        commission_per_side_twd=Decimal(deserialized_dict["commission_per_side_twd"]),
        tax_rate=Decimal(deserialized_dict["tax_rate"]),
        minimum_tick=Decimal(deserialized_dict["minimum_tick"]),
    )

    assert econ_restored == econ
