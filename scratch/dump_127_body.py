import urllib.request

try:
    # 2026-06-30 Gemini CLI: Get raw HTML from port 8500
    with urllib.request.urlopen("http://127.0.0.1:8500", timeout=5) as response:
        html = response.read().decode('utf-8')
        print(f"Status: {response.status}")
        print(f"Headers: {dict(response.headers)}")
        print("Body length:", len(html))
        print("Body:")
        print(html)
except Exception as e:
    print(f"Error: {e}")
