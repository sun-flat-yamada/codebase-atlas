# 開発の進め方（codebase-atlas 自体の変更）

この文書は、codebase-atlas 自体を変更するときの流れ（ブランチ・品質ゲート・PR・マージ）をまとめたものです。
解析対象の仕様書を作る作業の流れは [how-it-works.md](how-it-works.md) を参照してください。

Auto-Pilot（自動マージ）の仕組みは、github-copilot-dashboard の change-dev Auto-Pilot
（`scripts/change-dev-autopilot.ts` / `scripts/change-dev-branch.ts`）を、このリポジトリに合わせて
Python の標準ライブラリで移植したもの（[scripts/automerge.py](../scripts/automerge.py)）です。

## 1. ブランチを作る

ブランチ名は変更を表す `<type>/<issue>-<slug>`（Issue が無ければ `<type>/<slug>`）にします。

```bash
git fetch origin main
git switch -c "$(python3 scripts/automerge.py branch name feat 42 "Add graphify edge gate")" origin/main
# → feat/42-add-graphify-edge-gate
python3 scripts/automerge.py branch check          # 今のブランチ名を検査する
```

- `type`: `feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci` `chore` `revert`
- slug は英小文字・数字・ハイフン（40 文字以内）。ブランチ名全体は 60 文字以内。
- Claude Code のクラウドセッションが付ける既定名（`claude/<形容詞>-<名前>-<id>`）のまま push しないでください。
  PR の `Branch Name Check` が不合格にします（`main`・`dependabot/*` は対象外）。

## 2. 品質ゲート（PR の前に必ず）

```bash
python3 -m unittest discover -s tests        # atlas_graph.py / automerge.py の単体テスト
ruff check tools scripts tests && ruff format --check tools scripts tests
bash selftest.sh                             # 「結果: PASS <n> / FAIL 0」で終わること（10〜30 分）
```

graphify の結合テスト（graphify が要る）:

```bash
bash scripts/ci-graphify-e2e.sh
```

CI（`.github/workflows/ci.yml`）も同じものを実行します。

## 3. PR を作る

- コミットは Conventional Commits（`feat(gates): …`）。`main` に rebase した状態で push します。
- PR の本文は [PULL_REQUEST_TEMPLATE.md](../.github/PULL_REQUEST_TEMPLATE.md) に沿って書きます。
- Auto-Pilot が有効なら PR は **ready for review** で、無効なら **draft** で作ります。

## 4. マージする（Auto-Pilot）

Auto-Pilot は `CHG_DEV_AUTO_PILOT` で決まります。**このリポジトリの既定は有効**（[.env.example](../.env.example) で `true`）です。

| 解決順 | 場所 |
|--------|------|
| 1 | プロセスの環境変数（クラウドセッションでは環境の変数設定） |
| 2 | `.env`（git 管理外。手元で上書きする） |
| 3 | `.env.example`（リポジトリの既定） |

`true` / `1` なら有効、それ以外・未設定なら手動です。今の解決結果は次で確かめられます。

```bash
python3 scripts/automerge.py mode
```

PR を作ったら、次を実行します。

```bash
python3 scripts/automerge.py finish <PR 番号> --wait     # ローカル: 30 秒ごとに CI を確認（最大 30 分）
python3 scripts/automerge.py finish <PR 番号> --dry-run  # 何をするかだけ表示
```

`finish` が行うこと:

1. **ブランチ名の検査**（`<type>/<issue>-<slug>`）。
2. **ready for review**: draft なら解除する（クラウドでは REST の専用ルート、ローカルでは `gh pr ready`）。
3. **CI 待ち**: PR の head のすべての check run が完了するまで待つ。失敗が 1 つでもあれば止まる（直して push し直す）。
   push 直後で check run が 1 つも無いときは「まだ始まっていない」として待つ（通過とはみなさない）。
   `main` が先に進んで PR が遅れている（`behind`）ときは、ブランチを最新化（update-branch）してから CI を待ち直す。
   変更要求（Request changes）を出したレビュアーがいる、または未解決のレビュースレッドがあるときは止まる
   （スレッドの確認は GraphQL を使うので、GraphQL が拒否されるクラウドセッションでは確認できない旨を表示する。
   その場合はエージェントがスレッドに答えてから `finish` を実行する）。
   クラウドセッションでは `--wait` を使わず、CI 完了のイベントで起こされたら再実行する（終了コード 2 = まだ実行中）。
4. **承認**: 実行しているアカウントで承認する。PR の作成者自身の承認は GitHub が 422 で拒否するので、
   その場合は `main` が要求する承認数が 0 のときだけ承認なしで進む（1 以上なら止まって報告する）。
5. **Rebase & Merge**: CI を通った head の SHA を指定してマージする（確認後に push されたコミットはマージされない）。
6. **後片付け**: マージ済みのヘッドブランチを削除する。

終了コード: `0` = マージした（`--dry-run` ではマージできる）／ `1` = 止まった（理由を表示）／ `2` = CI がまだ実行中。

### 緩めない決まり

- `--admin` を使わない。ブランチ保護やルールセットを迂回しない。`main` に直接 push しない。
- 失敗・実行中の check、コンフリクト、未回答のレビューがある PR はマージしない。
- **フォークからの PR は扱わない**（`finish` は止まる）。エージェントが書いたコードではなく、ヘッドブランチの削除が
  origin の同名ブランチを消しうるため。フォークの PR は人がレビューしてマージする。
- 承認を出せない・rebase のコンフリクトが単純でない・直しても CI が赤いままのときは、止まって人に報告する。

### Auto-Pilot を止めるには

`.env` に `CHG_DEV_AUTO_PILOT=false` を書く（クラウドセッションでは環境の変数設定に書く）。
`finish` は `--force` を付けない限り実行されなくなり、PR は draft で作って人がマージします。

## 5. リポジトリの設定（管理者）

Auto-Pilot は次の設定を前提にしています。

| 設定 | 値 | 理由 |
|------|----|------|
| Allow rebase merging | 有効 | `finish` は `rebase` でマージする |
| Automatically delete head branches | 有効 | クラウドのプロキシがブランチ削除を拒否しても残らない |
| `main` のブランチ保護 | 必須 check に CI を指定。承認数 0 | 作成者は自分の PR を承認できないため |
| Private vulnerability reporting | 有効 | [SECURITY.md](../SECURITY.md) の報告窓口 |
