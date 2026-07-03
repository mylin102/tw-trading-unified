import sys
import os
import time
import subprocess
import signal
from playwright.sync_api import sync_playwright

def run():
    print("Starting temporary Streamlit dashboard on port 8501 without cpulimit wrapper...")
    cmd = [
        "taskpolicy", "-c", "background",
        "./venv/bin/python3", "-m", "streamlit", "run", "ui/dashboard.py",
        "--server.port", "8501",
        "--server.headless", "true"
    ]
    
    # Run the server in a separate process group so we can clean it up easily
    proc = subprocess.Popen(cmd, preexec_fn=os.setsid)
    time.sleep(8)  # Give Streamlit 8 seconds to start up
    
    print("Starting Playwright to visit port 8501...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            
            # Navigate to the temporary dashboard
            url = "http://localhost:8501"
            print(f"Navigating to {url}...")
            page.goto(url, wait_until="networkidle", timeout=30000)
            print("Loaded. Waiting 5 seconds...")
            time.sleep(5)
            
            print(f"Title: {page.title()}")
            
            # Check for password input
            password_input = page.locator('input[type="password"]')
            if password_input.count() > 0:
                print("Password input found! Filling password '5888'...")
                password_input.fill("5888")
                password_input.press("Enter")
                print("Password submitted. Waiting 10 seconds for main dashboard content...")
                time.sleep(10)
            else:
                print("No password input found. HTML body length:", len(page.locator("body").inner_html()))
            
            # Take screenshot of the main dashboard
            screenshot_path = "/Users/mylin/.gemini/antigravity-cli/brain/a1ec95f8-37fe-4e57-8f43-22d2705beb01/direct_dashboard_screenshot.png"
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"Direct dashboard screenshot saved to {screenshot_path}")
            
            # Check for error banners
            errors = page.locator(".stAlert, .element-container:has-text('Error'), .element-container:has-text('Exception')")
            error_count = errors.count()
            print(f"Found {error_count} potential error elements on the page.")
            for i in range(error_count):
                print(f"Error element {i}: {errors.nth(i).inner_text()}")
                
            browser.close()
    except Exception as e:
        print(f"Error occurred during diagnostics: {e}")
    finally:
        print("Terminating temporary Streamlit process...")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait()
            print("Temporary Streamlit process terminated.")
        except Exception as e:
            print(f"Failed to terminate Streamlit process: {e}")

if __name__ == "__main__":
    run()
