import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    print("Waiting for PM2 dashboard to stabilize...")
    time.sleep(8)
    
    print("Launching Playwright to verify dashboard on port 8500...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        url = "http://localhost:8500"
        print(f"Navigating to {url}...")
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            print("Initial page loaded.")
        except Exception as e:
            print(f"Navigation error: {e}")
            
        time.sleep(3)
        print(f"Page title: {page.title()}")
        
        # Check for password input
        password_input = page.locator('input[type="password"]')
        if password_input.count() > 0:
            print("Password input found. Logging in...")
            password_input.fill("5888")
            password_input.press("Enter")
            print("Login submitted. Waiting for main content to render...")
            time.sleep(10)
        else:
            print("Password input not found. Dump body length:", len(page.locator("body").inner_html()))
            
        # Capture screenshot
        screenshot_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/verified_dashboard.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"Verification screenshot saved to {screenshot_path}")
        
        # Check for potential errors
        errors = page.locator(".stAlert, .element-container:has-text('Error'), .element-container:has-text('Exception')")
        error_count = errors.count()
        print(f"Found {error_count} potential error elements on the page.")
        for i in range(error_count):
            print(f"Error element {i}: {errors.nth(i).inner_text()}")
            
        browser.close()

if __name__ == "__main__":
    run()
