#!/bin/bash

# ============================================================================
# 夜盤 Attribution 自動化監控啟動腳本
# ============================================================================
# 此腳本用於在夜盤時段 (15:00-05:00) 自動啟動 attribution 監控系統
# ============================================================================

set -e  # 遇到錯誤立即退出

# 顏色定義
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 項目根目錄
PROJECT_ROOT="/Users/mylin/Documents/mylin102/tw-trading-unified"

# 日誌目錄
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"

# PID 檔案
PID_FILE="$LOG_DIR/night_attribution.pid"
MONITOR_LOG="$LOG_DIR/night_monitor.log"
SIMULATOR_LOG="$LOG_DIR/night_simulator.log"

# 函數：列印帶顏色的訊息
print_info() {
    echo -e "${CYAN}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 函數：檢查是否為夜盤時段
is_night_session() {
    local hour=$(date +%H)
    # 夜盤時段: 15:00-05:00
    if [[ $hour -ge 15 ]] || [[ $hour -lt 5 ]]; then
        return 0  # true
    else
        return 1  # false
    fi
}

# 函數：檢查進程是否運行
check_process() {
    if [[ -f "$PID_FILE" ]]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0  # 進程運行中
        else
            # PID 檔案存在但進程不存在
            rm -f "$PID_FILE"
            return 1
        fi
    else
        return 1  # PID 檔案不存在
    fi
}

# 函數：啟動監控系統
start_monitor() {
    print_info "啟動夜盤 Attribution 監控系統..."
    
    # 檢查是否為夜盤時段
    if ! is_night_session; then
        print_warning "當前不是夜盤時段 (15:00-05:00)"
        print_info "當前時間: $(date '+%H:%M:%S')"
        print_info "是否繼續啟動? (y/N)"
        read -r response
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            print_info "取消啟動"
            exit 0
        fi
    fi
    
    # 檢查是否已運行
    if check_process; then
        print_warning "監控系統已在運行中 (PID: $(cat "$PID_FILE"))"
        print_info "是否重新啟動? (y/N)"
        read -r response
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            print_info "取消啟動"
            exit 0
        fi
        stop_monitor
    fi
    
    # 啟動監控
    cd "$PROJECT_ROOT"
    python3 scripts/monitor_night_with_attribution.py --interval 10 >> "$MONITOR_LOG" 2>&1 &
    
    local monitor_pid=$!
    echo "$monitor_pid" > "$PID_FILE"
    
    print_success "監控系統已啟動 (PID: $monitor_pid)"
    print_info "日誌檔案: $MONITOR_LOG"
    
    # 等待 5 秒檢查是否正常運行
    sleep 5
    if check_process; then
        print_success "監控系統運行正常"
    else
        print_error "監控系統啟動失敗，請檢查日誌"
        exit 1
    fi
}

# 函數：停止監控系統
stop_monitor() {
    print_info "停止夜盤 Attribution 監控系統..."
    
    if check_process; then
        local pid=$(cat "$PID_FILE")
        print_info "停止進程 (PID: $pid)..."
        kill "$pid" 2>/dev/null || true
        
        # 等待進程結束
        for i in {1..10}; do
            if ! kill -0 "$pid" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        
        # 強制終止如果還在運行
        if kill -0 "$pid" 2>/dev/null; then
            print_warning "進程未正常結束，強制終止..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        
        rm -f "$PID_FILE"
        print_success "監控系統已停止"
    else
        print_info "監控系統未在運行中"
        rm -f "$PID_FILE" 2>/dev/null || true
    fi
}

# 函數：檢查狀態
check_status() {
    print_info "檢查夜盤 Attribution 監控系統狀態..."
    
    if check_process; then
        local pid=$(cat "$PID_FILE")
        local uptime=$(ps -o etime= -p "$pid" 2>/dev/null || echo "未知")
        print_success "監控系統運行中 (PID: $pid, 運行時間: $uptime)"
        
        # 檢查日誌
        if [[ -f "$MONITOR_LOG" ]]; then
            local log_size=$(du -h "$MONITOR_LOG" | cut -f1)
            local last_log=$(tail -5 "$MONITOR_LOG" 2>/dev/null || echo "無日誌內容")
            print_info "日誌檔案: $MONITOR_LOG (大小: $log_size)"
            print_info "最近日誌:"
            echo "$last_log"
        fi
        
        # 檢查 attribution 數據
        local data_dir="$PROJECT_ROOT/data/attribution/night_session"
        if [[ -d "$data_dir" ]]; then
            local csv_count=$(find "$data_dir" -name "*.csv" | wc -l)
            local total_rows=0
            if [[ -f "$data_dir/router_evaluation_log.csv" ]]; then
                total_rows=$(wc -l < "$data_dir/router_evaluation_log.csv" 2>/dev/null || echo 0)
                total_rows=$((total_rows - 1))  # 減去標題行
            fi
            print_info "Attribution 數據: $csv_count 個 CSV 檔案，$total_rows 行數據"
        fi
        
    else
        print_info "監控系統未在運行中"
    fi
    
    # 檢查是否為夜盤時段
    if is_night_session; then
        print_info "✅ 當前是夜盤時段 (15:00-05:00)"
    else
        print_info "⏸️  當前不是夜盤時段"
    fi
}

# 函數：生成報告
generate_report() {
    print_info "生成 Attribution 報告..."
    
    local input_dir="$PROJECT_ROOT/data/attribution/night_session"
    local output_dir="$PROJECT_ROOT/data/attribution/night_reports_$(date +%Y%m%d_%H%M%S)"
    
    if [[ ! -d "$input_dir" ]]; then
        print_error "Attribution 數據目錄不存在: $input_dir"
        return 1
    fi
    
    # 檢查是否有數據
    if [[ ! -f "$input_dir/router_evaluation_log.csv" ]]; then
        print_error "未找到 router_evaluation_log.csv"
        return 1
    fi
    
    mkdir -p "$output_dir"
    
    cd "$PROJECT_ROOT"
    python3 scripts/attribution_report.py \
        --input-dir "$input_dir" \
        --output-dir "$output_dir" \
        --force >> "$SIMULATOR_LOG" 2>&1
    
    if [[ $? -eq 0 ]]; then
        print_success "報告生成完成: $output_dir"
        
        # 顯示飢餓報告
        if [[ -f "$output_dir/starvation_report.csv" ]]; then
            print_info "飢餓分析報告:"
            cat "$output_dir/starvation_report.csv"
        fi
    else
        print_error "報告生成失敗，請檢查日誌"
    fi
}

# 函數：運行重排序模擬
run_reorder_simulation() {
    print_info "運行策略重排序模擬..."
    
    local input_dir="$PROJECT_ROOT/data/attribution/night_session"
    local output_dir="$PROJECT_ROOT/data/attribution/reorder_sim_$(date +%Y%m%d_%H%M%S)"
    
    if [[ ! -d "$input_dir" ]]; then
        print_error "Attribution 數據目錄不存在: $input_dir"
        return 1
    fi
    
    mkdir -p "$output_dir"
    
    cd "$PROJECT_ROOT"
    python3 docs/strategy_reorder_simulator.py \
        --input-dir "$input_dir" \
        --output-dir "$output_dir" \
        --order counter_vwap,spring_upthrust,kbar_feature \
        --order kbar_feature,counter_vwap,spring_upthrust \
        --order spring_upthrust,kbar_feature,counter_vwap \
        >> "$SIMULATOR_LOG" 2>&1
    
    if [[ $? -eq 0 ]]; then
        print_success "重排序模擬完成: $output_dir"
        
        # 顯示結果
        if [[ -f "$output_dir/simulation_results.csv" ]]; then
            print_info "模擬結果:"
            cat "$output_dir/simulation_results.csv"
        fi
    else
        print_error "重排序模擬失敗，請檢查日誌"
    fi
}

# 函數：顯示幫助
show_help() {
    echo -e "${CYAN}夜盤 Attribution 自動化監控系統${NC}"
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  start       啟動監控系統"
    echo "  stop        停止監控系統"
    echo "  restart     重新啟動監控系統"
    echo "  status      檢查系統狀態"
    echo "  report      生成 Attribution 報告"
    echo "  simulate    運行策略重排序模擬"
    echo "  auto        自動模式 (啟動監控並定期生成報告)"
    echo "  help        顯示此幫助訊息"
    echo ""
    echo "範例:"
    echo "  $0 start    啟動夜盤監控"
    echo "  $0 status   檢查監控狀態"
    echo "  $0 report   生成分析報告"
}

# 函數：自動模式
auto_mode() {
    print_info "啟動自動模式..."
    
    # 啟動監控
    start_monitor
    
    # 設定定時任務
    local last_report_time=$(date +%s)
    local last_simulation_time=$(date +%s)
    
    print_info "自動模式運行中，按 Ctrl+C 停止..."
    
    trap 'print_info "收到停止訊號..."; stop_monitor; exit 0' INT TERM
    
    while true; do
        # 檢查監控是否還在運行
        if ! check_process; then
            print_error "監控系統異常停止，嘗試重新啟動..."
            start_monitor
        fi
        
        local current_time=$(date +%s)
        
        # 每小時生成報告
        if [[ $((current_time - last_report_time)) -ge 3600 ]]; then
            print_info "定時生成報告..."
            generate_report
            last_report_time=$current_time
        fi
        
        # 每 2 小時運行模擬
        if [[ $((current_time - last_simulation_time)) -ge 7200 ]]; then
            print_info "定時運行重排序模擬..."
            run_reorder_simulation
            last_simulation_time=$current_time
        fi
        
        # 顯示狀態
        if [[ $((current_time % 300)) -eq 0 ]]; then  # 每 5 分鐘
            print_info "系統運行中... (已運行 $(( (current_time - last_report_time) / 60 )) 分鐘)"
        fi
        
        sleep 60  # 每分鐘檢查一次
    done
}

# 主程式
main() {
    local command=${1:-"help"}
    
    case "$command" in
        start)
            start_monitor
            ;;
        stop)
            stop_monitor
            ;;
        restart)
            stop_monitor
            sleep 2
            start_monitor
            ;;
        status)
            check_status
            ;;
        report)
            generate_report
            ;;
        simulate)
            run_reorder_simulation
            ;;
        auto)
            auto_mode
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "未知命令: $command"
            show_help
            exit 1
            ;;
    esac
}

# 執行主程式
main "$@"