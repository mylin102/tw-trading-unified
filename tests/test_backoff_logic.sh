#!/bin/bash
# GSD Test: Verify autostart.sh circuit breaker and backoff

# 1. Backup original autostart.sh
cp autostart.sh autostart.sh.bak

# 2. Create a fake main.py that exits with error immediately
echo "import sys; print('Simulating crash...'); sys.exit(1)" > fake_main.py

# 3. Patch autostart.sh to use fake_main.py and lower thresholds for fast testing
sed -i '' 's/\$UNIFIED_DIR\/main.py/fake_main.py/g' autostart.sh
sed -i '' 's/MAX_RETRIES=10/MAX_RETRIES=3/g' autostart.sh
sed -i '' 's/BASE_SLEEP=15/BASE_SLEEP=1/g' autostart.sh

echo "🚀 Starting backoff test (3 retries)..."
# Use a temporary log to avoid polluting unified.log
> test_backoff.log
bash autostart.sh >> test_backoff.log 2>&1

echo "📊 Test Log Output:"
cat test_backoff.log

# 4. Cleanup
mv autostart.sh.bak autostart.sh
rm fake_main.py test_backoff.log
