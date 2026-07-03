import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    # 2026-06-30 Gemini CLI: Diagnostic script to capture dashboard UI
    print("Starting Playwright...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Set viewport size to capture a wide dashboard
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        # Subscribe to console events to capture errors
        page.on("console", lambda msg: print(f"BROWSER CONSOLE [{msg.type}]: {msg.text}"))
        
        url = "http://localhost:8500"
        print(f"Navigating to {url}...")
        page.goto(url)
        time.sleep(3) # Wait for page load
        
        # Log the current title and body content for debug
        print(f"Title: {page.title()}")
        
        # Check if login password input is present
        # Streamlit password inputs are usually text/password inputs
        password_input = page.locator('input[type="password"]')
        if password_input.count() > 0:
            print("Password input found, logging in...")
            password_input.fill("5888")
            password_input.press("Enter")
            time.sleep(5) # Wait for reload after login
        else:
            print("No password input found. Maybe already logged in or page structure is different.")
        
        # Save screenshot
        screenshot_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/dashboard_screenshot.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"Screenshot saved to {screenshot_path}")
        
        # Check if there are any obvious error divs or overlays
        errors = page.locator(".stAlert, .element-container:has-text('Error'), .element-container:has-text('Exception')")
        error_count = errors.count()
        print(f"Found {error_count} potential error elements on the page.")
        for i in range(error_count):
            print(f"Error element {i}: {errors.nth(i).inner_text()}")
            
        browser.close()

if __name__ == "__main__":
    run()
