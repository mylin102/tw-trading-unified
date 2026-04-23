# Attribution Monitoring System

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
