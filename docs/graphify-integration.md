# graphify との連携（GP-108）

codebase-atlas は、コードベースの解析手段として [graphify](https://github.com/sun-flat-yamada/graphify) を使います。
この文書は、graphify が何をし、何をしないか、どう使うか、困ったときにどうするかをまとめたものです。
規約の正本は [pipeline.md](../pipeline.md) の `GP-108`、手順の正本は生成先の `spec-extraction/pipeline-procedure.md` の Step 1-1b です。

## 何のために使うか

reverse-spec-flow は、Phase 1 で AI エージェントが書く構造マップ（`01-code-graph/code-graph.md`）を**網羅の分母**にします。
分母に漏れがあると、その先の仕様化がどれだけ丁寧でも「全部書いた」とは言えません。

graphify は tree-sitter の AST でソースを解析し（約 40 言語）、ファイル・クラス・関数・import・呼び出しのグラフを
`graph.json` に書きます。`--code-only` では LLM を使わず、同じ入力から同じ結果を出します。
codebase-atlas はこれを **AI エージェントとは独立した 2 つ目の数え方**として使い、次の 2 つを機械で確かめます。

| 点検 | 内容 | 判定 |
|------|------|------|
| `G-GRAPHIFY` | graphify が数えたソースファイル（`SOURCE_EXCLUDE` を除く）が、`code-graph.md` のどれかのノードの「パス」「path:line」列に属するか。グラフの断面（コミット）が `scope.md` の断面と一致するか | 1 つでも属さなければ **FAIL** |
| `G-GRAPHIFY-EDGE` | graphify が見つけたモジュール間の依存が、エッジレジストリに記録されているか | 無ければ **WARN**（推定を含むため） |

どちらも `bash spec-extraction/03-audit/gates.sh phase1` に含まれます。単独では `gates.sh graphify` で実行できます。

graphify が対応していない言語・設定・文書のファイル（例: 設定 XML・JCL・COBOL のコピー句）は突き合わせの対象外です。
`build` はそれらを拡張子別に数えて `build-info.json` と `inventory.md`（「graphify が数えなかったファイル」）に残すので、
分母に入るものは Playbook の `enumeration.md` など別の数え方で列挙してください。

## 何をしないか

- **コードグラフの正本にはしません。** 入口・外部境界・業務上のまとまりは graphify には分かりません。正本は `code-graph.md` のままです。
- **根拠にはしません。** graphify のエッジのうち `INFERRED` は名前解決の推定です。仕様に書く根拠は原典の `path:line` を開いて確かめます。
- **解析対象を変更しません。** 出力先は解析対象の外です（`atlas_graph.py build` は中を拒否し、実行の前後で解析対象の git の状態が変わっていないことを確かめます）。
- **常駐する基盤を持ちません。** データベース・エンベディング・サーバーは使いません。

## 準備

```bash
# graphify（Python 3.11.5 以上）
uv tool install git+https://github.com/sun-flat-yamada/graphify
# または: pipx install git+https://github.com/sun-flat-yamada/graphify
graphify --version
```

突き合わせ（`gates.sh` が呼ぶ `atlas_graph.py`）は Python 3.10 以上の標準ライブラリだけで動きます。
graphify が要るのはグラフを作る `build` だけです。

## 使い方

```bash
# 1. 仕様書用のリポジトリを作る（--code-graph-tool の既定は graphify）
./codebase-atlas/init.sh ./legacy-order-system order-system rebuild

# 2. 断面（scope.md のコミット）を checkout した状態で、graphify でグラフを作る
cd legacy-order-system-spec
python3 spec-extraction/tools/atlas_graph.py build \
  --source ../legacy-order-system \
  --out spec-extraction/01-code-graph/graphify \
  --exclude '(^|/)target/'          # gate-config.env の SOURCE_EXCLUDE と同じ値

# 3. inventory.md を 2 つ目の数え方として、code-graph.md のノードを列挙する（AI エージェントの作業）

# 4. 突き合わせる
bash spec-extraction/03-audit/gates.sh graphify
```

`build` が書くもの（`spec-extraction/01-code-graph/graphify/`）:

| ファイル | 内容 | git |
|----------|------|-----|
| `graphify-out/graph.json` | graphify のグラフ（ノード・エッジ・コミュニティ） | 追跡する |
| `graphify-out/GRAPH_REPORT.md` | graphify の要約（結合の多い概念・サブシステム） | 追跡する |
| `build-info.json` | 解析した断面（コミット）・graphify の版・作成日時 | 追跡する |
| `inventory.md` | ディレクトリ別の件数・ディレクトリ間の依存・結合の多いシンボル・ソースファイル一覧 | 追跡する |
| `graphify-out/cache/`・`graph.html`・`manifest.json` | キャッシュと可視化 | 追跡しない（`.gitignore`） |

### 探索に使う

```bash
G=spec-extraction/01-code-graph/graphify/graphify-out/graph.json
graphify explain "OrderService" --graph "$G"           # つながり（呼び出し元・呼び出し先・import）
graphify path "OrderController" "OrderRepository" --graph "$G"   # 2 点間の経路
graphify query "how are orders persisted?" --graph "$G"           # 問いに関係する部分グラフ
```

結果は**どこを読むかを決める索引**です。書く前に原典を開いてください。

### `atlas_graph.py` のサブコマンド

| サブコマンド | 用途 | 終了コード |
|--------------|------|-----------|
| `build --source S --out D` | graphify でグラフを作り、`build-info.json` と `inventory.md` を書く | 0 / 2 |
| `inventory --graph G` | `inventory.md` を作り直す（`--depth N` でディレクトリの深さを指定。既定は親ディレクトリ単位） | 0 / 2 |
| `crosscheck --graph G --code-graph C` | どのノードにも属さないソースファイルを列挙する | 0 = 漏れなし / 1 = 漏れあり / 2 = 入力不備 |
| `edge-gap --graph G --code-graph C` | エッジレジストリに無いモジュール間依存を列挙する（`--strict` で漏れを exit 1） | 0 / 1 / 2 |
| `stale --graph-dir D --source S` | グラフの断面が解析対象の HEAD と一致するか | 0 / 1 / 2 |

## code-graph.md の書き方の注意

- 「パス」列は**解析対象のルートからの相対パス**で書きます。ディレクトリ（`src/main/java/shop/web/`）・ファイル・glob（`src/*/legacy/**`）が使えます。
- 1 つのセルに複数書くときは `、` か `<br>` で区切ります。
- ファイルは**最も具体的な（最長の）パス**で一致したノードに属するとみなします。
- 解析対象のルート全体を指すパス（`.`・`*`・`**/*` など）は**被覆に数えません**（`BROAD` と表示されます）。
- 点検を通すために、解析対象のルート全体のような広すぎるパスを 1 つのノードに書かないでください（数え漏れを隠すだけです。ノードの粒度は `GP-2` に従います）。

## 困ったとき

| 症状 | 対処 |
|------|------|
| `G-GRAPHIFY` が INCOMPLETE「グラフが無い」 | 上の `build` を実行する。graphify を使えない環境なら `gate-config.env` を `CODE_GRAPH_TOOL=none` にして ADR に理由を書く |
| `G-GRAPHIFY` が FAIL「UNCOVERED …」 | 示されたファイルを受け持つノードを足す。対象外なら `SOURCE_EXCLUDE` に加えて ADR を書き、`build` をやり直す |
| `G-GRAPHIFY` が FAIL「断面が違う」 | `scope.md` の断面を checkout してから `build` をやり直す |
| 生成物（`target/` など）が大量に UNCOVERED になる | `SOURCE_EXCLUDE` を設定し、`build --exclude` にも同じ値を渡す（graphify は `.gitignore` も尊重します） |
| `G-GRAPHIFY-EDGE` の WARN が多い | graphify の推定（`INFERRED`）を含みます。原典で確かめて記録するか、記録しない理由を残してください |
| Windows で `python3` が見つからない | `gates.sh` は `python3` → `python` の順に探します。`build` は `python` で実行して構いません |
| Git Bash で `--source /tmp/...` が開けない | Windows の Python は MSYS のパスを解決できません。`cygpath -w` で Windows 形式にして渡してください |
