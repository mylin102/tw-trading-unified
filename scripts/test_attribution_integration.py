#!/usr/bin/env python3
"""
Complete Attribution System Integration Test

This script tests the entire attribution system integration:
1. Attribution recorder functionality
2. Router integration
3. Report generation
4. Starvation alerts
5. Reorder simulation
6. Night automation
"""

import sys
import os
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
import subprocess
import pandas as pd
import numpy as np

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

class AttributionSystemTest:
    """Complete attribution system integration test."""
    
    def __init__(self):
        self.test_dir = PROJECT_ROOT / "tests" / "integration" / "attribution"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "tests": {},
            "summary": {}
        }
        
    def log_test(self, name, success, message=""):
        """Log test result."""
        self.results["tests"][name] = {
            "success": success,
            "message": message,
            "timestamp": datetime.now().isoformat()
        }
        
        status = "✅" if success else "❌"
        print(f"{status} {name}: {message}")
        
    def test_attribution_recorder_import(self):
        """Test attribution recorder import."""
        try:
            from core.attribution_recorder import AttributionRecorder
            self.log_test("attribution_recorder_import", True, "Import successful")
            return True
        except Exception as e:
            self.log_test("attribution_recorder_import", False, f"Import failed: {e}")
            return False
    
    def test_attribution_recorder_functionality(self):
        """Test attribution recorder functionality."""
        try:
            from core.attribution_recorder import AttributionRecorder
            
            with tempfile.TemporaryDirectory() as tmpdir:
                recorder = AttributionRecorder(
                    output_dir=tmpdir,
                    buffer_size=10,
                    flush_interval_seconds=1,
                    flush_on_exit=True
                )
                
                # Test logging
                recorder.log_router_row(
                    timestamp="2026-04-22 21:00:00",
                    symbol="TX",
                    regime="WEAK",
                    strategy_name="test_strategy",
                    candidate_order=1,
                    status="evaluated",
                    evaluated=True,
                    winner=False,
                    signal_side=None,
                    signal_type=None,
                    notes="test"
                )
                
                # Test flush
                recorder.export_csv_if_needed(force=True)
                
                # Check files
                router_file = Path(tmpdir) / "router_evaluation_log.csv"
                if router_file.exists():
                    df = pd.read_csv(router_file)
                    if len(df) > 0:
                        self.log_test("attribution_recorder_functionality", True, 
                                     f"Logged {len(df)} rows successfully")
                        return True
                    else:
                        self.log_test("attribution_recorder_functionality", False, 
                                     "File created but empty")
                        return False
                else:
                    self.log_test("attribution_recorder_functionality", False, 
                                 "No CSV file created")
                    return False
                    
        except Exception as e:
            self.log_test("attribution_recorder_functionality", False, f"Error: {e}")
            return False
    
    def test_router_integration(self):
        """Test router integration with attribution."""
        try:
            # Check if router has attribution parameter
            router_file = PROJECT_ROOT / "core" / "futures_strategy_router.py"
            with open(router_file, 'r') as f:
                content = f.read()
                
            if "attribution_recorder" in content and "recorder=" in content:
                self.log_test("router_integration", True, "Router has attribution parameter")
                return True
            else:
                self.log_test("router_integration", False, "Router missing attribution parameter")
                return False
                
        except Exception as e:
            self.log_test("router_integration", False, f"Error: {e}")
            return False
    
    def test_monitor_integration(self):
        """Test monitor integration with attribution."""
        try:
            monitor_file = PROJECT_ROOT / "strategies" / "futures" / "monitor.py"
            with open(monitor_file, 'r') as f:
                content = f.read()
                
            if "_route_signal" in content and "attribution_recorder" in content:
                self.log_test("monitor_integration", True, "Monitor has attribution integration")
                return True
            else:
                self.log_test("monitor_integration", False, "Monitor missing attribution integration")
                return False
                
        except Exception as e:
            self.log_test("monitor_integration", False, f"Error: {e}")
            return False
    
    def test_attribution_backtest(self):
        """Test attribution backtest script."""
        try:
            cmd = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "attribution_backtest.py"),
                "--sample", "50"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=30
            )
            
            if result.returncode == 0:
                # Check output directory
                output_dir = PROJECT_ROOT / "exports" / "attribution_backtest"
                if output_dir.exists():
                    csv_files = list(output_dir.rglob("*.csv"))
                    if len(csv_files) > 0:
                        self.log_test("attribution_backtest", True, 
                                     f"Generated {len(csv_files)} CSV files")
                        return True
                    else:
                        self.log_test("attribution_backtest", False, "No CSV files generated")
                        return False
                else:
                    self.log_test("attribution_backtest", False, "Output directory not created")
                    return False
            else:
                self.log_test("attribution_backtest", False, 
                             f"Script failed: {result.stderr[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            self.log_test("attribution_backtest", False, "Script timeout")
            return False
        except Exception as e:
            self.log_test("attribution_backtest", False, f"Error: {e}")
            return False
    
    def test_attribution_report(self):
        """Test attribution report generation."""
        try:
            # First ensure we have test data
            test_data_dir = PROJECT_ROOT / "exports" / "attribution_backtest" / "attribution_data"
            if not test_data_dir.exists():
                # Run backtest first
                self.test_attribution_backtest()
            
            cmd = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "attribution_report.py"),
                "--input-dir", str(test_data_dir),
                "--output-dir", str(self.test_dir / "reports"),
                "--force"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=30
            )
            
            if result.returncode == 0:
                report_dir = self.test_dir / "reports"
                if report_dir.exists():
                    csv_files = list(report_dir.rglob("*.csv"))
                    if len(csv_files) > 0:
                        self.log_test("attribution_report", True, 
                                     f"Generated {len(csv_files)} report files")
                        return True
                    else:
                        self.log_test("attribution_report", False, "No report files generated")
                        return False
                else:
                    self.log_test("attribution_report", False, "Report directory not created")
                    return False
            else:
                self.log_test("attribution_report", False, 
                             f"Script failed: {result.stderr[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            self.log_test("attribution_report", False, "Script timeout")
            return False
        except Exception as e:
            self.log_test("attribution_report", False, f"Error: {e}")
            return False
    
    def test_starvation_alerts(self):
        """Test starvation alert system."""
        try:
            test_data_dir = PROJECT_ROOT / "exports" / "attribution_backtest" / "attribution_data"
            if not test_data_dir.exists():
                # Run backtest first
                self.test_attribution_backtest()
            
            cmd = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "starvation_alerts.py"),
                "--input-dir", str(test_data_dir),
                "--output-dir", str(self.test_dir / "alerts"),
                "--threshold", "0.7"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=30
            )
            
            # Return code 0 means no alerts, 1 means alerts found
            if result.returncode in [0, 1]:
                self.log_test("starvation_alerts", True, 
                             f"Alert check completed (return code: {result.returncode})")
                return True
            else:
                self.log_test("starvation_alerts", False, 
                             f"Script failed: {result.stderr[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            self.log_test("starvation_alerts", False, "Script timeout")
            return False
        except Exception as e:
            self.log_test("starvation_alerts", False, f"Error: {e}")
            return False
    
    def test_reorder_simulation(self):
        """Test strategy reorder simulation."""
        try:
            test_data_dir = PROJECT_ROOT / "exports" / "attribution_backtest" / "attribution_data"
            if not test_data_dir.exists():
                # Run backtest first
                self.test_attribution_backtest()
            
            cmd = [
                sys.executable,
                str(PROJECT_ROOT / "docs" / "strategy_reorder_simulator.py"),
                "--input-dir", str(test_data_dir),
                "--output-dir", str(self.test_dir / "reorder_sim"),
                "--order", "counter_vwap,spring_upthrust,kbar_feature",
                "--order", "kbar_feature,counter_vwap,spring_upthrust"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=30
            )
            
            if result.returncode == 0:
                sim_dir = self.test_dir / "reorder_sim"
                if sim_dir.exists():
                    csv_files = list(sim_dir.rglob("*.csv"))
                    if len(csv_files) > 0:
                        self.log_test("reorder_simulation", True, 
                                     f"Generated {len(csv_files)} simulation files")
                        return True
                    else:
                        self.log_test("reorder_simulation", False, "No simulation files generated")
                        return False
                else:
                    self.log_test("reorder_simulation", False, "Simulation directory not created")
                    return False
            else:
                self.log_test("reorder_simulation", False, 
                             f"Script failed: {result.stderr[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            self.log_test("reorder_simulation", False, "Script timeout")
            return False
        except Exception as e:
            self.log_test("reorder_simulation", False, f"Error: {e}")
            return False
    
    def test_night_automation(self):
        """Test night automation system."""
        try:
            cmd = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "night_automation.py"),
                "--summary"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=30
            )
            
            if result.returncode == 0:
                # Check if summary was printed
                if "timestamp" in result.stdout or "system_status" in result.stdout:
                    self.log_test("night_automation", True, "Automation summary generated")
                    return True
                else:
                    self.log_test("night_automation", False, "No summary output")
                    return False
            else:
                self.log_test("night_automation", False, 
                             f"Script failed: {result.stderr[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            self.log_test("night_automation", False, "Script timeout")
            return False
        except Exception as e:
            self.log_test("night_automation", False, f"Error: {e}")
            return False
    
    def test_launcher_script(self):
        """Test launcher script."""
        try:
            cmd = [
                "bash",
                str(PROJECT_ROOT / "scripts" / "night_attribution_launcher.sh"),
                "status"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=30
            )
            
            if result.returncode == 0:
                if "Night Session Attribution Automation" in result.stdout:
                    self.log_test("launcher_script", True, "Launcher script works")
                    return True
                else:
                    self.log_test("launcher_script", False, "Unexpected output")
                    return False
            else:
                self.log_test("launcher_script", False, 
                             f"Script failed: {result.stderr[:200]}")
                return False
                
        except subprocess.TimeoutExpired:
            self.log_test("launcher_script", False, "Script timeout")
            return False
        except Exception as e:
            self.log_test("launcher_script", False, f"Error: {e}")
            return False
    
    def test_dashboard_integration(self):
        """Test dashboard integration."""
        try:
            dashboard_file = PROJECT_ROOT / "ui" / "dashboard.py"
            with open(dashboard_file, 'r') as f:
                content = f.read()
                
            if "attribution_dashboard" in content and "Attribution" in content:
                self.log_test("dashboard_integration", True, "Dashboard has attribution tab")
                return True
            else:
                self.log_test("dashboard_integration", False, "Dashboard missing attribution tab")
                return False
                
        except Exception as e:
            self.log_test("dashboard_integration", False, f"Error: {e}")
            return False
    
    def run_all_tests(self):
        """Run all tests."""
        print("=" * 70)
        print("Attribution System Integration Test")
        print("=" * 70)
        print(f"Project: {PROJECT_ROOT}")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        
        # Run tests
        tests = [
            ("Attribution Recorder Import", self.test_attribution_recorder_import),
            ("Attribution Recorder Functionality", self.test_attribution_recorder_functionality),
            ("Router Integration", self.test_router_integration),
            ("Monitor Integration", self.test_monitor_integration),
            ("Attribution Backtest", self.test_attribution_backtest),
            ("Attribution Report", self.test_attribution_report),
            ("Starvation Alerts", self.test_starvation_alerts),
            ("Reorder Simulation", self.test_reorder_simulation),
            ("Night Automation", self.test_night_automation),
            ("Launcher Script", self.test_launcher_script),
            ("Dashboard Integration", self.test_dashboard_integration)
        ]
        
        results = []
        for name, test_func in tests:
            print(f"\n📋 Testing: {name}")
            print("-" * 40)
            success = test_func()
            results.append(success)
        
        # Generate summary
        total = len(results)
        passed = sum(results)
        failed = total - passed
        
        self.results["summary"] = {
            "total_tests": total,
            "passed": passed,
            "failed": failed,
            "success_rate": passed / total if total > 0 else 0
        }
        
        print("\n" + "=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        print(f"Total tests: {total}")
        print(f"Passed: {passed}")
        print(f"Failed: {failed}")
        print(f"Success rate: {passed/total*100:.1f}%")
        
        if failed == 0:
            print("\n🎉 All tests passed! Attribution system is fully integrated.")
        else:
            print(f"\n⚠️  {failed} test(s) failed. Check individual test results.")
        
        # Save results
        results_file = self.test_dir / f"integration_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(results_file, 'w') as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        
        print(f"\n📄 Detailed results saved to: {results_file}")
        
        return failed == 0

def main():
    """Main entry point."""
    test = AttributionSystemTest()
    success = test.run_all_tests()
    
    if success:
        print("\n" + "=" * 70)
        print("NEXT STEPS:")
        print("=" * 70)
        print("1. Setup the system:")
        print("   bash scripts/night_attribution_launcher.sh setup")
        print()
        print("2. Test the system:")
        print("   bash scripts/night_attribution_launcher.sh test")
        print()
        print("3. Setup automation (cron jobs):")
        print("   bash scripts/night_attribution_launcher.sh cron")
        print("   ./cron/night_session/install_cron.sh")
        print()
        print("4. Start manual monitoring:")
        print("   bash scripts/night_attribution_launcher.sh start")
        print()
        print("5. Check dashboard:")
        print("   streamlit run ui/dashboard.py")
        print("   Open http://localhost:8501 and click 'Attribution' tab")
        print()
        print("For more details, see docs/NIGHT_SESSION_AUTOMATION.md")
        print("=" * 70)
    else:
        print("\n" + "=" * 70)
        print("ISSUES DETECTED:")
        print("=" * 70)
        print("Some tests failed. Please check:")
        print("1. Attribution recorder implementation")
        print("2. Router and monitor integration")
        print("3. Script dependencies")
        print("4. File permissions and directories")
        print()
        print("Run individual tests to debug:")
        print("   python scripts/attribution_backtest.py --sample 50")
        print("   python scripts/attribution_report.py --help")
        print("   python scripts/starvation_alerts.py --help")
        print("=" * 70)
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()