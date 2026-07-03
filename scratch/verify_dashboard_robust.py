import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    print("Launching Playwright for robust verification of port 8500...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        # Capture console and page errors
        page.on("console", lambda msg: print(f"CONSOLE [{msg.type}]: {msg.text}"))
        page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
        
        url = "http://localhost:8500"
        print(f"Navigating to {url}...")
        page.goto(url)
        
        # Poll for password input for up to 30 seconds
        password_input = None
        start_time = time.time()
        while time.time() - start_time < 30:
            inputs = page.locator('input[type="password"]')
            if inputs.count() > 0:
                password_input = inputs.first
                print(f"Password input found after {time.time() - start_time:.1f} seconds!")
                break
            time.sleep(1)
            
        if password_input:
            password_input.fill("5888")
            password_input.press("Enter")
            print("Password submitted. Waiting 10 seconds for rendering...")
            time.sleep(10)
        else:
            print("Timed out waiting for password input. Body HTML length:", len(page.locator("body").inner_html()))
            
        # Capture screenshot
        screenshot_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/robust_dashboard_screenshot.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"Screenshot saved to {screenshot_path}")
        
        # Check for potential errors
        errors = page.locator(".stAlert, .element-container:has-text('Error'), .element-container:has-text('Exception')")
        error_count = errors.count()
        print(f"Found {error_count} potential error elements on the page.")
        for i in range(error_count):
            print(f"Error element {i}: {errors.nth(i).inner_text()}")
            
        browser.close()

if __name__ == "__main__":
    run()
