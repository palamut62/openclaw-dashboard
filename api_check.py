#!/usr/bin/env python3
"""
api_check.py — Günlük API limit & durum kontrolü
Cron: 0 6 * * * python3 /root/scripts/dashboard/api_check.py
"""
import json, urllib.request, urllib.error, ssl, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DASHBOARD_URL = "http://localhost:5300"
API_KEY       = "nootle-dashboard-2024-secret"
BOT_TOKEN     = "8513721436:AAGwqUlreX0BLSy7Abgdzp1aWYDCSIMRHt0"
CHAT_ID       = "7183350213"
TR_TZ         = timezone(timedelta(hours=3))

ctx = ssl.create_default_context()

def call(url, method="GET", data=None, extra_headers=None):
    headers = {"X-API-Key": API_KEY}
    if extra_headers:
        headers.update(extra_headers)
    body = json.dumps(data).encode() if data else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read())

def send_telegram(msg):
    body = json.dumps({
        "chat_id": CHAT_ID, "text": msg,
        "parse_mode": "HTML", "disable_web_page_preview": True
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10, context=ctx)
    except Exception as e:
        print(f"Telegram error: {e}")

def scan_env_files():
    """VPS'teki .env dosyalarını tara, yeni API key'leri bul."""
    env_files = [
        "/root/.openclaw/workspace/.env",
        "/root/scripts/x-ai-news/.env",
        "/root/scripts/ai-tool-evaluator/.env",
        "/root/scripts/multi-news/.env",
    ]
    found = {}
    key_patterns = {
        "FIRECRAWL_API_KEY": ("firecrawl", "Firecrawl", "🔥", "https://api.firecrawl.dev", "firecrawl"),
        "TWITTER_BEARER_TOKEN": ("twitter", "Twitter / X API", "𝕏", "https://api.twitter.com", "twitter"),
        "TELEGRAM_BOT_TOKEN": ("telegram", "Telegram Bot", "📱", "https://api.telegram.org", "telegram"),
        "GITHUB_TOKEN": ("github", "GitHub", "🐙", "https://api.github.com", "github"),
        "OPENAI_API_KEY": ("openai", "OpenAI", "🤖", "https://api.openai.com", "generic"),
        "ANTHROPIC_API_KEY": ("anthropic", "Anthropic", "🧠", "https://api.anthropic.com", "generic"),
        "GEMINI_API_KEY": ("gemini", "Google Gemini", "✨", "https://generativelanguage.googleapis.com", "generic"),
        "SERPER_API_KEY": ("serper", "Serper (Google Search)", "🔍", "https://google.serper.dev", "generic"),
        "GROQ_API_KEY": ("groq", "Groq", "⚡", "https://api.groq.com", "generic"),
    }
    for fpath in env_files:
        p = Path(fpath)
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in key_patterns and v:
                found[k] = (v, key_patterns[k])
    return found


def main():
    now = datetime.now(TR_TZ)
    print(f"[{now.strftime('%Y-%m-%d %H:%M')}] API Check başlatıldı")

    # 1. Mevcut API'leri al
    try:
        existing_apis = call(f"{DASHBOARD_URL}/api/apis")
    except Exception as e:
        print(f"Dashboard erişim hatası: {e}")
        sys.exit(1)

    existing_ids = {a["id"] for a in existing_apis}

    # 2. Env dosyalarını tara — yeni API var mı?
    scanned = scan_env_files()
    new_added = []
    for env_key, (api_key_val, (api_id, name, icon, base_url, ping_type)) in scanned.items():
        if api_id not in existing_ids:
            try:
                result = call(f"{DASHBOARD_URL}/api/apis", method="POST", data={
                    "id": api_id, "name": name, "icon": icon,
                    "base_url": base_url, "api_key": api_key_val,
                    "plan": "Free", "monthly_cost_usd": 0,
                    "ping_type": ping_type,
                    "notes": f"Otomatik tespit: {env_key}",
                    "tags": ["auto-detected"]
                })
                if result.get("ok"):
                    new_added.append(name)
                    existing_ids.add(api_id)
                    print(f"  Yeni API eklendi: {name}")
            except Exception as e:
                print(f"  Eklenemedi ({name}): {e}")

    # 3. Tüm API'leri test et
    try:
        ping_result = call(f"{DASHBOARD_URL}/api/apis/ping-all", method="POST")
        results = ping_result.get("results", [])
    except Exception as e:
        print(f"Ping-all hatası: {e}")
        results = []

    # 4. Güncel API listesini al
    try:
        apis = call(f"{DASHBOARD_URL}/api/apis")
    except Exception:
        apis = []

    # 5. Rapor hazırla
    STATUS_EMOJI = {
        "online": "🟢", "offline": "🔴", "error": "🔴",
        "auth_error": "🟡", "rate_limited": "🟣", "unknown": "⚪"
    }

    lines = [f"🔑 <b>Günlük API Kontrol</b> — {now.strftime('%d.%m %H:%M')}"]

    if new_added:
        lines.append(f"\n✨ <b>Yeni API tespit edildi:</b> {', '.join(new_added)}")

    lines.append("")
    total_cost = 0
    warn_lines = []

    for a in apis:
        em = STATUS_EMOJI.get(a.get("status","unknown"), "⚪")
        cost = a.get("monthly_cost_usd", 0) or 0
        total_cost += cost
        cost_str = f" · ${cost}/ay" if cost > 0 else ""
        lines.append(f"{em} <b>{a['icon']} {a['name']}</b> — {a.get('plan','?')}{cost_str}")

        # Remaining bilgisi
        rem = a.get("remaining", {})
        if rem:
            rem_parts = []
            for k, v in rem.items():
                if v is None:
                    continue
                lim_key = k.replace("remaining", "limit").replace("_remaining", "_limit")
                lim_val = rem.get(lim_key)
                if lim_val and isinstance(v, (int, float)) and lim_val > 0:
                    pct = round((v / lim_val) * 100)
                    bar = "🟩" if pct > 50 else ("🟨" if pct > 20 else "🟥")
                    rem_parts.append(f"{bar} {k.replace('_',' ')}: {v}/{lim_val} ({pct}%)")
                    if pct < 20:
                        warn_lines.append(f"⚠️ {a['name']} — {k}: sadece {pct}% kaldı!")
                else:
                    rem_parts.append(f"  {k.replace('_',' ')}: {v}")
            if rem_parts:
                lines.extend(["  " + p for p in rem_parts])

        if a.get("status") in ("offline", "auth_error", "error"):
            warn_lines.append(f"🔴 {a['name']} — {a.get('status')}: {a.get('last_checked','?')}")

    lines.append(f"\n💰 Toplam aylık maliyet: <b>${total_cost:.0f}</b>")

    if warn_lines:
        lines.append("\n⚠️ <b>Uyarılar:</b>")
        lines.extend(warn_lines)

    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)
    print("Rapor Telegram'a gönderildi.")


if __name__ == "__main__":
    main()
