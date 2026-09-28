# -*- coding: utf-8 -*-
"""
Подключение к уже запущенному Chrome по CDP и безопасная работа со страницей.

ЖЁСТКИЕ ПРАВИЛА (см. раздел 2 CLAUDE.md):
  * никаких кликов по странице X — кнопка "Following" отписывает от аккаунта;
  * никаких нажатий клавиш — у X есть горячие клавиши (`l` = лайк);
  * навигация только через page.goto(), прокрутка только колесом/scrollBy;
  * cookies и заголовки запросов не читаются и не сохраняются;
  * программа работает в СВОЕЙ вкладке и закрывает её в конце,
    вкладки пользователя не трогаются.
"""

import asyncio
import random
import sys

from playwright.async_api import async_playwright

import config


class BrowserError(RuntimeError):
    """Не удалось подключиться к Chrome или открыть вкладку."""


CONNECT_HELP = (
    "Не удалось подключиться к Chrome.\n"
    "  1. Запустите start_chrome.bat (откроется отдельное окно Chrome).\n"
    "  2. В этом окне войдите в свой аккаунт на x.com.\n"
    "  3. Не закрывайте окно и запустите сканер снова.\n"
    f"  Адрес отладочного порта: {config.CDP_URL}"
)


class XBrowser:
    """Обёртка над своей вкладкой в чужом (пользовательском) Chrome."""

    def __init__(self, cdp_url: str = None, speed: str = None):
        self.cdp_url = cdp_url or config.CDP_URL
        self.delays = config.speed_profile(speed)
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    # ------------------------------------------------------------ подключение

    async def connect(self):
        """Подключиться по CDP и открыть собственную вкладку."""
        self._pw = await async_playwright().start()
        try:
            self.browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as exc:
            await self._stop_playwright()
            raise BrowserError(f"{CONNECT_HELP}\n  Причина: {exc}") from exc

        if not self.browser.contexts:
            await self.close()
            raise BrowserError(
                "Chrome подключён, но не найдено ни одного окна.\n"
                "Откройте окно Chrome, запущенное через start_chrome.bat."
            )

        # Работаем в существующем контексте — там живёт сессия пользователя.
        self.context = self.browser.contexts[0]
        self.page = await self.context.new_page()
        self.page.set_default_navigation_timeout(config.NAVIGATION_TIMEOUT * 1000)
        self.page.set_default_timeout(config.NAVIGATION_TIMEOUT * 1000)
        return self.page

    async def close(self):
        """Закрыть свою вкладку. Вкладки пользователя не трогаем."""
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self.browser:
                # Отключение от CDP: сам Chrome при этом не закрывается.
                await self.browser.close()
        except Exception:
            pass
        await self._stop_playwright()

    async def _stop_playwright(self):
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    # ------------------------------------------------------------- навигация

    async def goto(self, url: str, wait_until: str = "domcontentloaded"):
        """Единственный разрешённый способ перехода — прямой goto()."""
        return await self.page.goto(url, wait_until=wait_until)

    async def scroll(self, pixels: int = None):
        """
        Прокрутка вниз. Только колесо мыши — никаких End / PageDown:
        клавиши на странице X запускают горячие клавиши.
        """
        if pixels is None:
            pixels = random.randint(700, 1100)
        try:
            await self.page.mouse.wheel(0, pixels)
        except Exception:
            # Запасной вариант, если колесо недоступно.
            await self.page.evaluate("(y) => window.scrollBy(0, y)", pixels)

    async def scroll_position(self) -> int:
        """Текущая позиция прокрутки — чтобы понять, дошли ли до конца."""
        try:
            return int(await self.page.evaluate("() => Math.round(window.scrollY)"))
        except Exception:
            return -1

    async def page_height(self) -> int:
        try:
            return int(await self.page.evaluate(
                "() => document.body ? document.body.scrollHeight : 0"))
        except Exception:
            return -1

    async def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    # --------------------------------------------------------------- паузы

    def _rand(self, kind: str) -> float:
        lo, hi = self.delays[kind]
        return random.uniform(lo, hi)

    async def pause_scroll(self, factor: float = 1.0):
        await asyncio.sleep(self._rand("scroll") * factor)

    async def pause_profile(self):
        await asyncio.sleep(self._rand("profile"))

    async def long_pause(self, verbose: bool = True):
        seconds = self._rand("long_pause")
        if verbose:
            print(f"  Long pause: {seconds:.0f}s (protection against X limits)")
            sys.stdout.flush()
        await asyncio.sleep(seconds)


# Признаки авторизованного интерфейса X (только чтение DOM, без кликов).
LOGGED_IN_SELECTORS = (
    '[data-testid="SideNav_AccountSwitcher_Button"]',
    '[data-testid="AppTabBar_Home_Link"]',
    '[data-testid="primaryColumn"]',
)

# Признаки гостевого состояния.
LOGGED_OUT_SELECTORS = (
    '[data-testid="loginButton"]',
    '[data-testid="signupButton"]',
    'a[href="/i/flow/login"]',
)


async def detect_login_state(page, timeout: float = 20.0) -> str:
    """
    Возвращает "in" | "out" | "unknown".
    Ждём появления характерных элементов, страница X собирается не сразу.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        url = page.url or ""
        if "/i/flow/login" in url or url.rstrip("/").endswith("x.com/login"):
            return "out"
        if "/account/access" in url:
            return "captcha"
        for sel in LOGGED_IN_SELECTORS:
            try:
                if await page.locator(sel).count():
                    return "in"
            except Exception:
                pass
        for sel in LOGGED_OUT_SELECTORS:
            try:
                if await page.locator(sel).count():
                    return "out"
            except Exception:
                pass
        await asyncio.sleep(1.0)
    return "unknown"


async def current_account(page) -> str:
    """
    Под каким аккаунтом открыт X. Только чтение текста элемента —
    по кнопке переключателя аккаунтов НЕ кликаем.
    """
    # Ссылка на свой профиль в боковом меню — самый надёжный источник.
    try:
        loc = page.locator('[data-testid="AppTabBar_Profile_Link"]')
        if await loc.count():
            href = await loc.first.get_attribute("href")
            if href and href.startswith("/"):
                return "@" + href.strip("/").split("/")[0]
    except Exception:
        pass
    # Запасной вариант: текст переключателя аккаунтов (по нему НЕ кликаем).
    try:
        loc = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]')
        if await loc.count():
            text = await loc.first.inner_text()
            for line in (text or "").splitlines():
                line = line.strip()
                if line.startswith("@"):
                    return line
    except Exception:
        pass
    return ""


async def check_connection(verbose: bool = True) -> bool:
    """
    Проверка этапа 1: подключиться к Chrome, открыть свою вкладку,
    зайти на x.com и убедиться, что пользователь авторизован.
    """
    br = XBrowser()
    try:
        await br.connect()
    except BrowserError as exc:
        print(f"ERROR: {exc}")
        return False

    try:
        version = None
        try:
            version = br.browser.version
        except Exception:
            pass
        if verbose:
            print(f"  Connected to Chrome via CDP ({br.cdp_url})")
            if version:
                print(f"  Browser: {version}")
            print(f"  Contexts: {len(br.browser.contexts)}, "
                  f"own tab opened (user tabs untouched)")

        await br.goto("https://x.com/home")
        state = await detect_login_state(br.page)
        url = await br.current_url()
        if verbose:
            print(f"  Opened: {url}")

        if state == "out":
            print("ERROR: X требует входа. Войдите в аккаунт в окне Chrome, "
                  "запущенном через start_chrome.bat, и повторите.")
            return False
        if state == "captcha":
            print("ERROR: X показывает проверку (CAPTCHA). Пройдите её вручную "
                  "в окне Chrome и повторите.")
            return False
        if state != "in":
            print("ERROR: не удалось определить состояние страницы X "
                  f"(url: {url}). Откройте x.com в окне Chrome и проверьте, "
                  "что лента загружается.")
            return False

        if verbose:
            handle = await current_account(br.page)
            if handle:
                print(f"  Logged in as: {handle}")
        print("  Session authorized. Connection check passed.")
        return True
    finally:
        await br.close()
