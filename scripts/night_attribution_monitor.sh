#!/bin/bash
# Night Session Attribution Monitoring System
# Complete automation for night trading with attribution tracking

set -e

PROJECT_ROOT="/Users/mylin/Documents/mylin102/tw-trading-unified"
LOG_DIR="$PROJECT_ROOT/logs"
ALERT_LOG="$LOG_DIR/night_attribution_monitor.log"
PID_FILE="$LOG_DIR/night_attribution_monitor.pid"
CONFIG_FILE="$PROJECT_ROOT/config/night_attribution_config.json"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$ALERT_LOG"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$ALERT_LOG"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$ALERT_LOG"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$ALERT_LOG"
}

check_night_session() {
    # Check if current time is within night session (15:00-05:00)
    HOUR=$(date +%H)
    if [[ $HOUR -ge 15 ]] || [[ $HOUR -lt 5 ]]; then
        return 0  # Night session active
    else
        return 1  # Day session
    fi
}

create_config() {
    cat > "$CONFIG_FILE" << EOF
{
    "project_root": "$PROJECT_ROOT",
    "attribution_dir": "$PROJECT_ROOT/data/attribution",
    "reports_dir": "$PROJECT_ROOT/reports/night_session",
    "alerts_dir": "$PROJECT_ROOT/alerts/night_session",
    "logs_dir": "$PROJECT_ROOT/logs",
    "check_interval_seconds": 300,
    "attribution_flush_interval": 300,
    "report_interval_minutes": 60,
    "starvation_threshold": 0.7,
    "priority_impact_threshold": 2.0,
    "low_evaluation_threshold": 10,
    "email_enabled": false,
    "email_recipient": "",
    "dashboard_enabled": true,
    "dashboard_port": 8500
}
EOF
    log_success "Configuration file created: $CONFIG_FILE"
}

start_monitoring() {
    log_info "Starting night session attribution monitoring..."
    
    # Check if already running
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            log_warning "Monitoring already running with PID $PID"
            return 1
        else
            log_warning "Stale PID file found, removing..."
            rm -f "$PID_FILE"
        fi
    fi
    
    # Check if night session is active
    if ! check_night_session; then
        log_warning "Not night session hours (15:00-05:00). Current time: $(date '+%H:%M')"
        log_info "Will start when night session begins"
        return 0
    fi
    
    # Create config if doesn't exist
    if [ ! -f "$CONFIG_FILE" ]; then
        create_config
    fi
    
    # Start monitoring in background
    cd "$PROJECT_ROOT"
    python3 scripts/night_attribution_monitor.py --live --config "$CONFIG_FILE" >> "$ALERT_LOG" 2>&1 &
    
    MONITOR_PID=$!
    echo "$MONITOR_PID" > "$PID_FILE"
    
    log_success "Night session attribution monitoring started with PID $MONITOR_PID"
    log_info "Log file: $ALERT_LOG"
    log_info "Attribution data: $PROJECT_ROOT/data/attribution"
    log_info "Reports: $PROJECT_ROOT/reports/night_session"
    
    return 0
}

stop_monitoring() {
    log_info "Stopping night session attribution monitoring..."
    
    if [ ! -f "$PID_FILE" ]; then
        log_warning "No PID file found, monitoring may not be running"
        return 1
    fi
    
    PID=$(cat "$PID_FILE")
    
    if ps -p "$PID" > /dev/null 2>&1; then
        log_info "Stopping process $PID..."
        kill -TERM "$PID"
        
        # Wait for process to stop
        for i in {1..10}; do
            if ! ps -p "$PID" > /dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        
        if ps -p "$PID" > /dev/null 2>&1; then
            log_warning "Process did not stop gracefully, forcing..."
            kill -KILL "$PID"
        fi
        
        rm -f "$PID_FILE"
        log_success "Monitoring stopped"
    else
        log_warning "Process $PID not found, removing stale PID file"
        rm -f "$PID_FILE"
    fi
    
    return 0
}

restart_monitoring() {
    log_info "Restarting night session attribution monitoring..."
    stop_monitoring
    sleep 2
    start_monitoring
}

status_monitoring() {
    log_info "Checking night session attribution monitoring status..."
    
    if [ ! -f "$PID_FILE" ]; then
        log_warning "Monitoring not running (no PID file)"
        return 1
    fi
    
    PID=$(cat "$PID_FILE")
    
    if ps -p "$PID" > /dev/null 2>&1; then
        log_success "Monitoring is running with PID $PID"
        
        # Check night session status
        if check_night_session; then
            log_info "Night session is ACTIVE (15:00-05:00)"
        else
            log_info "Night session is INACTIVE (current time: $(date '+%H:%M'))"
        fi
        
        # Check attribution data
        ATTRIBUTION_DIR="$PROJECT_ROOT/data/attribution"
        if [ -d "$ATTRIBUTION_DIR" ]; then
            ROUTER_LOG="$ATTRIBUTION_DIR/router_evaluation_log.csv"
            if [ -f "$ROUTER_LOG" ]; then
                SIZE=$(du -h "$ROUTER_LOG" | cut -f1)
                MOD_TIME=$(stat -f "%Sm" "$ROUTER_LOG")
                log_info "Attribution data: $ROUTER_LOG ($SIZE, modified: $MOD_TIME)"
            else
                log_warning "No attribution data found"
            fi
        fi
        
        # Check recent alerts
        ALERTS_DIR="$PROJECT_ROOT/alerts/night_session"
        if [ -d "$ALERTS_DIR" ]; then
            RECENT_ALERTS=$(find "$ALERTS_DIR" -name "*.json" -mtime -1 | wc -l | tr -d ' ')
            log_info "Recent alerts (last 24h): $RECENT_ALERTS"
        fi
        
        return 0
    else
        log_error "Monitoring PID $PID not found (stale PID file)"
        rm -f "$PID_FILE"
        return 1
    fi
}

generate_report() {
    log_info "Generating attribution report..."
    
    cd "$PROJECT_ROOT"
    python3 scripts/night_attribution_monitor.py --report --config "$CONFIG_FILE"
    
    if [ $? -eq 0 ]; then
        log_success "Report generated successfully"
        
        # Find latest report
        REPORTS_DIR="$PROJECT_ROOT/reports/night_session"
        if [ -d "$REPORTS_DIR" ]; then
            LATEST_REPORT=$(ls -td "$REPORTS_DIR"/*/ | head -1)
            if [ -n "$LATEST_REPORT" ]; then
                log_info "Latest report: $LATEST_REPORT"
                
                # Show summary
                SUMMARY_FILE="$LATEST_REPORT/merged_summary.csv"
                if [ -f "$SUMMARY_FILE" ]; then
                    log_info "Report summary:"
                    head -5 "$SUMMARY_FILE" | while IFS= read -r line; do
                        log_info "  $line"
                    done
                fi
            fi
        fi
    else
        log_error "Failed to generate report"
        return 1
    fi
}

check_alerts() {
    log_info "Checking for starvation alerts..."
    
    cd "$PROJECT_ROOT"
    python3 scripts/night_attribution_monitor.py --alert --config "$CONFIG_FILE"
    
    if [ $? -eq 1 ]; then
        log_warning "Starvation alerts found!"
        return 1
    else
        log_success "No starvation alerts"
        return 0
    fi
}

show_summary() {
    log_info "Generating night session summary..."
    
    cd "$PROJECT_ROOT"
    python3 scripts/night_attribution_monitor.py --summary --config "$CONFIG_FILE"
}

setup_cron_jobs() {
    log_info "Setting up cron jobs for night session monitoring..."
    
    # Create cron directory
    CRON_DIR="$PROJECT_ROOT/cron/night_session"
    mkdir -p "$CRON_DIR"
    
    # Create cron installation script
    cat > "$CRON_DIR/install_night_cron.sh" << 'EOF'
#!/bin/bash
# Install cron jobs for night session attribution monitoring

PROJECT_ROOT="/Users/mylin/Documents/mylin102/tw-trading-unified"
CRON_LOG="$PROJECT_ROOT/logs/cron_night.log"

echo "Installing night session attribution cron jobs..."

# 1. Start monitoring at 14:55 (5 minutes before night session)
(crontab -l 2>/dev/null; echo "55 14 * * * cd $PROJECT_ROOT && bash scripts/night_attribution_monitor.sh start >> $CRON_LOG 2>&1") | crontab -

# 2. Hourly reports during night session
(crontab -l 2>/dev/null; echo "0 16-23,0-4 * * * cd $PROJECT_ROOT && bash scripts/night_attribution_monitor.sh report >> $CRON_LOG 2>&1") | crontab -

# 3. Alert checks every 15 minutes during night session
(crontab -l 2>/dev/null; echo "*/15 15-23,0-4 * * * cd $PROJECT_ROOT && bash scripts/night_attribution_monitor.sh alert >> $CRON_LOG 2>&1") | crontab -

# 4. Stop monitoring at 05:05 (5 minutes after night session)
(crontab -l 2>/dev/null; echo "5 5 * * * cd $PROJECT_ROOT && bash scripts/night_attribution_monitor.sh stop >> $CRON_LOG 2>&1") | crontab -

# 5. Daily summary at 05:10
(crontab -l 2>/dev/null; echo "10 5 * * * cd $PROJECT_ROOT && bash scripts/night_attribution_monitor.sh summary >> $CRON_LOG 2>&1") | crontab -

echo "Cron jobs installed successfully!"
echo "Current crontab:"
crontab -l
EOF
    
    chmod +x "$CRON_DIR/install_night_cron.sh"
    
    # Create cron removal script
    cat > "$CRON_DIR/remove_night_cron.sh" << 'EOF'
#!/bin/bash
# Remove night session attribution cron jobs

echo "Removing night session attribution cron jobs..."

# Create temp crontab without our jobs
TEMP_CRON=$(mktemp)
crontab -l 2>/dev/null | grep -v "night_attribution_monitor" > "$TEMP_CRON"

# Install filtered crontab
crontab "$TEMP_CRON"
rm -f "$TEMP_CRON"

echo "Cron jobs removed successfully!"
echo "Current crontab:"
crontab -l
EOF
    
    chmod +x "$CRON_DIR/remove_night_cron.sh"
    
    # Create test script
    cat > "$CRON_DIR/test_cron.sh" << 'EOF'
#!/bin/bash
# Test night session cron jobs

PROJECT_ROOT="/Users/mylin/Documents/mylin102/tw-trading-unified"

echo "Testing night session attribution monitoring..."
echo ""

echo "1. Testing start monitoring..."
cd "$PROJECT_ROOT" && bash scripts/night_attribution_monitor.sh start
sleep 5

echo ""
echo "2. Testing status check..."
cd "$PROJECT_ROOT" && bash scripts/night_attribution_monitor.sh status
sleep 2

echo ""
echo "3. Testing report generation..."
cd "$PROJECT_ROOT" && bash scripts/night_attribution_monitor.sh report
sleep 2

echo ""
echo "4. Testing alert check..."
cd "$PROJECT_ROOT" && bash scripts/night_attribution_monitor.sh alert
sleep 2

echo ""
echo "5. Testing summary..."
cd "$PROJECT_ROOT" && bash scripts/night_attribution_monitor.sh summary
sleep 2

echo ""
echo "6. Testing stop monitoring..."
cd "$PROJECT_ROOT" && bash scripts/night_attribution_monitor.sh stop

echo ""
echo "Test completed!"
EOF
    
    chmod +x "$CRON_DIR/test_cron.sh"
    
    log_success "Cron job scripts created in $CRON_DIR"
    log_info "To install cron jobs: $CRON_DIR/install_night_cron.sh"
    log_info "To test: $CRON_DIR/test_cron.sh"
}

show_help() {
    cat << EOF
Night Session Attribution Monitoring System

Usage: $0 {start|stop|restart|status|report|alert|summary|setup-cron|help}

Commands:
    start       Start night session attribution monitoring
    stop        Stop monitoring
    restart     Restart monitoring
    status      Check monitoring status
    report      Generate attribution reports
    alert       Check for starvation alerts
    summary     Show night session summary
    setup-cron  Setup cron jobs for automation
    help        Show this help message

Night Session Hours: 15:00 - 05:00 (next day)

Features:
    - Automatic attribution tracking during night sessions
    - Real-time starvation alerts
    - Hourly attribution reports
    - Dashboard integration
    - Email notifications (configurable)
    - Cron job automation

Configuration: $CONFIG_FILE
Log file: $ALERT_LOG
EOF
}

# Main script logic
case "$1" in
    start)
        start_monitoring
        ;;
    stop)
        stop_monitoring
        ;;
    restart)
        restart_monitoring
        ;;
    status)
        status_monitoring
        ;;
    report)
        generate_report
        ;;
    alert)
        check_alerts
        ;;
    summary)
        show_summary
        ;;
    setup-cron)
        setup_cron_jobs
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac

exit 0