import asyncio, os, sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv(override=True)
from playwright.async_api import async_playwright

BASE_URL = 'https://d-port.communication-base.com'
EATING_URL = BASE_URL + '/#/eating'

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

        # next ボタン試行
        btn = page.locator('xpath=//span[normalize-space(.)="4月"]/../button[last()]')
        count = await btn.count()
        disabled = await btn.is_disabled() if count > 0 else 'N/A'
        print(f'nextボタン count={count} disabled={disabled}')

        if count > 0 and not disabled:
            await btn.click(force=True)
            await page.wait_for_timeout(2000)
            await page.screenshot(path='/tmp/eating_may.png', full_page=True)
            print('5月スクショ: /tmp/eating_may.png')

            rows = await page.query_selector_all('tbody.MuiTableBody-root tr.MuiTableRow-root')
            print(f'5月テーブル: {len(rows)}行')
            cb_total = 0
            no_cb_total = 0
            for row in rows[:5]:
                cells = await row.query_selector_all('td')
                day = (await cells[0].inner_text()).strip() if cells else '?'
                for j in [1, 2]:
                    if j < len(cells):
                        cbs = await cells[j].query_selector_all('input[type="checkbox"]')
                        txt = (await cells[j].inner_text()).strip()[:40]
                        if cbs:
                            cb_total += 1
                        else:
                            no_cb_total += 1
                            html = (await cells[j].inner_html())[:200]
                            print(f'  {day} cell[{j}] NO-CB text="{txt}" html={html}')
                        if cbs:
                            checked = await cbs[0].is_checked()
                            print(f'  {day} cell[{j}] checkbox checked={checked}')
            print(f'最初5行: checkbox={cb_total} no_checkbox={no_cb_total}')
        else:
            print('nextボタン見つからず or 無効')

asyncio.run(main())
