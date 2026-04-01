#!/usr/bin/env python3
"""
Fukuoka connect Webhook サーバー（Render.com用）
- 友だち追加 → clients.json 自動登録
- LINEコマンドでクライアント管理
  /set 名前      → ステップ式でGA4・URL・profile設定
  /activate 名前 → 配信開始（active: true）
  /stop 名前     → 配信停止（active: false）
  /list          → 登録済みクライアント一覧
  /help          → コマンド一覧
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
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_TOKEN          = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GH_TOKEN            = os.environ["GH_TOKEN_FC"]
GITHUB_REPO         = os.environ["GITHUB_REPO"]
GITHUB_BRANCH       = os.environ.get("GITHUB_BRANCH", "main")
ADMIN_USER_ID       = os.environ.get("ADMIN_USER_ID", "")
CLIENTS_JSON_PATH   = "clients.json"

# ── セッション管理（ステップ式入力用）────────────
# { user_id: { "step": "ga4"|"url"|"profile", "target_name": "..." } }
sessions = {}

# ── LINE署名検証 ──────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    secret = LINE_CHANNEL_SECRET.encode("utf-8")
    digest = hmac.new(secret, body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)

# ── GitHub: clients.json 取得 ─────────────────
def fetch_clients_json():
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        f"/contents/{CLIENTS_JSON_PATH}?ref={GITHUB_BRANCH}"
    )
    res = requests.get(url, headers={
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    })
    res.raise_for_status()
    data = res.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]

# ── GitHub: clients.json 書き込み ─────────────
def push_clients_json(clients: list, sha: str, message: str):
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        f"/contents/{CLIENTS_JSON_PATH}"
    )
    content = base64.b64encode(
        json.dumps(clients, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    res = requests.put(url, headers={
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }, json={
        "message": message,
        "content": content,
        "sha": sha,
        "branch": GITHUB_BRANCH,
    })
    res.raise_for_status()

# ── LINE: reply送信 ───────────────────────────
def reply(reply_token: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}]
        },
        timeout=10,
    )

# ── LINE: push送信 ────────────────────────────
def push(user_id: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        json={
            "to": user_id,
            "messages": [{"type": "text", "text": text}]
        },
        timeout=10,
    )

# ── LINE: プロフィール取得 ─────────────────────
def get_line_profile(user_id: str) -> dict:
    res = requests.get(
        f"https://api.line.me/v2/bot/profile/{user_id}",
        headers={"Authorization": f"Bearer {LINE_TOKEN}"}
    )
    return res.json() if res.status_code == 200 else {}

# ── ウェルカムメッセージ ──────────────────────
def send_welcome(user_id: str, display_name: str):
    message = (
        f"{display_name} さん、はじめまして。\n\n"
        "FUKUOKA CONNECT です。\n\n"
        "私たちは「売れるLP」ではなく\n"
        "「届くLP」をつくります。\n\n"
        "数字で見える化し、毎日改善する。\n"
        "そのサポートを、これからよろしくお願いします。\n\n"
        "━━━━━━━━━━━━━\n"
        "📊 毎朝レポート自動配信\n"
        "🛠 月1回のLP改善提案\n"
        "📈 GA4 × Search Console 分析\n"
        "━━━━━━━━━━━━━\n\n"
        "担当の後藤より、改めてご連絡いたします。"
    )
    push(user_id, message)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# コマンド処理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_list(reply_token: str):
    try:
        clients, _ = fetch_clients_json()
        if not clients:
            reply(reply_token, "登録済みクライアントはいません。")
            return
        lines = ["📋 登録済みクライアント一覧\n━━━━━━━━━━━━━"]
        for c in clients:
            status = "✅ 配信中" if c.get("active") else "⏸ 停止中"
            lines.append(
                f"\n{status} {c['name']}\n"
                f"GA4: {c.get('ga4_id') or '未設定'}\n"
                f"URL: {c.get('site_url') or '未設定'}"
            )
        reply(reply_token, "\n".join(lines))
    except Exception as e:
        reply(reply_token, f"❌ エラー: {e}")


def cmd_activate(reply_token: str, name: str):
    try:
        clients, sha = fetch_clients_json()
        target = next((c for c in clients if c["name"] == name), None)
        if not target:
            reply(reply_token,
                f"❌「{name}」が見つかりません。\n/list で名前を確認してください。")
            return
        if not target.get("ga4_id") or not target.get("site_url"):
            reply(reply_token,
                f"❌ GA4またはURLが未設定です。\n先に /set {name} で設定してください。")
            return
        target["active"] = True
        push_clients_json(clients, sha, f"Activate: {name}")
        reply(reply_token,
            f"✅ {name} の配信を開始しました！\n翌朝から毎日レポートが届きます📊")
    except Exception as e:
        reply(reply_token, f"❌ エラー: {e}")


def cmd_stop(reply_token: str, name: str):
    try:
        clients, sha = fetch_clients_json()
        target = next((c for c in clients if c["name"] == name), None)
        if not target:
            reply(reply_token,
                f"❌「{name}」が見つかりません。\n/list で名前を確認してください。")
            return
        target["active"] = False
        push_clients_json(clients, sha, f"Stop: {name}")
        reply(reply_token, f"⏸ {name} の配信を停止しました。")
    except Exception as e:
        reply(reply_token, f"❌ エラー: {e}")


def cmd_set(reply_token: str, user_id: str, name: str):
    try:
        clients, _ = fetch_clients_json()
        target = next((c for c in clients if c["name"] == name), None)
        if not target:
            reply(reply_token,
                f"❌「{name}」が見つかりません。\n/list で名前を確認してください。")
            return
        sessions[user_id] = {"step": "ga4", "target_name": name}
        reply(reply_token,
            f"🛠 {name} の設定を開始します。\n\n"
            f"【1/3】GA4 プロパティIDを入力してください。\n"
            f"例：529552579\n\n"
            f"（キャンセルは /cancel）"
        )
    except Exception as e:
        reply(reply_token, f"❌ エラー: {e}")


def handle_step(reply_token: str, user_id: str, text: str) -> bool:
    """ステップ式入力の処理。セッション中なら True を返す"""
    session = sessions.get(user_id)
    if not session:
        return False

    if text.strip() == "/cancel":
        del sessions[user_id]
        reply(reply_token, "❌ 設定をキャンセルしました。")
        return True

    step = session["step"]
    name = session["target_name"]

    try:
        clients, sha = fetch_clients_json()
        target = next((c for c in clients if c["name"] == name), None)
        if not target:
            del sessions[user_id]
            reply(reply_token, f"❌「{name}」が見つかりません。")
            return True

        if step == "ga4":
            target["ga4_id"] = text.strip()
            push_clients_json(clients, sha, f"Set GA4: {name}")
            sessions[user_id]["step"] = "url"
            reply(reply_token,
                f"✅ GA4 IDを設定しました。\n\n"
                f"【2/3】サイトURLを入力してください。\n"
                f"例：https://fukuoka-connect.github.io/makibo/"
            )

        elif step == "url":
            target["site_url"] = text.strip()
            push_clients_json(clients, sha, f"Set URL: {name}")
            sessions[user_id]["step"] = "profile"
            reply(reply_token,
                f"✅ URLを設定しました。\n\n"
                f"【3/3】プロフィールを入力してください。\n"
                f"業種・場所・強み・ターゲットなどを自由に。\n\n"
                f"例：業種：花火・イベント企画\n"
                f"場所：福岡県宇美町\n"
                f"強み：地域密着20年"
            )

        elif step == "profile":
            target["profile"] = text.strip()
            push_clients_json(clients, sha, f"Set Profile: {name}")
            del sessions[user_id]
            reply(reply_token,
                f"✅ すべての設定が完了しました！\n\n"
                f"📋 {name}\n"
                f"GA4: {target['ga4_id']}\n"
                f"URL: {target['site_url']}\n\n"
                f"配信を開始するには：\n"
                f"/activate {name}"
            )

    except Exception as e:
        del sessions[user_id]
        reply(reply_token, f"❌ エラー: {e}")

    return True

# ── 管理者チェック ────────────────────────────
def is_admin(user_id: str) -> bool:
    if not ADMIN_USER_ID:
        return True  # 未設定時は全員許可（初期設定用）
    return user_id == ADMIN_USER_ID

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Webhook エンドポイント
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    events = request.json.get("events", [])

    for event in events:
        event_type  = event.get("type")
        source      = event.get("source", {})
        user_id     = source.get("userId", "")
        reply_token = event.get("replyToken", "")

        if not user_id:
            continue

        # ── 友だち追加 ──────────────────────────
        if event_type == "follow":
            profile      = get_line_profile(user_id)
            display_name = profile.get("displayName", "（名前不明）")
            print(f"友だち追加: {display_name} / {user_id}")

            try:
                clients, sha = fetch_clients_json()
                if user_id not in [c.get("line_user_id") for c in clients]:
                    clients.append({
                        "name":         display_name,
                        "line_user_id": user_id,
                        "ga4_id":       "",
                        "site_url":     "",
                        "active":       False,
                        "profile":      "",
                        "memo":         "友だち追加から自動登録 / 設定待ち"
                    })
                    push_clients_json(clients, sha,
                        f"Auto-register: {display_name}")

                    # 管理者に通知
                    if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
                        push(ADMIN_USER_ID,
                            f"🔔 新規友だち追加\n"
                            f"名前：{display_name}\n\n"
                            f"設定するには：\n/set {display_name}"
                        )
            except Exception as e:
                print(f"登録エラー: {e}")

            send_welcome(user_id, display_name)

        # ── テキストメッセージ ───────────────────
        elif event_type == "message" \
                and event.get("message", {}).get("type") == "text":
            text = event["message"]["text"].strip()

            if not is_admin(user_id):
                continue

            # ステップ式入力中
            if handle_step(reply_token, user_id, text):
                continue

            # コマンド解析
            parts = text.split(maxsplit=1)
            cmd   = parts[0].lower()
            arg   = parts[1].strip() if len(parts) > 1 else ""

            if cmd == "/list":
                cmd_list(reply_token)

            elif cmd == "/set":
                if not arg:
                    reply(reply_token,
                        "使い方：/set クライアント名\n例：/set 寺田さん")
                else:
                    cmd_set(reply_token, user_id, arg)

            elif cmd == "/activate":
                if not arg:
                    reply(reply_token,
                        "使い方：/activate クライアント名\n例：/activate 寺田さん")
                else:
                    cmd_activate(reply_token, arg)

            elif cmd == "/stop":
                if not arg:
                    reply(reply_token,
                        "使い方：/stop クライアント名\n例：/stop 寺田さん")
                else:
                    cmd_stop(reply_token, arg)

            elif cmd == "/help":
                reply(reply_token,
                    "📋 コマンド一覧\n"
                    "━━━━━━━━━━━━━\n"
                    "/list\n登録済みクライアント一覧\n\n"
                    "/set 名前\nGA4・URL・profileを設定\n\n"
                    "/activate 名前\n配信開始\n\n"
                    "/stop 名前\n配信停止\n\n"
                    "/cancel\n設定中の操作をキャンセル"
                )

    return "OK", 200

# ── ヘルスチェック ────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "Fukuoka connect Webhook Server is running.", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
