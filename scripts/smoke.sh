#!/usr/bin/env bash
# 上线前冒烟：健康检查 + 三个页面 + 一个接口错误路径。
# 用法： ./scripts/smoke.sh [base_url]      默认 http://127.0.0.1:8000
set -uo pipefail

BASE="${1:-http://127.0.0.1:8000}"
fail=0

check() {  # check <名称> <期望码> <url>
  local name="$1" want="$2" url="$3"
  local code
  code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" "$url" || echo "000")
  if [ "$code" = "$want" ]; then
    printf "  [OK]   %-18s %s\n" "$name" "$code"
  else
    printf "  [FAIL] %-18s 期望 %s 实际 %s  %s\n" "$name" "$want" "$code" "$url"
    fail=$((fail + 1))
  fi
}

echo "冒烟目标：$BASE"

echo "[1] 健康检查"
health=$(curl -s -m 10 "$BASE/api/health" || echo "")
if echo "$health" | grep -q '"ok":true'; then
  echo "  [OK]   /api/health      ok=true"
else
  echo "  [FAIL] /api/health      未返回 ok=true"
  echo "         实际：${health:0:160}"
  fail=$((fail + 1))
fi

echo "[2] 页面可达"
check "/"          200 "$BASE/"
check "/agent"     200 "$BASE/agent"
check "/admin/login" 200 "$BASE/admin/login"

echo "[3] 未登录访问后台应跳转"
code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" "$BASE/admin" || echo "000")
if [ "$code" = "302" ] || [ "$code" = "307" ]; then
  echo "  [OK]   /admin          $code → 登录页"
else
  echo "  [FAIL] /admin          期望 302，实际 $code"
  fail=$((fail + 1))
fi

echo "[4] 参数校验"
code=$(curl -s -o /dev/null -m 10 -w "%{http_code}" -X POST "$BASE/api/chat" \
  -H "Content-Type: application/json" -d '{"question":"   "}' || echo "000")
if [ "$code" = "400" ]; then
  echo "  [OK]   空问题          400"
else
  echo "  [FAIL] 空问题          期望 400，实际 $code"
  fail=$((fail + 1))
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "全部通过（5 项）"
  exit 0
else
  echo "有 $fail 项未通过"
  exit 1
fi
