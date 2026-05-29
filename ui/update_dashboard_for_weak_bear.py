#!/usr/bin/env python3
"""
Dashboard 更新腳本 - 添加 weak_bear_trend 監控面板
"""
import sys
from pathlib import Path

# 讀取 dashboard.py
dashboard_path = Path(__file__).parent / "dashboard.py"

with open(dashboard_path, 'r') as f:
    content = f.read()

# 1. 添加 import 語句 (在 st.set_page_config 之後)
import_code = """
st.set_page_config(page_title="Trading Unified", page_icon="📊", layout="wide")


# ── [weak_bear_trend Monitor] Import auto_select monitoring panel ──
def render_weak_bear_panel():
    \"\"\"Render weak_bear_trend and auto_select monitoring panel.\"\"\"
    try:
        from ui.weak_bear_monitor import render_weak_bear_monitor
        render_weak_bear_monitor()
        return True
    except Exception as e:
        st.error(f"⚠️ weak_bear 監控面板載入失敗：{e}")
        return False


# ── [Audit Debug] Timestamp integrity logger ──
"""

# 替換
old_pattern = "st.set_page_config(page_title=\"Trading Unified\", page_icon=\"📊\", layout=\"wide\")\n\n\n# ── [Audit Debug] Timestamp integrity logger ──"
if old_pattern in content:
    content = content.replace(old_pattern, import_code)
    print("✅ 添加 import 語句")
else:
    print("⚠️ 找不到 import 位置")

# 2. 在 Sidebar 添加快速入口
sidebar_addition = """
    st.divider()
    
    # ── [weak_bear_trend] Quick Access ──
    st.markdown("##### 🤖 weak_bear 監控")
    if st.button("📊 auto_select 監控中心", use_container_width=True):
        st.session_state['show_weak_bear_panel'] = True
    
    if st.button("🔍 系統健康診斷"):
"""

old_sidebar = '    if st.button("🔍 系統健康診斷"):'
if old_sidebar in content:
    content = content.replace(old_sidebar, sidebar_addition, 1)
    print("✅ 添加 Sidebar 快速入口")
else:
    print("⚠️ 找不到 Sidebar 位置")

# 3. 在總覽頁面添加監控面板入口
# 找到 tabs 定義的位置
tabs_pattern = "tab_overview, tab_futures, tab_options, tab_stocks, tab_pipeline, tab_settings = st.tabs"
if tabs_pattern in content:
    # 在 tabs 定義後添加條件渲染
    render_code = """
# ── [weak_bear_trend Panel] Modal-like overlay ──
if st.session_state.get('show_weak_bear_panel', False):
    st.divider()
    with st.expander("🤖 auto_select 監控中心", expanded=True):
        if render_weak_bear_panel():
            if st.button("❌ 關閉監控面板"):
                st.session_state['show_weak_bear_panel'] = False
                st.rerun()
    st.divider()
"""
    
    # 找到 tabs 定義的位置並插入
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if tabs_pattern in line:
            # 在 tabs 定義後插入 (找到下一個空行)
            for j in range(i+1, len(lines)):
                if lines[j].strip() == '':
                    lines.insert(j+1, render_code)
                    print("✅ 添加監控面板渲染代碼")
                    break
            break
    
    content = '\n'.join(lines)
else:
    print("⚠️ 找不到 tabs 定義")

# 寫回文件
with open(dashboard_path, 'w') as f:
    f.write(content)

print("✅ Dashboard 更新完成！")
print("\n請重新啟動 Dashboard:")
print("  pm2 restart dashboard --update-env")
print("或訪問 http://localhost:8500 刷新頁面")
