#!/usr/bin/env python3
"""
WallaForged - Buscador de anuncios olvidados en Wallapop.

Busca anuncios de mandos PS4/PS5 que llevan dias/semanas publicados sin venderse,
ordenados por PRECIO ASCENDENTE (no newest). Filtra vendedores fantasma
(sin actividad reciente) y descarta anuncios recientes (< 24h) que ya cubre WallaScanner.

Uso:
    python wallaforged.py --once   # una sola pasada (ideal para GitHub Actions)
    python wallaforged.py          # modo monitor continuo

Requiere: requests
    pip install requests
"""

import argparse
import json
import os
import sys
import time
import threading
from datetime import datetime, timezone

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import requests
except ImportError:
    print("ERROR: falta el modulo 'requests'. Instalalo con: pip install requests")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

API_BASE = "https://api.wallapop.com/api/v3"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://es.wallapop.com",
    "Referer": "https://es.wallapop.com/",
    "X-DeviceOS": "0",
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: no existe {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state(state_file):
    path = os.path.join(BASE_DIR, state_file)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {"seen": []}
    return {"seen": []}


def save_state(state_file, state):
    path = os.path.join(BASE_DIR, state_file)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=15)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  [!] Rate limit (429). Esperando {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            return None
    return None


def get_user(user_id):
    return _get_json(f"{API_BASE}/users/{user_id}")


def get_user_stats(user_id):
    return _get_json(f"{API_BASE}/users/{user_id}/stats")


def get_user_online(user_id):
    """Obtiene el estado de conexion de un vendedor.

    Devuelve {"online": bool, "last_connection": str} o None.
    El campo last_connection puede ser: "3_hours", "1_day", "2_days",
    "4_days", "2_weeks", "more_than_1_month", etc.
    No requiere autenticacion.
    """
    return _get_json(f"{API_BASE}/users/{user_id}/online")


def parse_last_connection(last_connection):
    """Convierte el string last_connection a horas minimas estimadas.

    Ejemplos: "3_hours" -> 3, "1_day" -> 24, "more_than_1_month" -> 744
    """
    if not last_connection:
        return None
    lc = last_connection.lower().replace("_", " ")
    if "hour" in lc:
        parts = lc.split()
        for p in parts:
            if p.isdigit():
                return int(p)
        return 1
    if "day" in lc:
        parts = lc.split()
        for p in parts:
            if p.isdigit():
                return int(p) * 24
        return 24
    if "week" in lc:
        parts = lc.split()
        for p in parts:
            if p.isdigit():
                return int(p) * 168
        return 168
    if "month" in lc:
        return 744
    return None


def seller_is_ghost(user, stats, item, filters, now_ms, item_detail=None, online_data=None):
    """Determina si un vendedor es un perfil fantasma (no responde, no activo).

    ARMA SECRETA: GET /api/v3/users/{id}/online devuelve online+last_connection
    SIN autenticacion. Es el filtro #1 de WallaForged.
    Returns (is_ghost, razon).
    """
    if not filters.get("enable_ghost_filter", True):
        return False, ""

    reasons = []
    max_last_connection_hours = filters.get("max_last_connection_hours", 72)

    # PRIORIDAD #1: Estado online / ultima conexion directo desde la API
    if online_data and isinstance(online_data, dict):
        online = online_data.get("online", False)
        last_conn = online_data.get("last_connection")

        if online:
            return False, ""

        if last_conn:
            hours = parse_last_connection(last_conn)
            if hours is not None and hours > max_last_connection_hours:
                reasons.append(f"ultima conexion: {last_conn} (> {max_last_connection_hours}h)")
        else:
            reasons.append("sin datos de conexion")

    # Filtros secundarios (solo si /online fallo o necesitamos mas contexto)
    def _get_counter(c, key):
        if isinstance(c, dict):
            return c.get(key, 0) or 0
        if isinstance(c, list):
            for entry in c:
                if isinstance(entry, dict) and entry.get("type") == key:
                    return entry.get("value", 0) or 0
        return 0

    sells = buys = reviews = 0
    if stats:
        c = stats.get("counters", {})
        sells = _get_counter(c, "sells")
        buys = _get_counter(c, "buys")
        reviews = _get_counter(c, "reviews")

    if not online_data:
        reg = user.get("register_date") if user else None
        if reg:
            age_days = (now_ms - reg) / 86400000.0
            max_ghost_age = filters.get("max_ghost_account_age_days", 60)
            if age_days > max_ghost_age and sells == 0 and buys == 0 and reviews == 0:
                reasons.append(f"cuenta de {age_days:.0f} dias sin actividad")

        modified_ts = None
        if item_detail and isinstance(item_detail, dict):
            modified_ts = item_detail.get("modified_date")
        if not modified_ts:
            modified_ts = item.get("modified_at")
            if modified_ts:
                modified_ts = modified_ts / 1000.0
        if modified_ts:
            import time as _time
            days_since_mod = (_time.time() - modified_ts) / 86400.0
            max_days_mod = filters.get("max_days_since_item_modified", 14)
            if days_since_mod > max_days_mod and sells == 0:
                reasons.append(f"anuncio sin tocar {days_since_mod:.0f} dias")

    if reasons:
        return True, " | ".join(reasons[:2])
    return False, ""


def search_items(keywords, min_price, max_price, order_by, location, max_pages=5):
    """Busca anuncios con paginacion. Usa el endpoint directo /search.

    Devuelve lista de items. Itera hasta max_pages paginas o hasta que no haya
    mas resultados.
    """
    all_items = {}
    next_page = None

    for page in range(max_pages):
        params = {
            "keywords": keywords,
            "min_sale_price": min_price,
            "max_sale_price": max_price,
            "order_by": order_by,
            "source": "keywords",
            "step": 1,
            "limit": 40,
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "country_code": location.get("country_code", "ES"),
        }
        if next_page:
            params["next_page"] = next_page

        try:
            r = requests.get(f"{API_BASE}/search", params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [!] Error en busqueda pag. {page+1}: {e}")
            break

        items = []
        section = data.get("data", {}).get("section")
        if section and isinstance(section, dict):
            payload = section.get("payload", {})
            items = payload.get("items", []) if isinstance(payload, dict) else []
        if not items:
            items = data.get("data", {}).get("items", []) or data.get("search_objects", [])

        for it in items:
            all_items[it["id"]] = it

        # Siguiente pagina
        meta = data.get("meta", {})
        if isinstance(meta, dict):
            search_metadata = meta.get("search", {}) if isinstance(meta.get("search"), dict) else {}
            next_page = search_metadata.get("next_page") or meta.get("next_page")
        else:
            next_page = None

        if not next_page:
            break

        time.sleep(0.3)

    return list(all_items.values())


def title_has_excluded(title, exclude_keywords):
    t = title.lower()
    for kw in exclude_keywords:
        if kw.lower() in t:
            return kw
    return None


def item_age_hours(created_at_ms, now_ms):
    return (now_ms - created_at_ms) / 3600000.0


def format_age(age_h):
    total_min = int(age_h * 60)
    if total_min < 1:
        return "hace menos de 1 min"
    if total_min < 60:
        return f"hace {total_min} min"
    hours = total_min // 60
    minutes = total_min % 60
    if minutes == 0:
        return f"hace {hours} h"
    return f"hace {hours} h {minutes} min"


def format_age_days(age_h):
    """Formato para anuncios antiguos: 'hace 3 dias 12 h' o 'hace 2 semanas'."""
    days = int(age_h // 24)
    hours = int(age_h % 24)
    if days >= 7:
        weeks = days // 7
        return f"hace {weeks} sem" + (f" {days % 7} d" if days % 7 else "")
    if days > 0:
        return f"hace {days} d" + (f" {hours} h" if hours else "")
    return format_age(age_h)


def haversine_km(lat1, lon1, lat2, lon2):
    import math
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def distance_from_user(item_loc, user_loc):
    try:
        lat = float(item_loc.get("latitude"))
        lon = float(item_loc.get("longitude"))
        ulat = float(user_loc.get("latitude"))
        ulon = float(user_loc.get("longitude"))
        km = haversine_km(lat, lon, ulat, ulon)
        if km < 1:
            return "a menos de 1 km"
        return f"a ~{int(round(km))} km"
    except (TypeError, ValueError, AttributeError):
        return ""


def resolve_telegram_chat_id(token):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
        data = r.json()
        if data.get("ok") and data.get("result"):
            for upd in reversed(data["result"]):
                chat = (upd.get("message") or upd.get("edited_message") or {}).get("chat")
                if chat and chat.get("id"):
                    return str(chat["id"])
    except Exception:
        pass
    return None


def log_telegram(line):
    try:
        with open(os.path.join(BASE_DIR, "telegram.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def send_telegram(cfg, message):
    t = cfg.get("notifications", {}).get("telegram", {})
    if not t.get("enabled"):
        return False
    token = t.get("bot_token")
    chat_id = t.get("chat_id")
    if not token:
        return False
    if not chat_id:
        chat_id = resolve_telegram_chat_id(token)
        if chat_id:
            t["chat_id"] = chat_id
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                raw.setdefault("notifications", {}).setdefault("telegram", {})["chat_id"] = chat_id
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
                print(f"  [i] chat_id de Telegram guardado: {chat_id}")
            except Exception:
                pass
    if not chat_id:
        return False
    es_hour = (datetime.now(timezone.utc).hour + 2) % 24
    silent = 22 <= es_hour or es_hour < 8
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                      "disable_notification": silent},
                timeout=10,
            )
            j = r.json()
            if j.get("ok"):
                return True
            last_err = j.get("description")
            log_telegram(f"FAIL {r.status_code} {j.get('description')} :: {message[:60]}")
        except Exception as e:
            last_err = str(e)
            log_telegram(f"EXC {e} :: {message[:60]}")
        time.sleep(1)
    print(f"  [!] Telegram NO enviado: {last_err}")
    return False


def notify(cfg, items_to_report, search_name):
    n = cfg.get("notifications", {})
    if not items_to_report:
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"[WALLAFORGED] {ts} - {search_name} - {len(items_to_report)} olvidado(s)"]
    for it in items_to_report:
        lines.append("-" * 50)
        lines.append(f"  {it['title']}")
        lines.append(f"  Precio: {it['price']} EUR  |  {it['location']}")
        lines.append(f"  Vendedor: {it['seller']}  ({it['seller_info']})")
        conn_tag = f"  |  {it['last_connection']}" if it.get('last_connection') else ""
        lines.append(f"  Publicado: {it['age']}  |  {it['shipping']}  |  {it['interest']}  |  {it['distance']}{conn_tag}")
        lines.append(f"  {it['url']}")

    msg = "\n".join(lines)

    if n.get("console", True):
        print("\n" + msg + "\n")

    if n.get("log_file"):
        log_path = os.path.join(BASE_DIR, n["log_file"])
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n\n")

    header = f"🔎 <b>WallaForged - {search_name}</b>"
    tg_sent = 0
    for it in items_to_report:
        tg = (
            f"{header}\n"
            f"────────────────────────────\n"
            f"🔔 <a href=\"{it['url']}\">{it['title']}</a>\n"
            f"💶 {it['price']} EUR   📍 {it['location']} ({it['distance']})\n"
            f"{it['shipping']}   ·   {it['interest']}\n"
            f"👤 {it['seller']} · {it['seller_info']}\n"
            f"🕒 {it['age']}   🏷️ {it.get('order_tag', '')}\n"
            f"🟢 {it.get('last_connection', '?')}\n"
            f"🔗 {it['url']}"
        )
        if len(tg) > 4000:
            tg = tg[:3990] + "..."
        ok = send_telegram(cfg, tg)
        if ok:
            tg_sent += 1
        time.sleep(0.3)
    print(f"  [i] Telegram: enviados {tg_sent}/{len(items_to_report)} para '{search_name}'")


def run_once(cfg, initial=False):
    state = load_state(cfg.get("state_file", "wallaforged_seen_items.json"))
    seen = set(state.get("seen", []))
    now_ms = int(time.time() * 1000)
    new_count = 0
    initial_limit = cfg.get("initial_send_count", 10)

    tcfg = cfg.get("notifications", {}).get("telegram", {})
    if tcfg.get("enabled") and tcfg.get("bot_token") and not tcfg.get("chat_id"):
        resolved = resolve_telegram_chat_id(tcfg["bot_token"])
        if resolved:
            tcfg["chat_id"] = resolved
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                print(f"  [i] chat_id de Telegram guardado: {resolved}")
            except Exception:
                pass

    for s in cfg.get("searches", []):
        name = s.get("name", s.get("keywords"))
        keywords_list = s.get("keywords", [])
        min_p = s.get("min_price", 1)
        max_p = s.get("max_price", 100)
        order = s.get("order_by", "price_low_to_high")
        loc = cfg.get("location", {})
        flt = cfg.get("filters", {})
        max_pages = cfg.get("max_search_pages", 5)
        min_age_hours = flt.get("min_item_age_hours", 24)

        print(f"[*] Buscando: {name} ({keywords_list}) | orden: {order} | paginas: {max_pages}")
        collected = {}
        for kw in keywords_list:
            for it in search_items(kw, min_p, max_p, order, loc, max_pages):
                collected[it["id"]] = it

        print(f"  [i] Total items sin filtrar: {len(collected)}")

        for it in collected.values():
            if it["id"] in seen:
                continue

            age_h = item_age_hours(it.get("created_at", now_ms), now_ms)

            # Saltar anuncios RECIENTES (< min_age_hours) — eso lo cubre WallaScanner
            if age_h < min_age_hours:
                seen.add(it["id"])
                continue

            title = it.get("title", "")
            excl = title_has_excluded(title, flt.get("exclude_keywords", []))
            if excl:
                seen.add(it["id"])
                print(f"  [-]Descartado (contiene '{excl}'): {title}")
                continue

            must = flt.get("must_mention", [])
            if must and not any(k.lower() in title.lower() for k in must):
                seen.add(it["id"])
                print(f"  [-]Descartado (no es mando): {title}")
                continue

            req_kw = flt.get("require_keywords_in_title", [])
            if req_kw and not any(k.lower() in title.lower() for k in req_kw):
                seen.add(it["id"])
                continue

            # Filtrar anuncios de 1€ (suelen ser pegatinas/basura)
            price_amount = it.get("price", {}).get("amount", 0)
            if flt.get("skip_price_1euro", True) and price_amount == 1:
                seen.add(it["id"])
                print(f"  [-]Descartado (1€ basura): {title}")
                continue

            if flt.get("require_original", False):
                tl = title.lower()
                non_original = [
                    "compatible", "alternativo", "alternativa", "generico", "gen", "marca",
                    "third party", "third-party", "no original", "no oficial", "reacondicionado",
                    "recambio", "universal", "clone", "clon", "fake", "imitation",
                ]
                if any(k in tl for k in non_original):
                    seen.add(it["id"])
                    print(f"  [-]Descartado (no original): {title}")
                    continue

            # FILTRO GHOST — llamada RAPIDA a /online (sin auth) primero
            uid = it.get("user_id")
            online_data = get_user_online(uid) if uid else None
            time.sleep(0.15)

            online_valid = online_data and isinstance(online_data, dict)
            if online_valid:
                ghost, reason = seller_is_ghost({}, {}, it, flt, now_ms, None, online_data)
                if ghost:
                    seen.add(it["id"])
                    print(f"  [-]Descartado (fantasma: {reason}): {title}")
                    continue

            user = get_user(uid) if uid else None
            stats = get_user_stats(uid) if uid else None
            detail = _get_json(f"{API_BASE}/items/{it['id']}")
            time.sleep(0.2)

            if not online_valid:
                ghost, reason = seller_is_ghost(user, stats, it, flt, now_ms, detail, None)
                if ghost:
                    seen.add(it["id"])
                    print(f"  [-]Descartado (fantasma heuristico: {reason}): {title}")
                    continue

            # FILTRO DISTANCIA + ENVIO: si el anuncio esta lejos y no tiene envio, descartar
            max_dist = cfg.get("max_local_distance_km", 30)
            ship = it.get("shipping", {})
            allows_shipping = ship.get("user_allows_shipping", True) if isinstance(ship, dict) else True
            dist_str = distance_from_user(it.get("location", {}), cfg.get("location", {}))
            dist_km = None
            if dist_str and "km" in dist_str:
                try:
                    dist_km = int(dist_str.split("~")[1].split("km")[0].strip())
                except (ValueError, IndexError):
                    pass
            if dist_km is not None and dist_km > max_dist and not allows_shipping:
                seen.add(it["id"])
                print(f"  [-]Descartado (lejos + sin envio: {dist_str}): {title}")
                continue

            seller_name = (user or {}).get("micro_name", "desconocido")

            def _cnt(key):
                if not stats:
                    return 0
                c = stats.get("counters", [])
                if isinstance(c, dict):
                    return c.get(key, 0) or 0
                if isinstance(c, list):
                    for entry in c:
                        if isinstance(entry, dict) and entry.get("type") == key:
                            return entry.get("value", 0) or 0
                return 0

            seller_info = f"top={ (user or {}).get('is_top_profile') }"
            seller_info += f" | pub={ _cnt('publish') } vend={ _cnt('sells') } comp={ _cnt('buys') }"

            shipping_tag = "Envio" if allows_shipping else "Solo en persona"

            conv = views = 0
            if isinstance(detail, dict):
                c = detail.get("counters", {})
                if isinstance(c, dict):
                    conv = c.get("conversations", 0) or 0
                    views = c.get("views", 0) or 0
            interest_tag = f"{conv} contactos" + (f" · {views} vistas" if views else "")

            user_loc = cfg.get("location", {})
            if not dist_str:
                dist_tag = distance_from_user(it.get("location", {}), user_loc)
            else:
                dist_tag = dist_str

            seen.add(it["id"])
            new_count += 1
            it_url = f"https://es.wallapop.com/item/{it.get('web_slug', it['id'])}"

            # Tag de orden: ayuda a identificar si es una ganga en precio
            price_amount = it.get("price", {}).get("amount", 0)
            if price_amount <= 5:
                order_tag = "Ganga extrema"
            elif price_amount <= 10:
                order_tag = "Muy barato"
            elif price_amount <= 15:
                order_tag = "Barato"
            else:
                order_tag = f"Precio {price_amount}EUR"

            last_conn_str = ""
            if online_data and isinstance(online_data, dict):
                lc = online_data.get("last_connection")
                if lc:
                    last_conn_str = f"conexion: {lc}"

            report = {
                "title": title,
                "price": price_amount,
                "location": it.get("location", {}).get("city", "??"),
                "seller": seller_name,
                "seller_info": seller_info,
                "age": format_age_days(age_h),
                "url": it_url,
                "shipping": shipping_tag,
                "interest": interest_tag,
                "distance": dist_tag,
                "order_tag": order_tag,
                "last_connection": last_conn_str,
            }
            if not hasattr(run_once, "_reports"):
                run_once._reports = {}
            run_once._reports.setdefault(name, []).append(report)

    reports = getattr(run_once, "_reports", {})
    if initial:
        for sname in reports:
            reports[sname] = reports[sname][-initial_limit:]
        header_msg = (f" WALLAFORGED iniciado\n"
                      f"Te enviare un maximo de {initial_limit} anuncios olvidados de prueba "
                      f"por busqueda. Luego solo te avisare de los NUEVOS cada ciclo.")
        send_telegram(cfg, header_msg)
        time.sleep(0.5)
    for sname, items in reports.items():
        print(f"  [i] Notificando {len(items)} anuncios de '{sname}'")
        notify(cfg, items, sname)
    run_once._reports = {}

    if len(seen) > 5000:
        seen = set(list(seen)[-3000:])
    state["seen"] = list(seen)

    hb = cfg.get("heartbeat", {})
    every = hb.get("every_n_passes", 6)
    if new_count == 0:
        pass_count = state.get("pass_count", 0) + 1
    else:
        pass_count = 0
    state["pass_count"] = pass_count
    save_state(cfg.get("state_file", "wallaforged_seen_items.json"), state)

    if hb.get("enabled") and new_count == 0:
        if pass_count % every == 0:
            msg = (f" WALLAFORGED - latido\n"
                   f"Pasada #{pass_count} realizada. 0 anuncios olvidados encontrados.\n"
                   f"WallaForged sigue vigilando.")
            ok = send_telegram(cfg, msg)
            print(f"  [i] Heartbeat enviado: {ok} (pass {pass_count}, every {every})")

    print(f"[+] Pasada completada. {new_count} anuncio(s) olvidado(s) en Total.")
    return new_count


def main():
    parser = argparse.ArgumentParser(description="WallaForged - anuncios olvidados en Wallapop")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola pasada y sale")
    parser.add_argument("--initial", action="store_true", help="Envia los N ultimos como prueba")
    parser.add_argument("--setup", action="store_true", help="Ayuda para configurar Telegram")
    args = parser.parse_args()

    if args.setup:
        print("Para recibir alertas por Telegram:")
        print("1. Habla con @BotFather y crea un bot ( /newbot ).")
        print("2. Copia el token en config.json -> notifications.telegram.bot_token")
        print("3. Escribe a @myidbot y copia tu chat_id en notifications.telegram.chat_id")
        print("4. Pon notifications.telegram.enabled = true")
        return

    cfg = load_config()
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env_token:
        cfg.setdefault("notifications", {}).setdefault("telegram", {})["bot_token"] = env_token
    interval = cfg.get("check_interval_seconds", 43200)

    print("=== WallaForged iniciado ===")
    print(f"Intervalo: {interval}s | Busquedas: {len(cfg.get('searches', []))}")

    if args.once:
        run_once(cfg, initial=args.initial)
        return

    first = True
    PASS_TIMEOUT = 180

    def _watchdog():
        print(f"[!] Watchdog: pasada bloqueada > {PASS_TIMEOUT}s. Abortando proceso.")
        os._exit(1)

    while True:
        timer = threading.Timer(PASS_TIMEOUT, _watchdog)
        timer.daemon = True
        timer.start()
        try:
            run_once(cfg, initial=(first and args.initial))
        except Exception as e:
            print(f"[!] Error inesperado: {e}")
        finally:
            timer.cancel()
        first = False
        print(f"[*] Esperando {interval}s...\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
