import os
import json
from datetime import date, datetime
import jpholiday
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from database import get_all_meals, update_calendar_event_id as _update_event_id, get_setting, set_setting

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = os.environ.get("TOKEN_FILE", "token.json")
CREDENTIALS_FILE = os.environ.get("CREDENTIALS_FILE", "credentials.json")
TIMEZONE = "Asia/Tokyo"

# (start_hhmm, end_hhmm)
MEAL_TIMES = {
    "weekday": {
        "朝食": ("06:30", "08:00"),
        "夕食": ("19:00", "22:50"),
    },
    "holiday": {
        "朝食": ("08:00", "11:00"),
        "夕食": ("18:00", "21:30"),
    },
}


def _is_holiday(d: date) -> bool:
    return d.weekday() >= 5 or bool(jpholiday.is_holiday(d))


def _event_times(d: date, meal_type: str) -> tuple[str, str]:
    key = "holiday" if _is_holiday(d) else "weekday"
    start_t, end_t = MEAL_TIMES[key][meal_type]
    return f"{d.isoformat()}T{start_t}:00", f"{d.isoformat()}T{end_t}:00"


def _save_token(creds: Credentials):
    """トークンをDBとファイル（存在する場合）の両方に保存"""
    token_json = creds.to_json()
    set_setting("google_token", token_json)
    if os.path.exists(TOKEN_FILE) or os.path.exists(CREDENTIALS_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(token_json)


def get_service():
    creds = None

    # 1. DBから読む（最優先: リフレッシュ後の最新トークンが入っている）
    token_data = get_setting("google_token")
    # 2. 環境変数から読む
    if not token_data:
        token_data = os.environ.get("GOOGLE_TOKEN_JSON")
    # 3. ファイルから読む（ローカル開発用）
    if not token_data and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token_data = f.read()

    if token_data:
        creds = Credentials.from_authorized_user_info(json.loads(token_data), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            # 初回認証: credentials.jsonファイルまたは環境変数から
            creds_data = os.environ.get("GOOGLE_CREDENTIALS_JSON")
            if creds_data:
                flow = InstalledAppFlow.from_client_config(json.loads(creds_data), SCOPES)
            elif os.path.exists(CREDENTIALS_FILE):
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            else:
                raise RuntimeError("Google Calendar credentials not configured")
            creds = flow.run_local_server(port=0)
            _save_token(creds)

    return build("calendar", "v3", credentials=creds)


def _list_app_events(service, calendar_id: str) -> dict[str, list[str]]:
    """アプリが作成した予定を (date_iso, meal_type) → [event_id, ...] で返す"""
    import re
    result: dict[tuple, list] = {}
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin="2026-01-01T00:00:00+09:00",
            timeMax="2027-01-01T00:00:00+09:00",
            singleEvents=True,
            maxResults=500,
            pageToken=page_token,
        ).execute()
        for ev in resp.get("items", []):
            summary = ev.get("summary", "")
            m = re.match(r'^[✅❌] (朝食|夕食)', summary)
            start = ev.get("start", {}).get("dateTime", "")
            if m and start:
                d_str = start[:10]
                mt = m.group(1)
                key = (d_str, mt)
                result.setdefault(key, []).append(ev["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return result


def cleanup_duplicate_events():
    """カレンダー上の重複予定を削除し、DBのIDを正規化する"""
    if not _calendar_configured():
        return
    service = get_service()
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

    db_meals = {(m["date"], m["meal_type"]): m["calendar_event_id"] for m in get_all_meals()}
    cal_events = _list_app_events(service, calendar_id)

    deleted = 0
    for (d_str, mt), event_ids in cal_events.items():
        if len(event_ids) <= 1:
            continue
        keep_id = db_meals.get((d_str, mt))
        if keep_id not in event_ids:
            keep_id = event_ids[0]
        for eid in event_ids:
            if eid != keep_id:
                try:
                    service.events().delete(calendarId=calendar_id, eventId=eid).execute()
                    deleted += 1
                except Exception:
                    pass
        _update_event_id("", d_str, mt, keep_id)

    print(f"[Calendar] 重複削除: {deleted} 件")


def _calendar_configured() -> bool:
    return (
        os.path.exists(CREDENTIALS_FILE)
        or os.path.exists(TOKEN_FILE)
        or bool(os.environ.get("GOOGLE_CREDENTIALS_JSON"))
        or bool(os.environ.get("GOOGLE_TOKEN_JSON"))
        or bool(get_setting("google_token"))
    )


BATCH_SIZE = 50  # Google Calendar Batch API の上限


def _build_event_body(meal: dict, calendar_id: str) -> tuple[str, dict]:
    """(event_id_or_None, event_body) を返す"""
    d_str = meal["date"]
    mt = meal["meal_type"]
    registered = bool(meal["registered"])
    is_holiday = bool(meal.get("is_holiday"))
    menu = meal["menu"] or "（献立未取得）"

    d = date.fromisoformat(d_str)
    start_dt, end_dt = _event_times(d, mt)

    if is_holiday:
        title, color, desc = f"🔴 {mt} - お休み", "11", f"{mt}: 欠食日（お休み）"
    elif registered:
        title, color, desc = f"✅ {mt} - {menu}", "2", f"{mt}メニュー: {menu}"
    else:
        title, color, desc = f"❌ {mt} - {menu}", "6", f"{mt}メニュー: {menu}\n喫食届: 未提出"

    body = {
        "summary": title,
        "start": {"dateTime": start_dt, "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt, "timeZone": TIMEZONE},
        "description": desc,
        "colorId": color,
    }
    return meal.get("calendar_event_id"), body


def sync_meals(user_key: str = "", progress_cb=None):
    """全食事をカレンダーにバッチ反映: ✅登録済・❌未登録・🔴お休み"""
    if not _calendar_configured():
        print("[Calendar] 未設定のためスキップ")
        return
    service = get_service()
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
    meals = get_all_meals(user_key)
    total = len(meals)

    # insert が必要なもの（event_id なし）を別途収集
    needs_insert: list[dict] = []
    # update/insert 済みカウント用
    done = 0

    # ── PASS 1: update をバッチ実行 ──────────────────────────
    update_queue = []  # (meal, event_body) for update
    for meal in meals:
        event_id, body = _build_event_body(meal, calendar_id)
        if event_id:
            update_queue.append((meal, event_id, body))
        else:
            needs_insert.append(meal)

    # バッチを BATCH_SIZE 件ずつ実行
    for chunk_start in range(0, len(update_queue), BATCH_SIZE):
        chunk = update_queue[chunk_start:chunk_start + BATCH_SIZE]
        failed_meals = []

        def make_update_cb(m):
            def cb(req_id, resp, exc):
                nonlocal done
                if exc:
                    failed_meals.append(m)  # 失敗したら insert へ回す
                done += 1
                if progress_cb:
                    progress_cb(done, total)
            return cb

        batch = service.new_batch_http_request()
        for meal, event_id, body in chunk:
            batch.add(
                service.events().update(calendarId=calendar_id, eventId=event_id, body=body),
                callback=make_update_cb(meal),
            )
        batch.execute()
        needs_insert.extend(failed_meals)

    # ── PASS 2: insert をバッチ実行 ──────────────────────────
    inserted_ids: list[tuple[dict, str]] = []  # (meal, new_event_id)

    for chunk_start in range(0, len(needs_insert), BATCH_SIZE):
        chunk = needs_insert[chunk_start:chunk_start + BATCH_SIZE]
        chunk_results: list[tuple[dict, str]] = []

        def make_insert_cb(m):
            def cb(req_id, resp, exc):
                nonlocal done
                if resp and not exc:
                    chunk_results.append((m, resp["id"]))
                done += 1
                if progress_cb:
                    progress_cb(done, total)
            return cb

        batch = service.new_batch_http_request()
        for meal in chunk:
            _, body = _build_event_body(meal, calendar_id)
            batch.add(
                service.events().insert(calendarId=calendar_id, body=body),
                callback=make_insert_cb(meal),
            )
        batch.execute()
        inserted_ids.extend(chunk_results)

    # ── DB に新規 event_id を保存 ────────────────────────────
    for meal, new_id in inserted_ids:
        _update_event_id(user_key, meal["date"], meal["meal_type"], new_id)

    print(f"[Calendar] update:{len(update_queue)} insert:{len(needs_insert)} 合計:{total}件")
