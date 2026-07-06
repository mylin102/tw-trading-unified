"""Integration tests for AttributionRecorder CSV export."""

import os
import tempfile
import pandas as pd
import pytest

from core.attribution_recorder import AttributionRecorder


class TestAttributionRecorderIntegration:
    """Level 1.2: AttributionRecorder CSV export integration tests."""
    
    def test_csv_export_creates_files(self):
        """Test CSV export creates files with correct headers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = AttributionRecorder(output_dir=tmpdir, buffer_size=5)
            
            # Add some data
            for i in range(3):
                recorder.log_router_row(
                    timestamp=f"2026-04-22 09:{i:02d}:00",
                    symbol="TX",
                    regime="WEAK",
                    strategy_name=f"strategy_{i}",
                    candidate_order=i,
                    status="candidate",
                    evaluated=False,
                    winner=False,
                )
            
            # Force export
            recorder.export_csv_if_needed(force=True)
            
            # Check files exist
            router_path = os.path.join(tmpdir, "router_evaluation_log.csv")
            assert os.path.exists(router_path)
            
            # Read CSV and check content
            df = pd.read_csv(router_path)
            assert len(df) == 3
            assert "timestamp" in df.columns
            assert "strategy_name" in df.columns
            assert "status" in df.columns
    
    def test_csv_append_mode(self):
        """Test CSV append mode preserves existing data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = AttributionRecorder(output_dir=tmpdir, buffer_size=2)
            
            # First batch
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
            recorder.log_router_row(
                timestamp="2026-04-22 09:15:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="kbar_feature",
                candidate_order=1,
                status="winner",
                evaluated=True,
                winner=True,
            )
            
            # Should auto-flush after 2 rows
            router_path = os.path.join(tmpdir, "router_evaluation_log.csv")
            assert os.path.exists(router_path)
            
            first_df = pd.read_csv(router_path)
            assert len(first_df) == 2
            
            # Second batch
            recorder.log_router_row(
                timestamp="2026-04-22 09:16:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="spring_upthrust",
                candidate_order=0,
                status="no_signal",
                evaluated=True,
                winner=False,
            )
            recorder.log_router_row(
                timestamp="2026-04-22 09:16:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="counter_vwap",
                candidate_order=1,
                status="winner",
                evaluated=True,
                winner=True,
            )
            
            # Should append
            second_df = pd.read_csv(router_path)
            assert len(second_df) == 4  # 2 + 2
            
            # Check all strategies present
            strategies = set(second_df["strategy_name"])
            assert "counter_vwap" in strategies
            assert "kbar_feature" in strategies
            assert "spring_upthrust" in strategies
    
    def test_multiple_csv_files(self):
        """Test export creates router, signal, and trade CSV files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = AttributionRecorder(output_dir=tmpdir, buffer_size=1)
            
            # Add one of each type
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
            recorder.log_signal(
                timestamp="2026-04-22 09:15:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="test",
                candidate_order=0,
                side="SELL",
                signal_type="TEST",
                selected=True,
                score=-20.0,
                notes="test",
            )
            recorder.log_trade(
                trade_id="T1",
                symbol="TX",
                strategy_name="test",
                regime_at_entry="WEAK",
                side="SELL",
                entry_time="2026-04-22 09:15:00",
                exit_time="2026-04-22 09:18:00",
                entry_price=20100.0,
                exit_price=20070.0,
                pnl=30.0,
                exit_reason="target",
            )
            
            # Force export
            recorder.export_csv_if_needed(force=True)
            
            # Check all files exist
            assert os.path.exists(os.path.join(tmpdir, "router_evaluation_log.csv"))
            assert os.path.exists(os.path.join(tmpdir, "strategy_signal_log.csv"))
            assert os.path.exists(os.path.join(tmpdir, "trade_attribution_log.csv"))
    
    def test_buffer_size_respected(self):
        """Test buffer size triggers flush at correct threshold."""
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = AttributionRecorder(output_dir=tmpdir, buffer_size=3)
            
            # Add 2 rows - should not flush
            for i in range(2):
                recorder.log_router_row(
                    timestamp=f"2026-04-22 09:{i:02d}:00",
                    symbol="TX",
                    regime="WEAK",
                    strategy_name=f"strategy_{i}",
                    candidate_order=i,
                    status="candidate",
                    evaluated=False,
                    winner=False,
                )
            
            router_path = os.path.join(tmpdir, "router_evaluation_log.csv")
            assert not os.path.exists(router_path)
            
            # Add 3rd row - should flush
            recorder.log_router_row(
                timestamp="2026-04-22 09:02:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="strategy_2",
                candidate_order=2,
                status="candidate",
                evaluated=False,
                winner=False,
            )
            
            assert os.path.exists(router_path)
            df = pd.read_csv(router_path)
            assert len(df) == 3
    
    def test_clear_buffers_after_export(self):
        """Test buffers are cleared after successful export."""
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = AttributionRecorder(output_dir=tmpdir, buffer_size=2)
            
            # Add data
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
            recorder.log_router_row(
                timestamp="2026-04-22 09:16:00",
                symbol="TX",
                regime="WEAK",
                strategy_name="test",
                candidate_order=0,
                status="candidate",
                evaluated=False,
                winner=False,
            )
            
            # Should flush and clear
            assert len(recorder.router_rows) == 0  # Cleared by flush
    
    def test_export_with_no_data(self):
        """Test export with no data does nothing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = AttributionRecorder(output_dir=tmpdir)
            
            # Export with no data (force=False)
            recorder.export_csv_if_needed(force=False)
            
            # No files should be created
            router_path = os.path.join(tmpdir, "router_evaluation_log.csv")
            assert not os.path.exists(router_path)
            
            # Force export with no data - should create empty file with header
            recorder.export_csv_if_needed(force=True)
            
            # File should be created
            assert os.path.exists(router_path)
            
            # Read CSV - should have only header
            import pandas as pd
            try:
                df = pd.read_csv(router_path)
                # Empty DataFrame with only columns
                assert len(df) == 0
            except pd.errors.EmptyDataError:
                # Empty file is also acceptable
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])