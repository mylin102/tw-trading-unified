from playwright.sync_api import sync_playwright
import sys

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        print("Playwright is working")
        browser.close()
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
