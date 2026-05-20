"""D-Port喫食届ページのDOM構造を確認するデバッグスクリプト"""
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

        # ログイン
        await page.goto(BASE_URL, wait_until='networkidle')
        await page.fill('input[placeholder="企業コード"]', os.environ['COMPANY_CODE'])
        await page.fill('input[placeholder="ユーザーID"]', os.environ['USER_ID'])
        await page.fill('input[placeholder="パスワード"]', os.environ['PASSWORD'])
        await page.click('button[type="button"]:has-text("ログイン")')
        await page.wait_for_selector('input[placeholder="企業コード"]', state='detached', timeout=15000)
        print('=== ログイン成功 ===')

        # 喫食届ページ
        await page.goto(EATING_URL, wait_until='networkidle')
        await page.wait_for_selector('tbody.MuiTableBody-root', timeout=15000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path='/tmp/eating_april.png', full_page=True)
        print('スクショ: /tmp/eating_april.png')

        # 行確認
        rows = await page.query_selector_all('tbody.MuiTableBody-root tr.MuiTableRow-root')
        print(f'\n=== 4月テーブル: {len(rows)}行 ===')

        # 最初の3行と最後の3行を確認
        for idx in list(range(min(3, len(rows)))) + list(range(max(0, len(rows)-3), len(rows))):
            cells = await rows[idx].query_selector_all('td')
            row_txt = (await rows[idx].inner_text()).strip().replace('\n', ' | ')[:100]
            print(f'\n行[{idx}]: {row_txt}')
            for j, cell in enumerate(cells[:3]):
                cbs = await cell.query_selector_all('input[type="checkbox"]')
                txt = (await cell.inner_text()).strip()[:60]
                if j == 0:
                    print(f'  日付cell: "{txt}"')
                else:
                    html = (await cell.inner_html())[:300]
                    print(f'  cell[{j}] cb={len(cbs)} text="{txt}"')
                    if len(cbs) == 0:
                        print(f'    HTML: {html}')

        # 月ナビ周辺のHTML確認
        print('\n=== 月ナビ DOM ===')
        nav_info = await page.evaluate("""() => {
            const spans = Array.from(document.querySelectorAll('span'));
            const s = spans.find(s => /\d+月/.test(s.textContent.trim()));
            if (!s) return 'span not found';
            const parent = s.parentElement;
            return {
                spanText: s.textContent.trim(),
                spanInnerHTML: s.innerHTML,
                parentTag: parent.tagName,
                parentClass: parent.className,
                parentHTML: parent.outerHTML.substring(0, 600)
            };
        }""")
        print(nav_info)

        # 次月ボタンクリックを試みる
        print('\n=== 次月ナビ試行 ===')
        clicked = await page.evaluate("""() => {
            const spans = Array.from(document.querySelectorAll('span'));
            const s = spans.find(s => /\d+月/.test(s.textContent.trim()));
            if (!s) return 'no span';
            const parent = s.parentElement;
            const btns = Array.from(parent.querySelectorAll('button'));
            return {
                count: btns.length,
                btns: btns.map(b => ({
                    text: b.textContent.trim().substring(0, 20),
                    disabled: b.disabled,
                    outerHTML: b.outerHTML.substring(0, 150)
                }))
            };
        }""")
        print(clicked)

asyncio.run(main())
