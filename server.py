#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import json
import time
import uuid
import threading
import webbrowser
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_file, g

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "radar.html")

FL_PROJECTS_URL = "https://www.fl.ru/projects/"
FL_RSS_URL = "https://www.fl.ru/rss/all.xml"

CHECK_INTERVAL = 7  # seconds

DEFAULT_STOPWORDS = [
    "1с", "1c", "1-с", "1-c", "1 с", "1 c", "одинэс", "один эс", "мойскlad", "мой склад",
    "битрикс", "bitrix", "1с-битрикс", "1c-bitrix", "бус",
    "крипта", "криптой", "крипту", "криптовалют", "криптокошел", "crypto", "usdt", "btc", "eth", "ton", "bybit", "binance",
    "отзыв", "парсинг",
]

CATEGORIES = [
    {"id": "site",    "name": "Сайты под ключ",        "url": "https://www.fl.ru/projects/category/saity/sajt-pod-kljuch/"},
    {"id": "landing", "name": "Лендинги",               "url": "https://www.fl.ru/projects/category/saity/landing/"},
    {"id": "verstka", "name": "Вёрстка",                "url": "https://www.fl.ru/projects/category/saity/html-verstalschik/"},
    {"id": "webprog", "name": "Веб-программирование",   "url": "https://www.fl.ru/projects/category/saity/web-programmist-razrabotka-sajtov/"},
    {"id": "design",  "name": "Дизайн сайтов",          "url": "https://www.fl.ru/projects/category/dizajn/ui-ux-dizajn/"},
    {"id": "all",     "name": "Вся разработка сайтов",  "url": "https://www.fl.ru/projects/category/saity/"},
]

AI_PROVIDERS = {
    "pollinations": {"base_url": "https://text.pollinations.ai/openai", "model": "openai"},
    "openrouter":   {"base_url": "https://openrouter.ai/api/v1", "model": "google/gemini-2.0-flash-exp:free"},
    "groq":         {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "deepseek":     {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "gemini":       {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-2.0-flash"},
}

DEFAULT_AI = {
    "provider": "pollinations",
    "base_url": AI_PROVIDERS["pollinations"]["base_url"],
    "api_key": "free",
    "model": AI_PROVIDERS["pollinations"]["model"],
}

DEFAULT_SETTINGS = {
    "stopwords": DEFAULT_STOPWORDS,
    "categories": ["all"],
    "ai": DEFAULT_AI,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

STRONG_BLOCK_MARKERS = [
    "ddos-guard", "checking your browser", "attention required",
    "cf-browser-verification", "доступ ограничен", "too many requests",
]

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# --------------------------------------------------------------------------- #
# Settings (persisted in DB)
# --------------------------------------------------------------------------- #
def merge_defaults(s):
    d = {
        "stopwords": list(DEFAULT_SETTINGS["stopwords"]),
        "categories": list(DEFAULT_SETTINGS["categories"]),
        "ai": dict(DEFAULT_SETTINGS["ai"]),
    }
    if isinstance(s, dict):
        if isinstance(s.get("stopwords"), list):
            d["stopwords"] = [str(w) for w in s["stopwords"]]
        if isinstance(s.get("categories"), list):
            d["categories"] = [str(c) for c in s["categories"]]
        if isinstance(s.get("ai"), dict):
            for k in ("provider", "base_url", "api_key", "model"):
                if k in s["ai"]:
                    d["ai"][k] = s["ai"][k]
    return d


def load_settings():
    try:
        return merge_defaults(db.get_setting("settings"))
    except Exception as e:
        print("[!] load settings error:", e)
        return merge_defaults(None)


def save_settings_db():
    try:
        db.set_setting("settings", SETTINGS)
    except Exception as e:
        print("[!] save settings error:", e)


SETTINGS = load_settings()
SETTINGS_LOCK = threading.Lock()

ORDERS = {}
ORDERS_LOCK = threading.Lock()
STATS = {"last_check": None, "source": "none", "total_parsed": 0, "count": 0}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_blocked(html_text: str) -> bool:
    if not html_text:
        return True
    low = html_text.lower()
    if any(m in low for m in STRONG_BLOCK_MARKERS):
        return True
    if "/projects/" not in low and len(html_text) < 5000:
        return True
    return False


def is_excluded(order: dict) -> bool:
    with SETTINGS_LOCK:
        words = [w.lower() for w in SETTINGS["stopwords"] if w]
    text = (order.get("title", "") + " " + order.get("description", "")).lower()
    return any(w in text for w in words)


def parse_budget(card_text: str) -> str:
    patterns = [
        r"бюджет[:\s]*([^\n]{1,60})",
        r"оплата[:\s]*([^\n]{1,60})",
        r"([\d\s]{2,15}(?:руб|р\.|₽|рублей))",
        r"(₽\s*[\d\s]+)",
        r"([\d\s]{2,15}\s*(?:\$|€))",
        r"(по договоренности)",
    ]
    for p in patterns:
        m = re.search(p, card_text, re.IGNORECASE)
        if m:
            val = _clean(m.group(1))
            if val:
                return val
    return "По договоренности"


def parse_replies(text: str) -> Optional[int]:
    for p in [r"(?:отклик\w*|ответ\w*|заявк\w*)[:\s]*(\d+)",
              r"(\d+)\s*(?:отклик\w*|ответ\w*|заявк\w*)"]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- #
# Parsing list pages
# --------------------------------------------------------------------------- #
def parse_html(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    seen = set()

    for card in soup.select("div.b-post"):
        a = card.select_one(".b-post__title a") or card.select_one("a[href*='/projects/']")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/projects/(\d+)", href)
        if not m:
            continue
        oid = m.group(1)
        if oid in seen:
            continue
        title = _clean(a.get_text(" "))
        if not title or len(title) < 3:
            continue
        seen.add(oid)

        price_el = card.select_one(".b-post__price")
        price_txt = _clean(price_el.get_text(" ")) if price_el else ""
        budget = price_txt if price_txt else "По договоренности"

        body_el = card.select_one(".b-post__body")
        desc = _clean(body_el.get_text(" ")) if body_el else ""
        if not desc:
            desc = title

        card_text = _clean(card.get_text(" "))
        results.append({
            "id": oid,
            "url": href if href.startswith("http") else "https://www.fl.ru" + href,
            "title": title,
            "budget": budget,
            "description": desc,
            "replies": parse_replies(card_text),
        })

    if results:
        return results

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/projects/(\d+)(?:\.html)?", href)
        if not m:
            continue
        oid = m.group(1)
        if oid in seen:
            continue
        title = _clean(a.get_text(" "))
        if not title or len(title) < 3:
            continue
        seen.add(oid)

        container = a
        for _ in range(5):
            parent = container.parent
            if parent is None:
                break
            container = parent
            if len(_clean(container.get_text(" "))) > 40:
                break
        card_text = _clean(container.get_text(" "))
        results.append({
            "id": oid,
            "url": href if href.startswith("http") else "https://www.fl.ru" + href,
            "title": title,
            "budget": parse_budget(card_text),
            "description": card_text if len(card_text) > len(title) + 10 else title,
            "replies": parse_replies(card_text),
        })
    return results


def parse_rss(xml_bytes):
    import xml.etree.ElementTree as ET
    try:
        if isinstance(xml_bytes, str):
            xml_bytes = xml_bytes.encode("utf-8", "ignore")
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        print("[!] RSS parse error:", e)
        return []
    results = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "")
        link = _clean(item.findtext("link") or "")
        desc = _clean(item.findtext("description") or "")
        if not title:
            continue
        m = re.search(r"/projects/(\d+)", link)
        oid = m.group(1) if m else None
        if not oid:
            m = re.search(r"(\d{5,})", link)
            oid = m.group(1) if m else link
        results.append({
            "id": oid,
            "url": link or FL_PROJECTS_URL,
            "title": title,
            "budget": parse_budget(desc + " " + title),
            "description": desc or title,
            "replies": None,
        })
    return results


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_category(url):
    try:
        resp = SESSION.get(url, timeout=12)
        if resp.status_code == 200 and not is_blocked(resp.text):
            return parse_html(resp.text), True
    except Exception as e:
        print("[!] category fetch failed:", url, e)
    return [], False


def fetch_rss():
    try:
        resp = SESSION.get(FL_RSS_URL, timeout=12)
        if resp.status_code == 200:
            return parse_rss(resp.content), True
    except Exception as e:
        print("[!] RSS fetch failed:", e)
    return [], False


def ingest(raw_orders, category_name):
    added = 0
    with ORDERS_LOCK:
        for order in raw_orders:
            if not order.get("id"):
                continue
            if is_excluded(order):
                continue
            if order["id"] not in ORDERS:
                order["first_seen"] = datetime.now().isoformat(timespec="seconds")
                order["category"] = category_name
                ORDERS[order["id"]] = order
                added += 1
            else:
                ORDERS[order["id"]]["replies"] = order.get("replies", ORDERS[order["id"]].get("replies"))
    return added


def poll_loop():
    while True:
        try:
            with SETTINGS_LOCK:
                selected_ids = list(SETTINGS["categories"])
            cats = [c for c in CATEGORIES if c["id"] in selected_ids]
            if not cats:
                cats = [c for c in CATEGORIES if c["id"] == "all"] or CATEGORIES

            total_parsed = 0
            added = 0
            need_rss = False
            source = "html"
            for cat in cats:
                raw, ok = fetch_category(cat["url"])
                if ok:
                    total_parsed += len(raw)
                    added += ingest(raw, cat["name"])
                else:
                    need_rss = True

            if need_rss or total_parsed == 0:
                rss, rok = fetch_rss()
                if rok:
                    total_parsed += len(rss)
                    added += ingest(rss, "RSS (резерв)")
                    if not need_rss:
                        source = "rss"

            STATS["last_check"] = datetime.now().isoformat(timespec="seconds")
            STATS["source"] = source
            STATS["total_parsed"] = total_parsed
            STATS["count"] = len(ORDERS)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] src={source} parsed={total_parsed} new={added} cats={len(cats)}")
        except Exception as e:
            print("[!] poll error:", e)
        time.sleep(CHECK_INTERVAL)


def get_orders_sorted():
    with ORDERS_LOCK:
        orders = list(ORDERS.values())
    orders.sort(key=lambda o: o.get("first_seen", ""), reverse=True)
    return orders


# --------------------------------------------------------------------------- #
# Order details parsing
# --------------------------------------------------------------------------- #
def parse_order_details(html_text: str, url: str):
    soup = BeautifulSoup(html_text, "html.parser")
    info = {
        "url": url, "title": None, "description": None, "budget": None,
        "currency": None, "replies": None, "published": None,
        "customer": {}, "safe_deal": False,
    }

    h1 = soup.select_one("h1#prj_name_5529872") or soup.select_one("h1.text-3") or soup.select_one("h1")
    if h1:
        info["title"] = _clean(h1.get_text(" "))

    node = (soup.select_one("[id^=projectp]") or soup.select_one(".wizard__editor")
            or soup.select_one("[id^=project_info_]"))
    if node:
        info["description"] = _clean(node.get_text("\n"))

    for el in soup.select("div.text-4, span.text-4"):
        t = el.get_text(" ", strip=True)
        if t.startswith("Оплата") or t.startswith("Бюджет"):
            val = _clean(re.sub(r"^(Оплата|Бюджет)\s*:?\s*", "", t))
            if not val:
                spans = el.find_all("span")
                if spans:
                    val = _clean(spans[-1].get_text(" "))
            if val:
                info["budget"] = val
            break
    if not info["budget"]:
        m = re.search(r"(?:Оплата|Бюджет)\s*:?\s*([^\n<]{1,80})", soup.get_text("\n"))
        if m:
            info["budget"] = _clean(m.group(1))

    m = re.search(r"(₽|руб|RUB|\$|€|USD|EUR)", info["budget"] or "")
    info["currency"] = m.group(1) if m else None

    sidebar = soup.select_one(".fl-project-card") or soup
    name_el = sidebar.select_one(".fl-project-card__title .font-weight-bold")
    if name_el:
        info["customer"]["name"] = _clean(name_el.get_text(" "))

    for sec in sidebar.select("section"):
        t = _clean(sec.get_text(" "))
        if "Зарегистрирован" in t:
            m = re.search(r"Зарегистрирован\s*:?\s*(.+)", t)
            if m:
                info["customer"]["registered"] = _clean(m.group(1))
        if "Отзыв" in t:
            m = re.search(r"Отзыв\w*\s*·?\s*(\d+)", t)
            if m:
                info["customer"]["reviews"] = int(m.group(1))
            m = re.search(r"(\d+[.,]\d+|\d+)\s*$", t)
            if m:
                info["customer"]["rating"] = m.group(1)

    if sidebar.select_one('a[href*="/promo/bezopasnaya-sdelka/"]'):
        info["safe_deal"] = True

    full_text = soup.get_text("\n")
    info["replies"] = parse_replies(full_text)

    m = re.search(r"Опубликован\w*\s+([0-9]{2}\.[0-9]{2}\.[0-9]{4}(?:\s+в\s+[0-9:]+)?)", full_text)
    if m:
        info["published"] = _clean(m.group(1))

    return info


# --------------------------------------------------------------------------- #
# AI
# --------------------------------------------------------------------------- #
def ai_chat(messages, temperature=0.5):
    with SETTINGS_LOCK:
        ai = dict(SETTINGS["ai"])
    if not ai.get("api_key"):
        return None, "API-ключ не задан. Укажите его в настройках (шестерёнка)."
    url = ai["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + ai["api_key"], "Content-Type": "application/json"}
    payload = {"model": ai["model"], "messages": messages, "temperature": temperature}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            return None, f"Ошибка провайдера {r.status_code}: {r.text[:300]}"
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return content, None
    except Exception as e:
        return None, "Ошибка запроса к ИИ: " + str(e)


def build_order_context(order):
    return (f"Заголовок: {order.get('title','')}\n"
            f"Бюджет: {order.get('budget','')}\n"
            f"Описание:\n{order.get('description','')}")


# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #
flask_app = Flask(__name__)

# Инициализация БД и фонового парсера (один раз при импорте/запуске)
db.init_db()
threading.Thread(target=poll_loop, daemon=True).start()


@flask_app.before_request
def _uid():
    uid = request.cookies.get("fl_uid")
    if not uid:
        uid = uuid.uuid4().hex
        g.new_uid = uid
        try:
            db.ensure_user(uid)
        except Exception:
            pass
    else:
        g.new_uid = None
    g.uid = uid


@flask_app.after_request
def _after(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    if getattr(g, "new_uid", None):
        resp.set_cookie("fl_uid", g.new_uid, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@flask_app.get("/")
def index():
    if os.path.exists(HTML_PATH):
        return send_file(HTML_PATH)
    return "<h1>radar.html not found</h1>", 404


@flask_app.get("/api/categories")
def api_categories():
    return jsonify(CATEGORIES)


@flask_app.get("/api/providers")
def api_providers():
    return jsonify(AI_PROVIDERS)


@flask_app.get("/api/settings")
def api_settings_get():
    with SETTINGS_LOCK:
        return jsonify(SETTINGS)


@flask_app.post("/api/settings")
def api_settings_set():
    global SETTINGS
    data = request.get_json(force=True, silent=True) or {}
    with SETTINGS_LOCK:
        if isinstance(data.get("stopwords"), list):
            SETTINGS["stopwords"] = [str(w).strip() for w in data["stopwords"] if str(w).strip()]
        if isinstance(data.get("categories"), list):
            SETTINGS["categories"] = [str(c) for c in data["categories"]]
        if isinstance(data.get("ai"), dict):
            for k in ("provider", "base_url", "api_key", "model"):
                if k in data["ai"]:
                    SETTINGS["ai"][k] = data["ai"][k]
        save_settings_db()
    with ORDERS_LOCK:
        for oid, order in list(ORDERS.items()):
            if is_excluded(order):
                ORDERS.pop(oid, None)
    return jsonify({"ok": True, "settings": SETTINGS, "count": len(ORDERS)})


@flask_app.get("/api/orders")
def api_orders():
    with SETTINGS_LOCK:
        cats = set(SETTINGS["categories"])
    return jsonify({
        "orders": get_orders_sorted(),
        "stats": {**STATS, "count": len(ORDERS)},
        "categories": [c["name"] for c in CATEGORIES if c["id"] in cats],
    })


@flask_app.get("/api/order_details")
def api_order_details():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "url is required"}), 400
    if url.startswith("/"):
        url = "https://www.fl.ru" + url
    try:
        resp = SESSION.get(url, timeout=15)
        if resp.status_code != 200 or is_blocked(resp.text):
            return jsonify({"error": "Не удалось загрузить страницу заказа", "status": resp.status_code}), 502
        return jsonify(parse_order_details(resp.text, url))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@flask_app.post("/api/ai/summarize")
def api_ai_summarize():
    order = request.get_json(force=True, silent=True) or {}
    messages = [
        {"role": "system", "content": (
            "Ты — помощник фрилансера на бирже FL.ru. Прочитай ТЗ и верни СТРОГО 3 коротких пункта, "
            "каждый с новой строки, начинающийся с «• ». 1) Что конкретно надо сделать. "
            "2) На чём / ключевые требования (технологии, движки, сроки). "
            "3) Подводные камни (сложная база, 1С, крипта, сжатые сроки, размытый проект). "
            "Пиши максимально сжато и по делу, без воды.")},
        {"role": "user", "content": build_order_context(order)},
    ]
    content, err = ai_chat(messages, temperature=0.3)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"text": content})


@flask_app.post("/api/ai/proposal")
def api_ai_proposal():
    order = request.get_json(force=True, silent=True) or {}
    messages = [
        {"role": "system", "content": (
            "Ты — фрилансер, который пишет живой человеческий отклик на заказ. Напиши 2–4 коротких предложения "
            "строго по сути задачи: почему подходишь и что предлагаешь. Без канцеляризмов, штампов и воды. "
            "В конце задай один логичный уточняющий вопрос по задаче. Не используй списки, только связный текст.")},
        {"role": "user", "content": build_order_context(order)},
    ]
    content, err = ai_chat(messages, temperature=0.7)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"text": content})


@flask_app.get("/api/state")
def api_state():
    uid = g.uid
    try:
        return jsonify({
            "favorites": db.get_uid_list(uid, "favorites"),
            "hidden": db.get_uid_list(uid, "hidden"),
        })
    except Exception as e:
        return jsonify({"favorites": [], "hidden": [], "error": str(e)})


@flask_app.post("/api/favorite")
def api_favorite():
    d = request.get_json(force=True, silent=True) or {}
    oid = str(d.get("id", ""))
    val = bool(d.get("value", True))
    if oid:
        try:
            db.set_uid_item(g.uid, "favorites", oid, val)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True})


@flask_app.post("/api/hidden")
def api_hidden():
    d = request.get_json(force=True, silent=True) or {}
    oid = str(d.get("id", ""))
    val = bool(d.get("value", True))
    if oid:
        try:
            db.set_uid_item(g.uid, "hidden", oid, val)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True})


try:
    from asgiref.wsgi import WsgiToAsgi
    app = WsgiToAsgi(flask_app)
except Exception:
    app = flask_app


def main():
    port = int(os.environ.get("PORT", "8000"))
    host = "0.0.0.0"
    # Открываем браузер только при локальном запуске (PORT не задан)
    if not os.environ.get("PORT"):
        try:
            threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        except Exception:
            pass
    print(f"FL.Radar on {host}:{port}  (DB: {'PostgreSQL' if db.USE_PG else 'SQLite'})")
    flask_app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()