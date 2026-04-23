#!/usr/bin/env python3
"""
Night Session Attribution Automation System

Complete automation for night trading with attribution tracking, 
starvation monitoring, and strategy reorder simulation.

This system integrates:
1. Attribution recording during night sessions
2. Real-time starvation alerts
3. Strategy reorder simulation
4. Automated reporting
5. Dashboard integration
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
import logging


@dataclass
class NightSessionConfig:
    """Configuration for night session automation."""
    # Paths
    project_root: Path = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
    
    # Attribution system
    attribution_dir: Path = project_root / "data" / "attribution"
    attribution_enabled: bool = True
    attribution_buffer_size: int = 1000
    attribution_flush_interval: int = 300  # 5 minutes
    
    # Monitoring
    reports_dir: Path = project_root / "reports" / "night_session"
    alerts_dir: Path = project_root / "alerts" / "night_session"
    logs_dir: Path = project_root / "logs"
    
    # Alert thresholds
    starvation_threshold: float = 0.7
    priority_impact_threshold: float = 2.0
    low_evaluation_threshold: int = 10
    
    # Reorder simulation
    reorder_simulation_enabled: bool = True
    reorder_interval_minutes: int = 120  # Every 2 hours
    default_orders: List[List[str]] = None
    
    # Email alerts
    email_enabled: bool = False
    email_recipient: str = ""
    
    # Dashboard
    dashboard_enabled: bool = True
    dashboard_port: int = 8500
    
    # Night session hours (15:00-05:00)
    night_session_start_hour: int = 15
    night_session_end_hour: int = 5
    
    def __post_init__(self):
        """Initialize default values and create directories."""
        if self.default_orders is None:
            self.default_orders = [
                ["counter_vwap", "spring_upthrust", "kbar_feature"],
                ["kbar_feature", "counter_vwap", "spring_upthrust"],
                ["spring_upthrust", "kbar_feature", "counter_vwap"]
            ]
        
        # Create directories
        self.attribution_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


class NightSessionAutomation:
    """Complete night session automation system."""
    
    def __init__(self, config: NightSessionConfig):
        self.config = config
        self.running = False
        self.attribution_recorder = None
        self.logger = self.setup_logging()
        
        # State tracking
        self.last_report_time = None
        self.last_alert_check = None
        self.last_reorder_simulation = None
        self.night_session_active = False
        
    def setup_logging(self):
        """Setup logging configuration."""
        log_file = Path(self.config.logs_dir) / "night_automation.log"
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        return logging.getLogger(__name__)
    
    def is_night_session(self) -> bool:
        """Check if current time is within night session hours."""
        from core.date_utils import is_night_session
        
        try:
            return is_night_session(datetime.now())
        except ImportError:
            # Fallback if date_utils not available
            hour = datetime.now().hour
            return hour >= self.config.night_session_start_hour or hour < self.config.night_session_end_hour
    
    def setup_attribution_system(self) -> bool:
        """Setup attribution recording system."""
        if not self.config.attribution_enabled:
            self.logger.info("Attribution system disabled in config")
            return True
        
        try:
            # Import attribution recorder
            sys.path.insert(0, str(self.config.project_root))
            from core.attribution_recorder import AttributionRecorder
            
            # Create attribution recorder
            self.attribution_recorder = AttributionRecorder(
                output_dir=str(self.config.attribution_dir),
                buffer_size=self.config.attribution_buffer_size,
                flush_interval_seconds=self.config.attribution_flush_interval,
                auto_flush=True
            )
            
            self.logger.info("Attribution system setup complete")
            self.logger.info(f"Output directory: {self.config.attribution_dir}")
            self.logger.info(f"Buffer size: {self.config.attribution_buffer_size}")
            self.logger.info(f"Flush interval: {self.config.attribution_flush_interval}s")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to setup attribution system: {e}")
            return False
    
    def check_attribution_data(self) -> Tuple[bool, Dict]:
        """Check attribution data health and availability."""
        try:
            files = {
                "router_log": self.config.attribution_dir / "router_evaluation_log.csv",
                "signal_log": self.config.attribution_dir / "strategy_signal_log.csv",
                "trade_log": self.config.attribution_dir / "trade_attribution_log.csv"
            }
            
            stats = {}
            for name, path in files.items():
                exists = path.exists()
                stats[f"{name}_exists"] = exists
                if exists:
                    stats[f"{name}_size"] = path.stat().st_size
                    stats[f"{name}_modified"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
                else:
                    stats[f"{name}_size"] = 0
                    stats[f"{name}_modified"] = None
            
            # Check if data is recent (within last hour)
            is_recent = False
            if stats.get("router_log_modified"):
                last_modified = datetime.fromisoformat(stats["router_log_modified"])
                time_diff = datetime.now() - last_modified
                is_recent = time_diff.total_seconds() < 3600
            
            return is_recent, stats
            
        except Exception as e:
            self.logger.error(f"Error checking attribution data: {e}")
            return False, {}
    
    def generate_attribution_report(self) -> bool:
        """Generate attribution reports."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            report_dir = self.config.reports_dir / f"attribution_{timestamp}"
            report_dir.mkdir(parents=True, exist_ok=True)
            
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
                
                # Save metadata
                metadata = {
                    "timestamp": datetime.now().isoformat(),
                    "report_dir": str(report_dir),
                    "command": " ".join(cmd),
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
                    if any(marker in line for marker in ['🚨', '⚠️', '🔍', 'ALERT', 'WARNING']):
                        alerts.append({
                            "message": line.strip(),
                            "timestamp": datetime.now().isoformat(),
                            "level": "critical" if '🚨' in line else "warning"
                        })
            
            # Save alerts to file
            if alerts:
                alert_file = self.config.alerts_dir / f"alerts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                with open(alert_file, 'w') as f:
                    json.dump({
                        "timestamp": datetime.now().isoformat(),
                        "alerts": alerts,
                        "count": len(alerts)
                    }, f, indent=2)
                
                self.logger.warning(f"Found {len(alerts)} starvation alerts, saved to {alert_file}")
            
            return alerts
            
        except Exception as e:
            self.logger.error(f"Error checking starvation alerts: {e}")
            return []
    
    def run_reorder_simulation(self) -> bool:
        """Run strategy reorder simulation."""
        if not self.config.reorder_simulation_enabled:
            self.logger.info("Reorder simulation disabled in config")
            return True
        
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            sim_dir = self.config.reports_dir / f"reorder_sim_{timestamp}"
            sim_dir.mkdir(parents=True, exist_ok=True)
            
            # Build command
            cmd = [
                sys.executable,
                str(self.config.project_root / "docs" / "strategy_reorder_simulator.py"),
                "--input-dir", str(self.config.attribution_dir),
                "--output-dir", str(sim_dir),
                "--min-trades-per-strategy", "5"
            ]
            
            # Add orders
            for order in self.config.default_orders:
                cmd.extend(["--order", ",".join(order)])
            
            self.logger.info(f"Running reorder simulation: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.config.project_root)
            )
            
            if result.returncode == 0:
                self.logger.info(f"Reorder simulation completed: {sim_dir}")
                
                # Check results
                summary_file = sim_dir / "simulation_summary.csv"
                if summary_file.exists():
                    df = pd.read_csv(summary_file)
                    if not df.empty:
                        best_order = df.iloc[0]
                        self.logger.info(f"Best order: {best_order['simulated_order']}")
                        self.logger.info(f"Expected PnL delta: {best_order['expected_pnl_delta_sum']:.2f}")
                
                return True
            else:
                self.logger.error(f"Reorder simulation failed: {result.stderr}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error running reorder simulation: {e}")
            return False
    
    def update_dashboard(self) -> bool:
        """Update dashboard with latest data."""
        if not self.config.dashboard_enabled:
            return True
        
        try:
            # Check if dashboard is running
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', self.config.dashboard_port))
            sock.close()
            
            if result == 0:
                self.logger.info(f"Dashboard running on port {self.config.dashboard_port}")
                
                # Trigger dashboard refresh if possible
                try:
                    import requests
                    response = requests.get(f"http://localhost:{self.config.dashboard_port}/_stcore/health", timeout=5)
                    if response.status_code == 200:
                        self.logger.info("Dashboard health check passed")
                except:
                    pass  # Ignore if we can't refresh
                
                return True
            else:
                self.logger.warning(f"Dashboard not running on port {self.config.dashboard_port}")
                return False
                
        except Exception as e:
            self.logger.error(f"Error checking dashboard: {e}")
            return False
    
    def run_monitoring_cycle(self):
        """Run a complete monitoring cycle."""
        cycle_start = datetime.now()
        self.logger.info(f"Starting monitoring cycle at {cycle_start}")
        
        # Check if night session is active
        self.night_session_active = self.is_night_session()
        if not self.night_session_active:
            self.logger.info("Night session not active, skipping cycle")
            return
        
        self.logger.info("Night session active, running monitoring...")
        
        # 1. Setup attribution system if needed
        if self.config.attribution_enabled and not self.attribution_recorder:
            self.setup_attribution_system()
        
        # 2. Check attribution data health
        is_recent, stats = self.check_attribution_data()
        self.logger.info(f"Attribution data: recent={is_recent}, stats={stats}")
        
        # 3. Generate reports (hourly)
        if not self.last_report_time or (cycle_start - self.last_report_time).total_seconds() > 3600:
            if self.generate_attribution_report():
                self.last_report_time = cycle_start
        
        # 4. Check for alerts (every 15 minutes)
        if not self.last_alert_check or (cycle_start - self.last_alert_check).total_seconds() > 900:
            alerts = self.check_starvation_alerts()
            if alerts:
                self.logger.warning(f"Found {len(alerts)} starvation alerts")
            self.last_alert_check = cycle_start
        
        # 5. Run reorder simulation (every 2 hours)
        if (self.config.reorder_simulation_enabled and 
            (not self.last_reorder_simulation or 
             (cycle_start - self.last_reorder_simulation).total_seconds() > 7200)):
            if self.run_reorder_simulation():
                self.last_reorder_simulation = cycle_start
        
        # 6. Update dashboard
        self.update_dashboard()
        
        # 7. Log cycle completion
        cycle_duration = (datetime.now() - cycle_start).total_seconds()
        self.logger.info(f"Monitoring cycle completed in {cycle_duration:.2f} seconds")
    
    def run_continuous_monitoring(self):
        """Run continuous monitoring."""
        self.running = True
        self.logger.info("Starting continuous night session automation")
        self.logger.info(f"Night session hours: {self.config.night_session_start_hour}:00 - {self.config.night_session_end_hour}:00")
        self.logger.info(f"Check interval: {self.config.attribution_flush_interval} seconds")
        
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
                    time.sleep(self.config.attribution_flush_interval)
                    
        except KeyboardInterrupt:
            self.logger.info("Monitoring stopped by user")
        finally:
            self.logger.info("Night session automation stopped")
    
    def generate_system_summary(self) -> Dict:
        """Generate system summary report."""
        try:
            # Check attribution data
            is_recent, stats = self.check_attribution_data()
            
            # Check for alerts
            alerts = self.check_starvation_alerts()
            
            # Generate summary
            summary = {
                "timestamp": datetime.now().isoformat(),
                "night_session_active": self.night_session_active,
                "attribution_enabled": self.config.attribution_enabled,
                "attribution_data_available": stats.get("router_log_exists", False),
                "attribution_data_recent": is_recent,
                "starvation_alerts": len(alerts),
                "reorder_simulation_enabled": self.config.reorder_simulation_enabled,
                "dashboard_enabled": self.config.dashboard_enabled,
                "system_status": "healthy" if is_recent and len(alerts) == 0 else "needs_attention",
                "recommendations": []
            }
            
            # Add recommendations
            if not stats.get("router_log_exists"):
                summary["recommendations"].append("Enable attribution recording")
            
            if not is_recent:
                summary["recommendations"].append("Check attribution data source")
            
            if len(alerts) > 0:
                summary["recommendations"].append(f"Review {len(alerts)} starvation alerts")
            
            return summary
            
        except Exception as e:
            self.logger.error(f"Error generating system summary: {e}")
            return {"error": str(e)}


def create_default_config():
    """Create default configuration file."""
    config = NightSessionConfig()
    
    config_dict = {
        "attribution_dir": str(config.attribution_dir),
        "attribution_enabled": config.attribution_enabled,
        "attribution_buffer_size": config.attribution_buffer_size,
        "attribution_flush_interval": config.attribution_flush_interval,
        "reports_dir": str(config.reports_dir),
        "alerts_dir": str(config.alerts_dir),
        "logs_dir": str(config.logs_dir),
        "starvation_threshold": config.starvation_threshold,
        "priority_impact_threshold": config.priority_impact_threshold,
        "low_evaluation_threshold": config.low_evaluation_threshold,
        "reorder_simulation_enabled": config.reorder_simulation_enabled,
        "reorder_interval_minutes": config.reorder_interval_minutes,
        "default_orders": config.default_orders,
        "email_enabled": config.email_enabled,
        "email_recipient": config.email_recipient,
        "dashboard_enabled": config.dashboard_enabled,
        "dashboard_port": config.dashboard_port,
        "night_session_start_hour": config.night_session_start_hour,
        "night_session_end_hour": config.night_session_end_hour
    }
    
    config_file = config.project_root / "config" / "night_automation_config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(config_file, 'w') as f:
        json.dump(config_dict, f, indent=2)
    
    return config_file


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Night Session Attribution Automation")
    parser.add_argument("--live", action="store_true", help="Run live monitoring")
    parser.add_argument("--report", action="store_true", help="Generate reports only")
    parser.add_argument("--alert", action="store_true", help="Check alerts only")
    parser.add_argument("--simulate", action="store_true", help="Run reorder simulation only")
    parser.add_argument("--summary", action="store_true", help="Generate system summary")
    parser.add_argument("--setup", action="store_true", help="Setup system configuration")
    parser.add_argument("--config", type=str, help="Path to config file")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds")
    
    args = parser.parse_args()
    
    # Setup configuration
    if args.setup:
        config_file = create_default_config()
        print(f"Default configuration created: {config_file}")
        return
    
    # Load configuration
    config = NightSessionConfig()
    if args.config:
        try:
            with open(args.config, 'r') as f:
                config_data = json.load(f)
                for key, value in config_data.items():
                    if hasattr(config, key):
                        setattr(config, key, value)
        except Exception as e:
            print(f"Error loading config: {e}")
            return
    
    if args.interval:
        config.attribution_flush_interval = args.interval
    
    # Create automation system
    automation = NightSessionAutomation(config)
    
    # Run based on mode
    if args.live:
        print("Starting live night session automation...")
        print(f"Project root: {config.project_root}")
        print(f"Attribution data: {config.attribution_dir}")
        print(f"Night session: {config.night_session_start_hour}:00 - {config.night_session_end_hour}:00")
        print(f"Check interval: {config.attribution_flush_interval} seconds")
        print("Press Ctrl+C to stop\n")
        
        automation.run_continuous_monitoring()
        
    elif args.report:
        print("Generating attribution reports...")
        success = automation.generate_attribution_report()
        if success:
            print("Reports generated successfully")
        else:
            print("Failed to generate reports")
            sys.exit(1)
            
    elif args.alert:
        print("Checking for starvation alerts...")
        alerts = automation.check_starvation_alerts()
        if alerts:
            print(f"Found {len(alerts)} alerts:")
            for alert in alerts:
                print(f"  [{alert['level']}] {alert['message']}")
        else:
            print("No alerts found")
            
    elif args.simulate:
        print("Running reorder simulation...")
        success = automation.run_reorder_simulation()
        if success:
            print("Simulation completed successfully")
        else:
            print("Simulation failed")
            sys.exit(1)
            
    elif args.summary:
        print("Generating system summary...")
        summary = automation.generate_system_summary()
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        
    else:
        # Default: run single cycle
        print("Running single monitoring cycle...")
        automation.run_monitoring_cycle()
        print("Cycle completed")


if __name__ == "__main__":
    main()