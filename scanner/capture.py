# -*- coding: utf-8 -*-
"""
Перехват ответов X.

Слушаем page.on("response") и читаем ТОЛЬКО ТЕЛА ответов, которые браузер
получил сам при открытии страницы и прокрутке. Своих запросов к эндпоинтам X
не отправляем, заголовки запросов и cookies не читаем и не сохраняем.

Обработчик не блокирует: чтение тела уходит в asyncio.create_task, все
исключения гасятся (после навигации тело ответа может стать недоступным).
"""

import asyncio
import json
import os
import re
import time

# Все интересные ответы приходят на внутренний GraphQL X.
GRAPHQL_MARKER = "/i/api/graphql/"

# .../graphql/<hash>/<OperationName>?variables=...
_OPERATION_RE = re.compile(r"/i/api/graphql/[^/]+/([A-Za-z0-9_]+)")

# Имена операций X. Проверено на живом X 10.09.2026: посты профиля приходят
# в UserOriginalsTimeline, а не в UserTweets, как было раньше. Держим оба.
OP_PROFILE = "UserByScreenName"
OP_FOLLOWING = "Following"
OP_FOLLOWERS = "Followers"          # задел на будущее, см. раздел 15 ТЗ

POST_OPERATIONS = frozenset({
    "UserTweets",
    "UserOriginalsTimeline",
    "UserTweetsAndReplies",
})

LIST_OPERATIONS = frozenset({OP_FOLLOWING, OP_FOLLOWERS})


def operation_of(url: str) -> str:
    """Имя GraphQL-операции из URL ответа, иначе пустая строка."""
    m = _OPERATION_RE.search(url or "")
    return m.group(1) if m else ""


class ResponseCapture:
    """
    Собирает тела GraphQL-ответов в очередь.

    operations — какие операции ловить (None = все).
    debug_dir  — куда складывать образцы сырых ответов (для разработки).
    """

    def __init__(self, page, operations=None, debug_dir=None,
                 samples_per_operation: int = 3):
        self.page = page
        self.operations = set(operations) if operations else None
        self.debug_dir = debug_dir
        self.samples_per_operation = samples_per_operation

        self.payloads = []          # [(operation, dict)]
        self.statuses = []          # [(operation, http_status)] — для guards
        self.errors = []            # тексты ошибок чтения тела
        self._saved_counts = {}
        self._tasks = set()
        self._attached = False

    # ------------------------------------------------------------- подписка

    def attach(self):
        if not self._attached:
            self.page.on("response", self._on_response)
            self._attached = True

    def detach(self):
        if self._attached:
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass
            self._attached = False

    async def drain(self, timeout: float = 5.0):
        """Дождаться, пока дочитаются уже начатые тела ответов."""
        deadline = time.monotonic() + timeout
        while self._tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------ обработчик

    def _on_response(self, response):
        url = getattr(response, "url", "") or ""
        if GRAPHQL_MARKER not in url:
            return
        op = operation_of(url)
        if self.operations is not None and op not in self.operations:
            return

        # Не блокируем обработчик событий: читаем тело отдельной задачей.
        task = asyncio.create_task(self._read_body(response, op))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read_body(self, response, op: str):
        try:
            status = response.status
        except Exception:
            status = 0
        self.statuses.append((op, status))

        try:
            text = await response.text()
        except Exception as exc:
            # Обычное дело: после навигации тело уже недоступно.
            self.errors.append(f"{op}: body unavailable ({exc.__class__.__name__})")
            return

        try:
            data = json.loads(text)
        except Exception:
            self.errors.append(f"{op}: not JSON ({len(text)} chars)")
            return

        self.payloads.append((op, data))
        self._save_sample(op, text)

    def _save_sample(self, op: str, text: str):
        """Сохранить образец ответа для разработки парсера и тестов."""
        if not self.debug_dir:
            return
        n = self._saved_counts.get(op, 0)
        if n >= self.samples_per_operation:
            return
        self._saved_counts[op] = n + 1
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            path = os.path.join(self.debug_dir, f"{op}_{n + 1}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as exc:
            self.errors.append(f"{op}: sample not saved ({exc})")

    # ----------------------------------------------------------- получение

    def take(self, operation: str = None) -> list:
        """Забрать накопленные ответы (и очистить очередь)."""
        if operation is None:
            out = [d for _, d in self.payloads]
            self.payloads = []
            return out
        out = [d for op, d in self.payloads if op == operation]
        self.payloads = [(op, d) for op, d in self.payloads if op != operation]
        return out

    def seen_operations(self) -> dict:
        counts = {}
        for op, _ in self.payloads:
            counts[op] = counts.get(op, 0) + 1
        return counts


# =============================================================================
#  РАЗБОР ОТВЕТОВ
#
#  ВАЖНО. Схема пользователя в X изменилась: объекта `legacy` у пользователя
#  больше нет, поля переехали в core / profile_bio / relationship_counts /
#  tweet_counts / privacy / verification. У постов `legacy` пока сохранился.
#  Ниже сначала пробуются новые пути, затем старые — чтобы парсер пережил
#  и откат X к прежней схеме, и повторное изменение.
# =============================================================================


def dig(obj, *paths, default=None):
    """
    Достать значение по первому подошедшему пути.
    Путь — строка вида "profile_bio.entities.url.urls.0.expanded_url".
    Отсутствие любого звена не считается ошибкой.
    """
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict):
                if part in cur:
                    cur = cur[part]
                    continue
                ok = False
                break
            if isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                    continue
                except (ValueError, IndexError):
                    ok = False
                    break
            ok = False
            break
        if ok and cur is not None:
            return cur
    return default


def _instructions(payload) -> list:
    """Инструкции таймлайна, где бы X их ни положил."""
    for path in ("data.user.result.timeline.timeline.instructions",
                 "data.user.result.timeline.instructions",
                 "data.user.result.timeline_v2.timeline.instructions",
                 "data.timeline.timeline.instructions",
                 "data.timeline_response.timeline.instructions"):
        ins = dig(payload, path)
        if isinstance(ins, list):
            return ins
    return []


def timeline_terminated(payload) -> bool:
    """
    X прислал TimelineTerminateTimeline(Bottom) — клиент больше не будет
    подгружать список вниз. Признак конца выдачи (настоящего или обрезанного).
    """
    for ins in _instructions(payload):
        if ins.get("type") == "TimelineTerminateTimeline" and \
                ins.get("direction") == "Bottom":
            return True
    return False


def bottom_cursor(payload) -> str:
    """Курсор следующей страницы, если X его прислал."""
    for e in iter_entries(payload):
        c = e.get("content", {})
        if c.get("cursorType") == "Bottom" or \
                (e.get("entryId", "").startswith("cursor-bottom")):
            return c.get("value", "")
    return ""


def iter_entries(payload):
    """
    Все записи таймлайна: обычные (TimelineAddEntries) и закреплённая
    (TimelinePinEntry). Записи внутри модулей отдаются отдельно, с пометкой.
    """
    for ins in _instructions(payload):
        itype = ins.get("type")
        if itype == "TimelinePinEntry":
            entry = ins.get("entry")
            if entry:
                entry = dict(entry)
                entry["_pinned"] = True
                yield entry
        elif itype in ("TimelineAddEntries", "TimelineReplaceEntry"):
            entries = ins.get("entries")
            if entries is None and ins.get("entry"):
                entries = [ins["entry"]]
            for e in entries or []:
                yield e


def _is_service_entry(entry) -> bool:
    """Курсоры и служебные блоки вроде «Кого читать»."""
    eid = entry.get("entryId", "")
    if eid.startswith(("cursor-", "who-to-follow", "promoted-tweet-promoted",
                       "messageprompt-", "toptabsrpusermodule")):
        return True
    etype = dig(entry, "content.entryType")
    return etype == "TimelineTimelineCursor"


def is_promoted(entry, result=None) -> bool:
    """Рекламная запись."""
    if entry.get("entryId", "").startswith("promoted-"):
        return True
    if dig(entry, "content.itemContent.promotedMetadata") is not None:
        return True
    return result is not None and result.get("promotedMetadata") is not None


# ------------------------------------------------------------ пользователи


def _expand_urls(container) -> list:
    """Развернуть t.co-ссылки в настоящие адреса."""
    out = []
    for u in container or []:
        url = u.get("expanded_url") or u.get("url")
        if url:
            out.append(url)
    return out


def parse_user(result) -> dict:
    """
    Один пользователь из user_results.result → плоский словарь.
    Возвращает None, если это не пользователь (заглушка, ограничение и т.п.).
    """
    if not isinstance(result, dict):
        return None
    typename = result.get("__typename")
    if typename in ("UserUnavailable",):
        return {
            "user_id": dig(result, "rest_id", default=""),
            "username": "",
            "status": "unavailable",
            "unavailable_reason": dig(result, "reason", default=""),
            "data_source": "graphql",
        }
    if not result.get("rest_id") and not dig(result, "core.screen_name",
                                             "legacy.screen_name"):
        return None

    website = dig(result,
                  "profile_bio.entities.url.urls.0.expanded_url",
                  "legacy.entities.url.urls.0.expanded_url",
                  "website.url",
                  "legacy.url",
                  default="")
    bio_links = _expand_urls(
        dig(result,
            "profile_bio.entities.description.urls",
            "legacy.entities.description.urls",
            default=[]))

    categories = []
    for c in dig(result, "professional.category", default=[]) or []:
        name = c.get("name")
        if name:
            categories.append(name)

    return {
        "user_id": str(result.get("rest_id") or ""),
        "username": dig(result, "core.screen_name", "legacy.screen_name",
                        default=""),
        "name": dig(result, "core.name", "legacy.name", default=""),
        "created_at": dig(result, "core.created_at", "legacy.created_at",
                          default=""),
        "bio": dig(result, "profile_bio.description", "legacy.description",
                   default=""),
        "location": dig(result, "location.location", "legacy.location",
                        default=""),
        "followers": int(dig(result, "relationship_counts.followers",
                             "legacy.followers_count", default=0) or 0),
        "following": int(dig(result, "relationship_counts.following",
                             "legacy.friends_count", default=0) or 0),
        "tweets": int(dig(result, "tweet_counts.tweets",
                          "legacy.statuses_count", default=0) or 0),
        "verified": bool(result.get("is_blue_verified")
                         or dig(result, "verification.verified",
                                "legacy.verified", default=False)),
        "verified_type": dig(result, "verification.verified_type",
                             "legacy.verified_type", default=""),
        "protected": bool(dig(result, "privacy.protected", "legacy.protected",
                              default=False)),
        "website": website,
        "bio_links": bio_links,
        "professional_categories": categories,
        "avatar": dig(result, "avatar.image_url",
                      "legacy.profile_image_url_https", default=""),
        "profile_url": "https://x.com/" + dig(result, "core.screen_name",
                                              "legacy.screen_name", default=""),
        "status": "ok",
        "data_source": "graphql",
    }


def extract_users(payload) -> list:
    """Все пользователи из ответа Following / Followers."""
    users = []
    for entry in iter_entries(payload):
        if _is_service_entry(entry):
            continue
        result = dig(entry, "content.itemContent.user_results.result")
        if result is None:
            # пользователи внутри модулей (например, карусели)
            for item in dig(entry, "content.items", default=[]) or []:
                r = dig(item, "item.itemContent.user_results.result")
                u = parse_user(r) if r else None
                if u:
                    users.append(u)
            continue
        if is_promoted(entry, result):
            continue
        u = parse_user(result)
        if u:
            users.append(u)
    return users


def parse_profile(payload) -> dict:
    """Профиль из ответа UserByScreenName."""
    result = dig(payload, "data.user.result", "data.user_result.result")
    if result is None:
        return None
    return parse_user(result)


# ------------------------------------------------------------------ посты


def _unwrap_tweet(result):
    """TweetWithVisibilityResults → сам твит."""
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        return result.get("tweet") or {}
    return result


def _tweet_text(tweet) -> str:
    """Длинные посты лежат в note_tweet, обычные — в legacy.full_text."""
    return dig(tweet,
               "note_tweet.note_tweet_results.result.text",
               "legacy.full_text",
               "full_text",
               default="")


def parse_tweet(result, entry=None) -> dict:
    """Один пост из tweet_results.result → плоский словарь."""
    tweet = _unwrap_tweet(result)
    if not isinstance(tweet, dict):
        return None
    if tweet.get("__typename") == "TweetTombstone":
        return None
    legacy = tweet.get("legacy", {}) or {}

    repost_of = _unwrap_tweet(dig(legacy, "retweeted_status_result.result"))
    is_repost = repost_of is not None
    text = _tweet_text(tweet)
    if is_repost:
        # В legacy.full_text у репоста лежит обрезанное "RT @user: ...",
        # берём полный текст оригинала.
        original = _tweet_text(repost_of)
        if original:
            text = original

    # Внешние ссылки. t.co от вложенных фото и видео сюда не попадают:
    # они лежат в entities.media, а не в entities.urls.
    urls = _expand_urls(dig(legacy, "entities.urls", default=[]))
    if is_repost:
        urls += _expand_urls(dig(repost_of, "legacy.entities.urls", default=[]))

    def _tags(src, key, field):
        out = []
        for x in dig(src, f"legacy.entities.{key}", default=[]) or []:
            v = x.get(field)
            if v:
                out.append(v)
        return out

    cashtags = _tags(tweet, "symbols", "text")
    hashtags = _tags(tweet, "hashtags", "text")
    if is_repost:
        cashtags += _tags(repost_of, "symbols", "text")
        hashtags += _tags(repost_of, "hashtags", "text")

    author = dig(tweet, "core.user_results.result.core.screen_name",
                 "core.user_results.result.legacy.screen_name", default="")

    return {
        "tweet_id": str(tweet.get("rest_id") or legacy.get("id_str") or ""),
        "text": text,
        "created_at": legacy.get("created_at", ""),
        "author": author,
        "is_repost": is_repost,
        "repost_of": dig(repost_of, "core.user_results.result.core.screen_name",
                         default="") if is_repost else "",
        "is_reply": bool(legacy.get("in_reply_to_status_id_str")),
        "is_quote": bool(legacy.get("is_quote_status")),
        "pinned": bool(entry.get("_pinned")) if entry else False,
        "urls": urls,
        "cashtags": cashtags,
        "hashtags": hashtags,
        "lang": legacy.get("lang", ""),
        "favorite_count": legacy.get("favorite_count", 0),
        "retweet_count": legacy.get("retweet_count", 0),
    }


def extract_tweets(payload) -> list:
    """
    Все посты из ответа UserTweets / UserOriginalsTimeline /
    UserTweetsAndReplies. Реклама и служебные блоки отсеиваются,
    закреплённый пост помечается pinned и не дублируется.
    """
    tweets = []
    seen = set()
    for entry in iter_entries(payload):
        if _is_service_entry(entry):
            continue

        candidates = []
        result = dig(entry, "content.itemContent.tweet_results.result")
        if result is not None:
            candidates.append((result, entry))
        else:
            # модули: ветки обсуждений и подборки
            for item in dig(entry, "content.items", default=[]) or []:
                r = dig(item, "item.itemContent.tweet_results.result")
                if r is not None:
                    candidates.append((r, entry))

        for result, ent in candidates:
            if is_promoted(ent, result):
                continue
            t = parse_tweet(result, ent)
            if not t or not t["tweet_id"] or t["tweet_id"] in seen:
                continue
            seen.add(t["tweet_id"])
            tweets.append(t)
    return tweets
