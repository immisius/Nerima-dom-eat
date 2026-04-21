import os
from datetime import date
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://d-port.communication-base.com"
EATING_URL = f"{BASE_URL}/#/eating"


async def verify_credentials(company_code: str, user_id_str: str, password: str) -> bool:
    """認証情報の検証のみ（保存しない）"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(BASE_URL, wait_until="networkidle")
            await page.fill('input[placeholder="企業コード"]', company_code)
            await page.fill('input[placeholder="ユーザーID"]', user_id_str)
            await page.fill('input[placeholder="パスワード"]', password)
            await page.click('button[type="button"]:has-text("ログイン")')
            await page.wait_for_selector('input[placeholder="企業コード"]', state="detached", timeout=15000)
            return True
        except Exception:
            return False
        finally:
            await browser.close()


async def _login(page, company_code: str, user_id_str: str, password: str):
    """ページにログイン（D-PortはlocalStorageで認証するため毎回ログインが必要）"""
    await page.goto(BASE_URL, wait_until="networkidle")
    await page.fill('input[placeholder="企業コード"]', company_code)
    await page.fill('input[placeholder="ユーザーID"]', user_id_str)
    await page.fill('input[placeholder="パスワード"]', password)
    await page.click('button[type="button"]:has-text("ログイン")')
    await page.wait_for_selector('input[placeholder="企業コード"]', state="detached", timeout=15000)


async def _get_displayed_month(page) -> tuple[int, int]:
    """表示中の年月を取得 (year, month)"""
    import re
    spans = await page.query_selector_all("span")
    for span in spans:
        try:
            text = await span.inner_text(timeout=500)
        except Exception:
            continue
        m = re.fullmatch(r'(?:(\d{4})年)?(\d+)月', text.strip())
        if m:
            month = int(m.group(2))
            year_str = m.group(1)
            if year_str:
                return int(year_str), month
            today = date.today()
            if month < today.month - 6:
                year = today.year + 1
            elif month > today.month + 6:
                year = today.year - 1
            else:
                year = today.year
            return year, month
    return date.today().year, date.today().month


async def _scrape_month(page, year: int = 0, month: int = 0) -> list[dict]:
    """現在表示中の月の喫食データを取得（朝食・夕食それぞれ）"""
    import re as _re
    if not year or not month:
        year, month = await _get_displayed_month(page)

    rows = await page.query_selector_all("tbody.MuiTableBody-root tr.MuiTableRow-root")
    meals = []
    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) < 3:
            continue
        date_text = await cells[0].inner_text()
        day_match = _re.match(r'(\d+)', date_text.strip())
        if not day_match:
            continue
        day = int(day_match.group(1))
        try:
            meal_date = date(year, month, day).isoformat()
        except ValueError:
            continue

        for meal_type, cell_idx in [("朝食", 1), ("夕食", 2)]:
            if cell_idx >= len(cells):
                meals.append({"date": meal_date, "meal_type": meal_type,
                               "registered": False, "deadline_passed": True, "is_holiday": True})
                continue
            cell = cells[cell_idx]
            checkboxes = await cell.query_selector_all("input[type='checkbox']")
            is_holiday = False
            if checkboxes:
                registered = await checkboxes[0].is_checked()
                deadline_passed = False
            else:
                text = (await cell.inner_text()).strip()
                is_holiday = text == "欠食日"
                registered = text == "食べる"
                deadline_passed = True

            meals.append({
                "date": meal_date,
                "meal_type": meal_type,
                "registered": registered,
                "deadline_passed": deadline_passed,
                "is_holiday": is_holiday,
            })
    return meals


async def _wait_for_table(page, timeout=15000):
    await page.wait_for_selector("tbody.MuiTableBody-root", timeout=timeout)


async def scrape_eating_page(page) -> list[dict]:
    """喫食届ページから今月＋来月の日付・登録状況・締切済みフラグを取得"""
    from datetime import date as _date
    await page.goto(EATING_URL, wait_until="networkidle")
    await _wait_for_table(page)

    today = _date.today()
    meals = await _scrape_month(page, today.year, today.month)

    next_btn = page.locator("button.MuiIconButton-root").nth(4)
    await next_btn.click()
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        await page.wait_for_timeout(1000)

    if today.month == 12:
        ny, nm = today.year + 1, 1
    else:
        ny, nm = today.year, today.month + 1
    meals += await _scrape_month(page, ny, nm)

    return meals


async def scrape_menu_page(page, menu_url: str) -> dict[str, str]:
    """献立ページ(PDF埋め込み)から日付→メニューのマッピングを取得"""
    await page.goto("about:blank")
    await page.goto(menu_url, wait_until="networkidle")
    try:
        await page.wait_for_selector(".rpv-core__text-layer-text", timeout=10000)
    except Exception:
        pass

    text = await page.inner_text("body")
    return _parse_menu_text(text)


def _parse_menu_text(text: str) -> dict[str, str]:
    import re as _re
    WEEKDAYS = set("月火水木金土日")
    result: dict[str, str] = {}

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    month_pattern = _re.compile(r'^(?:(\d{4})年)?(\d{1,2})月$')
    today = date.today()

    i = 0
    current_year = today.year
    current_month = today.month

    while i < len(lines):
        m = month_pattern.match(lines[i])
        if m:
            yr = int(m.group(1)) if m.group(1) else None
            mo = int(m.group(2))
            if yr is None:
                if mo < today.month - 6:
                    yr = today.year + 1
                elif mo > today.month + 6:
                    yr = today.year - 1
                else:
                    yr = today.year
            current_year, current_month = yr, mo
            i += 1
            continue

        if _re.match(r'^\d{1,2}$', lines[i]):
            day = int(lines[i])
            if 1 <= day <= 31 and i + 1 < len(lines) and lines[i + 1] in WEEKDAYS:
                i += 2
                dish_parts = []
                while i < len(lines):
                    if _re.match(r'^\d{1,2}$', lines[i]) and i + 1 < len(lines) and lines[i + 1] in WEEKDAYS:
                        break
                    if month_pattern.match(lines[i]):
                        break
                    if _re.match(r'^\d+(\.\d+)?$', lines[i]):
                        i += 1
                        continue
                    if lines[i] in ("日付", "曜日", "献　立　名", "献立名", "小鉢", "E", "P", "F", "Ｎａ", "Na"):
                        i += 1
                        continue
                    if (lines[i].startswith("*") or lines[i].startswith("＊")
                            or "一覧に戻る" in lines[i] or "Copyright" in lines[i]
                            or lines[i].startswith("三菱") or lines[i].startswith("お　休　み")):
                        break
                    dish_parts.append(lines[i])
                    i += 1

                if dish_parts:
                    try:
                        meal_date = date(current_year, current_month, day).isoformat()
                        menu_str = " / ".join(dish_parts)
                        if meal_date in result:
                            result[meal_date] += " | " + menu_str
                        else:
                            result[meal_date] = menu_str
                    except ValueError:
                        pass
                continue
        i += 1

    return result


async def get_menu_urls(page) -> list[str]:
    """TOP画面のお知らせから献立ページのURLを自動取得"""
    await page.goto(BASE_URL, wait_until="networkidle")
    try:
        await page.wait_for_selector("a[href*='infodetail']", timeout=5000)
    except Exception:
        pass

    urls = []
    links = await page.query_selector_all("a[href*='infodetail']")
    for link in links:
        text = await link.inner_text()
        href = await link.get_attribute("href")
        if "献立" in text or "メニュー" in text:
            urls.append(f"{BASE_URL}/{href}")
    return urls


async def _find_cell_for_date_and_type(page, meal_date: str, meal_type: str):
    """指定日・食事種別のtdセルを返す。チェックボックスがなければ None"""
    import re as _re
    d = date.fromisoformat(meal_date)
    year, month = await _get_displayed_month(page)

    if (year, month) != (d.year, d.month):
        today = date.today()
        target = d.replace(day=1)
        current = today.replace(day=1)
        diff = (target.year - current.year) * 12 + (target.month - current.month)
        btn_idx = 4 if diff > 0 else 3
        for _ in range(abs(diff)):
            await page.locator("button.MuiIconButton-root").nth(btn_idx).click()
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                await page.wait_for_timeout(500)

    cell_idx = 1 if meal_type == "朝食" else 2
    rows = await page.query_selector_all("tbody.MuiTableBody-root tr.MuiTableRow-root")
    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) <= cell_idx:
            continue
        date_text = await cells[0].inner_text()
        m = _re.match(r'(\d+)', date_text.strip())
        if m and int(m.group(1)) == d.day:
            cell = cells[cell_idx]
            cbs = await cell.query_selector_all("input[type='checkbox']")
            return cell if cbs else None
    return None


async def _submit_registration(page):
    await page.locator('button:has-text("登録")').click()
    try:
        await page.wait_for_selector('[role="dialog"]', timeout=5000)
        ok_btn = page.locator('[role="dialog"] button:has-text("OK")')
        if await ok_btn.count() > 0:
            await ok_btn.click()
        await page.wait_for_selector('[role="dialog"]', state="detached", timeout=5000)
    except Exception:
        await page.wait_for_timeout(1000)


async def _ensure_eating_page(page):
    """EATINGページにいない場合のみ遷移"""
    if "#/eating" not in page.url:
        await page.goto(EATING_URL, wait_until="networkidle")
    await _wait_for_table(page)


async def register_meal(page, meal_date: str, meal_type: str) -> bool:
    await _ensure_eating_page(page)

    cell = await _find_cell_for_date_and_type(page, meal_date, meal_type)
    if cell is None:
        return False

    cb = (await cell.query_selector_all("input[type='checkbox']"))[0]
    if await cb.is_checked():
        return True

    await cb.dispatch_event("click")
    await _submit_registration(page)
    return True


async def cancel_meal(page, meal_date: str, meal_type: str) -> bool:
    await _ensure_eating_page(page)

    cell = await _find_cell_for_date_and_type(page, meal_date, meal_type)
    if cell is None:
        return False

    cb = (await cell.query_selector_all("input[type='checkbox']"))[0]
    if not await cb.is_checked():
        return True

    await cb.dispatch_event("click")
    await _submit_registration(page)
    return True


async def fetch_all_data(page, user_key: str) -> list[dict]:
    """認証済みページで献立+喫食届を一括取得（ブラウザ再利用）"""
    import time
    from database import upsert_meal

    t = time.time()
    menu_urls = await get_menu_urls(page)
    print(f"[Timing] get_menu_urls ({len(menu_urls)}件): {time.time()-t:.1f}s")
    if not menu_urls:
        manual = os.environ.get("MENU_URLS", "")
        menu_urls = [u.strip() for u in manual.split(",") if u.strip()]

    menus: dict[str, str] = {}
    for url in menu_urls:
        t = time.time()
        menus.update(await scrape_menu_page(page, url))
        print(f"[Timing] scrape_menu_page: {time.time()-t:.1f}s")

    t = time.time()
    eating_data = await scrape_eating_page(page)
    print(f"[Timing] scrape_eating_page ({len(eating_data)}件): {time.time()-t:.1f}s")

    for item in eating_data:
        d = item["date"]
        mt = item["meal_type"]
        menu_str = menus.get(d, "")
        parts = menu_str.split(" | ") if menu_str else []
        menu = (parts[0] if parts else "") if mt == "朝食" else (parts[1] if len(parts) > 1 else "")
        upsert_meal(
            user_key=user_key,
            meal_date=d,
            meal_type=mt,
            menu=menu,
            registered=item["registered"],
            deadline_passed=item["deadline_passed"],
            is_holiday=item.get("is_holiday", False),
        )

    return eating_data
