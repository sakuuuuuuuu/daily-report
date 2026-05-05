"""
Daily Intelligence — LINE報告自動化ツール + GitHub Pages

設計方針：
  ニュース取得を RSS フィード（無料）に切り替え、OpenAI はテキスト要約のみに使う。
  web_search ツールを廃止することでランニングコストを月 $3〜10 → 約 $0.20 に削減する。

フロー：
  1. fetch_category_rss()  → feedparser で RSS を取得（外部API費用ゼロ）
  2. summarize_category()  → chat.completions (json_object) で要約・訳・語彙を生成
  3. generate_html()       → JSON から HTML を生成（記事URL はクリッカブルリンク）
  4. save_html()           → docs/index.html に保存
  5. send_to_line()        → GitHub Pages の URL を LINE に送信

コスト試算（月30日）:
  RSS 取得       : $0.00（HTTP リクエスト）
  OpenAI トークン: ~$0.005/回 × 30日 ≒ $0.15/月
  LINE API       : 無料枠内（200通/月）
  GitHub Actions : 公開リポジトリ無料
  合計           : 約 $0.15〜$0.20/月
"""

import html as _html
import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path

import feedparser
import pytz
from openai import OpenAI
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

# feedparser の HTTP タイムアウト（秒）
socket.setdefaulttimeout(15)

# ── 定数 ──────────────────────────────────────────────────────────────────────

GITHUB_PAGES_URL = "https://sakuuuuuuuu.github.io/daily-report/"
HTML_OUTPUT_PATH = Path("docs/index.html")
MAX_CHARS = 4000

CATEGORIES_CONFIG = [
    {
        "id": "ai",
        "emoji": "🤖",
        "name": "AI & Machine Learning",
        "name_short": "AI",          # ナビ・LINE・バッジで使用する短縮名
        "name_ja": "人工知能・機械学習",
        "gradient": "from-blue-600 to-cyan-500",
        "color": "blue",
        "header_text": "blue-100",
        # RSS
        "rss_feeds": [
            "https://rss.itmedia.co.jp/rss/2.0/aiplus.xml",       # ITmedia AI+
            "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",  # ITmedia（フォールバック）
        ],
        "prefer_keywords": [
            "AI", "人工知能", "機械学習", "生成AI", "LLM",
            "GPT", "Claude", "Gemini", "ChatGPT", "深層学習",
        ],
        "summary_focus": "AI and machine learning: model releases, research breakthroughs, and real-world applications",
        "disclaimer": "",
    },
    {
        "id": "tech",
        "emoji": "💻",
        "name": "Technology",
        "name_short": "Tech",
        "name_ja": "テクノロジー",
        "gradient": "from-purple-600 to-violet-500",
        "color": "purple",
        "header_text": "purple-100",
        # RSS
        "rss_feeds": [
            "https://ascii.jp/rss.xml",                            # ASCII.jp
            "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",  # ITmedia
        ],
        "prefer_keywords": [
            "スマートフォン", "半導体", "ゲーム", "PC", "クラウド",
            "セキュリティ", "スタートアップ", "ロボット", "EV", "量子",
        ],
        "summary_focus": "technology excluding AI: hardware, software, semiconductors, and startups",
        "disclaimer": "",
    },
    {
        "id": "world",
        "emoji": "🌍",
        "name": "World Politics & Economy",
        "name_short": "World",
        "name_ja": "国際政治・経済",
        "gradient": "from-emerald-600 to-teal-500",
        "color": "emerald",
        "header_text": "emerald-100",
        # RSS
        "rss_feeds": [
            "https://www3.nhk.or.jp/rss/news/cat0.xml",  # NHK 総合ニュース
        ],
        "prefer_keywords": [
            "国際", "外交", "経済", "貿易", "関税", "制裁",
            "米国", "中国", "EU", "ロシア", "G7", "G20",
            "大統領", "首相", "外相", "サミット",
        ],
        "summary_focus": "world politics and international economics: diplomacy, trade, geopolitics",
        "disclaimer": "",
    },
    {
        "id": "flights",
        "emoji": "✈️",
        "name": "Airline Deals & Travel",
        "name_short": "Flights",
        "name_ja": "航空券セール・旅行",
        "gradient": "from-amber-500 to-orange-500",
        "color": "amber",
        "header_text": "amber-100",
        # RSS（TRAICY をメインに使用）
        "rss_feeds": [
            "https://www.traicy.com/feed",  # TRAICY（メイン）
        ],
        "prefer_keywords": [
            "セール", "特別運賃", "LCC", "割引", "キャンペーン",
            "JAL", "ANA", "ジェットスター", "ピーチ", "新路線",
        ],
        "summary_focus": "airline deals, flight sales, and travel news relevant to Japan",
        "disclaimer": "※情報の正確性は各社公式サイトでご確認ください",
    },
]


# ── ユーティリティ ─────────────────────────────────────────────────────────────

def safe_error(e: Exception) -> str:
    """例外メッセージからAPIキー等の機密情報をマスクして返す。

    完全一致だけでなく、共通のAPIキー形式（sk-...）や
    Bearer トークン形式もパターンマッチでマスクする。
    """
    msg = str(e)
    # 環境変数の実値をそのまま含む場合は完全置換
    for key_name in ["OPENAI_API_KEY", "LINE_ACCESS_TOKEN", "LINE_USER_ID"]:
        val = os.environ.get(key_name, "")
        if val and val in msg:
            msg = msg.replace(val, "***")
    # OpenAI キー形式（sk-... / sk-proj-...）をパターンマッチでマスク
    msg = re.sub(r"sk-[A-Za-z0-9_\-]{10,}", "***", msg)
    # Bearer トークン形式をマスク
    msg = re.sub(r"Bearer\s+[A-Za-z0-9._\-]{10,}", "Bearer ***", msg)
    return msg


def truncate(text: str, max_chars: int = MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 10] + "\n...(省略)"


def _strip_html(text: str) -> str:
    """RSS エントリの description などに含まれる HTML タグを除去する。"""
    text = _html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── RSS 取得 ───────────────────────────────────────────────────────────────────

def fetch_category_rss(cfg: dict) -> list[dict]:
    """
    カテゴリの RSS フィードから記事を取得する（外部 API 費用ゼロ）。
    複数フィードをフェッチし、prefer_keywords でスコアリングして上位を返す。
    """
    all_entries: list[dict] = []

    for feed_url in cfg["rss_feeds"]:
        try:
            feed = feedparser.parse(
                feed_url,
                agent="DailyIntelligence/1.0 (+https://github.com/sakuuuuuuuu/daily-report)",
            )
            # bozo は feedparser がフィードを正常にパースできなかった場合に True になる
            if getattr(feed, "bozo", False):
                print(f"  [WARN] RSS parse warning [{feed_url}]: {feed.bozo_exception}")

            source_name = feed.feed.get("title", cfg["name"])

            for entry in feed.entries[:20]:  # 最新20件を候補にする
                title = _strip_html(entry.get("title", ""))
                desc  = _strip_html(
                    entry.get("summary") or entry.get("description") or ""
                )[:500]
                url = entry.get("link", "")

                if title and url:
                    all_entries.append({
                        "title":       title,
                        "source":      source_name,
                        "url":         url,
                        "description": desc,
                    })

        except Exception as e:
            print(f"  [WARN] RSS fetch failed [{feed_url}]: {e}")

    if not all_entries:
        return []

    # prefer_keywords でスコアリング（マッチ数が多い順）
    keywords = [kw.lower() for kw in cfg.get("prefer_keywords", [])]
    if keywords:
        def score(art: dict) -> int:
            haystack = (art["title"] + " " + art["description"]).lower()
            return sum(1 for kw in keywords if kw in haystack)
        all_entries.sort(key=score, reverse=True)

    # 重複 URL を除去
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in all_entries:
        if entry["url"] not in seen:
            seen.add(entry["url"])
            unique.append(entry)

    return unique[:3]  # 最大3件


# ── OpenAI 要約 ────────────────────────────────────────────────────────────────

def summarize_category(client: OpenAI, articles: list[dict], cfg: dict) -> dict:
    """
    RSS から取得した記事テキストを OpenAI で要約する。
    web_search を使わず chat.completions のみ → トークン代のみ（月 ~$0.15 相当）。
    例外は呼び出し元（fetch_news）でカテゴリ単位にハンドリングする。
    """
    if not articles:
        return {
            "summary_en": "No articles were found for this category today.",
            "summary_ja": "本日はこのカテゴリの記事が見つかりませんでした。",
            "vocabulary": [],
        }

    disclaimer_note = (
        f'\n- End summary_ja with this sentence: 「{cfg["disclaimer"]}」'
        if cfg.get("disclaimer") else ""
    )

    system_prompt = f"""\
You are a bilingual Japanese/English news analyst.
Summarize the {cfg['name']} ({cfg['summary_focus']}) articles below.{disclaimer_note}

Return ONLY a valid JSON object (no markdown fences, no other text):
{{
  "summary_en": "Approximately 200-word English summary covering all articles",
  "summary_ja": "Natural Japanese translation of summary_en (~200字)",
  "vocabulary": [
    {{"word": "English word", "pos": "n.", "meaning_ja": "日本語の意味", "example": "A short sentence using this word from the article context."}},
    {{"word": "English word", "pos": "v.", "meaning_ja": "日本語の意味", "example": "A short sentence using this word from the article context."}},
    {{"word": "English word", "pos": "adj.", "meaning_ja": "日本語の意味", "example": "A short sentence using this word from the article context."}},
    {{"word": "English word", "pos": "n.", "meaning_ja": "日本語の意味", "example": "A short sentence using this word from the article context."}}
  ]
}}
vocabulary: 4 TOEIC B1+ level English words chosen from summary_en. Each "example" must be a natural English sentence (max 20 words) that shows the word in context."""

    articles_text = "\n\n".join(
        f"[{i}] {art['title']}\nSource: {art['source']}\nURL: {art['url']}\n{art['description']}"
        for i, art in enumerate(articles, 1)
    )

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": articles_text},
        ],
        max_tokens=1400,
        timeout=60,
    )

    return json.loads(resp.choices[0].message.content)


# ── ニュース取得（統合） ────────────────────────────────────────────────────────

def fetch_news(client: OpenAI) -> dict:
    """
    全カテゴリの RSS を取得し、OpenAI で要約して dict を返す。
    1カテゴリで summarize_category が失敗しても他カテゴリには影響しない。
    """
    categories = []
    for cfg in CATEGORIES_CONFIG:
        print(f"  [{cfg['id']}] RSS 取得中...")
        articles = fetch_category_rss(cfg)
        print(f"  [{cfg['id']}] {len(articles)} 件取得 → 要約中...")

        try:
            summary = summarize_category(client, articles, cfg)
        except Exception as e:
            # 1カテゴリの失敗が全体に波及しないようフォールバック
            print(f"  [{cfg['id']}] ERROR [summarize_category]: {safe_error(e)}")
            summary = {
                "summary_en": "Summary unavailable due to a processing error.",
                "summary_ja": "処理エラーのため要約を取得できませんでした。",
                "vocabulary": [],
            }

        # description は HTML 生成に不要なので article から除去して格納
        clean_articles = [
            {"title": a["title"], "source": a["source"], "url": a["url"]}
            for a in articles
        ]

        categories.append({
            "id":         cfg["id"],
            "articles":   clean_articles,
            "summary_en": summary.get("summary_en", ""),
            "summary_ja": summary.get("summary_ja", ""),
            "vocabulary": summary.get("vocabulary", []),
        })
        print(f"  [{cfg['id']}] 完了")

    return {"categories": categories}


# ── HTML生成 ───────────────────────────────────────────────────────────────────

def _vocab_item_html(v: dict, c: str, esc) -> str:
    """語彙1件分のHTMLを生成する。example がある場合は折りたたみ形式。"""
    word    = esc(v.get("word", ""))
    pos     = esc(v.get("pos", ""))
    meaning = esc(v.get("meaning_ja", ""))
    example = v.get("example", "").strip()

    header_inner = (
        f'<span class="font-bold text-sm text-slate-800">{word}</span>'
        f'<span class="text-xs font-semibold text-{c}-600 bg-{c}-50 border border-{c}-200 '
        f'px-1.5 py-0.5 rounded">{pos}</span>'
        f'<span class="text-xs text-slate-600 flex-1">{meaning}</span>'
    )

    if example:
        return (
            f'<details class="border-b border-{c}-100 last:border-b-0 group/vocab">'
            f'<summary class="flex items-center gap-2 px-4 py-3 cursor-pointer list-none '
            f'hover:bg-{c}-50/60 transition-colors">'
            f'{header_inner}'
            f'<span class="vocab-chevron text-{c}-300 text-xs shrink-0 transition-transform '
            f'duration-200">▼</span>'
            f'</summary>'
            f'<div class="px-4 pb-3 pt-0 ml-4 border-l-2 border-{c}-200">'
            f'<p class="text-xs text-slate-500 italic">&ldquo;{esc(example)}&rdquo;</p>'
            f'</div>'
            f'</details>'
        )
    else:
        return (
            f'<div class="flex items-center gap-2 px-4 py-3 border-b border-{c}-100 '
            f'last:border-b-0 bg-white">'
            f'{header_inner}'
            f'</div>'
        )


def _category_section_html(cat_data: dict, cfg: dict) -> str:
    """JSON の1カテゴリ分のデータから <section> HTML を生成する。"""
    esc = _html.escape
    c = cfg["color"]

    articles   = cat_data.get("articles", [])
    summary_en = cat_data.get("summary_en", "")
    summary_ja = cat_data.get("summary_ja", "")
    vocabulary = cat_data.get("vocabulary", [])

    # 記事リスト（最大3件、URLはクリッカブルリンク）
    articles_html = ""
    for i, art in enumerate(articles[:3], 1):
        url    = art.get("url") or "#"
        title  = art.get("title", "（タイトル不明）")
        source = art.get("source", "")
        source_badge = (
            f'<span class="text-xs text-{c}-700 bg-{c}-50 border border-{c}-100 '
            f'px-2 py-0.5 rounded-full font-medium leading-none mt-1.5 inline-block">'
            f'{esc(source)}</span>'
        ) if source else ""
        articles_html += f"""
          <a href="{esc(url)}" target="_blank" rel="noopener noreferrer"
             class="article-link flex items-start gap-3 px-4 py-4 border-b border-slate-100 hover:bg-{c}-50 group">
            <span class="flex-none w-6 h-6 rounded-full bg-{c}-100 text-{c}-700 text-xs font-bold flex items-center justify-center shrink-0 mt-0.5">{i}</span>
            <div class="flex-1 min-w-0">
              <p class="text-sm font-semibold text-slate-800 group-hover:text-{c}-600 transition-colors leading-snug line-clamp-2">{esc(title)}</p>
              {source_badge}
            </div>
            <span class="text-slate-300 group-hover:text-{c}-400 text-lg mt-0.5 shrink-0 transition-colors">›</span>
          </a>"""

    en_paras = "".join(
        f'<p class="text-sm text-slate-700 leading-relaxed mt-3">{esc(p)}</p>'
        for p in summary_en.split("\n") if p.strip()
    )
    ja_paras = "".join(
        f'<p class="text-sm text-slate-700 leading-relaxed mt-3">{esc(p)}</p>'
        for p in summary_ja.split("\n") if p.strip()
    )

    vocab_items = "".join(_vocab_item_html(v, c, esc) for v in vocabulary)
    vocab_section = f"""
          <details class="mt-5 rounded-xl overflow-hidden border border-{c}-200">
            <summary class="flex items-center justify-between px-4 py-3 bg-{c}-100/70 cursor-pointer select-none list-none">
              <div class="flex items-center gap-2">
                <span class="text-sm">📖</span>
                <span class="text-xs font-bold uppercase tracking-wider text-{c}-700">Vocabulary</span>
                <span class="text-xs text-{c}-500 font-normal">— タップで例文を表示</span>
              </div>
              <span class="chevron text-{c}-400 text-xs">▼</span>
            </summary>
            <div class="divide-y divide-{c}-100 bg-white">{vocab_items}</div>
          </details>""" if vocab_items else ""

    article_count = len(articles[:3])
    return f"""
    <section id="{cfg['id']}" class="card">
      <div class="rounded-2xl overflow-hidden shadow-md">

        <div class="bg-gradient-to-r {cfg['gradient']} px-5 py-4">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <span class="text-xl">{cfg['emoji']}</span>
              <div>
                <h2 class="text-white font-bold text-base leading-tight">{esc(cfg['name'])}</h2>
                <p class="text-{cfg['header_text']} text-xs mt-0.5">{esc(cfg['name_ja'])}</p>
              </div>
            </div>
            <span class="bg-white/20 text-white text-xs font-semibold px-2.5 py-1 rounded-full">{article_count} article{"s" if article_count != 1 else ""}</span>
          </div>
        </div>

        <div class="bg-white divide-y divide-slate-100">{articles_html}</div>

        <div class="bg-{c}-50 border-t-2 border-{c}-100 px-5 py-5">
          <div class="flex items-center gap-2 mb-3">
            <span class="text-base">🇬🇧</span>
            <span class="text-xs font-bold uppercase tracking-wider text-{c}-600">English Summary</span>
          </div>
          <div class="mb-5">{en_paras}</div>
          <div class="flex items-center gap-3 my-4">
            <div class="flex-1 h-px bg-{c}-200"></div>
            <span class="text-xs text-{c}-400 font-semibold">🇯🇵 日本語訳</span>
            <div class="flex-1 h-px bg-{c}-200"></div>
          </div>
          <div>{ja_paras}</div>
          {vocab_section}
        </div>

      </div>
    </section>"""


def generate_html(news_data: dict | None, date_str: str) -> str:
    """JSON データから完全な HTML レポートを生成する。"""
    esc = _html.escape

    if news_data:
        cat_by_id = {c["id"]: c for c in news_data.get("categories", [])}
        sections_html = "\n".join(
            _category_section_html(cat_by_id.get(cfg["id"], {}), cfg)
            for cfg in CATEGORIES_CONFIG
        )
        article_count = sum(
            len(cat_by_id.get(cfg["id"], {}).get("articles", []))
            for cfg in CATEGORIES_CONFIG
        )
    else:
        sections_html = """
        <div class="bg-white rounded-2xl shadow-md p-6 text-center">
          <p class="text-slate-500 text-sm">本日のレポートの取得に失敗しました。しばらくしてから再度お試しください。</p>
        </div>"""
        article_count = 0

    # name_short を使って表記を統一（ナビ・バッジ・LINE通知すべて同じ短縮名）
    nav_tabs = "".join(
        f'<a href="#{cfg["id"]}" data-section="{cfg["id"]}" data-color="{cfg["color"]}"'
        f' class="nav-tab flex items-center gap-1.5 px-4 py-3.5 text-xs font-medium'
        f' text-slate-500 border-b-2 border-transparent whitespace-nowrap transition-all'
        f' hover:text-{cfg["color"]}-600">'
        f'<span>{cfg["emoji"]}</span> {esc(cfg["name_short"])}</a>'
        for cfg in CATEGORIES_CONFIG
    )
    hero_badges = "".join(
        f'<a href="#{cfg["id"]}" class="inline-flex items-center gap-1 text-xs'
        f' bg-{cfg["color"]}-500/15 text-{cfg["color"]}-300'
        f' border border-{cfg["color"]}-500/25 px-2.5 py-1 rounded-full'
        f' hover:bg-{cfg["color"]}-500/25 transition-colors">'
        f'{cfg["emoji"]} {esc(cfg["name_short"])}</a>'
        for cfg in CATEGORIES_CONFIG
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Daily Intelligence &mdash; {esc(date_str)}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    :root {{ --nav-h: 48px; }}
    html {{ scroll-behavior: smooth; scroll-padding-top: var(--nav-h); }}
    .line-clamp-2 {{
      display: -webkit-box; -webkit-line-clamp: 2;
      -webkit-box-orient: vertical; overflow: hidden;
    }}
    .no-scrollbar::-webkit-scrollbar {{ display: none; }}
    .no-scrollbar {{ -ms-overflow-style: none; scrollbar-width: none; }}
    @keyframes fadeUp {{
      from {{ opacity: 0; transform: translateY(20px); }}
      to   {{ opacity: 1; transform: translateY(0); }}
    }}
    .card {{ animation: fadeUp 0.5s ease both; }}
    .card:nth-child(1) {{ animation-delay: 0.05s; }}
    .card:nth-child(2) {{ animation-delay: 0.15s; }}
    .card:nth-child(3) {{ animation-delay: 0.25s; }}
    .card:nth-child(4) {{ animation-delay: 0.35s; }}
    .article-link {{ transition: background-color 0.15s ease, transform 0.15s ease; }}
    .article-link:active {{ transform: scale(0.98); }}
    details > summary {{ list-style: none; }}
    details > summary::-webkit-details-marker {{ display: none; }}
    .chevron {{ transition: transform 0.2s ease; }}
    details[open] > summary .chevron {{ transform: rotate(180deg); }}
    .vocab-chevron {{ transition: transform 0.2s ease; }}
    details[open] > summary .vocab-chevron {{ transform: rotate(180deg); }}
    @media (prefers-reduced-motion: reduce) {{
      .card {{ animation: none; }}
      .article-link {{ transition: none; }}
      .chevron, .vocab-chevron {{ transition: none; }}
    }}
  </style>
</head>
<body class="bg-slate-100 text-slate-800 antialiased">

  <nav class="sticky top-0 z-50 bg-white/95 backdrop-blur-md border-b border-slate-200 shadow-sm">
    <div class="max-w-2xl mx-auto overflow-x-auto no-scrollbar">
      <div class="flex min-w-max" id="nav-tabs">{nav_tabs}</div>
    </div>
  </nav>

  <header class="bg-gradient-to-br from-slate-900 via-slate-800 to-slate-900 text-white">
    <div class="max-w-2xl mx-auto px-4 pt-8 pb-7">
      <div class="flex items-start justify-between mb-4">
        <div>
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-slate-500 mb-1.5">Daily Intelligence Report</p>
          <h1 class="text-2xl font-extrabold leading-tight">Today's Briefing</h1>
          <p class="text-slate-400 text-sm mt-1">{esc(date_str)}</p>
        </div>
        <div class="inline-flex items-center gap-1.5 bg-green-500/15 border border-green-500/25 text-green-300 text-xs font-medium px-2.5 py-1.5 rounded-full">
          <span class="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse"></span>
          Updated
        </div>
      </div>
      <div class="flex flex-wrap gap-2 mb-6">{hero_badges}</div>
      <div class="grid grid-cols-4 bg-white/5 border border-white/10 rounded-xl overflow-hidden">
        <div class="text-center py-3 border-r border-white/10"><p class="text-lg font-bold">{article_count}</p><p class="text-xs text-slate-500 mt-0.5">記事数</p></div>
        <div class="text-center py-3 border-r border-white/10"><p class="text-lg font-bold">4</p><p class="text-xs text-slate-500 mt-0.5">カテゴリ</p></div>
        <div class="text-center py-3 border-r border-white/10"><p class="text-lg font-bold">4</p><p class="text-xs text-slate-500 mt-0.5">要約</p></div>
        <div class="text-center py-3"><p class="text-lg font-bold text-indigo-300">EN/JP</p><p class="text-xs text-slate-500 mt-0.5">バイリンガル</p></div>
      </div>
    </div>
  </header>

  <main class="max-w-2xl mx-auto px-4 py-6 space-y-6">
    {sections_html}
  </main>

  <footer class="max-w-2xl mx-auto px-4 py-8 text-center text-xs text-slate-400 space-y-1">
    <p>Daily Intelligence &bull; {esc(date_str)}</p>
    <p>Generated automatically &middot; <a href="{GITHUB_PAGES_URL}" class="underline hover:text-slate-300 transition-colors">daily-report</a></p>
  </footer>

  <script>
    // ナビタブのアクティブ状態をセクションの交差に連動させる
    // アクティブ時: そのカテゴリのカラーで下線＋テキスト着色（color は data-color 属性で取得）
    const tabs = document.querySelectorAll('.nav-tab');
    const sections = document.querySelectorAll('section[id]');

    function setActiveTab(sectionId) {{
      tabs.forEach(tab => {{
        const isActive = tab.dataset.section === sectionId;
        const color = tab.dataset.color;
        // リセット
        tab.classList.remove('font-semibold', 'border-transparent', 'text-slate-500');
        tab.classList.remove(
          `text-blue-600`, `border-blue-500`,
          `text-purple-600`, `border-purple-500`,
          `text-emerald-600`, `border-emerald-500`,
          `text-amber-600`, `border-amber-500`
        );
        if (isActive) {{
          tab.classList.add('font-semibold', `text-${{color}}-600`, `border-${{color}}-500`);
        }} else {{
          tab.classList.add('border-transparent', 'text-slate-500');
        }}
      }});
    }}

    const observer = new IntersectionObserver(
      entries => {{
        entries.forEach(e => {{
          if (e.isIntersecting) setActiveTab(e.target.id);
        }});
      }},
      {{ rootMargin: '-48px 0px -60% 0px', threshold: 0 }}
    );
    sections.forEach(s => observer.observe(s));
  </script>

</body>
</html>"""


def save_html(html_content: str) -> None:
    HTML_OUTPUT_PATH.parent.mkdir(exist_ok=True)
    HTML_OUTPUT_PATH.write_text(html_content, encoding="utf-8")
    print(f"SUCCESS: HTML saved → {HTML_OUTPUT_PATH}")


# ── LINE送信 ───────────────────────────────────────────────────────────────────

def build_line_message(date_str: str) -> str:
    # name_short と同じ短縮名を使用してカテゴリ表記を統一
    names = " ／ ".join(
        f"{cfg['emoji']} {cfg['name_short']}" for cfg in CATEGORIES_CONFIG
    )
    return (
        f"【Daily Intelligence】\n"
        f"{date_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📰 今日のレポートが届きました\n\n"
        f"{names}\n\n"
        f"🔗 フルレポートを読む：\n"
        f"{GITHUB_PAGES_URL}"
    )


def send_to_line(messages: list[str]) -> None:
    configuration = Configuration(access_token=os.environ["LINE_ACCESS_TOKEN"])
    text_messages = [TextMessage(type="text", text=truncate(msg)) for msg in messages]
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=os.environ["LINE_USER_ID"], messages=text_messages)
        )


# ── メイン処理 ─────────────────────────────────────────────────────────────────

def main() -> None:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.now(jst)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    date_str = (
        f"{now.year}年{now.month}月{now.day}日"
        f"（{weekdays[now.weekday()]}）"
        f"{now.strftime('%H:%M')}"
    )

    # ── フェーズ1: ニュース取得（カテゴリ単位で失敗を吸収）──
    news_data: dict | None = None
    try:
        news_data = fetch_news(client)
    except Exception as e:
        print(f"ERROR [fetch_news]: {safe_error(e)}")

    # ── フェーズ2: HTML生成・保存 ──────────────────────────
    html_saved = False
    try:
        html_content = generate_html(news_data, date_str)
        save_html(html_content)
        html_saved = True
    except Exception as e:
        print(f"ERROR [generate_html/save_html]: {safe_error(e)}")

    # HTMLが保存できなかった場合は exit(1) でプッシュをスキップ
    # （壊れたファイルをコミットしないため）
    if not html_saved:
        print("FATAL: HTML を保存できなかったためプッシュをスキップします")
        sys.exit(1)

    # ── フェーズ3: LINE通知 ────────────────────────────────
    # LINE失敗は exit(1) しない。プッシュ（GitHub Pages更新）を優先する。
    try:
        send_to_line([build_line_message(date_str)])
        print("SUCCESS: LINE 送信完了")
    except Exception as e:
        print(f"ERROR [send_to_line]: {safe_error(e)}")
        print("WARNING: LINE 通知は失敗しましたが、HTML は GitHub Pages に公開されます")


if __name__ == "__main__":
    main()
