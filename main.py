"""
Daily Intelligence — LINE報告自動化ツール + GitHub Pages
毎朝7時（JST）に最新ニュースを収集・要約して
HTMLレポートをGitHub Pagesに公開し、URLをLINEに送信する。
"""

import html as _html
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

INSTRUCTIONS = """You are a professional news analyst. Search the web and report today's latest news
for ALL FOUR of the following categories. For each category, provide exactly 3 articles
and a ~200-word English summary followed by its Japanese translation and 4 vocabulary words.

Output MUST follow this exact plain-text format for each category:

[CATEGORY_TAG]
[絵文字] [カテゴリ名]
━━━━━━━━━━━━━━━━━━━━
① [記事タイトル]（[ソース名]）
② [記事タイトル]（[ソース名]）
③ [記事タイトル]（[ソース名]）

📝 Summary
[英語200語前後の要約]

🇯🇵 まとめ
[上記の自然な日本語訳]

📖 Vocabulary
・[英単語] [品詞] [日本語の意味]
・[英単語] [品詞] [日本語の意味]
・[英単語] [品詞] [日本語の意味]
・[英単語] [品詞] [日本語の意味]
[END_CATEGORY]

The four categories are:
1. 🤖 AI & Machine Learning
2. 💻 Technology
3. 🌍 World Politics & Economy
4. ✈️ Airline Deals & Travel（このカテゴリのみ、[END_CATEGORY]の直前に「※情報の正確性は各社公式サイトでご確認ください」を追加すること）

Output all four categories in order, with no additional commentary outside the tags."""


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

def fetch_news(client: OpenAI) -> str:
    """OpenAI Responses API + web_search で4カテゴリのニュースを一括取得する。"""
    jst = pytz.timezone("Asia/Tokyo")
    today = datetime.now(jst).strftime("%Y-%m-%d")

    response = client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search"}],
        instructions=INSTRUCTIONS,
        input=(
            "Search for today's latest news in all four categories: "
            "AI & Machine Learning, Technology, World Politics & Economy, "
            f"and Airline Deals & Travel (Japan routes focus). Today's date: {today}"
        ),
    )
    return response.output_text


# ── テキスト分割 ───────────────────────────────────────────────────────────────

def split_categories(raw_text: str) -> list[str] | None:
    """
    [CATEGORY_TAG]～[END_CATEGORY] タグでテキストを4カテゴリに分割する。
    分割に失敗した場合はNoneを返す。
    """
    matches = re.findall(r"\[CATEGORY_TAG\](.*?)\[END_CATEGORY\]", raw_text, re.DOTALL)
    if len(matches) == 4:
        return [m.strip() for m in matches]
    print(f"WARNING: カテゴリ分割失敗 (見つかったタグ数: {len(matches)})")
    return None


# ── HTML生成 ───────────────────────────────────────────────────────────────────

def _parse_category_text(text: str) -> dict:
    """カテゴリテキストブロックを構造化辞書にパースする。"""
    articles: list[dict] = []
    summary_en_lines: list[str] = []
    summary_ja_lines: list[str] = []
    vocab_items: list[dict] = []
    current_section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if "━━━" in line:
            current_section = "articles"
            continue
        if re.search(r"📝\s*Summary", line):
            current_section = "summary_en"
            continue
        if "🇯🇵" in line and "まとめ" in line:
            current_section = "summary_ja"
            continue
        if re.search(r"📖\s*Vocabulary", line):
            current_section = "vocab"
            continue

        if current_section == "articles":
            m = re.match(r"^[①②③]\s+(.+?)（(.+?)）\s*$", line)
            if m:
                articles.append({"title": m.group(1), "source": m.group(2)})
            elif re.match(r"^[①②③]", line):
                title = re.sub(r"^[①②③]\s*", "", line)
                articles.append({"title": title, "source": ""})

        elif current_section == "summary_en":
            summary_en_lines.append(line)

        elif current_section == "summary_ja":
            summary_ja_lines.append(line)

        elif current_section == "vocab":
            m = re.match(
                r"^・?(\S+)\s+(\[[^\]]+\]|\([^)]+\)|[a-z]+\.)\s+(.+)$", line
            )
            if m:
                vocab_items.append(
                    {"word": m.group(1), "pos": m.group(2), "meaning": m.group(3)}
                )

    return {
        "articles": articles,
        "summary_en": "\n".join(summary_en_lines),
        "summary_ja": "\n".join(summary_ja_lines),
        "vocabulary": vocab_items,
    }


def _category_section_html(text: str, cfg: dict) -> str:
    """1カテゴリ分の <section> HTMLを生成する。"""
    esc = _html.escape
    parsed = _parse_category_text(text)
    c = cfg["color"]
    grad = cfg["gradient"]

    # ── 記事リスト ──
    articles_html = ""
    for i, art in enumerate(parsed["articles"], 1):
        source_badge = (
            f'<span class="text-xs text-{c}-700 bg-{c}-50 border border-{c}-100 '
            f'px-2 py-0.5 rounded-full font-medium leading-none mt-1.5 inline-block">'
            f'{esc(art["source"])}</span>'
            if art["source"] else ""
        )
        articles_html += f"""
          <div class="article-link flex items-start gap-3 px-4 py-4 border-b border-slate-100 hover:bg-{c}-50 group">
            <span class="flex-none w-6 h-6 rounded-full bg-{c}-100 text-{c}-700 text-xs font-bold flex items-center justify-center shrink-0 mt-0.5">{i}</span>
            <div class="flex-1 min-w-0">
              <p class="text-sm font-semibold text-slate-800 group-hover:text-{c}-600 transition-colors leading-snug line-clamp-2">{esc(art["title"])}</p>
              {source_badge}
            </div>
            <span class="text-slate-300 group-hover:text-{c}-400 text-lg mt-0.5 shrink-0 transition-colors">›</span>
          </div>"""

    # ── English Summary ──
    en_paras = "".join(
        f'<p class="text-sm text-slate-700 leading-relaxed mt-3">{esc(p)}</p>'
        for p in parsed["summary_en"].split("\n") if p.strip()
    )

    # ── Japanese Summary ──
    ja_paras = "".join(
        f'<p class="text-sm text-slate-700 leading-relaxed mt-3">{esc(p)}</p>'
        for p in parsed["summary_ja"].split("\n") if p.strip()
    )

    # ── Vocabulary accordion ──
    vocab_rows = ""
    for v in parsed["vocabulary"]:
        vocab_rows += f"""
              <div class="flex items-center gap-2 px-4 py-3 border-b border-{c}-100 last:border-b-0 bg-white">
                <span class="font-bold text-sm text-slate-800">{esc(v["word"])}</span>
                <span class="text-xs font-semibold text-{c}-600 bg-{c}-50 border border-{c}-200 px-1.5 py-0.5 rounded">{esc(v["pos"])}</span>
                <span class="text-xs text-slate-600">{esc(v["meaning"])}</span>
              </div>"""

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

    article_count = len(parsed["articles"])

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
            <span class="bg-white/20 text-white text-xs font-semibold px-2.5 py-1 rounded-full">{article_count} articles</span>
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


def generate_html(categories: list[str] | None, raw_text: str, date_str: str) -> str:
    """HTMLレポートを生成する。categories が None の場合はフォールバック表示。"""
    esc = _html.escape

    if categories and len(categories) == 4:
        sections_html = "\n".join(
            _category_section_html(cat_text, cfg)
            for cat_text, cfg in zip(categories, CATEGORIES_CONFIG)
        )
        article_count = "12"
    else:
        sections_html = f"""
        <div class="bg-white rounded-2xl shadow-md p-6">
          <pre class="text-sm text-slate-700 whitespace-pre-wrap leading-relaxed font-sans">{esc(raw_text)}</pre>
        </div>"""
        article_count = "—"

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
    // アクティブなナビタブをハイライト
    const tabs = document.querySelectorAll('.nav-tab');
    const sections = document.querySelectorAll('section[id]');
    const observer = new IntersectionObserver(
      entries => {{
        entries.forEach(e => {{
          if (e.isIntersecting) {{
            tabs.forEach(t => {{
              const active = t.dataset.section === e.target.id;
              t.classList.toggle('border-b-2', true);
              t.classList.toggle('font-semibold', active);
              t.style.color = active ? '' : '';
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
    date_str = f"{now.year}年{now.month}月{now.day}日（{weekdays[now.weekday()]}）{now.strftime('%H:%M')}"

    # Step 1: ニュース取得
    raw_text = ""
    try:
        raw_text = fetch_news(client)
        print("SUCCESS: ニュース取得完了")
    except Exception as e:
        print(f"ERROR [fetch_news]: {safe_error(e)}")

    # Step 2: カテゴリ分割
    categories = split_categories(raw_text) if raw_text else None

    # Step 3: HTML生成 → 保存
    try:
        html_content = generate_html(categories, raw_text, date_str)
        save_html(html_content)
    except Exception as e:
        print(f"ERROR [generate_html]: {safe_error(e)}")

    # Step 4: LINEにURL通知を送信
    try:
        send_to_line([build_line_message(date_str)])
        print("SUCCESS: LINE送信完了")
    except Exception as e:
        print(f"ERROR [send_to_line]: {safe_error(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
