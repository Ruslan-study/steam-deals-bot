import json
import logging
import os
import threading
import time
import urllib.request
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.environ.get("BOT_TOKEN_STEAM", "").strip()
ADMIN_CHAT_ID = os.environ.get("CHAT_ID", "").strip()

SEND_HOUR = 10
MINSK_TZ = ZoneInfo("Europe/Minsk")
DISCOUNT_DAYS = 7
SEEN_FILE = "seen_deals.json"

API = f"https://api.telegram.org/bot{TOKEN}"

# --- Кнопки ---
BTN_ALL_DEALS = "🔥 Все скидки сейчас"
KB_MAIN = json.dumps({"keyboard": [[{"text": BTN_ALL_DEALS}]], "resize_keyboard": True})
KB_INLINE = json.dumps({"inline_keyboard": [[{"text": "🎮 Показать скидки", "callback_data": "show_deals"}]]})


def tg_send(chat_id: int, text: str, reply_markup=None, parse_mode=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup: params["reply_markup"] = reply_markup
    if parse_mode: params["parse_mode"] = parse_mode
    data = urlencode(params).encode()
    req = urllib.request.Request(f"{API}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp: resp.read()


def tg_send_photo(chat_id: int, photo_url: str, caption: str, reply_markup=None, parse_mode="HTML"):
    """Отправляет изображение с подписью (поддерживает HTML)."""
    params = {"chat_id": chat_id, "photo": photo_url, "caption": caption, "parse_mode": parse_mode}
    if reply_markup: params["reply_markup"] = reply_markup
    data = urlencode(params).encode()
    req = urllib.request.Request(f"{API}/sendPhoto", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp: resp.read()


def tg_answer_callback(callback_id: str):
    url = f"{API}/answerCallbackQuery?callback_query_id={callback_id}"
    with urllib.request.urlopen(url, timeout=10) as resp: resp.read()


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f: json.dump(seen, f, ensure_ascii=False)


def fetch_specials():
    """Список текущих акций из Steam API с обложками и ценами для региона BY."""
    url = "https://store.steampowered.com/api/featuredcategories?cc=by" # cc=by для региона Беларусь
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    
    items = data.get("specials", {}).get("items", [])
    result = []
    
    for i in items:
        app_id = str(i["id"])
        # Дозапрашиваем детали, чтобы получить точную цену в местной валюте (price_overview)
        details_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=by&filters=price_overview"
        try:
            with urllib.request.urlopen(details_url, timeout=10) as d_resp:
                details = json.loads(d_resp.read())
            
            # Если данные получены и цена есть (игра не бесплатная)
            if details.get(app_id, {}).get("success") and details[app_id]["data"].get("price_overview"):
                price_info = details[app_id]["data"]["price_overview"]
                final_price = price_info["final"] / 100 # Переводим центы в рубли
                currency = price_info.get("currency", "BYN") # Обычно для BY это BYN (или USD в зависимости от Steam)
                formatted_price = f"{final_price:.2f} {currency}"
            else:
                # Фоллбэк, если price_overview нет (например, игра бесплатна или нет региональной цены)
                formatted_price = f"{i['final_price'] / 100:.2f} USD" 
        except Exception:
            logging.exception(f"Не удалось получить цену для {app_id}")
            formatted_price = "Цена не найдена"
            
        result.append({
            "id": app_id,
            "name": i["name"],
            "discount": i["discount_percent"],
            "price": formatted_price,
            "url": f"https://store.steampowered.com/app/{app_id}",
            "image": i.get("header_image", ""), # Ссылка на картинку-обложку
        })
        
    return result


def fmt_new(d):
    return f"🆕 <b>{d['name']}</b>\n{d['price']} (Скидка -{d['discount']}%)\n{d['url']}"

def fmt_last(d):
    return f"⏳ <b>{d['name']}</b> (Последний день!)\n{d['price']} (Скидка -{d['discount']}%)\n{d['url']}"


def daily_check():
    today_dt = datetime.now(MINSK_TZ).date()
    today = today_dt.isoformat()
    try:
        deals = fetch_specials()
    except Exception:
        logging.exception("Не удалось получить скидки Steam")
        return

    seen = load_seen()
    to_show = []

    for d in deals:
        first = seen.get(d["id"])
        if first is None:
            to_show.append(d) # Сохраняем объект для отправки фото
            seen[d["id"]] = today
        elif (today_dt - date.fromisoformat(first)).days == DISCOUNT_DAYS - 1:
            to_show.append(d)

    current = {d["id"] for d in deals}
    for gone in [i for i in seen if i not in current]:
        del seen[gone]
    save_seen(seen)

    for d in to_show[:10]:
        try:
            caption = fmt_new(d) if seen[d["id"]] == today else fmt_last(d)
            tg_send_photo(ADMIN_CHAT_ID, d["image"], caption)
        except Exception:
            logging.exception(f"Не удалось отправить {d['name']}")
        time.sleep(1)


def scheduler_loop():
    sent_for = None
    while True:
        now = datetime.now(MINSK_TZ)
        if now.hour == SEND_HOUR and sent_for != now.date().isoformat():
            daily_check()
            sent_for = now.date().isoformat()
        time.sleep(60)


def send_current_deals(chat_id):
    """Формирует и отправляет список текущих скидок с картинками."""
    try:
        deals = fetch_specials()
        deals.sort(key=lambda x: x["discount"], reverse=True)
        
        if not deals:
            tg_send(chat_id, "Сейчас нет активных скидок 😢", KB_MAIN)
            return
            
        tg_send(chat_id, "🔥 <b>Топ скидок в Steam прямо сейчас:</b>", parse_mode="HTML")
        
        # Отправляем каждую игру отдельным сообщением с фото
        for d in deals[:15]:
            safe_name = d['name'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            caption = (f"<b>{safe_name}</b>\n"
                       f"<b>-{d['discount']}%</b> → {d['price']}\n"
                       f"<a href='{d['url']}'>Открыть в Steam</a>")
            
            try:
                tg_send_photo(chat_id, d["image"], caption)
                time.sleep(0.5) # Небольшая пауза, чтобы не превысить лимиты Telegram
            except Exception:
                logging.exception(f"Не удалось отправить фото для {d['name']}")
                # Если фото не отправилось (битая ссылка), отправляем текстом
                tg_send(chat_id, f"{safe_name}\n-{d['discount']}% → {d['price']}\n{d['url']}", parse_mode="HTML")
                
        tg_send(chat_id, "Показать ещё раз:", KB_INLINE)
        
    except Exception:
        logging.exception("Ошибка при выводе скидок")
        tg_send(chat_id, "Не удалось получить скидки. Попробуйте позже.", KB_MAIN)


def main():
    if not TOKEN or not ADMIN_CHAT_ID:
        raise SystemExit("Задайте BOT_TOKEN и CHAT_ID в .env")
    
    threading.Thread(target=scheduler_loop, daemon=True).start()
    logging.info("Steam-бот запущен")
    
    offset = None
    while True:
        try:
            url = f"{API}/getUpdates?timeout=30"
            if offset: url += f"&offset={offset}"
            with urllib.request.urlopen(url, timeout=40) as resp:
                updates = json.loads(resp.read())
            
            for upd in updates.get("result", []):
                offset = upd["update_id"] + 1
                
                callback = upd.get("callback_query")
                if callback:
                    tg_answer_callback(callback["id"])
                    if callback.get("data") == "show_deals":
                        send_current_deals(callback["message"]["chat"]["id"])
                    continue

                msg = upd.get("message")
                if msg and "text" in msg:
                    chat_id = msg["chat"]["id"]
                    text = msg["text"].strip()
                    
                    if text == "/start":
                        tg_send(chat_id, "Привет! Я бот для отслеживания скидок Steam. Нажми кнопку ниже 👇", KB_MAIN)
                        tg_send(chat_id, "Или нажми сюда:", KB_INLINE)
                    
                    elif text == BTN_ALL_DEALS:
                        tg_send(chat_id, "⏳ Собираю актуальные скидки...")
                        send_current_deals(chat_id)
                        
        except Exception:
            logging.exception("Ошибка getUpdates")
            time.sleep(5)

if __name__ == "__main__":
    main()