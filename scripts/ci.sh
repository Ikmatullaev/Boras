#!/usr/bin/env bash
# CI-скрипт: запускает все проверки перед push.
#
# Использование:
#   bash scripts/ci.sh
#
# Что делает:
#   1. python3 -m compileall -q .   — синтаксическая проверка всех .py файлов
#   2. python3 -m pytest tests/     — юнит- и интеграционные тесты
#   3. python3 scripts/e2e_trace_check.py — end-to-end smoke test (без камеры)
#
# Все три шага должны пройти. Если любой падает — не пушить.

set -e
cd "$(dirname "$0")/.."

# Test credentials — needed because config.py validates that creds are set.
# These are FAKE credentials for CI only — never use in production.
export CRANE_CAMERA_IP="${CRANE_CAMERA_IP:-10.0.0.1}"
export CRANE_CAMERA_USER="${CRANE_CAMERA_USER:-test_user}"
export CRANE_CAMERA_PASS="${CRANE_CAMERA_PASS:-test_pass}"
export CRANE_API_TOKEN="${CRANE_API_TOKEN:-test_token_long_enough}"

echo "=== Step 1/3: compileall ==="
python3 -m compileall -q .
echo "OK"
echo

echo "=== Step 2/3: pytest ==="
python3 -m pytest tests/ --tb=short
echo

echo "=== Step 3/3: e2e_trace_check ==="
python3 scripts/e2e_trace_check.py > /tmp/e2e_output.txt 2>&1 || {
    echo "FAILED — e2e_trace_check output:"
    cat /tmp/e2e_output.txt
    exit 1
}
# Проверяем, что в выводе есть ожидаемые маркеры
grep -q '"decision": "hold"' /tmp/e2e_output.txt || {
    echo "FAILED — scenario 1 (hold) not found in e2e output"
    cat /tmp/e2e_output.txt
    exit 1
}
grep -q '"decision": "pan"' /tmp/e2e_output.txt || {
    echo "FAILED — scenario 2 (pan) not found in e2e output"
    cat /tmp/e2e_output.txt
    exit 1
}
grep -q '"http": 200' /tmp/e2e_output.txt || {
    echo "FAILED — HTTP 200 not found in e2e output"
    cat /tmp/e2e_output.txt
    exit 1
}
echo "OK — both scenarios produced expected trace output"
echo

echo "=== ALL CHECKS PASSED ==="
