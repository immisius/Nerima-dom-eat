import asyncio, os, sys, time
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv(override=True)
from playwright.async_api import async_playwright

BASE_URL = 'https://d-port.communication-base.com'
EATING_URL = BASE_URL + '/#/eating'

async def get_month_span(page):
    spans = await page.query_selector_all('span')
    for sp in spans:
        txt = await sp.inner_text()
        txt = txt.strip()
        if '月' in txt and len(txt) <= 8:
            return txt
    return None

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={'width': 390, 'height': 844})
        page = await ctx.new_page()
        await page.goto(BASE_URL, wait_until='networkidle')
        await page.fill('input[placeholder="企業コード"]', os.environ['COMPANY_CODE'])
        await page.fill('input[placeholder="ユーザーID"]', os.environ['USER_ID'])
        await page.fill('input[placeholder="パスワード"]', os.environ['PASSWORD'])
        await page.click('button[type="button"]:has-text("ログイン")')
        await page.wait_for_selector('input[placeholder="企業コード"]', state='detached', timeout=15000)

        await page.goto(EATING_URL, wait_until='networkidle')
        await page.wait_for_selector('tbody.MuiTableBody-root', timeout=15000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path='/tmp/s0_before.png')
        print(f'クリック前の月: {await get_month_span(page)}')

        btn = page.locator('xpath=//span[normalize-space(.)="4月"]/../button[last()]')
        print(f'btn count={await btn.count()} disabled={await btn.is_disabled()}')

        # 方法1: 通常クリック（force=False）
        await btn.scroll_into_view_if_needed()
        t0 = time.time()
        await btn.click()
        print(f'方法1 click() 完了 ({time.time()-t0:.2f}s)')

        for wait in [300, 300, 500, 1000, 2000]:
            await page.wait_for_timeout(wait)
            m = await get_month_span(page)
            print(f'  {wait}ms後: {m}')
            if m and '5月' in m:
                break

        await page.screenshot(path='/tmp/s1_after_normal_click.png')
        m1 = await get_month_span(page)
        print(f'方法1後の月: {m1}')

        if m1 and '5月' in m1:
            print('✓ 通常クリック成功')
            rows = await page.query_selector_all('tbody.MuiTableBody-root tr.MuiTableRow-root')
            print(f'5月テーブル: {len(rows)}行')
            return

        print('→ 通常クリック失敗、方法2試行')

        # 方法2: JS dispatch
        await page.evaluate("""() => {
            const spans = Array.from(document.querySelectorAll('span'));
            const s = spans.find(s => /\\d+月/.test(s.textContent.trim()));
            if (!s) return;
            const btns = Array.from(s.parentElement.querySelectorAll('button'));
            const btn = btns[btns.length - 1];
            if (btn && !btn.disabled) {
                btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            }
        }""")
        print('方法2 JS dispatch 完了')

        for wait in [300, 300, 500, 1000, 2000]:
            await page.wait_for_timeout(wait)
            m = await get_month_span(page)
            print(f'  {wait}ms後: {m}')
            if m and '5月' in m:
                break

        await page.screenshot(path='/tmp/s2_after_js_click.png')
        print(f'方法2後の月: {await get_month_span(page)}')

asyncio.run(main())
