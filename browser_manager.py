import asyncio
import time
from playwright.async_api import async_playwright, BrowserContext

class UserBrowser:
    """ユーザーごとの認証済みブラウザを保持する。再ログイン不要で操作を高速化。"""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._last_login: float = 0

    async def start(self, company_code: str, user_id_str: str, password: str):
        from scraper import _login
        t = time.time()
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        print(f"[Timing] chromium launch: {time.time()-t:.1f}s")
        t = time.time()
        self._context = await self._browser.new_context()
        page = await self._context.new_page()
        await _login(page, company_code, user_id_str, password)
        await page.close()
        print(f"[Timing] D-Port login: {time.time()-t:.1f}s")
        self._last_login = time.time()

    async def new_page(self):
        return await self._context.new_page()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def ensure_logged_in(self, company_code: str, user_id_str: str, password: str):
        """セッション切れなら再ログイン。ログイン直後5分以内はスキップ。"""
        if time.time() - self._last_login < 300:
            return
        from scraper import BASE_URL
        page = await self.new_page()
        try:
            await page.goto(BASE_URL, wait_until="networkidle")
            login_form = await page.query_selector('input[placeholder="企業コード"]')
            if login_form:
                from scraper import _login
                await _login(page, company_code, user_id_str, password)
                self._last_login = time.time()
        finally:
            await page.close()

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._browser = None
        self._context = None


# token → UserBrowser
_browsers: dict[str, UserBrowser] = {}


async def get_or_create(token: str, company_code: str, user_id_str: str, password: str) -> UserBrowser:
    if token not in _browsers:
        ub = UserBrowser()
        await ub.start(company_code, user_id_str, password)
        _browsers[token] = ub
    return _browsers[token]


def get(token: str) -> UserBrowser | None:
    return _browsers.get(token)


async def close_and_remove(token: str):
    ub = _browsers.pop(token, None)
    if ub:
        await ub.close()
