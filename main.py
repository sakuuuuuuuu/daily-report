"""
Daily Intelligence — LINE報告自動化ツール
毎朝7時（JST）に最新ニュースを収集・要約してLINEに送信する。
"""

import os
import sys
from datetime import datetime

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

MAX_CHARS = 4000  # LINEテキストメッセージの安全上限（公式上限5,000文字）

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


def build_header() -> str:
    """当日の日付・曜日を含むヘッダーメッセージを生成する。"""
    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.now(jst)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    weekday = weekdays[now.weekday()]
    date_str = now.strftime(f"%Y/%m/%d（{weekday}）%H:%M")
    return (
        f"【Daily Intelligence】\n"
        f"{date_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 AI ／ 💻 Tech ／ 🌍 World ／ ✈️ Flights"
    )


# ── ニュース取得 ───────────────────────────────────────────────────────────────

def fetch_news(client: OpenAI) -> str:
    """OpenAI Responses API + web_search で4カテゴリのニュースを一括取得する。"""
    jst = pytz.timezone("Asia/Tokyo")
    today = datetime.now(jst).strftime("%Y-%m-%d")

    user_input = (
        "Search for today's latest news in all four categories: "
        "AI & Machine Learning, Technology, World Politics & Economy, "
        "and Airline Deals & Travel (Japan routes focus).\n"
        f"Today's date: {today}"
    )

    response = client.responses.create(
        model="gpt-4.1-mini",
        tools=[{"type": "web_search"}],
        instructions=INSTRUCTIONS,
        input=user_input,
    )
    return response.output_text


# ── テキスト分割 ───────────────────────────────────────────────────────────────

def split_categories(raw_text: str) -> list[str]:
    """
    [CATEGORY_TAG]～[END_CATEGORY] タグでテキストを4カテゴリに分割する。
    分割に失敗した場合はテキスト全体をリストの1要素として返す。
    """
    import re
    pattern = r"\[CATEGORY_TAG\](.*?)\[END_CATEGORY\]"
    matches = re.findall(pattern, raw_text, re.DOTALL)
    if len(matches) == 4:
        return [m.strip() for m in matches]

    # 分割失敗時：全体をそのまま返す
    print("WARNING: カテゴリ分割に失敗しました。全文を1メッセージとして送信します。")
    return [raw_text.strip()]


# ── LINE送信 ───────────────────────────────────────────────────────────────────

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

    # Step 1: ニュース取得
    fallback = "ニュースの取得に失敗しました"
    try:
        raw_text = fetch_news(client)
    except Exception as e:
        print(f"ERROR [fetch_news]: {safe_error(e)}")
        raw_text = fallback

    # Step 2: カテゴリ分割
    try:
        categories = split_categories(raw_text)
    except Exception as e:
        print(f"ERROR [split_categories]: {safe_error(e)}")
        categories = [fallback]

    # Step 3: LINEメッセージ構成
    header = build_header()

    if len(categories) == 4:
        # 正常：ヘッダー + 4カテゴリ = 5メッセージ
        messages_to_send = [header] + categories
    else:
        # 分割失敗時：ヘッダー + 全文 = 2メッセージ
        messages_to_send = [header] + categories

    # Step 4: LINE送信
    try:
        send_to_line(messages_to_send)
        print(f"SUCCESS: {len(messages_to_send)}件のメッセージをLINEに送信しました。")
    except Exception as e:
        print(f"ERROR [send_to_line]: {safe_error(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
