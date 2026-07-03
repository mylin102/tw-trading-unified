import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    print("Launching Playwright to verify DOM on port 8500...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        # Log page console and errors
        page.on("console", lambda msg: print(f"CONSOLE [{msg.type}]: {msg.text}"))
        page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
        
        url = "http://127.0.0.1:8500"
        print(f"Navigating to {url} with wait_until='commit'...")
        try:
            page.goto(url, wait_until="commit")
            print("Navigation committed. Waiting for password input...")
            
            # Wait for password input element
            page.wait_for_selector('input[type="password"]', timeout=15000)
            print("Password input found! Filling and submitting...")
            
            password_input = page.locator('input[type="password"]')
            password_input.fill("5888")
            password_input.press("Enter")
            
            print("Waiting for main content...")
            time.sleep(10)
            
            screenshot_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/dom_verified.png"
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"Screenshot saved to {screenshot_path}")
            
        except Exception as e:
            print(f"Failed: {e}")
            # Take screenshot of whatever is currently on screen
            try:
                fail_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/dom_fail.png"
                page.screenshot(path=fail_path, full_page=True)
                print(f"Fail screenshot saved to {fail_path}")
            except Exception as se:
                print("Failed to take screenshot:", se)
            
        browser.close()

if __name__ == "__main__":
    run()
