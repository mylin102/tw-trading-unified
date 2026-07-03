import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    print("Launching Playwright to diagnose port 8500 console...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        # Log page console, errors and requests
        page.on("console", lambda msg: print(f"CONSOLE [{msg.type.upper()}]: {msg.text}"))
        page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
        page.on("requestfailed", lambda req: print(f"REQUEST FAILED: {req.url} - {req.failure}"))
        
        url = "http://127.0.0.1:8500"
        print(f"Navigating to {url} with wait_until='commit'...")
        try:
            page.goto(url, wait_until="commit")
            print("Navigation committed. Waiting 5 seconds for password field...")
            
            # Wait for password input element (up to 15 seconds)
            page.wait_for_selector('input[type="password"]', timeout=15000)
            print("Password input found! Filling and submitting...")
            
            password_input = page.locator('input[type="password"]')
            password_input.fill("5888")
            password_input.press("Enter")
            
            print("Password submitted. Waiting 15 seconds for main content to render...")
            time.sleep(15)
            
            screenshot_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/diagnosed_dashboard.png"
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"Screenshot saved to {screenshot_path}")
            
        except Exception as e:
            print(f"Diagnostics failed: {e}")
            try:
                fail_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/diagnose_fail.png"
                page.screenshot(path=fail_path, full_page=True)
                print(f"Fail screenshot saved to {fail_path}")
            except Exception as se:
                print("Failed to take screenshot:", se)
            
        browser.close()

if __name__ == "__main__":
    run()
