from playwright.sync_api import sync_playwright
import sys

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 800})
        print("Navigating to http://127.0.0.1:8500...")
        page.goto('http://127.0.0.1:8500')
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(5000) # Give Streamlit extra time to render charts
        path = 'exports/screenshots/dashboard_8500.png'
        import os
        os.makedirs('exports/screenshots', exist_ok=True)
        page.screenshot(path=path, full_page=True)
        print(f"Screenshot saved to {path}")
        browser.close()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
