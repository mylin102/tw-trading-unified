import sys
import os
import time
from playwright.sync_api import sync_playwright

def run():
    print("Launching Playwright to verify 127.0.0.1:8500...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        
        # Navigate using 127.0.0.1
        url = "http://127.0.0.1:8500"
        print(f"Navigating to {url}...")
        try:
            page.goto(url, wait_until="networkidle", timeout=15000)
            print("Loaded successfully!")
        except Exception as e:
            print(f"Failed: {e}")
            
        print(f"Title: {page.title()}")
        
        # Check for password input
        password_input = page.locator('input[type="password"]')
        if password_input.count() > 0:
            print("Password input found! Filling password '5888'...")
            password_input.fill("5888")
            password_input.press("Enter")
            time.sleep(5)
            print("After submit title:", page.title())
        else:
            print("Password input not found. Body length:", len(page.locator("body").inner_html()))
            
        browser.close()

if __name__ == "__main__":
    run()
