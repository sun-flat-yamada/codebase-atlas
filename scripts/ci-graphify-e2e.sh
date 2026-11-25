#!/bin/bash
# =============================================================================
# ci-graphify-e2e.sh — graphify 連携（GP-108）の結合テスト
# =============================================================================
#
# 実物の graphify を使って、init.sh → atlas_graph.py build → gates.sh graphify を通す。
#   1. 何も書いていない code-graph.md では G-GRAPHIFY が FAIL する
#   2. 全ソースファイルをノードに割り当てると PASS する
#   3. 解析対象（サンプル）は変更されない
#
# 前提: graphify が PATH にある（または GRAPHIFY_CMD）。Python 3.10+・git。
# 使い方: bash scripts/ci-graphify-e2e.sh   （終了コード 0 = 全通過）
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/atlas-e2e.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PY="$(command -v python3 || command -v python)"

cp -R "$ROOT/tests/fixtures/sample-java" "$WORK/shop"
( cd "$WORK/shop" && git init -q && git add -A && git commit -qm "sample" )

( cd "$WORK" && bash "$ROOT/init.sh" ./shop shop maintenance ) > "$WORK/init.log"
SPEC="$WORK/shop-spec"
cd "$SPEC"

"$PY" spec-extraction/tools/atlas_graph.py build \
  --source "$WORK/shop" --out spec-extraction/01-code-graph/graphify
test -f spec-extraction/01-code-graph/graphify/graphify-out/graph.json
test -f spec-extraction/01-code-graph/graphify/graphify-out/GRAPH_REPORT.md
grep -q 'ソースファイル: \*\*3\*\*' spec-extraction/01-code-graph/graphify/inventory.md

echo "--- 1. empty code-graph.md must FAIL"
if bash spec-extraction/03-audit/gates.sh graphify; then
  echo "::error::G-GRAPHIFY passed with an empty node registry"; exit 1
fi

echo "--- 2. all files assigned must PASS"
"$PY" - spec-extraction/01-code-graph/code-graph.md <<'PYEOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text(encoding="utf-8")
row = "| NODE-MOD-01 | | | | 未着手 |"
assert row in s
s = s.replace(row, "\n".join([
    "| NODE-MOD-01 | web | src/main/java/shop/web/ | 受付 | 未着手 |",
    "| NODE-MOD-02 | service | src/main/java/shop/service/ | 業務 | 未着手 |",
    "| NODE-MOD-03 | repo | src/main/java/shop/repo/ | 永続化 | 未着手 |",
]), 1)
p.write_text(s, encoding="utf-8")
PYEOF
bash spec-extraction/03-audit/gates.sh graphify

echo "--- 3. the analysis target is untouched"
test -z "$(git -C "$WORK/shop" status --porcelain)"

echo "✅ graphify end-to-end passed"
