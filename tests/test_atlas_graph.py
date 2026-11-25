"""tools/atlas_graph.py の単体テスト（標準ライブラリの unittest だけで動く）。

graph.json のフィクスチャは tests/fixtures/sample-java を graphify（--code-only）で解析した実物。
graphify が無くても走る。graphify を使う結合テストは CI の graphify ジョブが行う。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import atlas_graph  # noqa: E402

GRAPH = ROOT / "tests" / "fixtures" / "graphify-out" / "graph.json"
BASE = "src/main/java/shop"

CODE_GRAPH_HEADER = """\
# コードグラフ（Phase 1）

<!-- 例示行は拾わない:
| NODE-MOD-99 | 例 | src/ | 例 | 未着手 |
-->

## ノードレジストリ

### ノード: モジュール一覧

| NODE-ID | モジュール | パス | 役割（推定） | 仕様化状態 |
|---------|-----------|------|-------------|-----------|
{modules}

### ノード: エントリポイント一覧

| NODE-ID | 種別 | エントリポイント | path:line | 仕様化状態 |
|---------|------|----------------|-----------|-----------|
| NODE-EP-01 | HTTP | placeOrder | `{base}/web/OrderController.java:9` | 未着手 |

## エッジレジストリ

| From (NODE-ID) | To (NODE-ID) | 種別（depends/calls/dataflow） | 根拠 path:line |
|----------------|--------------|------------------------------|----------------|
{edges}
"""


def write_code_graph(tmp: Path, modules: list[str], edges: list[str]) -> Path:
    p = tmp / "code-graph.md"
    p.write_text(
        CODE_GRAPH_HEADER.format(modules="\n".join(modules), edges="\n".join(edges), base=BASE),
        encoding="utf-8",
    )
    return p


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = atlas_graph.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


ALL_MODULES = [
    f"| NODE-MOD-01 | web | `{BASE}/web/` | 受付 | 未着手 |",
    f"| NODE-MOD-02 | service | `{BASE}/service` | 業務 | 未着手 |",
    f"| NODE-MOD-03 | repo | {BASE}/repo/OrderRepository.java | 永続化 | 未着手 |",
]


class LoadGraphTest(unittest.TestCase):
    def test_files_are_relative_posix_paths(self):
        g = atlas_graph.load_graph(GRAPH)
        self.assertEqual(
            g.files,
            {
                f"{BASE}/web/OrderController.java",
                f"{BASE}/service/OrderService.java",
                f"{BASE}/repo/OrderRepository.java",
            },
        )

    def test_file_edges_ignore_structure(self):
        g = atlas_graph.load_graph(GRAPH)
        pairs = atlas_graph.file_edges(g)
        self.assertIn((f"{BASE}/web/OrderController.java", f"{BASE}/service/OrderService.java"), pairs)
        self.assertIn((f"{BASE}/service/OrderService.java", f"{BASE}/repo/OrderRepository.java"), pairs)
        for a, b in pairs:
            self.assertNotEqual(a, b)

    def test_missing_graph_is_input_error(self):
        rc, _, err = run("crosscheck", "--graph", "no/such/graph.json", "--code-graph", "x.md")
        self.assertEqual(rc, 2)
        self.assertIn("graph.json", err)

    def test_graph_path_accepts_directories(self):
        self.assertEqual(atlas_graph.resolve_graph_path(GRAPH.parent), GRAPH)
        self.assertEqual(atlas_graph.resolve_graph_path(GRAPH.parent.parent), GRAPH)


class ParseCodeGraphTest(unittest.TestCase):
    def test_paths_and_edges(self):
        with tempfile.TemporaryDirectory() as t:
            p = write_code_graph(Path(t), ALL_MODULES, ["| NODE-MOD-01 | NODE-MOD-02 | calls | x:1 |"])
            doc = atlas_graph.parse_code_graph(p)
        self.assertEqual(doc.node_paths["NODE-MOD-01"], [f"{BASE}/web/"])
        self.assertEqual(doc.node_paths["NODE-MOD-02"], [f"{BASE}/service"])
        # path:line の行番号は剥がす
        self.assertEqual(doc.node_paths["NODE-EP-01"], [f"{BASE}/web/OrderController.java"])
        self.assertNotIn("NODE-MOD-99", doc.node_paths)  # HTML コメントの中は無視
        self.assertEqual(doc.edges, {("NODE-MOD-01", "NODE-MOD-02")})
        self.assertEqual(set(doc.module_nodes), {"NODE-MOD-01", "NODE-MOD-02", "NODE-MOD-03"})

    def test_absolute_paths_are_made_relative(self):
        with tempfile.TemporaryDirectory() as t:
            p = write_code_graph(Path(t), [f"| NODE-MOD-01 | all | /work/legacy/{BASE}/ | x | 未着手 |"], [])
            doc = atlas_graph.parse_code_graph(p, source_root="/work/legacy")
        self.assertEqual(doc.node_paths["NODE-MOD-01"], [f"{BASE}/"])

    def test_multiple_paths_in_one_cell(self):
        with tempfile.TemporaryDirectory() as t:
            p = write_code_graph(
                Path(t), [f"| NODE-MOD-01 | x | `{BASE}/web`<br>`{BASE}/service`、{BASE}/repo | x | 未着手 |"], []
            )
            doc = atlas_graph.parse_code_graph(p)
        self.assertEqual(doc.node_paths["NODE-MOD-01"], [f"{BASE}/web", f"{BASE}/service", f"{BASE}/repo"])


class PathMatchTest(unittest.TestCase):
    def test_prefix_is_directory_boundary(self):
        self.assertTrue(atlas_graph.path_matches("src/web/A.java", "src/web"))
        self.assertTrue(atlas_graph.path_matches("src/web/A.java", "src/web/"))
        self.assertFalse(atlas_graph.path_matches("src/webapp/A.java", "src/web"))

    def test_glob(self):
        self.assertTrue(atlas_graph.path_matches("src/web/A.java", "src/*/A.java"))
        self.assertTrue(atlas_graph.path_matches("src/web/A.java", "src/web*"))

    def test_owner_is_most_specific(self):
        owner = atlas_graph.owner_of("src/web/api/A.java", {"N1": ["src"], "N2": ["src/web/api"]})
        self.assertEqual(owner, "N2")


class CrosscheckTest(unittest.TestCase):
    def test_all_covered(self):
        with tempfile.TemporaryDirectory() as t:
            cg = write_code_graph(Path(t), ALL_MODULES, [])
            rc, out, _ = run("crosscheck", "--graph", str(GRAPH), "--code-graph", str(cg))
        self.assertEqual(rc, 0, out)
        self.assertIn("uncovered: 0", out)

    def test_uncovered_file_is_reported(self):
        with tempfile.TemporaryDirectory() as t:
            cg = write_code_graph(Path(t), ALL_MODULES[:2], [])
            rc, out, _ = run("crosscheck", "--graph", str(GRAPH), "--code-graph", str(cg))
        self.assertEqual(rc, 1)
        self.assertIn(f"UNCOVERED {BASE}/repo/OrderRepository.java", out)

    def test_exclude_removes_from_denominator(self):
        with tempfile.TemporaryDirectory() as t:
            cg = write_code_graph(Path(t), ALL_MODULES[:2], [])
            rc, out, _ = run("crosscheck", "--graph", str(GRAPH), "--code-graph", str(cg), "--exclude", "(^|/)repo/")
        self.assertEqual(rc, 0, out)

    def test_empty_registry_fails(self):
        with tempfile.TemporaryDirectory() as t:
            # テンプレートを置いただけの状態（ノード行のパスが空）
            cg = Path(t) / "code-graph.md"
            cg.write_text(
                "| NODE-ID | モジュール | パス | 役割（推定） | 仕様化状態 |\n"
                "|---------|-----------|------|-------------|-----------|\n"
                "| NODE-MOD-01 | | | | 未着手 |\n",
                encoding="utf-8",
            )
            rc, out, _ = run("crosscheck", "--graph", str(GRAPH), "--code-graph", str(cg))
        self.assertEqual(rc, 1)
        self.assertIn("パスが 1 件もありません", out)

    def test_bad_exclude_is_input_error(self):
        with tempfile.TemporaryDirectory() as t:
            cg = write_code_graph(Path(t), ALL_MODULES, [])
            rc, _, err = run("crosscheck", "--graph", str(GRAPH), "--code-graph", str(cg), "--exclude", "(")
        self.assertEqual(rc, 2)
        self.assertIn("SOURCE_EXCLUDE", err)


class BroadPathTest(unittest.TestCase):
    def test_root_patterns(self):
        for p in (".", "./", "*", "**", "**/*", "/"):
            self.assertTrue(atlas_graph.is_root_pattern(p), p)
        for p in ("src", "src/*", "*.java", "src/main/java/shop/web/"):
            self.assertFalse(atlas_graph.is_root_pattern(p), p)

    def test_root_path_does_not_cover(self):
        """`.` のような広すぎるパスで G-GRAPHIFY を素通りさせない。"""
        rows = ["| NODE-MOD-01 | all | . | 全部 | 未着手 |", "| NODE-MOD-02 | all2 | `**/*` | 全部 | 未着手 |"]
        with tempfile.TemporaryDirectory() as t:
            cg = write_code_graph(Path(t), rows, [])
            rc, out, _ = run("crosscheck", "--graph", str(GRAPH), "--code-graph", str(cg))
        self.assertEqual(rc, 1)
        self.assertIn("BROAD NODE-MOD-01: '.'", out)
        self.assertIn("BROAD NODE-MOD-02: '**/*'", out)
        self.assertIn(f"UNCOVERED {BASE}/repo/OrderRepository.java", out)


class NotInGraphTest(unittest.TestCase):
    def test_grouped_by_extension(self):
        files = ["a/A.java", "conf/struts.xml", "conf/web.xml", "jcl/RUN", "README.md"]
        got = atlas_graph.not_in_graph_by_ext(files, {"a/A.java"})
        self.assertEqual(got[".xml"], {"count": 2, "example": "conf/struts.xml"})
        self.assertEqual(got["(なし)"]["count"], 1)
        self.assertNotIn(".java", got)

    def test_list_source_files_without_git(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "src").mkdir()
            (root / "src" / "A.java").write_text("class A {}", encoding="utf-8")
            (root / ".hidden").mkdir()
            (root / ".hidden" / "x").write_text("x", encoding="utf-8")
            (root / "target").mkdir()
            (root / "target" / "A.class").write_text("x", encoding="utf-8")
            files = atlas_graph.list_source_files(root, atlas_graph.compile_exclude("(^|/)target/"))
        # 一時ディレクトリが git の中にある環境では ls-files が空になるため、非 git のときだけ厳密に見る
        if atlas_graph._git(root, "rev-parse", "HEAD") is None:
            self.assertEqual(files, ["src/A.java"])

    def test_inventory_shows_unparsed_files(self):
        info = {"not_in_graph_by_ext": {".xml": {"count": 2, "example": "conf/struts.xml"}}}
        text = atlas_graph.render_inventory(atlas_graph.load_graph(GRAPH), 0, None, info)
        self.assertIn("graphify が数えなかったファイル", text)
        self.assertIn("| `.xml` | 2 | `conf/struts.xml` |", text)


class EdgeGapTest(unittest.TestCase):
    def test_missing_edges_are_reported(self):
        with tempfile.TemporaryDirectory() as t:
            cg = write_code_graph(Path(t), ALL_MODULES, ["| NODE-MOD-01 | NODE-MOD-02 | calls | x:1 |"])
            rc, out, _ = run("edge-gap", "--graph", str(GRAPH), "--code-graph", str(cg))
            self.assertEqual(rc, 0)  # 既定は報告のみ
            self.assertIn("MISSING NODE-MOD-02 -> NODE-MOD-03", out)
            self.assertNotIn("NODE-MOD-01 -> NODE-MOD-02", out)
            rc, _, _ = run("edge-gap", "--graph", str(GRAPH), "--code-graph", str(cg), "--strict")
            self.assertEqual(rc, 1)

    def test_complete_registry(self):
        edges = ["| NODE-MOD-01 | NODE-MOD-02 | calls | x:1 |", "| NODE-MOD-02 | NODE-MOD-03 | dataflow | x:2 |"]
        with tempfile.TemporaryDirectory() as t:
            cg = write_code_graph(Path(t), ALL_MODULES, edges)
            rc, out, _ = run("edge-gap", "--graph", str(GRAPH), "--code-graph", str(cg), "--strict")
        self.assertEqual(rc, 0, out)
        self.assertIn(": 0", out)


class InventoryTest(unittest.TestCase):
    def test_inventory_lists_files_and_dependencies(self):
        rc, out, _ = run("inventory", "--graph", str(GRAPH))
        self.assertEqual(rc, 0)
        self.assertIn("ソースファイル: **3**", out)
        self.assertIn(f"| `{BASE}/web` | `{BASE}/service` |", out)
        self.assertIn(f"- `{BASE}/repo/OrderRepository.java`", out)

    def test_inventory_depth(self):
        rc, out, _ = run("inventory", "--graph", str(GRAPH), "--depth", "2")
        self.assertEqual(rc, 0)
        self.assertIn("| `src/main` | 3 |", out)


class StaleTest(unittest.TestCase):
    def test_missing_build_info(self):
        with tempfile.TemporaryDirectory() as t:
            rc, _, err = run("stale", "--graph-dir", t, "--source", t)
        self.assertEqual(rc, 2)
        self.assertIn("build-info.json", err)

    def test_non_git_source_matches_its_marker(self):
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "src"
            src.mkdir()
            (Path(t) / "build-info.json").write_text(json.dumps({"source_commit": "x"}), encoding="utf-8")
            commit, _ = atlas_graph.source_state(src)
            rc, out, _ = run("stale", "--graph-dir", t, "--source", str(src))
        # 一時ディレクトリが git の中に無ければ "not-a-git-repository" になり、記録 "x" と一致しない
        if commit == "not-a-git-repository":
            self.assertEqual(rc, 1)
            self.assertIn("STALE", out)


class BuildGuardTest(unittest.TestCase):
    def test_refuses_output_inside_source(self):
        with tempfile.TemporaryDirectory() as t:
            rc, _, err = run("build", "--source", t, "--out", str(Path(t) / "out"), "--graphify", "graphify")
        self.assertEqual(rc, 2)
        self.assertIn("解析対象の中", err)
        self.assertFalse((Path(t) / "out").exists())


if __name__ == "__main__":
    unittest.main()
