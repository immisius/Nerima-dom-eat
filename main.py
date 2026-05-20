import os
import asyncio
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv(override=True)

from database import init_db, get_all_meals, set_meal_registered
from scheduler import start_scheduler
from auth import create_session, get_session, delete_session
import browser_manager

SESSION_COOKIE = "session"
templates = Jinja2Templates(directory="templates")

# user_key → {"status": "idle"|"running"|"done"|"error", "message": str}
_cal_sync_status: dict[str, dict] = {}
_menu_fetch_status: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown()
    # 全ブラウザを閉じる
    for token in list(browser_manager._browsers):
        await browser_manager.close_and_remove(token)


app = FastAPI(lifespan=lifespan)


def current_session(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    session = get_session(token) if token else None
    if not session:
        raise HTTPException(status_code=401, detail="ログインが必要です")
    return {"token": token, **session}


async def get_user_browser(session: dict) -> browser_manager.UserBrowser:
    """ブラウザを取得。消えていれば再作成（サーバ再起動後など）"""
    token = session["token"]
    ub = browser_manager.get(token)
    if ub is None:
        ub = await browser_manager.get_or_create(
            token, session["company_code"], session["user_id_str"], session["password"]
        )
    else:
        await ub.ensure_logged_in(
            session["company_code"], session["user_id_str"], session["password"]
        )
    return ub


# ── 認証 ───────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


class LoginRequest(BaseModel):
    company_code: str
    user_id: str
    password: str


@app.post("/api/login")
async def api_login(body: LoginRequest, response: Response):
    import time
    token = secrets.token_urlsafe(32)
    t0 = time.time()
    try:
        await browser_manager.get_or_create(token, body.company_code, body.user_id, body.password)
        print(f"[Timing] /api/login (browser起動+ログイン): {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[Login ERROR] {type(e).__name__}: {e}")
        await browser_manager.close_and_remove(token)
        raise HTTPException(status_code=401, detail="ログインに失敗しました。入力内容を確認してください。")

    user_key = f"{body.company_code}:{body.user_id}"
    create_session(user_key, body.company_code, body.user_id, body.password, token)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")

    # バックグラウンドで登録可能月を確認
    asyncio.create_task(_check_available_months_bg(token, body.company_code, body.user_id))

    return {"ok": True}


async def _check_available_months_bg(token: str, company_code: str, user_id: str):
    """バックグラウンドで登録可能月を確認"""
    try:
        from scraper import check_available_months
        ub = browser_manager.get(token)
        if ub:
            page = await ub.new_page()
            try:
                first_month, last_month = await check_available_months(page)
                print(f"[Login] {company_code}:{user_id} - 登録可能月: {first_month} 〜 {last_month}")
            finally:
                await page.close()
    except Exception as e:
        print(f"[check_months_bg] エラー: {e}")


@app.post("/api/logout")
async def api_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_session(token)
        await browser_manager.close_and_remove(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ── ページ ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not (token and get_session(token)):
        return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request})


# ── API ────────────────────────────────────────────────

@app.get("/api/meals")
async def api_meals(session: dict = Depends(current_session)):
    meals = get_all_meals(session["user_key"])
    total = len(meals)
    changeable = sum(1 for m in meals if not m["deadline_passed"] and not m["is_holiday"])
    by_month = {}
    for m in meals:
        ym = m["date"][:7]
        if ym not in by_month:
            by_month[ym] = {"total": 0, "changeable": 0, "registered": 0}
        by_month[ym]["total"] += 1
        if not m["deadline_passed"] and not m["is_holiday"]:
            by_month[ym]["changeable"] += 1
        if m["registered"]:
            by_month[ym]["registered"] += 1
    print(f"[meals] {session['user_key']} total={total} changeable={changeable} by_month={by_month}")
    return meals


@app.get("/api/debug/meals")
async def api_debug_meals(session: dict = Depends(current_session)):
    """DBの喫食データ統計（デバッグ用）"""
    meals = get_all_meals(session["user_key"])
    by_month = {}
    for m in meals:
        ym = m["date"][:7]
        if ym not in by_month:
            by_month[ym] = {"total": 0, "changeable": 0, "deadline_passed": 0, "is_holiday": 0, "registered": 0}
        by_month[ym]["total"] += 1
        if m["deadline_passed"]:
            by_month[ym]["deadline_passed"] += 1
        if m["is_holiday"]:
            by_month[ym]["is_holiday"] += 1
        if not m["deadline_passed"] and not m["is_holiday"]:
            by_month[ym]["changeable"] += 1
        if m["registered"]:
            by_month[ym]["registered"] += 1
    return {"user_key": session["user_key"], "total": len(meals), "by_month": by_month}


class MealBatchItem(BaseModel):
    date: str
    meal_type: str
    registered: bool


@app.post("/api/batch-update")
async def api_batch_update(changes: list[MealBatchItem], session: dict = Depends(current_session)):
    from scraper import apply_changes_for_month
    from itertools import groupby

    if not changes:
        return {"ok": True, "processed": 0, "meals": get_all_meals(session["user_key"]), "calendar_error": None}

    # 月ごとにグループ化
    sorted_changes = sorted([c.model_dump() for c in changes], key=lambda x: x["date"][:7])
    months = {k: list(v) for k, v in groupby(sorted_changes, key=lambda x: x["date"][:7])}

    ub = await get_user_browser(session)
    async with ub.lock:
        page = await ub.new_page()
        try:
            failed = []
            for month_key, month_items in months.items():
                print(f"[batch] {month_key}: {len(month_items)}件")
                failed += await apply_changes_for_month(page, month_items)
        except Exception as e:
            import traceback
            print(f"[batch] EXCEPTION: {e}")
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"内部エラー: {e}")
        finally:
            await page.close()

    failed_set = set(failed)
    for item in changes:
        if f"{item.date} {item.meal_type}" not in failed_set:
            set_meal_registered(session["user_key"], item.date, item.meal_type, item.registered)

    if failed:
        raise HTTPException(status_code=500, detail=f"失敗: {', '.join(failed)}")

    asyncio.create_task(_sync_calendar_bg(session["user_key"]))
    return {"ok": True, "processed": len(changes), "meals": get_all_meals(session["user_key"]), "calendar_error": None}


async def _sync_calendar_bg(user_key: str):
    """カレンダー同期をバックグラウンドで実行"""
    import time
    from calendar_sync import sync_meals
    _cal_sync_status[user_key] = {"status": "running", "message": "カレンダー同期中...", "done": 0, "total": 0}
    t = time.time()

    def on_progress(done: int, total: int):
        _cal_sync_status[user_key] = {"status": "running", "message": f"{done}/{total}件", "done": done, "total": total}

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sync_meals(user_key, on_progress))
        elapsed = time.time() - t
        print(f"[Timing] sync_meals (BG): {elapsed:.1f}s")
        _cal_sync_status[user_key] = {"status": "done", "message": f"同期完了 ({elapsed:.0f}s)", "done": 0, "total": 0}
    except Exception as e:
        print(f"[Calendar] バックグラウンド同期エラー: {e}")
        _cal_sync_status[user_key] = {"status": "error", "message": str(e), "done": 0, "total": 0}


@app.post("/api/refresh")
async def api_refresh(session: dict = Depends(current_session)):
    import time
    from scraper import fetch_eating_data

    t0 = time.time()
    print(f"[refresh] 開始: {session['user_key']}")
    ub = await get_user_browser(session)
    print(f"[refresh] ブラウザ取得: {time.time()-t0:.1f}s")

    async with ub.lock:
        print(f"[refresh] ロック取得: {time.time()-t0:.1f}s")
        page = await ub.new_page()
        try:
            await fetch_eating_data(page, session["user_key"])
        finally:
            await page.close()

    print(f"[refresh] フェーズ1完了 (喫食のみ): {time.time()-t0:.1f}s → レスポンス返却")

    from database import get_menu_cache_age_seconds
    cache_age = get_menu_cache_age_seconds()
    CACHE_TTL = 30 * 24 * 3600  # 30日
    if cache_age is None or cache_age > CACHE_TTL:
        print(f"[refresh] メニューキャッシュ期限切れ(age={cache_age}) → バックグラウンド取得開始")
        asyncio.create_task(_fetch_menus_bg(session))
    else:
        print(f"[refresh] メニューキャッシュ有効(age={cache_age/3600:.1f}h) → phase2スキップ")
        _menu_fetch_status[session["user_key"]] = {"status": "done", "message": f"キャッシュ使用({cache_age/3600:.1f}h前)"}

    return {"ok": True, "meals": get_all_meals(session["user_key"])}


async def _fetch_menus_bg(session: dict):
    """メニューをバックグラウンドで取得してDB更新→カレンダー同期"""
    import time
    from scraper import fetch_and_update_menus
    user_key = session["user_key"]
    print(f"[menu_bg] タスク開始: {user_key}")
    _menu_fetch_status[user_key] = {"status": "running", "message": "メニュー取得中..."}
    t = time.time()
    try:
        ub = await get_user_browser(session)
        print(f"[menu_bg] ブラウザ取得: {time.time()-t:.1f}s")
        async with ub.lock:
            print(f"[menu_bg] ロック取得: {time.time()-t:.1f}s")
            page = await ub.new_page()
            try:
                await fetch_and_update_menus(page, user_key)
            finally:
                await page.close()
        elapsed = time.time() - t
        print(f"[menu_bg] 完了: {elapsed:.1f}s")
        _menu_fetch_status[user_key] = {"status": "done", "message": "メニュー取得完了"}
        asyncio.create_task(_sync_calendar_bg(user_key))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[menu_bg] エラー: {e}")
        _menu_fetch_status[user_key] = {"status": "error", "message": str(e)}


@app.get("/api/calendar-sync-status")
async def api_calendar_sync_status(session: dict = Depends(current_session)):
    return _cal_sync_status.get(session["user_key"], {"status": "idle", "message": ""})


@app.get("/api/menu-status")
async def api_menu_status(session: dict = Depends(current_session)):
    return _menu_fetch_status.get(session["user_key"], {"status": "idle", "message": ""})


@app.post("/api/sync-calendar")
async def api_sync_calendar(session: dict = Depends(current_session)):
    asyncio.create_task(_sync_calendar_bg(session["user_key"]))
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
