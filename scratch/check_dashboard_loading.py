import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    print("Starting Playwright diagnostic...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        # Capture all console and uncaught errors
        page.on("console", lambda msg: print(f"CONSOLE [{msg.type}]: {msg.text}"))
        page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
        
        url = "http://localhost:8500"
        print(f"Navigating to {url}...")
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            print("Navigation done (networkidle).")
        except Exception as e:
            print(f"Navigation timed out or failed: {e}")
            
        print("Waiting 5 more seconds...")
        time.sleep(5)
        
        print(f"Current page title: {page.title()}")
        
        # Check if there is an input element
        inputs = page.locator("input")
        print(f"Number of input elements found: {inputs.count()}")
        for i in range(inputs.count()):
            try:
                print(f"Input {i}: type={inputs.nth(i).get_attribute('type')}, id={inputs.nth(i).get_attribute('id')}")
            except Exception:
                pass
                
        # Take a screenshot
        screenshot_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/diag_screenshot.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"Screenshot saved to {screenshot_path}")
        
        # Dump HTML body
        body_html = page.locator("body").inner_html()
        print(f"Body length: {len(body_html)}")
        # Print first 500 chars and last 500 chars of body
        print("--- Body HTML Start ---")
        print(body_html[:1000])
        print("--- Body HTML End ---")
        print(body_html[-1000:] if len(body_html) > 1000 else "")
        
        browser.close()

if __name__ == "__main__":
    run()
