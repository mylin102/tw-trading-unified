import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    print("Launching Playwright to screenshot tabs on port 8500...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        url = "http://127.0.0.1:8500"
        print(f"Navigating to {url}...")
        try:
            page.goto(url, wait_until="commit")
            
            # Wait for password input element (up to 15 seconds)
            page.wait_for_selector('input[type="password"]', timeout=15000)
            print("Password input found! Filling and submitting...")
            
            password_input = page.locator('input[type="password"]')
            password_input.fill("5888")
            password_input.press("Enter")
            
            print("Password submitted. Waiting 10 seconds for main content...")
            time.sleep(10)
            
            # Save overview screenshot
            overview_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/tab_overview.png"
            page.screenshot(path=overview_path)
            print(f"Overview screenshot saved to {overview_path}")
            
            # Find and click the "期貨 TMF" tab
            # Streamlit tabs are usually buttons containing the tab label
            futures_tab = page.locator('button:has-text("期貨 TMF")')
            if futures_tab.count() > 0:
                print("Clicking '期貨 TMF' tab...")
                futures_tab.first.click()
                time.sleep(5)
                futures_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/tab_futures.png"
                page.screenshot(path=futures_path)
                print(f"Futures tab screenshot saved to {futures_path}")
            else:
                print("Could not find '期貨 TMF' tab button.")
                
            # Find and click the "選擇權 TXO" tab
            options_tab = page.locator('button:has-text("選擇權 TXO")')
            if options_tab.count() > 0:
                print("Clicking '選擇權 TXO' tab...")
                options_tab.first.click()
                time.sleep(5)
                options_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/tab_options.png"
                page.screenshot(path=options_path)
                print(f"Options tab screenshot saved to {options_path}")
            else:
                print("Could not find '選擇權 TXO' tab button.")
                
        except Exception as e:
            print(f"Tab screenshot failed: {e}")
            
        browser.close()

if __name__ == "__main__":
    run()
