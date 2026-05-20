import os
from datetime import date, timezone, timedelta
from playwright.async_api import async_playwright
from dotenv import load_dotenv

JST = timezone(timedelta(hours=9))


def today_jst() -> date:
    """Railway(UTC)でも東京時刻の today を返す"""
    from datetime import datetime
    return datetime.now(JST).date()

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
            today = today_jst()
            if month < today.month - 6:
                year = today.year + 1
            elif month > today.month + 6:
                year = today.year - 1
            else:
                year = today.year
            return year, month
    return today_jst().year, today_jst().month


async def _scrape_month(page, year: int = 0, month: int = 0) -> list[dict]:
    """現在表示中の月の喫食データを取得（朝食・夕食それぞれ）"""
    import re as _re
    if not year or not month:
        year, month = await _get_displayed_month(page)

    rows = await page.query_selector_all("tbody.MuiTableBody-root tr.MuiTableRow-root")
    checkbox_count = 0
    no_checkbox_count = 0
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
                checkbox_count += 1
            else:
                text = (await cell.inner_text()).strip()
                is_holiday = text == "欠食日"
                registered = text == "食べる"
                deadline_passed = True
                no_checkbox_count += 1

            meals.append({
                "date": meal_date,
                "meal_type": meal_type,
                "registered": registered,
                "deadline_passed": deadline_passed,
                "is_holiday": is_holiday,
            })
    print(f"[scrape_month] {year}年{month}月: {len(meals)}件 checkbox={checkbox_count} no_checkbox={no_checkbox_count}")
    return meals


async def _wait_for_table(page, timeout=15000):
    await page.wait_for_selector("tbody.MuiTableBody-root", timeout=timeout)
    # ReactがDOMを描画し終えるまで少し待つ
    await page.wait_for_timeout(800)


async def _wait_for_month(page, target_year: int, target_month: int, timeout: int = 10000) -> bool:
    """月表示が target_year/target_month になるまでポーリング。タイムアウト時 False"""
    import time as _time
    deadline = _time.time() + timeout / 1000
    while _time.time() < deadline:
        y, m = await _get_displayed_month(page)
        if (y, m) == (target_year, target_month):
            return True
        await page.wait_for_timeout(300)
    return False


async def _click_month_nav(page, direction: str) -> bool:
    """月ナビの < (prev) または > (next) ボタンをクリック"""
    cur_y, cur_m = await _get_displayed_month(page)

    # デバッグ: 月spanとその周辺DOM構造を確認
    spans_with_month = await page.evaluate("""() => {
        return Array.from(document.querySelectorAll('span'))
            .map(s => s.textContent.trim())
            .filter(t => /\\d+月/.test(t))
            .slice(0, 10);
    }""")
    print(f"[nav] 月span一覧: {spans_with_month}")

    # DOM構造デバッグ: spanから祖先をたどってボタンの位置を特定
    dom_debug = await page.evaluate("""() => {
        const spans = Array.from(document.querySelectorAll('span'));
        const s = spans.find(s => /\\d+月/.test(s.textContent.trim()));
        if (!s) return 'no month span found';
        let el = s;
        const levels = [];
        for (let i = 0; i < 8; i++) {
            const tag = el.tagName;
            const cls = (el.className || '').substring(0, 40);
            const btns = Array.from(el.querySelectorAll('button')).length;
            const directBtns = Array.from(el.children).filter(c => c.tagName === 'BUTTON').length;
            levels.push(`L${i}: <${tag} class="${cls}"> btns=${btns} directBtns=${directBtns}`);
            if (!el.parentElement) break;
            el = el.parentElement;
        }
        return levels;
    }""")
    print(f"[nav] DOM構造: {dom_debug}")

    # "4月" と "2026年4月" 両方のフォーマットに対応
    month_texts = [f"{cur_m}月", f"{cur_y}年{cur_m}月"]

    # DOM構造: <DIV class="jss18"> に button[1]=prev, button[2]=next が直接ある
    # span→親div→その子ボタン でアクセス
    for month_text in month_texts:
        if direction == "next":
            xpaths = [
                f'//span[normalize-space(.)="{month_text}"]/../button[last()]',
                f'//span[normalize-space(.)="{month_text}"]/parent::*/button[last()]',
            ]
        else:
            xpaths = [
                f'//span[normalize-space(.)="{month_text}"]/../button[1]',
                f'//span[normalize-space(.)="{month_text}"]/parent::*/button[1]',
            ]

        for xpath in xpaths:
            btn = page.locator(f"xpath={xpath}")
            count = await btn.count()
            if count > 0:
                print(f"[nav] {direction}: ヒット text='{month_text}' xpath={xpath}")
                disabled = await btn.is_disabled()
                if disabled:
                    print(f"[nav] {direction}: ボタン無効")
                    return False
                await btn.scroll_into_view_if_needed()
                await btn.click()  # force=True を外す：Reactのイベント処理に必要
                print(f"[nav] {direction}: クリック完了")
                return True

    print(f"[nav] {direction}: ボタン見つからず (tried {month_texts})")
    return False


async def scrape_eating_page(page) -> list[dict]:
    """喫食届ページから利用可能な月（当月＋次月）の喫食データを取得"""
    import time as _time
    t = _time.time()
    print(f"[scrape_eating] 開始: goto {EATING_URL}")
    await page.goto(EATING_URL, wait_until="networkidle")
    await _wait_for_table(page)
    print(f"[scrape_eating] ページロード完了: {_time.time()-t:.1f}s")

    year, month = await _get_displayed_month(page)
    print(f"[scrape_eating] 当月: {year}年{month}月 ({_time.time()-t:.1f}s)")
    meals = await _scrape_month(page, year, month)
    print(f"[scrape_eating] 当月スクレイプ完了: {len(meals)}件 ({_time.time()-t:.1f}s)")

    # 次の月ボタンが有効なら取得
    clicked = await _click_month_nav(page, "next")
    if clicked:
        nm = month + 1 if month < 12 else 1
        ny = year + 1 if month == 12 else year
        print(f"[scrape_eating] 次月待機: {ny}年{nm}月")
        ok = await _wait_for_month(page, ny, nm, timeout=10000)
        if ok:
            await _wait_for_table(page, timeout=8000)  # 月切替後テーブル再描画を待つ
            next_meals = await _scrape_month(page, ny, nm)
            meals += next_meals
            print(f"[scrape_eating] 次月スクレイプ完了: {len(next_meals)}件 ({_time.time()-t:.1f}s)")
        else:
            print(f"[scrape_eating] 次月タイムアウト ({_time.time()-t:.1f}s)")
    else:
        print(f"[scrape_eating] 次月ボタン無効 ({_time.time()-t:.1f}s)")

    print(f"[scrape_eating] 合計: {len(meals)}件 ({_time.time()-t:.1f}s)")
    return meals


async def fetch_eating_data(page, user_key: str) -> list[dict]:
    """喫食届のみ取得してDB保存。共有メニューキャッシュがあれば即時適用（高速）"""
    import time
    from database import upsert_meal, get_menu_cache, get_menu_cache_age_seconds
    t = time.time()
    eating_data = await scrape_eating_page(page)
    print(f"[Timing] scrape_eating_page ({len(eating_data)}件): {time.time()-t:.1f}s")

    cache_age = get_menu_cache_age_seconds()
    CACHE_TTL = 30 * 24 * 3600  # 30日
    menu_cache = {}
    if cache_age is not None and cache_age < CACHE_TTL:
        menu_cache = get_menu_cache()
        print(f"[fetch_eating] メニューキャッシュ適用: {len(menu_cache)}件 (age={cache_age/3600:.1f}h)")
    else:
        print(f"[fetch_eating] メニューキャッシュなし/期限切れ (age={cache_age}s)")

    for item in eating_data:
        cached_menu = menu_cache.get((item["date"], item["meal_type"]), "")
        upsert_meal(
            user_key=user_key,
            meal_date=item["date"],
            meal_type=item["meal_type"],
            menu=cached_menu,
            registered=item["registered"],
            deadline_passed=item["deadline_passed"],
            is_holiday=item.get("is_holiday", False),
        )
    return eating_data


async def fetch_and_update_menus(page, user_key: str):
    """TOPページからメニューを取得→共有キャッシュ保存→ユーザーのDB更新"""
    import time
    from database import update_meal_menu, upsert_menu_cache
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

    cache_count = 0
    for meal_date, menu_str in menus.items():
        parts = menu_str.split(" | ") if menu_str else []
        for meal_type, idx in [("朝食", 0), ("夕食", 1)]:
            menu = parts[idx] if idx < len(parts) else ""
            if menu:
                upsert_menu_cache(meal_date, meal_type, menu)   # 共有キャッシュに保存
                update_meal_menu(user_key, meal_date, meal_type, menu)  # このユーザーに適用
                cache_count += 1
    print(f"[menu] {len(menus)}日分 ({cache_count}件) キャッシュ保存＋DB更新完了")


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
    today = today_jst()

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



async def _submit_registration(page):
    print("[submit] 登録ボタンクリック")
    btn = page.locator('button:has-text("登録")')
    await btn.wait_for(state="visible", timeout=10000)
    await btn.scroll_into_view_if_needed()
    await btn.click()
    try:
        await page.wait_for_selector('[role="dialog"]', timeout=5000)
        print("[submit] ダイアログ表示")
        ok_btn = page.locator('[role="dialog"] button:has-text("OK")')
        if await ok_btn.count() > 0:
            await ok_btn.scroll_into_view_if_needed()
            await ok_btn.click()
            print("[submit] OK クリック")
        await page.wait_for_selector('[role="dialog"]', state="detached", timeout=8000)
        print("[submit] 登録完了")
    except Exception as e:
        print(f"[submit] 警告: {e}")
        await page.wait_for_timeout(1000)


async def _ensure_eating_page(page):
    """EATINGページにいない場合のみ遷移"""
    if "#/eating" not in page.url:
        await page.goto(EATING_URL, wait_until="networkidle")
    await _wait_for_table(page)


async def apply_changes_for_month(page, changes: list[dict]) -> list[str]:
    """同一月の変更をまとめてチェック→登録1回で送信。失敗した item の key リストを返す"""
    import re as _re
    if not changes:
        return []

    target_date = date.fromisoformat(changes[0]["date"])
    target_year, target_month = target_date.year, target_date.month

    await _ensure_eating_page(page)
    year, month = await _get_displayed_month(page)
    print(f"[apply] 現在: {year}年{month}月 → 目標: {target_year}年{target_month}月")

    if (year, month) != (target_year, target_month):
        diff = (target_year - year) * 12 + (target_month - month)
        direction = "next" if diff > 0 else "prev"
        print(f"[apply] {abs(diff)}回ナビ ({direction})")
        for step in range(abs(diff)):
            cur_y, cur_m = await _get_displayed_month(page)
            delta = 1 if direction == "next" else -1
            next_m = cur_m + delta
            if next_m > 12:
                next_y, next_m = cur_y + 1, 1
            elif next_m < 1:
                next_y, next_m = cur_y - 1, 12
            else:
                next_y = cur_y
            clicked = await _click_month_nav(page, direction)
            if not clicked:
                print(f"[apply] ERROR: ボタンクリック失敗")
                return [f"{item['date']} {item['meal_type']}" for item in changes]
            ok = await _wait_for_month(page, next_y, next_m, timeout=8000)
            if ok:
                await _wait_for_table(page, timeout=8000)
            print(f"[apply]  step{step+1}: → {next_y}年{next_m}月 ok={ok}")
            if not ok:
                print(f"[apply] ERROR: 月移動タイムアウト")
                return [f"{item['date']} {item['meal_type']}" for item in changes]
        year, month = await _get_displayed_month(page)
        print(f"[apply] ナビ後: {year}年{month}月")

    if (year, month) != (target_year, target_month):
        print(f"[apply] ERROR: 月移動失敗 → 全件失敗")
        return [f"{item['date']} {item['meal_type']}" for item in changes]

    # テーブルを一度スキャンして day → cells マップを作る
    rows = await page.query_selector_all("tbody.MuiTableBody-root tr.MuiTableRow-root")
    print(f"[apply] 行数: {len(rows)}")
    day_cells: dict[int, list] = {}
    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) < 3:
            continue
        date_text = await cells[0].inner_text()
        m = _re.match(r'(\d+)', date_text.strip())
        if m:
            day_cells[int(m.group(1))] = cells
    print(f"[apply] 日付マップ: {sorted(day_cells.keys())}")

    failed = []
    changed_any = False
    for item in changes:
        d = date.fromisoformat(item["date"])
        cell_idx = 1 if item["meal_type"] == "朝食" else 2

        if d.day not in day_cells:
            print(f"[apply] FAIL: {item['date']} {item['meal_type']} - 行なし")
            failed.append(f"{item['date']} {item['meal_type']}")
            continue

        cells = day_cells[d.day]
        if cell_idx >= len(cells):
            print(f"[apply] FAIL: {item['date']} {item['meal_type']} - セル不足({len(cells)})")
            failed.append(f"{item['date']} {item['meal_type']}")
            continue

        cbs = await cells[cell_idx].query_selector_all("input[type='checkbox']")
        if not cbs:
            print(f"[apply] SKIP: {item['date']} {item['meal_type']} - チェックボックスなし(締切済?)")
            failed.append(f"{item['date']} {item['meal_type']}")
            continue

        is_checked = await cbs[0].is_checked()
        if item["registered"] != is_checked:
            await cbs[0].dispatch_event("click")
            changed_any = True
            print(f"[apply] 変更: {item['date']} {item['meal_type']} {is_checked}→{item['registered']}")

    if changed_any:
        await _submit_registration(page)
    else:
        print("[apply] 変更なし、登録スキップ")

    return failed


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
