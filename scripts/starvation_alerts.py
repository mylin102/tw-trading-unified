#!/usr/bin/env python3
"""
Starvation Alert System

Monitors attribution data and sends alerts for severe starvation.
Can be run as a cron job or manually.

Usage:
    python scripts/starvation_alerts.py --input-dir ./data/attribution --threshold 0.7
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import argparse
import json
from typing import Dict, List, Optional
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os


class StarvationAlertSystem:
    """System for detecting and alerting on strategy starvation."""
    
    def __init__(self, attribution_dir: Path, threshold: float = 0.7):
        self.attribution_dir = Path(attribution_dir)
        self.threshold = threshold
        self.router_log_path = self.attribution_dir / "router_evaluation_log.csv"
        
    def load_data(self) -> pd.DataFrame:
        """Load router evaluation data."""
        if not self.router_log_path.exists():
            print(f"Error: Router log not found at {self.router_log_path}")
            return pd.DataFrame()
        
        df = pd.read_csv(self.router_log_path)
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        return df
    
    def analyze_starvation(self, df: pd.DataFrame) -> Dict:
        """Analyze starvation from router data."""
        if df.empty:
            return {}
        
        # Filter out router entries
        strategy_df = df[df['strategy_name'] != 'router'].copy()
        
        if strategy_df.empty:
            return {}
        
        # Group by strategy
        summary = strategy_df.groupby('strategy_name').agg({
            'candidate_order': 'count',  # Total candidate occurrences
            'evaluated': 'sum',
            'winner': 'sum',
            'status': lambda x: (x == 'shadowed').sum()  # Count shadowed status
        }).reset_index()
        
        summary.columns = ['strategy_name', 'candidate_count', 'evaluated_count', 'winner_count', 'shadowed_count']
        
        # Calculate starvation index
        summary['evaluation_rate'] = summary['evaluated_count'] / summary['candidate_count']
        summary['starvation_index'] = 1 - summary['evaluation_rate']
        
        # Identify severe starvation
        severe = summary[summary['starvation_index'] > self.threshold].copy()
        
        # Calculate priority impact
        severe['priority_impact'] = severe['shadowed_count'] / severe['winner_count'].replace(0, np.nan)
        severe = severe.fillna(0)
        
        return {
            "summary": summary.to_dict('records'),
            "severe_starvation": severe.to_dict('records'),
            "total_strategies": len(summary),
            "severe_count": len(severe),
            "analysis_time": datetime.now().isoformat()
        }
    
    def generate_alerts(self, analysis: Dict) -> List[Dict]:
        """Generate alerts from analysis."""
        alerts = []
        
        severe_strategies = analysis.get('severe_starvation', [])
        
        for strategy in severe_strategies:
            alert = {
                "type": "starvation",
                "level": "critical",
                "strategy": strategy['strategy_name'],
                "starvation_index": strategy['starvation_index'],
                "evaluation_rate": strategy['evaluation_rate'],
                "shadowed_count": strategy['shadowed_count'],
                "winner_count": strategy['winner_count'],
                "priority_impact": strategy.get('priority_impact', 0),
                "message": f"策略 {strategy['strategy_name']} 嚴重飢餓 (index={strategy['starvation_index']:.2f})",
                "details": f"評估率僅 {strategy['evaluation_rate']:.1%}，被 shadowed {strategy['shadowed_count']} 次，贏 {strategy['winner_count']} 次"
            }
            alerts.append(alert)
        
        # Add summary alert if any severe starvation
        if alerts:
            summary_alert = {
                "type": "summary",
                "level": "info",
                "message": f"發現 {len(alerts)} 個策略嚴重飢餓 (threshold={self.threshold})",
                "details": f"總策略數: {analysis.get('total_strategies', 0)}，嚴重飢餓: {analysis.get('severe_count', 0)}",
                "strategies": [a['strategy'] for a in alerts]
            }
            alerts.append(summary_alert)
        
        return alerts
    
    def send_email_alert(self, alerts: List[Dict], recipient: str):
        """Send email alert."""
        if not alerts:
            return
        
        # Email configuration
        smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.getenv("SMTP_PORT", 587))
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_password = os.getenv("SMTP_PASSWORD", "")
        
        if not smtp_user or not smtp_password:
            print("Warning: SMTP credentials not configured. Skipping email alert.")
            return
        
        # Create email
        subject = f"[Starvation Alert] {len(alerts)} 個策略嚴重飢餓"
        
        # Build HTML content
        html_content = """
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; }
                .alert { padding: 15px; margin: 10px 0; border-radius: 5px; }
                .critical { background-color: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
                .warning { background-color: #fff3cd; border: 1px solid #ffeaa7; color: #856404; }
                .info { background-color: #d1ecf1; border: 1px solid #bee5eb; color: #0c5460; }
                .strategy { font-weight: bold; }
                .metric { color: #666; font-size: 0.9em; }
            </style>
        </head>
        <body>
            <h2>📊 Starvation Alert Report</h2>
            <p>Generated: {timestamp}</p>
        """
        
        for alert in alerts:
            level_class = alert['level']
            html_content += f"""
            <div class="alert {level_class}">
                <div class="strategy">{alert['message']}</div>
                <div class="metric">{alert['details']}</div>
            </div>
            """
        
        html_content += """
            <hr>
            <p><small>This is an automated alert from the trading system.</small></p>
        </body>
        </html>
        """.format(timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        # Create message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = recipient
        
        # Attach HTML
        msg.attach(MIMEText(html_content, "html"))
        
        # Send email
        try:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            print(f"Email alert sent to {recipient}")
        except Exception as e:
            print(f"Error sending email: {e}")
    
    def save_alerts_to_file(self, alerts: List[Dict], output_dir: Path):
        """Save alerts to JSON file."""
        output_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"starvation_alerts_{timestamp}.json"
        
        alert_data = {
            "timestamp": datetime.now().isoformat(),
            "threshold": self.threshold,
            "total_alerts": len(alerts),
            "alerts": alerts
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(alert_data, f, ensure_ascii=False, indent=2)
        
        print(f"Alerts saved to {output_path}")
        return output_path
    
    def run(self, output_dir: Optional[Path] = None, email_recipient: Optional[str] = None):
        """Run the alert system."""
        print(f"Running starvation alert system (threshold={self.threshold})")
        print(f"Data directory: {self.attribution_dir}")
        
        # Load data
        df = self.load_data()
        if df.empty:
            print("No data available. Exiting.")
            return
        
        # Analyze starvation
        analysis = self.analyze_starvation(df)
        if not analysis:
            print("No strategies found in data.")
            return
        
        # Generate alerts
        alerts = self.generate_alerts(analysis)
        
        if not alerts:
            print("No severe starvation detected.")
            return
        
        # Print alerts
        print(f"\n{'='*60}")
        print(f"STARVATION ALERTS ({len(alerts)})")
        print(f"{'='*60}")
        
        for alert in alerts:
            level_icon = "🔴" if alert['level'] == 'critical' else "🟡" if alert['level'] == 'warning' else "🔵"
            print(f"{level_icon} [{alert['level'].upper()}] {alert['message']}")
            print(f"   {alert['details']}")
            if alert.get('strategies'):
                print(f"   Strategies: {', '.join(alert['strategies'])}")
            print()
        
        # Save to file
        if output_dir:
            self.save_alerts_to_file(alerts, output_dir)
        
        # Send email
        if email_recipient:
            self.send_email_alert(alerts, email_recipient)
        
        print(f"Analysis complete. {len(alerts)} alerts generated.")


def main():
    parser = argparse.ArgumentParser(description="Starvation Alert System")
    parser.add_argument("--input-dir", type=Path, default=Path("./data/attribution"),
                       help="Directory containing attribution CSV files")
    parser.add_argument("--output-dir", type=Path, default=Path("./alerts"),
                       help="Directory to save alert JSON files")
    parser.add_argument("--threshold", type=float, default=0.7,
                       help="Starvation index threshold for critical alerts (default: 0.7)")
    parser.add_argument("--email", type=str, help="Email address to send alerts to")
    parser.add_argument("--cron", action="store_true", help="Cron mode: only output if alerts exist")
    
    args = parser.parse_args()
    
    # Create alert system
    alert_system = StarvationAlertSystem(args.input_dir, args.threshold)
    
    # Run analysis
    alert_system.run(
        output_dir=args.output_dir,
        email_recipient=args.email
    )
    
    # Exit code for cron
    if args.cron:
        # Check if alerts would be generated
        df = alert_system.load_data()
        if not df.empty:
            analysis = alert_system.analyze_starvation(df)
            alerts = alert_system.generate_alerts(analysis)
            if alerts:
                sys.exit(1)  # Alerts exist
            else:
                sys.exit(0)  # No alerts
        else:
            sys.exit(2)  # No data


if __name__ == "__main__":
    main()