#!/usr/bin/env python3
"""
Fukuoka connect Webhook サーバー（Render.com用）
LINE公式アカウントへの友だち追加を検知し、
clients.json に自動登録する
"""

import os
import json
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, abort

app = Flask(__name__)

# ── 環境変数 ──────────────────────────────────
LINE_CHANNEL_SECRET  = os.environ["LINE_CHANNEL_SECRET"]
LINE_TOKEN           = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GITHUB_TOKEN         = os.environ["GITHUB_TOKEN"]          # repo書き込み権限付き
GITHUB_REPO          = os.environ["GITHUB_REPO"]           # 例: kentaro/fukuoka-connect-report
GITHUB_BRANCH        = os.environ.get("GITHUB_BRANCH", "main")
CLIENTS_JSON_PATH    = "clients.json"

# ── LINE署名検証 ──────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    secret = LINE_CHANNEL_SECRET.encode("utf-8")
    digest = hmac.new(secret, body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)

# ── GitHub から clients.json を取得 ───────────
def fetch_clients_json():
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        f"/contents/{CLIENTS_JSON_PATH}?ref={GITHUB_BRANCH}"
    )
    res = requests.get(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    })
    res.raise_for_status()
    data = res.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]

# ── GitHub へ clients.json を書き込み ─────────
def push_clients_json(clients: list, sha: str, message: str):
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        f"/contents/{CLIENTS_JSON_PATH}"
    )
    content = base64.b64encode(
        json.dumps(clients, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    res = requests.put(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }, json={
        "message": message,
        "content": content,
        "sha": sha,
        "branch": GITHUB_BRANCH,
    })
    res.raise_for_status()
    return res.json()

# ── LINE プロフィール取得 ──────────────────────
def get_line_profile(user_id: str) -> dict:
    res = requests.get(
        f"https://api.line.me/v2/bot/profile/{user_id}",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"}
    )
    if res.status_code == 200:
        return res.json()
    return {}

# ── ウェルカムメッセージ送信 ──────────────────
def send_welcome(user_id: str, display_name: str):
    message = (
        f"{display_name} さん、友だち追加ありがとうございます！\n\n"
        "Fukuoka connect です。\n"
        "毎朝9時にアクセスレポートをお届けします📊\n\n"
        "設定が完了次第、担当者よりご連絡いたします。"
    )
    requests.post(
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

# ── Webhook エンドポイント ────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        print("署名検証失敗")
        abort(400)

    events = request.json.get("events", [])

    for event in events:
        event_type = event.get("type")
        source     = event.get("source", {})
        user_id    = source.get("userId", "")

        if not user_id:
            continue

        # ── 友だち追加イベント ──
        if event_type == "follow":
            print(f"友だち追加: {user_id}")

            profile      = get_line_profile(user_id)
            display_name = profile.get("displayName", "（名前不明）")

            try:
                clients, sha = fetch_clients_json()

                # 既存チェック（重複登録防止）
                existing_ids = [c.get("line_user_id") for c in clients]
                if user_id in existing_ids:
                    print(f"既登録済み: {user_id}")
                else:
                    # 新規クライアントとして追加（未設定状態）
                    new_client = {
                        "name":         display_name,
                        "line_user_id": user_id,
                        "ga4_id":       "",
                        "site_url":     "",
                        "active":       False,   # GA4設定完了後にtrueに変更
                        "profile":      "",
                        "memo":         "友だち追加から自動登録 / GA4・SC設定待ち"
                    }
                    clients.append(new_client)
                    push_clients_json(
                        clients, sha,
                        f"Auto-register: {display_name} ({user_id[:8]}...)"
                    )
                    print(f"✅ 登録完了: {display_name} / {user_id}")

                # ウェルカムメッセージ
                send_welcome(user_id, display_name)

            except Exception as e:
                print(f"登録エラー: {e}")

        # ── ブロック解除イベント ──
        elif event_type == "unfollow":
            print(f"ブロック/削除: {user_id}")
            # 必要に応じて active: false に更新する処理をここに追加

    return "OK", 200

# ── ヘルスチェック ────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "Fukuoka connect Webhook Server is running.", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
