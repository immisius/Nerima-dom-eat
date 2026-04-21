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
    except Exception:
        await browser_manager.close_and_remove(token)
        raise HTTPException(status_code=401, detail="ログインに失敗しました。入力内容を確認してください。")

    user_key = f"{body.company_code}:{body.user_id}"
    create_session(user_key, body.company_code, body.user_id, body.password, token)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return {"ok": True}


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
    return get_all_meals(session["user_key"])


class MealBatchItem(BaseModel):
    date: str
    meal_type: str
    registered: bool


@app.post("/api/batch-update")
async def api_batch_update(changes: list[MealBatchItem], session: dict = Depends(current_session)):
    from scraper import register_meal, cancel_meal

    if not changes:
        return {"ok": True, "processed": 0, "meals": get_all_meals(session["user_key"]), "calendar_error": None}

    ub = await get_user_browser(session)
    async with ub.lock:
        page = await ub.new_page()
        try:
            failed = []
            for item in changes:
                if item.registered:
                    ok = await register_meal(page, item.date, item.meal_type)
                else:
                    ok = await cancel_meal(page, item.date, item.meal_type)
                if not ok:
                    failed.append(f"{item.date} {item.meal_type}")
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
    from scraper import fetch_all_data

    t0 = time.time()
    ub = await get_user_browser(session)
    print(f"[Timing] get_user_browser: {time.time()-t0:.1f}s")

    async with ub.lock:
        page = await ub.new_page()
        try:
            t1 = time.time()
            await fetch_all_data(page, session["user_key"])
            print(f"[Timing] fetch_all_data: {time.time()-t1:.1f}s")
        finally:
            await page.close()

    print(f"[Timing] /api/refresh 合計: {time.time()-t0:.1f}s")
    asyncio.create_task(_sync_calendar_bg(session["user_key"]))
    return {"ok": True, "meals": get_all_meals(session["user_key"]), "calendar_error": None}


@app.get("/api/calendar-sync-status")
async def api_calendar_sync_status(session: dict = Depends(current_session)):
    return _cal_sync_status.get(session["user_key"], {"status": "idle", "message": ""})


@app.post("/api/sync-calendar")
async def api_sync_calendar(session: dict = Depends(current_session)):
    asyncio.create_task(_sync_calendar_bg(session["user_key"]))
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
