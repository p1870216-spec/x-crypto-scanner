# -*- coding: utf-8 -*-
"""
Клиент twitterapi.io (раздел 6.0 ТЗ).

Только чтение публичных данных. На сервис уходят исключительно имена
публичных аккаунтов. Ключ читается из переменной окружения TWITTERAPI_KEY
или из файла .env рядом с main.py; в логи, кеш и выгрузки он не попадает.

HTTP — на стандартной библиотеке, чтобы не тянуть новых зависимостей.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import config

# ------------------------------------------------------------------ ошибки


class ApiError(RuntimeError):
    """Ошибка обращения к twitterapi.io."""

    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ApiKeyMissing(ApiError):
    pass


KEY_HELP = (
    "Не найден ключ twitterapi.io.\n"
    "  1. Заведите аккаунт на twitterapi.io и пополните баланс.\n"
    "  2. Создайте рядом с main.py файл .env со строкой:\n"
    "       TWITTERAPI_KEY=ваш_ключ\n"
    "     (или задайте переменную окружения TWITTERAPI_KEY).\n"
    "  Файл .env уже в .gitignore, ключ никуда, кроме api.twitterapi.io,\n"
    "  не отправляется."
)


# -------------------------------------------------------------------- ключ


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env_file(path=None) -> dict:
    """Прочитать .env: строки вида КЛЮЧ=значение, # — комментарий."""
    path = path or os.path.join(_project_root(), ".env")
    values = {}
    if not os.path.exists(path):
        return values
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def get_api_key(explicit=None) -> str:
    """Ключ из аргумента, переменной окружения или .env — в этом порядке."""
    if explicit:
        return explicit.strip()
    name = getattr(config, "TWITTERAPI_KEY_ENV", "TWITTERAPI_KEY")
    key = os.environ.get(name, "").strip()
    if key:
        return key
    return load_env_file().get(name, "").strip()


def mask_key(key: str) -> str:
    """Для сообщений: показываем только хвост, чтобы можно было сверить ключ."""
    if not key:
        return "—"
    return f"…{key[-4:]} ({len(key)} символов)"


# ------------------------------------------------------------------ клиент


class TwitterApiClient:
    """
    Запросы к twitterapi.io с курсорной пагинацией, повторами и учётом расходов.

    Счётчики (requests / profiles / tweets) нужны, чтобы в конце прогона
    показать фактический расход, а не только смету.
    """

    ENDPOINT_PROFILE = "/twitter/user/info"
    ENDPOINT_FOLLOWINGS = "/twitter/user/followings"
    ENDPOINT_FOLLOWERS = "/twitter/user/followers"
    ENDPOINT_TWEETS = "/twitter/user/last_tweets"
    ENDPOINT_BATCH = "/twitter/user/batch_info_by_ids"

    # Пакет пользователей за один запрос. От 100 сервис берёт 10 кредитов
    # за профиль вместо 18, поэтому пакеты делаем крупными.
    BATCH_SIZE = 100

    def __init__(self, api_key=None, base=None, timeout=None, retries=None,
                 debug_dir=None, samples_per_endpoint=2):
        self.api_key = get_api_key(api_key)
        if not self.api_key:
            raise ApiKeyMissing(KEY_HELP)
        self.base = (base or config.TWITTERAPI_BASE).rstrip("/")
        self.timeout = timeout or config.TWITTERAPI_TIMEOUT
        self.retries = retries if retries is not None else config.TWITTERAPI_RETRIES
        self.debug_dir = debug_dir
        self.samples_per_endpoint = samples_per_endpoint

        self.requests_made = 0
        self.profiles_received = 0
        self.tweets_received = 0
        self._samples = {}
        # Курсор следующей страницы. Сохраняется в кеш, чтобы прерванный
        # сбор продолжился с этого места, а не с начала списка.
        self.last_cursor = ""

    # ------------------------------------------------------------- запрос

    def _url(self, endpoint, params) -> str:
        clean = {k: v for k, v in (params or {}).items()
                 if v is not None and v != ""}
        return f"{self.base}{endpoint}?{urllib.parse.urlencode(clean)}"

    def get(self, endpoint, params=None) -> dict:
        """Один GET с повторами при 429 и 5xx."""
        url = self._url(endpoint, params)
        pause = float(getattr(config, "TWITTERAPI_RETRY_PAUSE", 5))
        last = None

        for attempt in range(self.retries + 1):
            try:
                data = self._raw_get(url)
                self.requests_made += 1
                self._save_sample(endpoint, data)
                status = str(data.get("status", "")).lower()
                if status == "error":
                    raise ApiError(
                        f"twitterapi.io вернул ошибку: "
                        f"{data.get('message') or 'без описания'}")
                return data
            except ApiError as exc:
                last = exc
                if not exc.retryable or attempt >= self.retries:
                    raise
                wait = pause * (attempt + 1)
                print(f"  twitterapi.io: {exc}. Повтор через {wait:.0f} с "
                      f"({attempt + 1}/{self.retries})")
                time.sleep(wait)
        raise last

    def _raw_get(self, url) -> dict:
        request = urllib.request.Request(url, method="GET")
        # Единственный заголовок с ключом. Никуда больше он не уходит.
        request.add_header("x-api-key", self.api_key)
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise self._http_error(exc.code, body) from None
        except urllib.error.URLError as exc:
            raise ApiError(f"нет связи с twitterapi.io ({exc.reason})",
                           retryable=True) from None
        except TimeoutError:
            raise ApiError("twitterapi.io не ответил вовремя",
                           retryable=True) from None

        try:
            return json.loads(body)
        except ValueError:
            raise ApiError(f"ответ не является JSON ({len(body)} символов)") from None

    @staticmethod
    def _http_error(code, body):
        detail = ""
        try:
            payload = json.loads(body)
            detail = payload.get("message") or payload.get("msg") or ""
        except Exception:
            detail = (body or "")[:200]
        detail = f": {detail}" if detail else ""

        if code in (401, 403):
            return ApiError(
                f"ключ не принят (HTTP {code}){detail}.\n"
                f"Проверьте TWITTERAPI_KEY в .env.", status=code)
        if code == 402:
            return ApiError(
                f"на балансе twitterapi.io кончились кредиты (HTTP 402){detail}.",
                status=code)
        if code == 429:
            return ApiError(f"лимит запросов сервиса (HTTP 429){detail}",
                            status=code, retryable=True)
        if code >= 500:
            return ApiError(f"сбой на стороне сервиса (HTTP {code}){detail}",
                            status=code, retryable=True)
        return ApiError(f"HTTP {code}{detail}", status=code)

    def _save_sample(self, endpoint, data):
        """Образцы реальных ответов — по ним строится и проверяется парсер."""
        if not self.debug_dir:
            return
        name = endpoint.strip("/").replace("/", "_")
        n = self._samples.get(name, 0)
        if n >= self.samples_per_endpoint:
            return
        self._samples[name] = n + 1
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            path = os.path.join(self.debug_dir, f"api_{name}_{n + 1}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ----------------------------------------------------------- эндпоинты

    def get_profile(self, username) -> dict:
        """Профиль по username. Возвращает сырой ответ сервиса."""
        return self.get(self.ENDPOINT_PROFILE, {"userName": username})

    def iter_user_list(self, username, kind="followings", page_size=None,
                       max_users=None, on_page=None, start_cursor=""):
        """
        Постранично отдаёт списки подписок или подписчиков.

        kind     — "followings" | "followers" (общая функция, как требует
                   раздел 15 ТЗ: Followers добавляются без переделки);
        on_page  — колбэк(список_записей, номер_страницы) после каждой страницы,
                   чтобы кеш писался сразу и прогон можно было продолжить.

        Отдаёт (запись, номер_страницы) по одной.
        """
        endpoint = (self.ENDPOINT_FOLLOWINGS if kind == "followings"
                    else self.ENDPOINT_FOLLOWERS)
        field = "followings" if kind == "followings" else "followers"
        page_size = page_size or config.TWITTERAPI_PAGE_SIZE

        cursor, page, total = start_cursor or "", 0, 0
        self.last_cursor = cursor
        pause = float(getattr(config, "TWITTERAPI_PAUSE", 0.2))
        while True:
            data = self.get(endpoint, {"userName": username,
                                       "cursor": cursor,
                                       "pageSize": page_size})
            page += 1
            items = _list_field(data, field)
            self.profiles_received += len(items)

            has_more = bool(data.get("has_next_page") and data.get("next_cursor")
                            and items)
            # Курсор запоминаем ДО выдачи записей: если работу прервут прямо
            # сейчас, в кеше окажется правильная точка продолжения.
            self.last_cursor = data.get("next_cursor", "") if has_more else ""
            if on_page:
                on_page(items, page)

            for item in items:
                yield item, page
                total += 1
                if max_users and total >= max_users:
                    return
            if not has_more:
                return
            cursor = data["next_cursor"]
            if pause:
                time.sleep(pause)

    def batch_user_info(self, user_ids, on_batch=None):
        """
        Обогащение профилей пакетами по BATCH_SIZE.

        Зачем: в списке подписок сервис отдаёт урезанную запись — ссылка на
        сайт приходит нераскрытой (t.co), нет ссылок из bio и нет признака
        Premium. Этот эндпоинт всё это добавляет и стоит примерно
        $0.0001 за профиль.

        Отдаёт сырые записи пользователей.
        """
        ids = [str(i) for i in user_ids if i]
        pause = float(getattr(config, "TWITTERAPI_PAUSE", 0.2))
        for start in range(0, len(ids), self.BATCH_SIZE):
            chunk = ids[start:start + self.BATCH_SIZE]
            data = self.get(self.ENDPOINT_BATCH, {"userIds": ",".join(chunk)})
            users = _list_field(data, "users")
            self.profiles_received += len(users)
            if on_batch:
                on_batch(users, start // self.BATCH_SIZE + 1)
            for user in users:
                yield user
            if pause and start + self.BATCH_SIZE < len(ids):
                time.sleep(pause)

    def iter_tweets(self, username=None, user_id=None, max_tweets=30,
                    include_replies=False):
        """Последние посты пользователя, по 20 на страницу."""
        cursor, total = "", 0
        pause = float(getattr(config, "TWITTERAPI_PAUSE", 0.2))
        while True:
            data = self.get(self.ENDPOINT_TWEETS, {
                "userId": user_id,
                "userName": None if user_id else username,
                "cursor": cursor,
                "includeReplies": "true" if include_replies else "false",
            })
            items = _list_field(data, "tweets")
            self.tweets_received += len(items)
            for item in items:
                yield item
                total += 1
                if max_tweets and total >= max_tweets:
                    return
            if not items or not data.get("has_next_page") \
                    or not data.get("next_cursor"):
                return
            cursor = data["next_cursor"]
            if pause:
                time.sleep(pause)

    # ------------------------------------------------------------- деньги

    def spent(self) -> float:
        """Фактический расход по счётчикам, доллары."""
        return estimate_cost(profiles=self.profiles_received,
                             tweets=self.tweets_received,
                             requests=self.requests_made)

    def spent_report(self) -> str:
        return (f"twitterapi.io: запросов {self.requests_made}, "
                f"профилей {self.profiles_received}, "
                f"постов {self.tweets_received}, "
                f"потрачено примерно ${self.spent():.4f}")


def _list_field(data, field) -> list:
    """
    Достать список из ответа. Имя поля берём из документации, но подстраховываемся:
    сервис мог назвать его иначе.
    """
    for name in (field, "data", "results", "users", "items"):
        value = data.get(name)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            inner = value.get(field)
            if isinstance(inner, list):
                return inner
    return []


def estimate_cost(profiles=0, tweets=0, requests=0) -> float:
    """Оценка стоимости по ценам из config.py."""
    cost = (profiles / 1000.0) * config.PRICE_PER_1K_PROFILES
    cost += (tweets / 1000.0) * config.PRICE_PER_1K_TWEETS
    minimum = requests * config.PRICE_MIN_PER_REQUEST
    return round(max(cost, minimum) if requests else cost, 6)
