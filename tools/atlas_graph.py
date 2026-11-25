#!/usr/bin/env python3
"""atlas_graph.py — graphify を Phase 1（コードグラフ）の「独立した 2 つ目の数え方」として使う補助ツール。

codebase-atlas は、AI エージェントが書く Markdown の構造マップ（``01-code-graph/code-graph.md``）を
網羅の分母にする。このツールは、graphify（tree-sitter AST による決定的な解析。LLM を使わない）で
作ったグラフ（``graph.json``）とその構造マップを突き合わせ、**AI の数え漏れを機械的に見つける**。

graphify は解析対象を変更しない（出力は ``--out`` で指定した場所にだけ書く）。
このツール自身は Python 標準ライブラリだけで動く。graphify が要るのは ``build`` だけである。

サブコマンド:
  build       graphify で解析対象のコードグラフを作り、build-info.json と inventory.md を書く
  inventory   graph.json から、構造マップを書くための下調べ（ディレクトリ別の件数・ハブ・依存）を Markdown で出す
  crosscheck  graph.json のソースファイルが、code-graph.md のどのノードの「パス」にも属さなければ報告する（exit 1）
  edge-gap    graph.json のモジュール間依存のうち、code-graph.md のエッジレジストリに無いものを報告する
  stale       build-info.json の断面（コミット）が解析対象の現在の HEAD と一致するかを確かめる（不一致は exit 1）

終了コード: 0 = 問題なし / 1 = 検出あり / 2 = 入力の不備（ファイルが無い・形式が違う）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

GRAPH_SUBDIR = "graphify-out"
GRAPH_FILE = "graph.json"
BUILD_INFO = "build-info.json"
INVENTORY_FILE = "inventory.md"

# ファイル間の依存とみなす関係。`contains`（ファイル→シンボル）と `method`（クラス→メソッド）は
# 同じファイルの中の構造なので依存に数えない。
STRUCTURAL_RELATIONS = {"contains", "method", "defines", "member", "declares"}

_LINE_SUFFIX = re.compile(r":L?\d+(?:-L?\d+)?$")
_SPLIT_PATHS = re.compile(r"\s*(?:<br\s*/?>|,|、|;|；)\s*")
_NODE_ID = re.compile(r"^NODE-[A-Za-z0-9_-]+$")


class InputError(Exception):
    """入力の不備（exit 2）。"""


# ---------------------------------------------------------------------------
# graph.json
# ---------------------------------------------------------------------------


@dataclass
class Graph:
    nodes: dict[str, dict]
    edges: list[dict]
    meta: dict

    @property
    def files(self) -> set[str]:
        return {n["source_file"] for n in self.nodes.values() if is_code_node(n)}

    def file_of(self, node_id: str) -> str | None:
        node = self.nodes.get(node_id)
        if node is None or not is_code_node(node):
            return None
        return node["source_file"]


def is_code_node(node: dict) -> bool:
    return node.get("file_type", "code") == "code" and bool(node.get("source_file"))


def load_graph(path: Path) -> Graph:
    if not path.is_file():
        raise InputError(f"graph.json がありません: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise InputError(f"graph.json を JSON として読めません: {path}: {e}") from e
    if not isinstance(data, dict) or "nodes" not in data:
        raise InputError(f"graph.json の形式が想定と違います（nodes がありません）: {path}")
    raw_edges = data.get("links", data.get("edges", []))
    nodes = {}
    for n in data["nodes"]:
        if isinstance(n, dict) and "id" in n:
            n = dict(n)
            if n.get("source_file"):
                n["source_file"] = normalize_path(n["source_file"])
            nodes[n["id"]] = n
    edges = [e for e in raw_edges if isinstance(e, dict) and "source" in e and "target" in e]
    return Graph(nodes=nodes, edges=edges, meta=data.get("graph", {}) or {})


def resolve_graph_path(path: Path) -> Path:
    """graph.json そのもの・graphify-out/・build の出力先のどれを渡されても graph.json を指す。"""
    if path.is_dir():
        for candidate in (path / GRAPH_FILE, path / GRAPH_SUBDIR / GRAPH_FILE):
            if candidate.is_file():
                return candidate
        return path / GRAPH_SUBDIR / GRAPH_FILE
    return path


def normalize_path(p: str) -> str:
    p = p.strip().strip("`").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def compile_exclude(pattern: str | None) -> re.Pattern | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as e:
        raise InputError(f"除外パターン（SOURCE_EXCLUDE）を正規表現として解釈できません: {pattern!r}: {e}") from e


def is_excluded(path: str, exclude: re.Pattern | None) -> bool:
    return bool(exclude and exclude.search(path))


def file_edges(graph: Graph) -> Counter:
    """シンボル間の依存を、ファイル間の依存 (from_file, to_file) に畳み込む。"""
    pairs: Counter = Counter()
    for e in graph.edges:
        if e.get("relation") in STRUCTURAL_RELATIONS:
            continue
        a, b = graph.file_of(e["source"]), graph.file_of(e["target"])
        if a and b and a != b:
            pairs[(a, b)] += 1
    return pairs


# ---------------------------------------------------------------------------
# code-graph.md
# ---------------------------------------------------------------------------


@dataclass
class CodeGraphDoc:
    # NODE-ID → そのノードが受け持つパス（ディレクトリ・ファイル・glob）
    node_paths: dict[str, list[str]] = field(default_factory=dict)
    # エッジレジストリの (From, To)
    edges: set[tuple[str, str]] = field(default_factory=set)
    # 広すぎて被覆に数えなかったパス (NODE-ID, パス)
    broad: list[tuple[str, str]] = field(default_factory=list)

    @property
    def module_nodes(self) -> dict[str, list[str]]:
        mods = {k: v for k, v in self.node_paths.items() if k.startswith("NODE-MOD")}
        return mods or self.node_paths


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    # `\|`（セル内の縦棒）で割らない
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def _path_column(header: str) -> bool:
    h = header.lower()
    return "パス" in header or "path" in h


def parse_code_graph(path: Path, source_root: str | None = None) -> CodeGraphDoc:
    if not path.is_file():
        raise InputError(f"code-graph.md がありません: {path}")
    doc = CodeGraphDoc()
    header: list[str] | None = None
    prev_cells: list[str] | None = None
    in_comment = False
    root_prefix = normalize_path(source_root).rstrip("/") + "/" if source_root else None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # HTML コメントの中の例示行を拾わない
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if line.startswith("<!--") and "-->" not in line:
            in_comment = True
            continue
        if not line.startswith("|"):
            header = None
            prev_cells = None
            continue
        cells = _cells(line)
        if _is_separator(cells):
            header = prev_cells
            continue
        prev_cells = cells
        if not cells or not _NODE_ID.match(cells[0]):
            continue
        # エッジ行: | NODE-A | NODE-B | 種別 | 根拠 |
        if len(cells) >= 2 and _NODE_ID.match(cells[1]):
            doc.edges.add((cells[0], cells[1]))
            continue
        # ノード行: 「パス」「path:line」の列からパスを取る
        if header is None:
            continue
        paths: list[str] = []
        for h, c in zip(header, cells, strict=False):  # 列数の違う行（崩れた表）でも読める範囲だけ読む
            if not _path_column(h) or not c:
                continue
            for token in _SPLIT_PATHS.split(c):
                p = normalize_path(_LINE_SUFFIX.sub("", normalize_path(token)))
                if root_prefix and p.startswith(root_prefix):
                    p = p[len(root_prefix) :]
                if not p or p in ("-", "—", "N/A"):
                    continue
                if is_root_pattern(p):
                    # 解析対象のルート全体を受け持つパスは数え漏れを隠すだけなので、被覆に数えない
                    doc.broad.append((cells[0], p))
                    continue
                paths.append(p)
        if paths:
            doc.node_paths.setdefault(cells[0], []).extend(paths)
    return doc


_ROOT_PATTERNS = {".", "/", "*", "**", "*/*", "**/*", "./*", "./**"}


def is_root_pattern(p: str) -> bool:
    """解析対象のルート全体を指すパス（`.`・`*`・`**/*` など）。GP-108 アンチパターンの機械的な歯止め。"""
    return p.rstrip("/") in _ROOT_PATTERNS or p in _ROOT_PATTERNS or set(p) <= set("*/.")


def path_matches(file: str, pattern: str) -> bool:
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(file, pattern) or fnmatch.fnmatchcase(file, pattern.rstrip("/") + "/*")
    pattern = pattern.rstrip("/")
    return file == pattern or file.startswith(pattern + "/")


def owner_of(file: str, node_paths: dict[str, list[str]]) -> str | None:
    """最も具体的な（最長の）パスで一致したノードを返す。"""
    best: tuple[int, str] | None = None
    for node_id, paths in node_paths.items():
        for p in paths:
            if path_matches(file, p):
                score = len(p)
                if best is None or score > best[0]:
                    best = (score, node_id)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------


def cmd_crosscheck(args: argparse.Namespace) -> int:
    graph = load_graph(resolve_graph_path(Path(args.graph)))
    doc = parse_code_graph(Path(args.code_graph), args.source_root)
    exclude = compile_exclude(args.exclude)
    files = sorted(f for f in graph.files if not is_excluded(f, exclude))
    if not files:
        raise InputError("graph.json にソースファイルが 1 件もありません（解析対象のパス・除外設定を確認してください）")
    if not doc.node_paths:
        print(f"UNCOVERED {len(files)}/{len(files)}: code-graph.md のノード行にパスが 1 件もありません")
        for node_id, p in doc.broad:
            print(f"  BROAD {node_id}: '{p}' は解析対象のルート全体を指すため被覆に数えない")
        return 1
    uncovered = [f for f in files if owner_of(f, doc.node_paths) is None]
    covered = len(files) - len(uncovered)
    print(f"graphify files: {len(files)}  covered: {covered}  uncovered: {len(uncovered)}")
    for node_id, p in doc.broad:
        print(f"  BROAD {node_id}: '{p}' は解析対象のルート全体を指すため被覆に数えない")
    for f in uncovered[: args.limit]:
        print(f"  UNCOVERED {f}")
    if len(uncovered) > args.limit:
        print(f"  ... ほか {len(uncovered) - args.limit} 件")
    return 1 if uncovered else 0


def cmd_edge_gap(args: argparse.Namespace) -> int:
    graph = load_graph(resolve_graph_path(Path(args.graph)))
    doc = parse_code_graph(Path(args.code_graph), args.source_root)
    exclude = compile_exclude(args.exclude)
    modules = doc.module_nodes
    if not modules:
        raise InputError(
            "code-graph.md のノード行にパスが 1 件もありません（モジュールを列挙してから実行してください）"
        )
    gaps: Counter = Counter()
    evidence: dict[tuple[str, str], tuple[str, str]] = {}
    for (a, b), n in file_edges(graph).items():
        if is_excluded(a, exclude) or is_excluded(b, exclude):
            continue
        ma, mb = owner_of(a, modules), owner_of(b, modules)
        if not ma or not mb or ma == mb:
            continue
        if (ma, mb) in doc.edges:
            continue
        gaps[(ma, mb)] += n
        evidence.setdefault((ma, mb), (a, b))
    print(f"module dependencies in graphify but not in the edge registry: {len(gaps)}")
    for (ma, mb), n in gaps.most_common(args.limit):
        a, b = evidence[(ma, mb)]
        print(f"  MISSING {ma} -> {mb}  ({n} symbol edges; e.g. {a} -> {b})")
    return 1 if (gaps and args.strict) else 0


def _top_dir(path: str, depth: int) -> str:
    """ファイルの属するディレクトリ。depth <= 0 は親ディレクトリそのもの（パッケージ単位）。"""
    parts = path.split("/")
    if len(parts) <= 1:
        return "."
    if depth <= 0:
        return "/".join(parts[:-1])
    return "/".join(parts[: min(depth, len(parts) - 1)])


def render_inventory(graph: Graph, depth: int, exclude: re.Pattern | None, build_info: dict | None) -> str:
    files = sorted(f for f in graph.files if not is_excluded(f, exclude))
    by_dir_files: dict[str, set[str]] = defaultdict(set)
    by_dir_symbols: Counter = Counter()
    for f in files:
        by_dir_files[_top_dir(f, depth)].add(f)
    file_set = set(files)
    degree: Counter = Counter()
    for e in graph.edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    for n in graph.nodes.values():
        if is_code_node(n) and n["source_file"] in file_set and n.get("label") != Path(n["source_file"]).name:
            by_dir_symbols[_top_dir(n["source_file"], depth)] += 1

    dir_edges: Counter = Counter()
    for (a, b), n in file_edges(graph).items():
        if a in file_set and b in file_set:
            da, db = _top_dir(a, depth), _top_dir(b, depth)
            if da != db:
                dir_edges[(da, db)] += n

    out: list[str] = []
    out.append("# graphify インベントリ（Phase 1 の下調べ）")
    out.append("")
    out.append("<!-- 生成物。手で編集しない。再生成: python3 spec-extraction/tools/atlas_graph.py build -->")
    out.append("")
    out.append("graphify（tree-sitter AST。LLM を使わない決定的な解析）で数えた、解析対象のソースの一覧です。")
    out.append("`code-graph.md` のノードを列挙するときの**2 つ目の数え方**として使います。")
    out.append("ここに出るファイルは、`code-graph.md` のどれかのノードの「パス」に属している必要があります")
    out.append("（`gates.sh phase1` の `G-GRAPHIFY` が検査します）。")
    out.append("")
    if build_info:
        out.append("| 項目 | 値 |")
        out.append("|------|----|")
        for k in ("source_commit", "source_dirty", "graphify_version", "built_at"):
            if k in build_info:
                out.append(f"| {k} | `{build_info[k]}` |")
        out.append("")
    out.append(f"- ソースファイル: **{len(files)}**")
    out.append(f"- ノード（ファイル・クラス・関数など）: {len(graph.nodes)}")
    out.append(f"- エッジ: {len(graph.edges)}")
    out.append("")
    out.append("## ディレクトリ別" + (f"（深さ {depth}）" if depth > 0 else "（ファイルの親ディレクトリ単位）"))
    out.append("")
    out.append("| ディレクトリ | ファイル数 | シンボル数 | 他ディレクトリへの依存（件） |")
    out.append("|-------------|-----------:|-----------:|----------------------------:|")
    for d in sorted(by_dir_files):
        outbound = sum(n for (da, _), n in dir_edges.items() if da == d)
        out.append(f"| `{d}` | {len(by_dir_files[d])} | {by_dir_symbols[d]} | {outbound} |")
    out.append("")
    out.append("## ディレクトリ間の依存（エッジレジストリの候補）")
    out.append("")
    if dir_edges:
        out.append("| From | To | シンボル間の依存（件） |")
        out.append("|------|----|---------------------:|")
        for (da, db), n in dir_edges.most_common(100):
            out.append(f"| `{da}` | `{db}` | {n} |")
    else:
        out.append("（ディレクトリをまたぐ依存は検出されませんでした）")
    out.append("")
    out.append("## 結合の多いシンボル（god object の候補）")
    out.append("")
    out.append("| シンボル | 位置 | 次数 |")
    out.append("|----------|------|-----:|")
    hubs = [
        (nid, d)
        for nid, d in degree.most_common()
        if nid in graph.nodes and is_code_node(graph.nodes[nid]) and graph.nodes[nid]["source_file"] in file_set
    ][:30]
    for nid, d in hubs:
        n = graph.nodes[nid]
        loc = n.get("source_location", "")
        line = loc[1:] if loc.startswith("L") else loc
        label = str(n.get("label", nid)).replace("|", "\\|")
        out.append(f"| `{label}` | `{n['source_file']}:{line}` | {d} |")
    out.append("")
    not_in_graph = (build_info or {}).get("not_in_graph_by_ext")
    if not_in_graph:
        out.append("## graphify が数えなかったファイル（拡張子別）")
        out.append("")
        out.append("graphify が解析しなかった（対応していない言語・設定・文書などの）ファイルです。")
        out.append("**これらは `G-GRAPHIFY` の突き合わせの対象外**なので、分母に入るものは Playbook の")
        out.append("`enumeration.md` など別の数え方で列挙してください（例: 設定 XML・JCL・COBOL・SQL）。")
        out.append("")
        out.append("| 拡張子 | ファイル数 | 例 |")
        out.append("|--------|-----------:|----|")
        for ext, info in sorted(not_in_graph.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
            out.append(f"| `{ext}` | {info['count']} | `{info['example']}` |")
        out.append("")
    out.append("## ソースファイル一覧")
    out.append("")
    for f in files:
        out.append(f"- `{f}`")
    out.append("")
    return "\n".join(out)


def list_source_files(source: Path, exclude: re.Pattern | None) -> list[str]:
    """解析対象のファイル一覧（相対パス）。git なら追跡中のファイル、そうでなければ隠しディレクトリを除いて走査する。"""
    tracked = _git(source, "ls-files", "-z")
    if tracked is not None:
        files = [normalize_path(f) for f in tracked.split("\0") if f]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(source):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                files.append(normalize_path(os.path.relpath(os.path.join(dirpath, name), source)))
    return sorted(f for f in files if not is_excluded(f, exclude))


def not_in_graph_by_ext(all_files: list[str], graph_files: set[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for f in all_files:
        if f in graph_files:
            continue
        name = f.rsplit("/", 1)[-1]
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name.lstrip(".") else "(なし)"
        entry = result.setdefault(ext, {"count": 0, "example": f})
        entry["count"] += 1
    return result


def cmd_inventory(args: argparse.Namespace) -> int:
    graph_path = resolve_graph_path(Path(args.graph))
    graph = load_graph(graph_path)
    info_path = graph_path.parent.parent / BUILD_INFO
    build_info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else None
    text = render_inventory(graph, args.depth, compile_exclude(args.exclude), build_info)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def _git(source: Path, *argv: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(source), *argv], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return r.stdout.strip()


def source_state(source: Path) -> tuple[str, bool | None]:
    commit = _git(source, "rev-parse", "HEAD")
    if commit is None:
        return "not-a-git-repository", None
    status = _git(source, "status", "--porcelain")
    return commit, bool(status)


def find_graphify(explicit: str | None) -> list[str]:
    if explicit:
        return explicit.split()
    env = os.environ.get("GRAPHIFY_CMD")
    if env:
        return env.split()
    exe = shutil.which("graphify")
    if exe:
        return [exe]
    raise InputError(
        "graphify が見つかりません。次のどれかで用意してください:\n"
        "  uv tool install git+https://github.com/sun-flat-yamada/graphify\n"
        "  （または GRAPHIFY_CMD / --graphify で実行コマンドを指定）"
    )


def cmd_build(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    if not source.is_dir():
        raise InputError(f"解析対象のディレクトリがありません: {source}")
    out = Path(args.out).resolve()
    try:
        out.relative_to(source)
        raise InputError("出力先が解析対象の中にあります。解析対象は変更しません（出力先を外に置いてください）")
    except ValueError:
        pass
    out.mkdir(parents=True, exist_ok=True)
    graphify = find_graphify(args.graphify)

    before = source_state(source)
    extract = [*graphify, "extract", str(source), "--code-only", "--out", str(out)]
    print("$ " + " ".join(extract), flush=True)
    subprocess.run(extract, check=True)
    if not args.no_report:
        report = [*graphify, "cluster-only", str(out)]
        print("$ " + " ".join(report), flush=True)
        subprocess.run(report, check=True, cwd=str(out))
    after = source_state(source)
    if before != after:
        raise InputError("解析の前後で解析対象の状態が変わりました。解析対象を変更しない前提が崩れています")

    version = ""
    try:
        version = subprocess.run([*graphify, "--version"], capture_output=True, text=True).stdout.strip()
    except OSError:
        pass
    info = {
        "tool": "graphify",
        "graphify_version": version,
        "mode": "code-only",
        "source_path": str(source),
        "source_commit": after[0],
        "source_dirty": after[1],
        "built_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    graph = load_graph(out / GRAPH_SUBDIR / GRAPH_FILE)
    exclude = compile_exclude(args.exclude)
    # graphify が数えなかったファイルを残す（対応していない言語は G-GRAPHIFY の対象外になるため、見えるようにする）
    info["not_in_graph_by_ext"] = not_in_graph_by_ext(list_source_files(source, exclude), graph.files)
    (out / BUILD_INFO).write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    text = render_inventory(graph, args.depth, exclude, info)
    (out / INVENTORY_FILE).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out / BUILD_INFO}")
    print(f"wrote {out / INVENTORY_FILE}  ({len(graph.files)} source files)")
    if after[1]:
        print("WARN: 解析対象に未コミットの変更があります。断面（コミット）と内容が一致しません", file=sys.stderr)
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    info_path = Path(args.graph_dir) / BUILD_INFO
    if not info_path.is_file():
        raise InputError(f"build-info.json がありません: {info_path}（先に build を実行してください）")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    current, dirty = source_state(Path(args.source))
    built = info.get("source_commit")
    if built != current:
        print(f"STALE: graph was built at {built}, source HEAD is {current}")
        return 1
    if dirty:
        print(f"DIRTY: source has uncommitted changes (HEAD {current})")
        return 1
    print(f"FRESH: {current}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlas_graph.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="graphify でコードグラフを作る")
    b.add_argument("--source", required=True, help="解析対象のルート（参照専用）")
    b.add_argument("--out", required=True, help="出力先（解析対象の外）")
    b.add_argument("--graphify", help="graphify の実行コマンド（既定: GRAPHIFY_CMD → PATH 上の graphify）")
    b.add_argument("--exclude", default="", help="除外する相対パスの正規表現（SOURCE_EXCLUDE）")
    b.add_argument(
        "--depth", type=int, default=0, help="インベントリでまとめるディレクトリの深さ（0 = ファイルの親ディレクトリ）"
    )
    b.add_argument("--no-report", action="store_true", help="GRAPH_REPORT.md / graph.html を作らない")
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("inventory", help="graph.json から下調べの Markdown を出す")
    i.add_argument("--graph", required=True)
    i.add_argument("--exclude", default="")
    i.add_argument("--depth", type=int, default=0)
    i.add_argument("--output")
    i.set_defaults(func=cmd_inventory)

    for name, func, help_text in (
        ("crosscheck", cmd_crosscheck, "ノードの数え漏れ（どのノードにも属さないソースファイル）を報告する"),
        ("edge-gap", cmd_edge_gap, "エッジレジストリに無いモジュール間依存を報告する"),
    ):
        c = sub.add_parser(name, help=help_text)
        c.add_argument("--graph", required=True)
        c.add_argument("--code-graph", required=True)
        c.add_argument("--exclude", default="")
        c.add_argument("--source-root", help="code-graph.md に絶対パスで書かれている場合に剥がす接頭辞")
        c.add_argument("--limit", type=int, default=50)
        if name == "edge-gap":
            c.add_argument("--strict", action="store_true", help="検出があれば exit 1")
        c.set_defaults(func=func)

    s = sub.add_parser("stale", help="グラフの断面が解析対象の HEAD と一致するか")
    s.add_argument("--graph-dir", required=True)
    s.add_argument("--source", required=True)
    s.set_defaults(func=cmd_stale)
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return args.func(args)
    except InputError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as e:
        print(f"ERROR: command failed (rc={e.returncode}): {' '.join(map(str, e.cmd))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
