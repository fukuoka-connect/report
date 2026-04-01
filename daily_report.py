#!/usr/bin/env python3
"""
Fukuoka connect 日次アクセス解析レポート
毎日朝9時（JST）に自動実行
clients.json からクライアント情報を読み込み、個別にLINE配信
"""

import os
import json
import datetime
import requests
import anthropic
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest, Dimension, Metric, DateRange
)
from googleapiclient.discovery import build

# ── 環境変数 ──────────────────────────────────
LINE_TOKEN        = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GCP_KEY_JSON      = os.environ["GCP_SERVICE_ACCOUNT_KEY"]

# ── clients.json 読み込み ─────────────────────
def load_clients():
    """
    clients.json を読み込む。
    line_user_id が "ENV:XXX" 形式の場合は環境変数から取得。
    active: false のクライアントはスキップ。
    """
    with open("clients.json", "r", encoding="utf-8") as f:
        clients = json.load(f)

    result = []
    for c in clients:
        if not c.get("active", True):
            print(f"スキップ（inactive）: {c['name']}")
            continue

        # line_user_id の解決
        uid = c.get("line_user_id", "")
        if uid.startswith("ENV:"):
            env_key = uid[4:]  # "ENV:LINE_USER_ID_SOFUREN" → "LINE_USER_ID_SOFUREN"
            uid = os.environ.get(env_key, "")
            if not uid:
                print(f"⚠️  環境変数 {env_key} が未設定: {c['name']} をスキップ")
                continue
        c["line_user_id"] = uid
        result.append(c)

    return result

# ── Google認証 ────────────────────────────────
def get_credentials():
    key_dict = json.loads(GCP_KEY_JSON)
    scopes = [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ]
    return service_account.Credentials.from_service_account_info(
        key_dict, scopes=scopes
    )

# ── GA4データ取得 ─────────────────────────────
def get_ga4_data(creds, ga4_id):
    client = BetaAnalyticsDataClient(credentials=creds)
    today     = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    week_ago  = yesterday - datetime.timedelta(days=7)
    month_ago = yesterday - datetime.timedelta(days=30)

    def report(start, end, metrics, dims=None):
        return client.run_report(RunReportRequest(
            property=f"properties/{ga4_id}",
            dimensions=[Dimension(name=d) for d in (dims or [])],
            metrics=[Metric(name=m) for m in metrics],
            date_ranges=[DateRange(
                start_date=str(start), end_date=str(end)
            )],
        ))

    def v(r, row=0, col=0):
        try: return r.rows[row].metric_values[col].value
        except: return "0"

    r_day = report(yesterday, yesterday,
        ["sessions","totalUsers","bounceRate","screenPageViews"])
    r_lw  = report(week_ago,  week_ago,  ["sessions"])
    r_lm  = report(month_ago, month_ago, ["sessions"])
    r_ev  = report(yesterday, yesterday, ["eventCount"], ["eventName"])
    r_ch  = report(yesterday, yesterday, ["sessions"],
        ["sessionDefaultChannelGroup"])

    events = {}
    for row in r_ev.rows:
        events[row.dimension_values[0].value] = int(
            row.metric_values[0].value)

    channels = {}
    for row in r_ch.rows:
        channels[row.dimension_values[0].value] = int(
            row.metric_values[0].value)

    return {
        "date":        str(yesterday),
        "sessions":    int(v(r_day, 0, 0)),
        "users":       int(v(r_day, 0, 1)),
        "bounce_rate": round(float(v(r_day, 0, 2)) * 100, 1),
        "pageviews":   int(v(r_day, 0, 3)),
        "sessions_lw": int(v(r_lw,  0, 0)),
        "sessions_lm": int(v(r_lm,  0, 0)),
        "events":      events,
        "channels":    channels,
    }

# ── Search Console データ取得 ─────────────────
def get_sc_data(creds, site_url):
    svc = build("searchconsole", "v1", credentials=creds)
    today = datetime.date.today()
    end   = today - datetime.timedelta(days=3)
    start = end   - datetime.timedelta(days=7)
    try:
        res = svc.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": str(start),
                "endDate":   str(end),
                "dimensions": ["query"],
                "rowLimit": 5,
                "orderBy": [{"fieldName": "clicks",
                             "sortOrder": "DESCENDING"}],
            }
        ).execute()
        rows = res.get("rows", [])
        keywords, total_clicks, total_imp = [], 0, 0
        for row in rows:
            c = int(row.get("clicks", 0))
            i = int(row.get("impressions", 0))
            keywords.append({
                "query":       row["keys"][0],
                "clicks":      c,
                "impressions": i,
                "position":    round(row.get("position", 0), 1),
            })
            total_clicks += c
            total_imp    += i
        return {"keywords": keywords,
                "total_clicks": total_clicks,
                "total_impressions": total_imp}
    except Exception as e:
        print(f"SC error ({site_url}): {e}")
        return {"keywords": [], "total_clicks": 0, "total_impressions": 0}

# ── Claude AIアドバイス生成 ───────────────────
def generate_advice(name, ga4, sc, profile=""):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    lw_diff = ga4["sessions"] - ga4["sessions_lw"]
    lm_diff = ga4["sessions"] - ga4["sessions_lm"]
    kw_str  = ", ".join([k["query"] for k in sc["keywords"][:3]]) or "なし"
    weekday = datetime.date.today().strftime("%A")
    weekday_jp = {
        "Monday":"月曜日","Tuesday":"火曜日","Wednesday":"水曜日",
        "Thursday":"木曜日","Friday":"金曜日","Saturday":"土曜日",
        "Sunday":"日曜日"
    }.get(weekday, weekday)

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=250,
        messages=[{"role": "user", "content": f"""
あなたは飲食店専門のデジタルマーケティングアドバイザーです。

【店舗・サービス情報】
{profile}

【昨日のアクセスデータ】
セッション数: {ga4['sessions']}件
先週比: {lw_diff:+d}件 / 先月比: {lm_diff:+d}件
直帰率: {ga4['bounce_rate']}%
電話タップ: {ga4['events'].get('phone_call', 0)}件
LINEクリック: {ga4['events'].get('line_click', 0)}件
マップクリック: {ga4['events'].get('map_click', 0)}件
メニュー表示: {ga4['events'].get('menu_modal_open', 0)}件
検索クリック: {sc['total_clicks']}件
TOP検索ワード: {kw_str}
今日: {weekday_jp}

上記の店舗情報とデータをもとに、
このお店のオーナーが今日すぐ実行できる
具体的なアドバイスを2文で生成してください。
データの数字に言及しつつ、この店舗に特化した
実践的な内容にしてください。
"""}]
    )
    return msg.content[0].text

# ── レポート文章生成 ──────────────────────────
def build_report(name, ga4, sc, advice):
    lw_diff = ga4["sessions"] - ga4["sessions_lw"]
    lm_diff = ga4["sessions"] - ga4["sessions_lm"]
    lw_icon = "↑" if lw_diff >= 0 else "↓"
    lm_icon = "↑" if lm_diff >= 0 else "↓"

    ch_map = {
        "Organic Search": "🔍 検索",
        "Direct":         "📌 直接",
        "Organic Social": "📱 SNS",
        "Referral":       "🔗 参照",
    }
    ch_lines = ""
    for ch, cnt in sorted(
        ga4["channels"].items(), key=lambda x: x[1], reverse=True
    )[:4]:
        ch_lines += f"  {ch_map.get(ch, ch)}: {cnt}件\n"
    if not ch_lines:
        ch_lines = "  （データなし）\n"

    kw_lines = ""
    for i, kw in enumerate(sc["keywords"][:3], 1):
        kw_lines += (
            f"  {i}. {kw['query']}\n"
            f"     表示{kw['impressions']}回・"
            f"クリック{kw['clicks']}回・{kw['position']}位\n"
        )
    if not kw_lines:
        kw_lines = "  （データ集計中）\n"

    return f"""📊 {name}
アクセスレポート {ga4['date']}

━━━━━━━━━━━━━
📈 昨日のアクセス
━━━━━━━━━━━━━
セッション: {ga4['sessions']}件
  先週比: {lw_icon}{abs(lw_diff)}件
  先月比: {lm_icon}{abs(lm_diff)}件
ユーザー: {ga4['users']}人
PV: {ga4['pageviews']}
直帰率: {ga4['bounce_rate']}%

━━━━━━━━━━━━━
📞 CVアクション
━━━━━━━━━━━━━
電話タップ: {ga4['events'].get('phone_call', 0)}件
LINEクリック: {ga4['events'].get('line_click', 0)}件
マップ開く: {ga4['events'].get('map_click', 0)}件
メニュー表示: {ga4['events'].get('menu_modal_open', 0)}件

━━━━━━━━━━━━━
🔀 流入チャネル
━━━━━━━━━━━━━
{ch_lines}
━━━━━━━━━━━━━
🔍 検索キーワード
━━━━━━━━━━━━━
{kw_lines}
━━━━━━━━━━━━━
💡 今日のアドバイス
━━━━━━━━━━━━━
{advice}

Powered by Fukuoka connect"""

# ── LINE送信 ─────────────────────────────────
def send_line(user_id, message):
    res = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        json={
            "to": user_id,
            "messages": [{"type": "text", "text": message}]
        },
        timeout=10,
    )
    print(f"LINE送信 → {user_id[:8]}...: {res.status_code}")
    if res.status_code != 200:
        print(res.text)

# ── メイン ───────────────────────────────────
def main():
    print("=== Fukuoka connect 日次レポート開始 ===")
    creds   = get_credentials()
    clients = load_clients()

    print(f"対象クライアント数: {len(clients)}")

    for client in clients:
        print(f"\n--- {client['name']} ---")
        ga4    = get_ga4_data(creds, client["ga4_id"])
        sc     = get_sc_data(creds, client["site_url"])
        advice = generate_advice(
            client["name"], ga4, sc, client.get("profile", "")
        )
        report = build_report(client["name"], ga4, sc, advice)
        print(report)
        send_line(client["line_user_id"], report)

    print("\n=== 全クライアント完了 ===")

if __name__ == "__main__":
    main()
