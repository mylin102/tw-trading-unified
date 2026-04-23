#!/usr/bin/env python3
"""
Night Session Attribution Monitor

Integrated monitoring system for night trading sessions with attribution tracking.
Runs attribution analysis, generates alerts, and updates dashboard.

Usage:
    python scripts/night_attribution_monitor.py --live     # Live monitoring mode
    python scripts/night_attribution_monitor.py --report   # Generate reports only
    python scripts/night_attribution_monitor.py --alert    # Check alerts only
"""

import sys
import os
import time
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import subprocess
from typing import Dict, List, Optional, Tuple
import signal
import threading
from dataclasses import dataclass, asdict


@dataclass
class MonitorConfig:
    """Configuration for night session monitoring."""
    # Paths
    project_root: Path = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
    attribution_dir: Path = project_root / "data" / "attribution"
    reports_dir: Path = project_root / "reports" / "night_session"
    alerts_dir: Path = project_root / "alerts" / "night_session"
    logs_dir: Path = project_root / "logs"
    
    # Monitoring intervals
    check_interval_seconds: int = 300  # 5 minutes
    attribution_flush_interval: int = 300  # 5 minutes
    report_interval_minutes: int = 60  # Hourly reports
    
    # Alert thresholds
    starvation_threshold: float = 0.7
    priority_impact_threshold: float = 2.0
    low_evaluation_threshold: int = 10  # Minimum evaluations
    
    # Email alerts
    email_enabled: bool = False
    email_recipient: str = ""
    
    # Dashboard integration
    dashboard_enabled: bool = True
    dashboard_port: int = 8500
    
    def __post_init__(self):
        """Create directories if they don't exist."""
        self.attribution_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


class NightAttributionMonitor:
    """Main monitoring class for night session attribution."""
    
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.running = False
        self.attribution_enabled = False
        self.last_report_time = None
        self.last_alert_check = None
        
        # Setup logging
        self.log_file = config.logs_dir / "night_attribution_monitor.log"
        self.setup_logging()
        
    def setup_logging(self):
        """Setup logging configuration."""
        import logging
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(self.log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def check_night_session_active(self) -> bool:
        """Check if night session is currently active."""
        from core.date_utils import is_night_session
        
        now = datetime.now()
        return is_night_session(now)
    
    def enable_attribution(self) -> bool:
        """Enable attribution recording for night session."""
        try:
            # Check if attribution is already enabled
            if self.attribution_enabled:
                self.logger.info("Attribution already enabled")
                return True
            
            # Import attribution recorder
            sys.path.insert(0, str(self.config.project_root))
            from core.attribution_recorder import AttributionRecorder
            
            # Create attribution recorder
            self.attribution_recorder = AttributionRecorder(
                output_dir=str(self.config.attribution_dir),
                buffer_size=1000,
                flush_interval_seconds=self.config.attribution_flush_interval,
                auto_flush=True
            )
            
            self.attribution_enabled = True
            self.logger.info("Attribution enabled for night session")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to enable attribution: {e}")
            return False
    
    def check_attribution_data(self) -> Tuple[bool, Dict]:
        """Check attribution data availability and health."""
        try:
            router_log = self.config.attribution_dir / "router_evaluation_log.csv"
            signal_log = self.config.attribution_dir / "strategy_signal_log.csv"
            trade_log = self.config.attribution_dir / "trade_attribution_log.csv"
            
            stats = {
                "router_log_exists": router_log.exists(),
                "signal_log_exists": signal_log.exists(),
                "trade_log_exists": trade_log.exists(),
                "router_log_size": router_log.stat().st_size if router_log.exists() else 0,
                "signal_log_size": signal_log.stat().st_size if signal_log.exists() else 0,
                "trade_log_size": trade_log.stat().st_size if trade_log.exists() else 0,
                "last_modified": None
            }
            
            # Get last modified time
            if router_log.exists():
                stats["last_modified"] = datetime.fromtimestamp(router_log.stat().st_mtime)
            
            # Check if data is recent (within last hour)
            is_recent = False
            if stats["last_modified"]:
                time_diff = datetime.now() - stats["last_modified"]
                is_recent = time_diff.total_seconds() < 3600  # 1 hour
            
            return is_recent, stats
            
        except Exception as e:
            self.logger.error(f"Error checking attribution data: {e}")
            return False, {}
    
    def generate_attribution_report(self) -> bool:
        """Generate attribution reports for night session."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            report_dir = self.config.reports_dir / f"report_{timestamp}"
            report_dir.mkdir(parents=True, exist_ok=True)
            
            # Run attribution report script
            cmd = [
                sys.executable,
                str(self.config.project_root / "scripts" / "attribution_report.py"),
                "--input-dir", str(self.config.attribution_dir),
                "--output-dir", str(report_dir),
                "--force",
                "--verbose"
            ]
            
            self.logger.info(f"Generating attribution report: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.config.project_root)
            )
            
            if result.returncode == 0:
                self.logger.info(f"Attribution report generated: {report_dir}")
                
                # Save report metadata
                metadata = {
                    "timestamp": datetime.now().isoformat(),
                    "report_dir": str(report_dir),
                    "command": " ".join(cmd),
                    "output": result.stdout[-1000:] if result.stdout else "",
                    "success": True
                }
                
                metadata_file = report_dir / "metadata.json"
                with open(metadata_file, 'w') as f:
                    json.dump(metadata, f, indent=2)
                
                return True
            else:
                self.logger.error(f"Failed to generate attribution report: {result.stderr}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error generating attribution report: {e}")
            return False
    
    def check_starvation_alerts(self) -> List[Dict]:
        """Check for starvation alerts."""
        try:
            cmd = [
                sys.executable,
                str(self.config.project_root / "scripts" / "starvation_alerts.py"),
                "--input-dir", str(self.config.attribution_dir),
                "--output-dir", str(self.config.alerts_dir),
                "--threshold", str(self.config.starvation_threshold)
            ]
            
            if self.config.email_enabled and self.config.email_recipient:
                cmd.extend(["--email", self.config.email_recipient])
            
            self.logger.info(f"Checking starvation alerts: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.config.project_root)
            )
            
            alerts = []
            if result.returncode == 1:  # Alerts exist
                # Parse alerts from output
                for line in result.stdout.split('\n'):
                    if '🚨' in line or '⚠️' in line or '🔍' in line:
                        alerts.append({"message": line.strip(), "timestamp": datetime.now().isoformat()})
            
            return alerts
            
        except Exception as e:
            self.logger.error(f"Error checking starvation alerts: {e}")
            return []
    
    def update_dashboard(self) -> bool:
        """Update dashboard with latest attribution data."""
        if not self.config.dashboard_enabled:
            return True
            
        try:
            # Check if dashboard is running
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', self.config.dashboard_port))
            sock.close()
            
            if result == 0:
                self.logger.info(f"Dashboard is running on port {self.config.dashboard_port}")
                return True
            else:
                self.logger.warning(f"Dashboard not running on port {self.config.dashboard_port}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error checking dashboard: {e}")
            return False
    
    def run_monitoring_cycle(self):
        """Run a single monitoring cycle."""
        cycle_start = datetime.now()
        self.logger.info(f"Starting monitoring cycle at {cycle_start}")
        
        # 1. Check if night session is active
        if not self.check_night_session_active():
            self.logger.info("Night session not active, skipping cycle")
            return
        
        # 2. Enable attribution if not already enabled
        if not self.attribution_enabled:
            self.enable_attribution()
        
        # 3. Check attribution data health
        is_recent, stats = self.check_attribution_data()
        self.logger.info(f"Attribution data health: recent={is_recent}, stats={stats}")
        
        # 4. Generate reports if needed (hourly)
        if not self.last_report_time or (cycle_start - self.last_report_time).total_seconds() > 3600:
            if self.generate_attribution_report():
                self.last_report_time = cycle_start
        
        # 5. Check for alerts (every 15 minutes)
        if not self.last_alert_check or (cycle_start - self.last_alert_check).total_seconds() > 900:
            alerts = self.check_starvation_alerts()
            if alerts:
                self.logger.warning(f"Found {len(alerts)} starvation alerts")
                for alert in alerts:
                    self.logger.warning(f"Alert: {alert['message']}")
            self.last_alert_check = cycle_start
        
        # 6. Update dashboard
        self.update_dashboard()
        
        # 7. Log cycle completion
        cycle_duration = (datetime.now() - cycle_start).total_seconds()
        self.logger.info(f"Monitoring cycle completed in {cycle_duration:.2f} seconds")
    
    def run_continuous_monitoring(self):
        """Run continuous monitoring."""
        self.running = True
        self.logger.info("Starting continuous night session monitoring")
        
        # Signal handler for graceful shutdown
        def signal_handler(signum, frame):
            self.logger.info("Received shutdown signal")
            self.running = False
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            while self.running:
                try:
                    self.run_monitoring_cycle()
                except Exception as e:
                    self.logger.error(f"Error in monitoring cycle: {e}")
                
                # Wait for next cycle
                if self.running:
                    time.sleep(self.config.check_interval_seconds)
                    
        except KeyboardInterrupt:
            self.logger.info("Monitoring stopped by user")
        finally:
            self.logger.info("Night session monitoring stopped")
    
    def generate_summary_report(self) -> Dict:
        """Generate summary report of night session."""
        try:
            # Load latest attribution data
            router_log = self.config.attribution_dir / "router_evaluation_log.csv"
            if not router_log.exists():
                return {"error": "No attribution data found"}
            
            df = pd.read_csv(router_log)
            if 'timestamp' in df.columns:
                df['timestamp'] = pd.to_datetime(df['timestamp'])
            
            # Filter for night session
            from core.date_utils import is_night_session
            if 'timestamp' in df.columns:
                night_mask = df['timestamp'].apply(is_night_session)
                night_df = df[night_mask].copy()
            else:
                night_df = df.copy()
            
            # Calculate statistics
            total_bars = len(night_df)
            strategies = night_df['strategy_name'].nunique() if 'strategy_name' in night_df.columns else 0
            
            # Strategy exposure
            if 'strategy_name' in night_df.columns and 'evaluated' in night_df.columns:
                strategy_exposure = night_df.groupby('strategy_name')['evaluated'].sum().to_dict()
            else:
                strategy_exposure = {}
            
            # Generate summary
            summary = {
                "timestamp": datetime.now().isoformat(),
                "night_session_active": self.check_night_session_active(),
                "total_bars_night": total_bars,
                "unique_strategies": strategies,
                "strategy_exposure": strategy_exposure,
                "attribution_data_healthy": router_log.exists(),
                "last_attribution_update": None,
                "alerts_generated": len(self.check_starvation_alerts()) > 0
            }
            
            # Add last modified time
            if router_log.exists():
                summary["last_attribution_update"] = datetime.fromtimestamp(
                    router_log.stat().st_mtime
                ).isoformat()
            
            return summary
            
        except Exception as e:
            self.logger.error(f"Error generating summary report: {e}")
            return {"error": str(e)}


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Night Session Attribution Monitor")
    parser.add_argument("--live", action="store_true", help="Run live monitoring")
    parser.add_argument("--report", action="store_true", help="Generate reports only")
    parser.add_argument("--alert", action="store_true", help="Check alerts only")
    parser.add_argument("--summary", action="store_true", help="Generate summary report")
    parser.add_argument("--config", type=str, help="Path to config file")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds")
    
    args = parser.parse_args()
    
    # Load configuration
    config = MonitorConfig()
    if args.config:
        try:
            with open(args.config, 'r') as f:
                config_data = json.load(f)
                for key, value in config_data.items():
                    if hasattr(config, key):
                        setattr(config, key, value)
        except Exception as e:
            print(f"Error loading config: {e}")
    
    if args.interval:
        config.check_interval_seconds = args.interval
    
    # Create monitor
    monitor = NightAttributionMonitor(config)
    
    # Run based on mode
    if args.live:
        print("Starting live night session monitoring...")
        print(f"Log file: {monitor.log_file}")
        print(f"Attribution data: {config.attribution_dir}")
        print(f"Check interval: {config.check_interval_seconds} seconds")
        print("Press Ctrl+C to stop\n")
        
        monitor.run_continuous_monitoring()
        
    elif args.report:
        print("Generating attribution reports...")
        success = monitor.generate_attribution_report()
        if success:
            print("Reports generated successfully")
        else:
            print("Failed to generate reports")
            sys.exit(1)
            
    elif args.alert:
        print("Checking for starvation alerts...")
        alerts = monitor.check_starvation_alerts()
        if alerts:
            print(f"Found {len(alerts)} alerts:")
            for alert in alerts:
                print(f"  - {alert['message']}")
        else:
            print("No alerts found")
            
    elif args.summary:
        print("Generating summary report...")
        summary = monitor.generate_summary_report()
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        
    else:
        # Default: run single cycle
        print("Running single monitoring cycle...")
        monitor.run_monitoring_cycle()
        print("Cycle completed")


if __name__ == "__main__":
    main()