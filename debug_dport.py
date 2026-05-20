"""D-Portの喫食届ページ全体をスクロールしながらスクショして動作確認"""
import asyncio
import os
from dotenv import load_dotenv
load_dotenv(override=True)

from playwright.async_api import async_playwright
import sys
sys.path.insert(0, ".")

BASE_URL = "https://d-port.communication-base.com"
EATING_URL = f"{BASE_URL}/#/eating"

COMPANY = os.environ["COMPANY_CODE"]
USER_ID = os.environ["USER_ID"]
PASSWORD = os.environ["PASSWORD"]


async def scroll_shots(page, prefix):
    """ページを上から下までスクロールしながら複数スクショ"""
    height = await page.evaluate("document.body.scrollHeight")
    vp = 800
    y = 0
    i = 0
    while y < height:
        await page.evaluate(f"window.scrollTo(0, {y})")
        await page.wait_for_timeout(300)
        await page.screenshot(path=f"{prefix}_{i:02d}.png")
        print(f"  {prefix}_{i:02d}.png (scroll={y})")
        y += vp - 100
        i += 1
    await page.evaluate("window.scrollTo(0, 0)")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 390, "height": 800})
        page = await context.new_page()

        # ログイン
        print("ログイン中...")
        await page.goto(BASE_URL, wait_until="networkidle")
        await page.fill('input[placeholder="企業コード"]', COMPANY)
        await page.fill('input[placeholder="ユーザーID"]', USER_ID)
        await page.fill('input[placeholder="パスワード"]', PASSWORD)
        await page.click('button[type="button"]:has-text("ログイン")')
        await page.wait_for_selector('input[placeholder="企業コード"]', state="detached", timeout=15000)

        await page.goto(EATING_URL, wait_until="networkidle")
        await page.wait_for_selector("tbody.MuiTableBody-root", timeout=15000)
        print("=== 初期表示 ===")
        await scroll_shots(page, "shot_1_initial")

        # 複数チェックボックスをまとめてON
        print("\n複数チェックを入れる...")
        rows = await page.query_selector_all("tbody.MuiTableBody-root tr.MuiTableRow-root")
        checked_count = 0
        for row in rows[:5]:  # 最初の5行
            cells = await row.query_selector_all("td")
            for idx in [1, 2]:  # 朝食・夕食
                if idx >= len(cells):
                    continue
                cbs = await cells[idx].query_selector_all("input[type='checkbox']")
                if cbs and not await cbs[0].is_checked():
                    await cbs[0].dispatch_event("click")
                    checked_count += 1
                    if checked_count >= 3:
                        break
            if checked_count >= 3:
                break

        print(f"  {checked_count}件チェック")
        await page.wait_for_timeout(500)
        print("=== チェック後 ===")
        await scroll_shots(page, "shot_2_checked")

        # 登録ボタン確認
        btns = await page.query_selector_all('button:has-text("登録")')
        print(f"\n登録ボタン: {len(btns)}個")
        for i, b in enumerate(btns):
            bb = await b.bounding_box()
            print(f"  [{i}] bbox={bb}")

        # 登録ボタンクリック
        if btns:
            print("登録ボタン(force=True)クリック...")
            await btns[0].click(force=True)
            await page.wait_for_timeout(1000)
            print("=== 登録後（ダイアログ？） ===")
            await scroll_shots(page, "shot_3_dialog")

            # ダイアログのOKをクリック
            ok = page.locator('[role="dialog"] button:has-text("OK")')
            if await ok.count() > 0:
                print("OKボタンをクリック...")
                await ok.click(force=True)
                await page.wait_for_selector('[role="dialog"]', state="detached", timeout=8000)
                await page.wait_for_timeout(500)
                print("=== OK後 ===")
                await scroll_shots(page, "shot_4_done")

        await browser.close()
        print("\n完了。shot_*.png を確認してください")


asyncio.run(main())
