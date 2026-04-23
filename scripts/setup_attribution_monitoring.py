#!/usr/bin/env python3
"""
Configure cron jobs for attribution monitoring.

This script sets up automated monitoring of strategy starvation.
"""

import sys
from pathlib import Path
import subprocess
import json
from datetime import datetime


def setup_cron_jobs():
    """Setup cron jobs for attribution monitoring."""
    
    # Paths
    project_root = Path(__file__).parent.parent
    alert_script = project_root / "scripts" / "starvation_alerts.py"
    report_script = project_root / "scripts" / "attribution_report.py"
    
    if not alert_script.exists():
        print(f"Error: Alert script not found at {alert_script}")
        return False
    
    # Create cron directory
    cron_dir = project_root / "cron"
    cron_dir.mkdir(exist_ok=True)
    
    # Create cron configuration
    cron_config = {
        "name": "attribution_monitoring",
        "description": "Automated attribution monitoring and alerting",
        "jobs": [
            {
                "name": "hourly_starvation_check",
                "schedule": "0 * * * *",  # Every hour
                "command": f"cd {project_root} && python {alert_script} --input-dir ./data/attribution --output-dir ./alerts --threshold 0.7 --cron",
                "description": "Check for severe starvation every hour"
            },
            {
                "name": "daily_attribution_report",
                "schedule": "0 9 * * *",  # 9 AM daily
                "command": f"cd {project_root} && python {report_script} --input-dir ./data/attribution --output-dir ./reports/daily --force",
                "description": "Generate daily attribution reports"
            },
            {
                "name": "weekly_summary",
                "schedule": "0 9 * * 1",  # 9 AM every Monday
                "command": f"cd {project_root} && python {report_script} --input-dir ./data/attribution --output-dir ./reports/weekly --force --summary-only",
                "description": "Generate weekly summary report"
            }
        ],
        "created": datetime.now().isoformat()
    }
    
    # Save configuration
    config_path = cron_dir / "attribution_cron.json"
    with open(config_path, 'w') as f:
        json.dump(cron_config, f, indent=2)
    
    print(f"Cron configuration saved to {config_path}")
    
    # Generate install script
    install_script = cron_dir / "install_cron.sh"
    with open(install_script, 'w') as f:
        f.write("#!/bin/bash\n")
        f.write("# Auto-generated cron installation script\n")
        f.write("# Generated: " + datetime.now().isoformat() + "\n")
        f.write("\n")
        f.write("echo 'Installing attribution monitoring cron jobs...'\n")
        f.write("\n")
        
        for job in cron_config["jobs"]:
            f.write(f"# {job['description']}\n")
            f.write(f"(crontab -l 2>/dev/null; echo \"{job['schedule']} {job['command']}\") | crontab -\n")
            f.write("\n")
        
        f.write("echo 'Cron jobs installed successfully!'\n")
        f.write("echo 'Current crontab:'\n")
        f.write("crontab -l\n")
    
    # Make executable
    install_script.chmod(0o755)
    
    print(f"\nInstall script created: {install_script}")
    print("\nTo install cron jobs, run:")
    print(f"  {install_script}")
    
    # Create test script
    test_script = cron_dir / "test_cron.sh"
    with open(test_script, 'w') as f:
        f.write("#!/bin/bash\n")
        f.write("# Test cron jobs manually\n")
        f.write("\n")
        f.write("echo 'Testing hourly starvation check...'\n")
        f.write(f"cd {project_root} && python {alert_script} --input-dir ./data/attribution --output-dir ./alerts --threshold 0.7\n")
        f.write("\n")
        f.write("echo '\\nTesting daily report generation...'\n")
        f.write(f"cd {project_root} && python {report_script} --input-dir ./data/attribution --output-dir ./reports/test --force\n")
    
    test_script.chmod(0o755)
    
    print(f"\nTest script created: {test_script}")
    print("To test cron jobs manually, run:")
    print(f"  {test_script}")
    
    return True


def create_dashboard_integration():
    """Create dashboard integration files."""
    
    project_root = Path(__file__).parent.parent
    ui_dir = project_root / "ui"
    
    # Create attribution dashboard page
    attribution_page = ui_dir / "attribution_dashboard.py"
    
    if not attribution_page.exists():
        # Copy from core module
        core_module = project_root / "core" / "attribution_dashboard.py"
        if core_module.exists():
            import shutil
            shutil.copy(core_module, attribution_page)
            print(f"Attribution dashboard page created: {attribution_page}")
    
    # Create README
    readme_path = project_root / "docs" / "ATTRIBUTION_MONITORING.md"
    
    readme_content = """# Attribution Monitoring System

## Overview

The attribution monitoring system tracks strategy exposure and detects starvation in the futures strategy router.

## Components

### 1. AttributionRecorder (`core/attribution_recorder.py`)
- Logs router evaluations, strategy signals, and trade attribution
- Auto-flush based on buffer size (1000 rows) and time interval (300 seconds)
- CSV export with append mode

### 2. Attribution Report (`scripts/attribution_report.py`)
- Generates 7 types of reports:
  - `router_summary.csv` - Strategy exposure stats
  - `starvation_report.csv` - Starvation analysis
  - `priority_impact_report.csv` - Priority suppression analysis
  - `trade_performance.csv` - PnL by strategy
  - `merged_summary.csv` - Combined metrics
  - `regime_summary.csv` - Regime distribution
  - Visualizations (PNG charts)

### 3. Starvation Alert System (`scripts/starvation_alerts.py`)
- Monitors attribution data for severe starvation
- Configurable threshold (default: 0.7)
- Email alerts and JSON file output
- Cron job ready

### 4. Dashboard Integration (`core/attribution_dashboard.py`)
- Streamlit dashboard for attribution analysis
- Real-time metrics and alerts
- Integrated into main dashboard

## Key Metrics

### Starvation Index
```
starvation_index = 1 - (evaluated_count / candidate_count)
```

| Range | Level | Action |
|-------|-------|--------|
| 0.0-0.3 | Acceptable | Monitor |
| 0.3-0.7 | Moderate | Consider priority adjustment |
| 0.7-1.0 | Severe | Priority adjustment needed |

### Priority Impact
```
priority_impact = shadowed_count / winner_count
```

| Impact | Meaning |
|--------|---------|
| < 1.0 | Low suppression |
| 1.0-2.0 | Moderate suppression |
| > 2.0 | High suppression |

## Usage

### Enable Attribution in Production
```python
from core.attribution_recorder import AttributionRecorder

recorder = AttributionRecorder(
    output_dir="./data/attribution",
    buffer_size=1000,
    flush_interval_seconds=300
)

# Pass to router
signal = route_futures_signal(context, recorder=recorder)
```

### Generate Reports
```bash
# Basic report
python scripts/attribution_report.py --input-dir ./data/attribution --output-dir ./reports

# Strategy detail
python scripts/attribution_report.py --input-dir ./data/attribution --strategy kbar_feature

# Regime-filtered
python scripts/attribution_report.py --input-dir ./data/attribution --regime WEAK
```

### Monitor Starvation
```bash
# Manual check
python scripts/starvation_alerts.py --input-dir ./data/attribution --threshold 0.7

# Cron job (every hour)
0 * * * * cd /path/to/project && python scripts/starvation_alerts.py --input-dir ./data/attribution --output-dir ./alerts --threshold 0.7 --cron
```

### Dashboard
```bash
# Start dashboard
streamlit run ui/dashboard.py

# Navigate to "Attribution" tab
```

## Cron Job Setup

1. Install cron jobs:
```bash
./cron/install_cron.sh
```

2. Test manually:
```bash
./cron/test_cron.sh
```

3. Check installed jobs:
```bash
crontab -l
```

## Alert Configuration

### Email Alerts
Set environment variables:
```bash
export SMTP_SERVER="smtp.gmail.com"
export SMTP_PORT=587
export SMTP_USER="your-email@gmail.com"
export SMTP_PASSWORD="your-app-password"
```

### File Alerts
Alerts are saved to `./alerts/starvation_alerts_YYYYMMDD_HHMMSS.json`

## Troubleshooting

### No Attribution Data
- Ensure `AttributionRecorder` is passed to router
- Check `./data/attribution` directory exists
- Verify CSV files are being created

### Dashboard Errors
- Install required dependencies: `pip install plotly`
- Check attribution data directory path
- Verify Streamlit version compatibility

### Cron Job Issues
- Check crontab syntax: `crontab -l`
- Verify script paths are absolute
- Check permissions on script files

## Performance Considerations

- Buffer size: 1000 rows (adjust based on memory)
- Flush interval: 300 seconds (adjust based on data volume)
- CSV append mode for efficiency
- Optional visualizations (matplotlib not required)

## Maintenance

### Daily
- Review starvation alerts
- Check attribution dashboard
- Verify cron job execution

### Weekly
- Review weekly summary reports
- Adjust strategy priorities if needed
- Clean up old alert files (> 30 days)

### Monthly
- Analyze long-term trends
- Optimize buffer settings
- Update alert thresholds
"""
    
    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write(readme_content)
    
    print(f"Documentation created: {readme_path}")
    
    return True


def main():
    """Main setup function."""
    print("Setting up attribution monitoring system...")
    
    # Setup cron jobs
    if not setup_cron_jobs():
        print("Failed to setup cron jobs")
        sys.exit(1)
    
    # Create dashboard integration
    if not create_dashboard_integration():
        print("Failed to create dashboard integration")
        sys.exit(1)
    
    print("\n" + "="*60)
    print("Attribution monitoring system setup complete!")
    print("="*60)
    print("\nNext steps:")
    print("1. Enable attribution in your router calls")
    print("2. Install cron jobs: ./cron/install_cron.sh")
    print("3. Test the system: ./cron/test_cron.sh")
    print("4. Check the dashboard: streamlit run ui/dashboard.py")
    print("5. Review documentation: docs/ATTRIBUTION_MONITORING.md")
    print("\nFor email alerts, set SMTP environment variables:")
    print("  export SMTP_USER='your-email@gmail.com'")
    print("  export SMTP_PASSWORD='your-app-password'")


if __name__ == "__main__":
    main()