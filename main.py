"""
Daily Intelligence — LINE報告自動化ツール + GitHub Pages

設計方針：
  OpenAI に JSON を返させることで、テキスト正規表現パースの脆弱性を排除する。
  JSON スキーマに url フィールドを含め、実際の記事リンクを HTML に埋め込む。

フロー：
  1. fetch_news()  → OpenAI (web_search) → JSON を取得・パース
  2. generate_html() → JSON から sample_report.v4.html 相当の HTML を生成
  3. save_html()   → docs/index.html に保存（GitHub Actions が後続でコミット）
  4. send_to_line() → GitHub Pages の URL を LINE に送信
"""

import html as _html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytz
from openai import OpenAI
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

# ── 定数 ──────────────────────────────────────────────────────────────────────

GITHUB_PAGES_URL = "https://sakuuuuuuuu.github.io/daily-report/"
HTML_OUTPUT_PATH = Path("docs/index.html")
MAX_CHARS = 4000

# カテゴリ設定（表示順・スタイル・IDの正規定義）
CATEGORIES_CONFIG = [
    {
        "id": "ai",
        "emoji": "🤖",
        "name": "AI & Machine Learning",
        "name_ja": "人工知能・機械学習",
        "gradient": "from-blue-600 to-cyan-500",
        "color": "blue",
        "header_text": "blue-100",
    },
    {
        "id": "tech",
        "emoji": "💻",
        "name": "Technology",
        "name_ja": "テクノロジー",
        "gradient": "from-purple-600 to-violet-500",
        "color": "purple",
        "header_text": "purple-100",
    },
    {
        "id": "world",
        "emoji": "🌍",
        "name": "World Politics & Economy",
        "name_ja": "国際政治・経済",
        "gradient": "from-emerald-600 to-teal-500",
        "color": "emerald",
        "header_text": "emerald-100",
    },
    {
        "id": "flights",
        "emoji": "✈️",
        "name": "Airline Deals & Travel",
        "name_ja": "航空券セール・旅行",
        "gradient": "from-amber-500 to-orange-500",
        "color": "amber",
        "header_text": "amber-100",
    },
]

# ── OpenAI プロンプト（JSON 形式で返すよう指示） ────────────────────────────────

INSTRUCTIONS = """\
You are a professional news analyst with web search capability.

Search today's web for the latest news and return ONLY a JSON object — no markdown,
no code fences, no explanation, just the raw JSON.

Required JSON schema (follow exactly):
{
  "categories": [
    {
      "id": "<category-id>",
      "articles": [
        {"title": "<headline>", "source": "<outlet name>", "url": "<full https URL>"},
        {"title": "<headline>", "source": "<outlet name>", "url": "<full https URL>"},
        {"title": "<headline>", "source": "<outlet name>", "url": "<full https URL>"}
      ],
      "summary_en": "<~200-word English summary>",
      "summary_ja": "<natural Japanese translation of summary_en>",
      "vocabulary": [
        {"word": "<English word>", "pos": "<n.|v.|adj.|adv.>", "meaning_ja": "<日本語の意味>"},
        {"word": "<English word>", "pos": "<n.|v.|adj.|adv.>", "meaning_ja": "<日本語の意味>"},
        {"word": "<English word>", "pos": "<n.|v.|adj.|adv.>", "meaning_ja": "<日本語の意味>"},
        {"word": "<English word>", "pos": "<n.|v.|adj.|adv.>", "meaning_ja": "<日本語の意味>"}
      ]
    }
  ]
}

The "categories" array MUST contain EXACTLY these 4 objects in this order:
  1. id "ai"      — AI & Machine Learning (latest AI model releases, research, applications)
  2. id "tech"    — Technology (hardware, software, startups — excluding AI)
  3. id "world"   — World Politics & Economy (geopolitics, international economics, diplomacy)
  4. id "flights" — Airline Deals & Travel (sales, new routes, travel news relevant to Japan)

Strict rules:
- Each category must have exactly 3 articles.
- Every "url" must be a real, working HTTPS URL discovered via your web search.
- summary_en: approximately 200 words covering all 3 articles.
- summary_ja: natural Japanese translation of summary_en.
- vocabulary: exactly 4 TOEIC B1+ level English words chosen from summary_en.
- For id "flights" only: append the sentence \
"※情報の正確性は各社公式サイトでご確認ください" at the end of summary_ja.
- Output ONLY the JSON object. No other text whatsoever.\
"""


# ── ユーティリティ ─────────────────────────────────────────────────────────────

def safe_error(e: Exception) -> str:
    """例外メッセージからAPIキー等の機密情報をマスクして返す。"""
    msg = str(e)
    for key_name in ["OPENAI_API_KEY", "LINE_ACCESS_TOKEN", "LINE_USER_ID"]:
        val = os.environ.get(key_name, "")
        if val and val in msg:
            msg = msg.replace(val, "***")
    return msg


def truncate(text: str, max_chars: int = MAX_CHARS) -> str:
    """テキストが上限を超える場合、末尾を切り捨てて省略記号を付ける。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 10] + "\n...(省略)"


# ── ニュース取得 ───────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """
    OpenAI の出力テキストから JSON オブジェクトを抽出してパースする。
    マークダウンコードブロックで囲まれていても正しく処理する。
    """
    # コードフェンスを除去
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # 最初の { から最後の } の範囲を取り出す
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON object not found in OpenAI response")

    return json.loads(cleaned[start : end + 1])


def fetch_news(client: OpenAI) -> dict:
    """
    OpenAI Responses API + web_search で4カテゴリのニュースを取得し、
    パース済みの dict を返す。
    """
    jst = pytz.timezone("Asia/Tokyo")
    today = datetime.now(jst).strftime("%Y-%m-%d")

    response = client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search"}],
        instructions=INSTRUCTIONS,
        input=(
            "Search today's web for the latest news in all four categories "
            "(AI & Machine Learning, Technology, World Politics & Economy, "
            f"Airline Deals & Travel). Today's date: {today}. "
            "Return only the JSON as instructed."
        ),
    )

    raw = response.output_text
    data = _extract_json(raw)

    # 最低限のスキーマ検証
    categories = data.get("categories", [])
    if len(categories) != 4:
        raise ValueError(
            f"Expected 4 categories in JSON, got {len(categories)}. "
            f"Raw output (first 200 chars): {raw[:200]}"
        )

    return data


# ── HTML生成 ───────────────────────────────────────────────────────────────────

def _category_section_html(cat_data: dict, cfg: dict) -> str:
    """JSON の1カテゴリ分のデータから <section> HTML を生成する。"""
    esc = _html.escape
    c = cfg["color"]
    grad = cfg["gradient"]

    articles = cat_data.get("articles", [])
    summary_en = cat_data.get("summary_en", "")
    summary_ja = cat_data.get("summary_ja", "")
    vocabulary = cat_data.get("vocabulary", [])

    # ── 記事リスト（URLをクリッカブルリンクに） ──
    articles_html = ""
    for i, art in enumerate(articles[:3], 1):
        url = art.get("url", "#") or "#"
        title = art.get("title", "（タイトル不明）")
        source = art.get("source", "")

        source_badge = (
            f'<span class="text-xs text-{c}-700 bg-{c}-50 border border-{c}-100 '
            f"px-2 py-0.5 rounded-full font-medium leading-none mt-1.5 inline-block\">"
            f"{esc(source)}</span>"
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

    # ── サマリー（英語） ──
    en_paras = "".join(
        f'<p class="text-sm text-slate-700 leading-relaxed mt-3">{esc(p)}</p>'
        for p in summary_en.split("\n")
        if p.strip()
    )

    # ── サマリー（日本語） ──
    ja_paras = "".join(
        f'<p class="text-sm text-slate-700 leading-relaxed mt-3">{esc(p)}</p>'
        for p in summary_ja.split("\n")
        if p.strip()
    )

    # ── ボキャブラリー accordion ──
    vocab_rows = "".join(
        f"""
              <div class="flex items-center gap-2 px-4 py-3 border-b border-{c}-100 last:border-b-0 bg-white">
                <span class="font-bold text-sm text-slate-800">{esc(v.get("word", ""))}</span>
                <span class="text-xs font-semibold text-{c}-600 bg-{c}-50 border border-{c}-200 px-1.5 py-0.5 rounded">{esc(v.get("pos", ""))}</span>
                <span class="text-xs text-slate-600">{esc(v.get("meaning_ja", ""))}</span>
              </div>"""
        for v in vocabulary
    )

    vocab_section = f"""
          <details class="mt-5 rounded-xl overflow-hidden border border-{c}-200">
            <summary class="flex items-center justify-between px-4 py-3 bg-{c}-100/70 cursor-pointer select-none">
              <div class="flex items-center gap-2">
                <span class="text-sm">📖</span>
                <span class="text-xs font-bold uppercase tracking-wider text-{c}-700">Vocabulary</span>
              </div>
              <span class="chevron text-{c}-400 text-xs">▼</span>
            </summary>
            <div class="divide-y divide-{c}-100">
              {vocab_rows}
            </div>
          </details>""" if vocab_rows else ""

    return f"""
    <section id="{cfg['id']}" class="card">
      <div class="rounded-2xl overflow-hidden shadow-md">

        <div class="bg-gradient-to-r {grad} px-5 py-4">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <span class="text-xl">{cfg['emoji']}</span>
              <div>
                <h2 class="text-white font-bold text-base leading-tight">{esc(cfg['name'])}</h2>
                <p class="text-{cfg['header_text']} text-xs mt-0.5">{esc(cfg['name_ja'])}</p>
              </div>
            </div>
            <span class="bg-white/20 text-white text-xs font-semibold px-2.5 py-1 rounded-full">{len(articles[:3])} articles</span>
          </div>
        </div>

        <div class="bg-white divide-y divide-slate-100">
          {articles_html}
        </div>

        <div class="bg-{c}-50 border-t-2 border-{c}-100 px-5 py-5">

          <div class="flex items-center gap-2 mb-3">
            <span class="text-base">🇬🇧</span>
            <span class="text-xs font-bold uppercase tracking-wider text-{c}-600">English</span>
          </div>
          <div class="mb-5">
            {en_paras}
          </div>

          <div class="flex items-center gap-3 my-4">
            <div class="flex-1 h-px bg-{c}-200"></div>
            <span class="text-xs text-{c}-400 font-semibold">🇯🇵 日本語訳</span>
            <div class="flex-1 h-px bg-{c}-200"></div>
          </div>
          <div>
            {ja_paras}
          </div>

          {vocab_section}
        </div>

      </div>
    </section>"""


def generate_html(news_data: dict | None, date_str: str) -> str:
    """
    JSON データから完全な HTML レポートを生成する。
    news_data が None の場合はエラーページを返す。
    """
    esc = _html.escape

    if news_data:
        # JSON の categories を id でインデックス化し、CATEGORIES_CONFIG の順で HTML を構築
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

    nav_tabs = "".join(
        f'<a href="#{cfg["id"]}" data-section="{cfg["id"]}"'
        f' class="nav-tab flex items-center gap-1.5 px-4 py-3.5 text-xs font-medium'
        f' text-slate-500 border-b-2 border-transparent whitespace-nowrap transition-colors'
        f' hover:text-{cfg["color"]}-600">'
        f'<span>{cfg["emoji"]}</span> {cfg["name"].split()[0]}</a>'
        for cfg in CATEGORIES_CONFIG
    )

    hero_badges = "".join(
        f'<a href="#{cfg["id"]}" class="inline-flex items-center gap-1 text-xs'
        f' bg-{cfg["color"]}-500/15 text-{cfg["color"]}-300'
        f' border border-{cfg["color"]}-500/25 px-2.5 py-1 rounded-full'
        f' hover:bg-{cfg["color"]}-500/25 transition-colors">'
        f'{cfg["emoji"]} {cfg["name"].split()[0]}</a>'
        for cfg in CATEGORIES_CONFIG
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Daily Intelligence — {esc(date_str)}</title>
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
  </style>
</head>
<body class="bg-slate-100 text-slate-800 antialiased">

  <nav class="sticky top-0 z-50 bg-white/95 backdrop-blur-md border-b border-slate-200 shadow-sm">
    <div class="max-w-2xl mx-auto overflow-x-auto no-scrollbar">
      <div class="flex min-w-max" id="nav-tabs">
        {nav_tabs}
      </div>
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
      <div class="flex flex-wrap gap-2 mb-6">
        {hero_badges}
      </div>
      <div class="grid grid-cols-4 bg-white/5 border border-white/10 rounded-xl overflow-hidden">
        <div class="text-center py-3 border-r border-white/10"><p class="text-lg font-bold">{article_count}</p><p class="text-xs text-slate-500 mt-0.5">Articles</p></div>
        <div class="text-center py-3 border-r border-white/10"><p class="text-lg font-bold">4</p><p class="text-xs text-slate-500 mt-0.5">Categories</p></div>
        <div class="text-center py-3 border-r border-white/10"><p class="text-lg font-bold">4</p><p class="text-xs text-slate-500 mt-0.5">Summaries</p></div>
        <div class="text-center py-3"><p class="text-lg font-bold text-indigo-300">EN/JP</p><p class="text-xs text-slate-500 mt-0.5">Bilingual</p></div>
      </div>
    </div>
  </header>

  <main class="max-w-2xl mx-auto px-4 py-6 space-y-6">
    {sections_html}
  </main>

  <footer class="max-w-2xl mx-auto px-4 py-8 text-center text-xs text-slate-400 space-y-1">
    <p>Daily Intelligence • {esc(date_str)}</p>
    <p>Generated automatically · <a href="{GITHUB_PAGES_URL}" class="underline hover:text-slate-300 transition-colors">daily-report</a></p>
  </footer>

  <script>
    const tabs = document.querySelectorAll('.nav-tab');
    const sections = document.querySelectorAll('section[id]');
    const observer = new IntersectionObserver(
      entries => {{
        entries.forEach(e => {{
          if (e.isIntersecting) {{
            tabs.forEach(t => {{
              const active = t.dataset.section === e.target.id;
              t.classList.toggle('font-semibold', active);
            }});
          }}
        }});
      }},
      {{ rootMargin: '-48px 0px -60% 0px', threshold: 0 }}
    );
    sections.forEach(s => observer.observe(s));
  </script>

</body>
</html>"""


def save_html(html_content: str) -> None:
    """HTMLファイルを docs/index.html に保存する。"""
    HTML_OUTPUT_PATH.parent.mkdir(exist_ok=True)
    HTML_OUTPUT_PATH.write_text(html_content, encoding="utf-8")
    print(f"SUCCESS: HTML saved → {HTML_OUTPUT_PATH}")


# ── LINE送信 ───────────────────────────────────────────────────────────────────

def build_line_message(date_str: str) -> str:
    """LINEに送る通知メッセージを生成する。"""
    return (
        f"【Daily Intelligence】\n"
        f"{date_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📰 今日のレポートが届きました\n\n"
        f"🤖 AI ／ 💻 Tech ／ 🌍 World ／ ✈️ Flights\n\n"
        f"🔗 フルレポートを読む：\n"
        f"{GITHUB_PAGES_URL}"
    )


def send_to_line(messages: list[str]) -> None:
    """LINE Messaging API v3 でPushメッセージを送信する。"""
    configuration = Configuration(access_token=os.environ["LINE_ACCESS_TOKEN"])
    text_messages = [
        TextMessage(type="text", text=truncate(msg))
        for msg in messages
    ]
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=os.environ["LINE_USER_ID"],
                messages=text_messages,
            )
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

    # Step 1: ニュースを JSON で取得
    news_data: dict | None = None
    try:
        news_data = fetch_news(client)
        cat_ids = [c["id"] for c in news_data.get("categories", [])]
        print(f"SUCCESS: ニュース取得完了 — categories: {cat_ids}")
    except Exception as e:
        print(f"ERROR [fetch_news]: {safe_error(e)}")

    # Step 2: HTML 生成 → 保存
    try:
        html_content = generate_html(news_data, date_str)
        save_html(html_content)
    except Exception as e:
        print(f"ERROR [generate_html]: {safe_error(e)}")

    # Step 3: LINE に URL 通知を送信
    try:
        send_to_line([build_line_message(date_str)])
        print("SUCCESS: LINE 送信完了")
    except Exception as e:
        print(f"ERROR [send_to_line]: {safe_error(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
