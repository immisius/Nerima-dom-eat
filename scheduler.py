import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


def run_fetch_all_users():
    from auth import _sessions
    from scraper import fetch_all_data, _login
    from calendar_sync import sync_meals

    async def _run():
        import browser_manager
        for token, session in list(_sessions.items()):
            try:
                ub = await browser_manager.get_or_create(
                    token, session["company_code"],
                    session["user_id_str"], session["password"]
                )
                async with ub.lock:
                    page = await ub.new_page()
                    try:
                        await fetch_all_data(page, session["user_key"])
                    finally:
                        await page.close()
                sync_meals(session["user_key"])
            except Exception as e:
                print(f"[Scheduler] {session['user_key']} 失敗: {e}")

    asyncio.run(_run())


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
    scheduler.add_job(run_fetch_all_users, CronTrigger(hour=7, minute=0))
    scheduler.start()
    return scheduler
