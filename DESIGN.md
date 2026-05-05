# Daily Intelligence — 設計書

> 毎朝7時（JST）に最新ニュースをバイリンガル要約し、LINE通知と GitHub Pages レポートとして届ける個人向け自動化ツール。

---

## 目次

1. [システム概要](#1-システム概要)
2. [アーキテクチャ設計](#2-アーキテクチャ設計)
3. [データフロー](#3-データフロー)
4. [カテゴリ設定](#4-カテゴリ設定)
5. [セキュリティ設計](#5-セキュリティ設計)
6. [コスト設計](#6-コスト設計)
7. [エラーハンドリング設計](#7-エラーハンドリング設計)
8. [ファイル構成](#8-ファイル構成)
9. [運用ガイド](#9-運用ガイド)
10. [設計上のトレードオフと今後の改善余地](#10-設計上のトレードオフと今後の改善余地)

---

## 1. システム概要

### 目的

毎朝のニュースチェックを自動化し、以下を一つの体験として提供する：

- **4カテゴリ**（AI / Tech / World / Flights）の最新日本語ニュースを取得
- **英日バイリンガル要約**（各200語/字）と **英単語4語＋例文**（語彙学習）
- LINEに届く **URL1通**でスマホブラウザから即閲覧できる HTML レポート

### 4つの自動化パーツ

| パーツ | 実装 | 選定理由 |
|--------|------|---------|
| **トリガー** | GitHub Actions cron `0 22 * * *`（UTC） | サーバー不要・無料・信頼性が高い |
| **ソース元** | RSS フィード（feedparser） | API課金ゼロ・日本語メディアに最適化できる |
| **処理** | OpenAI `gpt-4.1-mini` chat.completions | 要約のみに特化し低コスト（$0.20/月） |
| **届ける先** | GitHub Pages + LINE Messaging API | HTMLリッチ表示 × LINEの即時通知 |

---

## 2. アーキテクチャ設計

### 設計上の重要な意思決定

#### なぜ RSS + OpenAI の2段階構成か

当初は OpenAI の `web_search` ツール1本で「検索＋要約」を同時に行う設計だった。しかし：

- `web_search` は1回のツール呼び出しで **$0.025〜$0.035** かかる（月 $3〜10）
- 4カテゴリの検索結果が混在したテキストを JSON に変換する際、**パースエラーが頻発**した
- 検索が英語バイアスになり、**日本語記事が得られない**問題があった

→ **RSS（無料）でニュース取得** + **OpenAI（有料）は要約だけ** に役割分離することで、月コストを **1/50 に削減**しつつ安定性を向上させた。

#### なぜ `json_object` モードか

OpenAI の `chat.completions` は通常テキストを返すが、HTML 生成には構造化データが必要。  
`response_format={"type": "json_object"}` を指定することで：

- モデルが必ず有効な JSON を返すことが保証される
- 記事リスト・英語要約・日本語要約・語彙を1回の API 呼び出しで取得できる
- 後段の HTML 生成でパースエラーが発生しない

#### なぜ GitHub Pages か

LINE Messaging API は **テキストメッセージ**しか送れない（画像・HTML 埋め込みは不可）。  
リッチな表示を実現するには外部 URL を送る必要がある。GitHub Pages は：

- 公開リポジトリであれば **完全無料**
- `docs/index.html` を push するだけで自動デプロイされる
- GitHub Actions のワークフロー内でそのまま commit & push できる

---

## 3. データフロー

```
GitHub Actions (毎朝 07:00 JST)
        │
        ▼
[1] fetch_category_rss(cfg)          ← feedparser で RSS 取得
        │  各カテゴリの RSS URL にリクエスト
        │  最新20件を取得し prefer_keywords でスコアリング
        │  上位3件（最大）を返す
        │
        ▼
[2] summarize_category(client, articles, cfg)  ← OpenAI API
        │  system_prompt に要約指示＋語彙フォーマット
        │  articles_text に記事タイトル・URL・概要を渡す
        │  json_object モードで構造化 JSON を受け取る
        │  返却: { summary_en, summary_ja, vocabulary: [{word, pos, meaning_ja, example}] }
        │
        ▼
[3] generate_html(news_data, date_str)  ← Python で HTML 生成
        │  CATEGORIES_CONFIG の順序通りに4セクションを生成
        │  Tailwind CSS CDN / sticky nav / IntersectionObserver
        │
        ▼
[4] save_html(html_content)          ← docs/index.html に書き込み
        │
        ▼
[5] GitHub Actions: git commit & push  ← GitHub Pages に自動デプロイ
        │
        ▼
[6] send_to_line([message])          ← LINE Messaging API で URL を1通送信
```

### 終了コード設計

| 状況 | exit コード | 後続の push ステップ |
|------|------------|---------------------|
| 全て正常 | 0 | 実行される |
| HTML 保存失敗 | **1** | **スキップされる**（壊れたファイルを push しない） |
| LINE 通知失敗のみ | 0 | 実行される（HTML は公開される） |

HTML 保存とプッシュを優先し、LINE 通知の失敗はプッシュをブロックしない設計。

---

## 4. カテゴリ設定

カテゴリはすべて `CATEGORIES_CONFIG`（`main.py` 冒頭）に宣言。  
コードを触らずに**設定だけで**カテゴリの追加・変更が可能。

### 各フィールドの意味

| フィールド | 用途 |
|-----------|------|
| `id` | HTML の `section[id]`、ナビアンカーに使用 |
| `name` | カードのヘッダーに表示する英語フルネーム |
| `name_short` | ナビタブ・バッジ・LINE 通知で統一して使用する短縮名 |
| `name_ja` | カードのサブタイトルに表示する日本語名 |
| `emoji` | ナビ・カードヘッダー・LINE 通知に使用 |
| `gradient` | カードヘッダーの背景グラデーション（Tailwind クラス） |
| `color` | カード全体のアクセントカラー（Tailwind カラー名） |
| `rss_feeds` | RSS URL のリスト。先頭から順に取得し候補をマージ |
| `prefer_keywords` | スコアリング用キーワード。記事タイトル＋概要に含まれる数でソート |
| `summary_focus` | OpenAI プロンプトに埋め込む要約の方向性を指定する英語の説明 |
| `disclaimer` | 要約の末尾に付加する免責文（任意） |

### 現在のカテゴリと RSS ソース

| カテゴリ | name_short | RSS ソース |
|---------|-----------|-----------|
| 🤖 AI & Machine Learning | AI | ITmedia AI+、ITmedia（フォールバック） |
| 💻 Technology | Tech | ASCII.jp、ITmedia |
| 🌍 World Politics & Economy | World | NHK 総合ニュース |
| ✈️ Airline Deals & Travel | Flights | TRAICY（メイン） |

### RSS の記事選定ロジック

```
1. 各 RSS URL から最新 20 件を取得
2. prefer_keywords に含まれるキーワードが
   タイトル＋概要に何件含まれるかでスコアリング
3. URL 重複を除去
4. 上位3件を採用（記事が少なければ1〜2件でも可）
```

---

## 5. セキュリティ設計

### APIキーの管理

| キー | 保管場所 | スクリプトへの渡し方 |
|------|---------|---------------------|
| `OPENAI_API_KEY` | GitHub Secrets | Actions の `env:` 経由で環境変数 |
| `LINE_ACCESS_TOKEN` | GitHub Secrets | 同上 |
| `LINE_USER_ID` | GitHub Secrets | 同上 |

**絶対に `main.py` やその他のファイルに直接書かない。**

### ログのマスキング（`safe_error()` 関数）

例外メッセージにシークレットが漏れないよう、2段階でマスクする：

1. **完全一致**：環境変数の実値が文字列に含まれている場合に `***` で置換
2. **パターンマッチ**：完全一致で取れなかった場合のフォールネット
   - `sk-[A-Za-z0-9_\-]{10,}` → OpenAI キー形式（`sk-proj-...` も含む）
   - `Bearer\s+[A-Za-z0-9._\-]{10,}` → HTTP Bearer トークン形式

### GitHub Actions のセキュリティ設定

- **SHA ピン留め**：使用する Actions を可変タグ（`@v4`）ではなくコミット SHA で固定
  ```yaml
  actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
  ```
- **最小権限**：ワークフロー全体は `permissions: {}` で全権限を無効化し、
  ジョブレベルで `contents: write` のみ付与
- **タイムアウト**：`timeout-minutes: 10` で無限ループを防止

### OpenAI アカウント設定

- **Project API Key** を使用（Organization Key より権限が限定される）
- **月次スペンディング上限**: $5 に設定
- **アラート**: 上限の 80% に達した時点でメール通知

---

## 6. コスト設計

### 月次ランニングコスト（30日）

| コンポーネント | 単価 | 月額 |
|--------------|------|------|
| RSS 取得 | $0 | **$0.00** |
| GitHub Actions | 公開リポジトリ無料 | **$0.00** |
| GitHub Pages | 公開リポジトリ無料 | **$0.00** |
| LINE Messaging API | 無料枠 200通/月 | **$0.00** |
| OpenAI `gpt-4.1-mini` | 下記参照 | **~$0.20** |

### OpenAI トークンコスト詳細

モデル `gpt-4.1-mini` の料金（2025年時点）：

- 入力：$0.40 / 1M tokens
- 出力：$1.60 / 1M tokens

1回あたりの推定消費トークン（4カテゴリ合計）：

| | トークン数 | コスト |
|--|-----------|--------|
| 入力（記事テキスト×4 ＋ プロンプト×4） | ~3,200 | $0.0013 |
| 出力（要約×4 ＋ 語彙×16 ＋ 例文×16） | ~3,000 | $0.0048 |
| **1回合計** | | **~$0.006** |

**月30日：$0.006 × 30 ≒ $0.18〜$0.22 / 月（約 30 円）**

> スペンディング上限 $5 の 4〜5% 程度。上限に達する心配はほぼない。

---

## 7. エラーハンドリング設計

### 各レイヤーの障害と対応

| 障害箇所 | 対応 | ユーザーへの影響 |
|---------|------|----------------|
| RSS 1件取得失敗 | `[WARN]` を出力して次の URL に進む | そのフィードの記事が減る（他は正常） |
| RSS 全件取得失敗 | 空リストを返す → OpenAI に渡さずプレースホルダー文字列を使用 | カテゴリの記事数が0になるが HTML は生成される |
| OpenAI 呼び出し失敗（1カテゴリ） | `[ERROR]` を出力、**そのカテゴリのみ**フォールバック文字列 | 失敗カテゴリに「処理エラー」と表示されるが他カテゴリは正常 |
| OpenAI 呼び出し失敗（全カテゴリ） | 全カテゴリがフォールバック文字列 | HTML には全カテゴリが「取得失敗」として表示される |
| HTML 生成・保存失敗 | `exit(1)` でワークフローを中断 | push がスキップされ、既存の GitHub Pages は変更されない |
| LINE 通知失敗 | `[WARNING]` を出力、`exit(0)` で継続 | HTML は push・公開される。LINE 通知だけが届かない |

### `feedparser.bozo` フラグ

RSS フィードが正常にパースできなかった場合（不正な XML、証明書エラー等）、  
feedparser は `bozo=True` をセットして結果を返す（例外を投げない）。  
`bozo` を確認してログに出すことで、**空結果と本物のエラーを区別**できる。

---

## 8. ファイル構成

```
daily-report/
├── main.py                          # メインスクリプト（全処理）
├── requirements.txt                 # Python 依存パッケージ（バージョン固定）
├── DESIGN.md                        # 本設計書
├── .github/
│   └── workflows/
│       └── daily_report.yml         # GitHub Actions ワークフロー
├── docs/
│   ├── .gitkeep                     # docs/ ディレクトリを Git に追跡させるため
│   └── index.html                   # 生成済み HTML（GitHub Actions が自動更新）
├── output/                          # デザイン参考用サンプル HTML（Git 管理外）
│   ├── sample_report.v4.html
│   └── ...
└── .gitignore
```

### `docs/index.html` について

- 手動で編集しない（毎朝 GitHub Actions が上書きする）
- GitHub Pages の公開ソースとして使用（`main` ブランチの `docs/` フォルダ）
- 公開 URL：`https://sakuuuuuuuu.github.io/daily-report/`

### `requirements.txt` のバージョン固定方針

```
openai==1.66.0
line-bot-sdk==3.13.0
pytz==2024.2
feedparser==6.0.12
```

意図的に `==` で固定している。`>=` にするとマイナーバージョンアップで  
API の破壊的変更を取り込むリスクがある。更新する際は動作確認を行ってから変更すること。

---

## 9. 運用ガイド

### 手動実行（テスト）

GitHub Actions の UI から **"Run workflow"** ボタンで即時実行できる。  
`workflow_dispatch:` が設定されているため、スケジュール待ちなしにテスト可能。

### GitHub Actions ログの確認

リポジトリ → Actions タブ → 最新のワークフロー実行 → `send-report` ジョブ  
各ステップの標準出力に `[INFO]` / `[WARN]` / `ERROR` が出力される。

### RSSフィード URL の変更方法

`main.py` の `CATEGORIES_CONFIG` 内の `rss_feeds` リストを編集する：

```python
"rss_feeds": [
    "https://新しいフィードのURL",  # 変更・追加したいフィード
    "https://既存のフォールバックURL",
],
```

変更後は `git push` すれば翌朝から反映される。

### カテゴリの追加方法

`CATEGORIES_CONFIG` に以下の形式でエントリを追加するだけ：

```python
{
    "id": "一意のID（英小文字）",
    "emoji": "絵文字",
    "name": "英語カテゴリ名",
    "name_short": "短縮名（ナビ表示用）",
    "name_ja": "日本語カテゴリ名",
    "gradient": "from-色-600 to-色-500",  # Tailwind グラデーション
    "color": "色名",                         # Tailwind カラー名
    "header_text": "色名-100",
    "rss_feeds": ["https://フィードURL"],
    "prefer_keywords": ["キーワード1", "キーワード2"],
    "summary_focus": "OpenAIへの要約方向性の指示（英語）",
    "disclaimer": "",  # 不要な場合は空文字
},
```

---

## 10. 設計上のトレードオフと今後の改善余地

### 現在の制約（意図的なトレードオフ）

| 制約 | 理由 |
|------|------|
| RSS フィードの URL はコードに埋め込み | シンプルさ優先。変更頻度が低いため環境変数化しない |
| OpenAI 呼び出しは4カテゴリを逐次処理 | 並列化すると実装が複雑になる。10秒程度の差でありトレードオフに値しない |
| HTML テンプレートは f-string | Jinja2 等の導入はオーバーエンジニアリング。テンプレートの変更頻度が低い |
| `socket.setdefaulttimeout(15)` はグローバル | feedparser 専用にできれば理想。OpenAI/LINE SDK への副作用は許容範囲として残置 |

### 将来の改善候補

- **記事への日付表示**：RSS の `published_parsed` を使えば「何時間前の記事か」が出せる
- **過去レポートの保存**：`docs/2026-05-01/index.html` のように日付別に保管するとアーカイブになる
- **Vocabulary まとめページ**：ページ下部に全カテゴリの語彙をまとめた「今日の語彙集」セクションを追加
- **依存パッケージのロックファイル**：`pip-compile` で間接依存も含めた完全なロックファイルを生成し、再現性をさらに高める
