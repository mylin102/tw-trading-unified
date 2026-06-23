"""V-Model tests for AttributionRecorder integration."""

import pandas as pd
import pytest

from core.attribution_recorder import (
    AttributionRecorder,
    RouterEvaluationRow,
    StrategySignalRow,
    TradeAttributionRow,
    summarize_router,
    build_starvation_report,
)


class TestAttributionRecorderUnit:
    """Level 1.1: AttributionRecorder unit tests."""
    
    def test_recorder_initialization(self):
        """Test recorder initializes with empty buffers."""
        recorder = AttributionRecorder()
        assert len(recorder.router_rows) == 0
        assert len(recorder.signal_rows) == 0
        assert len(recorder.trade_rows) == 0
        assert recorder.output_dir is None
    
    def test_log_router_row(self):
        """Test logging router evaluation rows."""
        recorder = AttributionRecorder()
        
        recorder.log_router_row(
            timestamp="2026-04-22 09:15:00",
            symbol="TX",
            regime="WEAK",
            strategy_name="counter_vwap",
            candidate_order=0,
            status="no_signal",
            evaluated=True,
            winner=False,
            notes="squeeze not fired",
        )
        
        assert len(recorder.router_rows) == 1
        row = recorder.router_rows[0]
        assert row.strategy_name == "counter_vwap"
        assert row.status == "no_signal"
        assert row.evaluated is True
        assert row.winner is False
    
    def test_log_signal(self):
        """Test logging strategy signals."""
        recorder = AttributionRecorder()
        
        recorder.log_signal(
            timestamp="2026-04-22 09:15:00",
            symbol="TX",
            regime="WEAK",
            strategy_name="kbar_feature",
            candidate_order=2,
            side="SELL",
            signal_type="KBAR_FEATURE_SHORT",
            selected=True,
            score=-30.0,
            notes="router selected",
        )
        
        assert len(recorder.signal_rows) == 1
        row = recorder.signal_rows[0]
        assert row.strategy_name == "kbar_feature"
        assert row.side == "SELL"
        assert row.selected is True
        assert row.score == -30.0
    
    def test_log_trade(self):
        """Test logging trade attribution."""
        recorder = AttributionRecorder()
        
        recorder.log_trade(
            trade_id="T1",
            symbol="TX",
            strategy_name="kbar_feature",
            regime_at_entry="WEAK",
            side="SELL",
            entry_time="2026-04-22 09:15:00",
            exit_time="2026-04-22 09:18:00",
            entry_price=20100.0,
            exit_price=20070.0,
            pnl=30.0,
            mae=-8.0,
            mfe=36.0,
            hold_bars=3,
            exit_reason="target",
        )
        
        assert len(recorder.trade_rows) == 1
        row = recorder.trade_rows[0]
        assert row.strategy_name == "kbar_feature"
        assert row.pnl == 30.0
        assert row.mae == -8.0
        assert row.exit_reason == "target"
    
    def test_dataframe_conversion(self):
        """Test converting buffers to DataFrames."""
        recorder = AttributionRecorder()
        
        # Add some data
        recorder.log_router_row(
            timestamp="2026-04-22 09:15:00",
            symbol="TX",
            regime="WEAK",
            strategy_name="counter_vwap",
            candidate_order=0,
            status="no_signal",
            evaluated=True,
            winner=False,
        )
        
        router_df = recorder.router_df()
        assert isinstance(router_df, pd.DataFrame)
        assert len(router_df) == 1
        assert "strategy_name" in router_df.columns
        assert "status" in router_df.columns
    
    def test_summarize_router_empty(self):
        """Test summarize_router with empty DataFrame."""
        df = pd.DataFrame()
        summary = summarize_router(df)
        assert summary.empty
    
    def test_summarize_router_basic(self):
        """Test summarize_router with basic data."""
        data = [
            {
                "timestamp": "2026-04-22 09:15:00",
                "symbol": "TX",
                "regime": "WEAK",
                "strategy_name": "counter_vwap",
                "candidate_order": 0,
                "status": "no_signal",
                "evaluated": True,
                "winner": False,
                "notes": "",
            },
            {
                "timestamp": "2026-04-22 09:15:00",
                "symbol": "TX",
                "regime": "WEAK",
                "strategy_name": "kbar_feature",
                "candidate_order": 1,
                "status": "winner",
                "evaluated": True,
                "winner": True,
                "notes": "",
            },
        ]
        df = pd.DataFrame(data)
        
        summary = summarize_router(df)
        assert not summary.empty
        assert len(summary) == 2
        
        # Check counter_vwap
        counter_row = summary[summary["strategy_name"] == "counter_vwap"].iloc[0]
        assert counter_row["candidate_count"] == 1
        assert counter_row["eval_count"] == 1
        assert counter_row["winner_count"] == 0
        assert counter_row["shadowed_count"] == 0
        
        # Check kbar_feature
        kbar_row = summary[summary["strategy_name"] == "kbar_feature"].iloc[0]
        assert kbar_row["candidate_count"] == 1
        assert kbar_row["eval_count"] == 1
        assert kbar_row["winner_count"] == 1
        assert kbar_row["shadowed_count"] == 0
    
    def test_starvation_report(self):
        """Test building starvation report."""
        # Simulate kbar_feature being shadowed 5 times, winning 5 times
        data = []
        for i in range(10):
            data.append({
                "timestamp": f"2026-04-22 09:{i:02d}:00",
                "symbol": "TX",
                "regime": "WEAK",
                "strategy_name": "counter_vwap",
                "candidate_order": 0,
                "status": "winner" if i < 5 else "no_signal",
                "evaluated": True,
                "winner": i < 5,
                "notes": "",
            })
            data.append({
                "timestamp": f"2026-04-22 09:{i:02d}:00",
                "symbol": "TX",
                "regime": "WEAK",
                "strategy_name": "kbar_feature",
                "candidate_order": 1,
                "status": "shadowed" if i < 5 else "winner",
                "evaluated": i >= 5,  # only evaluated when counter_vwap fails
                "winner": i >= 5,
                "notes": "",
            })
        
        df = pd.DataFrame(data)
        summary = summarize_router(df)
        
        # Find kbar_feature row
        kbar_row = summary[summary["strategy_name"] == "kbar_feature"].iloc[0]
        assert kbar_row["candidate_count"] == 10
        assert kbar_row["eval_count"] == 5  # evaluated 5 times (when counter_vwap failed)
        assert kbar_row["shadowed_count"] == 5
        assert kbar_row["winner_count"] == 5  # won all 5 times it was evaluated
        assert kbar_row["starvation_index"] == 0.5  # 1 - (5/10) = 0.5
        
        # Priority impact should be shadowed_count / winner_count = 5 / 5 = 1.0
        assert abs(kbar_row["priority_impact"] - 1.0) < 0.001
    
    def test_priority_impact_calculation(self):
        """Test priority impact score calculation."""
        data = [
            {
                "timestamp": "2026-04-22 09:15:00",
                "symbol": "TX",
                "regime": "WEAK",
                "strategy_name": "strategy_a",
                "candidate_order": 0,
                "status": "winner",
                "evaluated": True,
                "winner": True,
            },
            {
                "timestamp": "2026-04-22 09:15:00",
                "symbol": "TX",
                "regime": "WEAK",
                "strategy_name": "strategy_b",
                "candidate_order": 1,
                "status": "shadowed",
                "evaluated": False,
                "winner": False,
            },
            {
                "timestamp": "2026-04-22 09:16:00",
                "symbol": "TX",
                "regime": "WEAK",
                "strategy_name": "strategy_b",
                "candidate_order": 0,
                "status": "winner",
                "evaluated": True,
                "winner": True,
            },
        ]
        df = pd.DataFrame(data)
        summary = summarize_router(df)
        
        # strategy_b should have priority_impact = shadowed_count / winner_count = 1 / 1 = 1.0
        strategy_b = summary[summary["strategy_name"] == "strategy_b"].iloc[0]
        assert strategy_b["priority_impact"] == 1.0
    
    def test_export_csv_no_output_dir(self):
        """Test export_csv when output_dir is None."""
        recorder = AttributionRecorder(output_dir=None)
        recorder.log_router_row(
            timestamp="2026-04-22 09:15:00",
            symbol="TX",
            regime="WEAK",
            strategy_name="test",
            candidate_order=0,
            status="candidate",
            evaluated=False,
            winner=False,
        )
        
        # Should not crash
        recorder._check_and_flush()
    
    def test_buffer_flush_logic(self):
        """Test buffer size triggers flush."""
        import tempfile
        import os
        
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = AttributionRecorder(output_dir=tmpdir, buffer_size=2)
            
            # Add 1 row - should not flush
            recorder.log_router_row(
                timestamp="2026-04-22 09:15:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="test1",
                candidate_order=0,
                status="candidate",
                evaluated=False,
                winner=False,
            )
            
            # Check file doesn't exist yet
            csv_path = os.path.join(tmpdir, "router_evaluation_log.csv")
            assert not os.path.exists(csv_path)
            
            # Add 2nd row - should flush
            recorder.log_router_row(
                timestamp="2026-04-22 09:16:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="test2",
                candidate_order=1,
                status="candidate",
                evaluated=False,
                winner=False,
            )
            
            # File should exist now
            assert os.path.exists(csv_path)
            
            # Buffer should be cleared
            assert len(recorder.router_rows) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])