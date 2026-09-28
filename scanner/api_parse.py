# -*- coding: utf-8 -*-
"""
Ответы twitterapi.io → тот же плоский формат, что даёт браузерный парсер
(scanner/capture.py). Благодаря этому скоринг, кеш и выгрузки не зависят
от источника данных.

Построено по РЕАЛЬНЫМ ответам сервиса, снятым 10.09.2026 (образцы лежат
в cache/_debug/api_*.json и в tests/fixtures/). Имена полей у разных
эндпоинтов различаются, поэтому каждый путь пробуется в нескольких вариантах:

    /twitter/user/info             data.followers,  data.isBlueVerified, camelCase
    /twitter/user/followings       followers_count, verified,            snake_case
    /twitter/user/batch_info_by_ids  followers,     isBlueVerified,      camelCase
"""

import datetime
import re

from .capture import dig

DATA_SOURCE = "api"

_TWITTER_DATE = "%a %b %d %H:%M:%S %z %Y"


def parse_date(value) -> str:
    """
    Дата регистрации приходит в двух форматах:
        '2018-02-24T08:31:35.000000Z'
        'Sun Jul 25 00:20:01 +0000 2021'
    Приводим к 'YYYY-MM-DD', чтобы в выгрузке была одна форма.
    """
    if not value or not isinstance(value, str):
        return ""
    text = value.strip()
    try:
        return datetime.datetime.strptime(text, _TWITTER_DATE).strftime("%Y-%m-%d")
    except ValueError:
        pass
    iso = text.replace("Z", "+00:00")
    iso = re.sub(r"\.(\d{6})\d+", r".\1", iso)
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except ValueError:
        return text


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _expanded(container) -> list:
    out = []
    for item in container or []:
        url = item.get("expanded_url") or item.get("url")
        if url:
            out.append(url)
    return out


def _website(node) -> str:
    """
    Сайт профиля. В списке подписок url приходит нераскрытой ссылкой t.co —
    её брать нельзя, домен из неё не виден. Настоящий адрес лежит в
    entities.url.urls[].expanded_url (есть в batch_info_by_ids и user/info).
    """
    expanded = dig(node, "entities.url.urls.0.expanded_url")
    if expanded:
        return expanded
    url = node.get("url") or ""
    if url and "t.co/" not in url:
        return url
    return ""


def parse_api_user(node) -> dict:
    """Пользователь из любого эндпоинта twitterapi.io → общий формат."""
    if not isinstance(node, dict):
        return None
    # /twitter/user/info кладёт профиль в data
    if "data" in node and isinstance(node.get("data"), dict) and \
            not node.get("userName") and not node.get("screen_name"):
        node = node["data"]

    username = (node.get("userName") or node.get("screen_name")
                or node.get("username") or "")
    user_id = str(node.get("id") or node.get("id_str") or "")
    if not username and not user_id:
        return None

    if node.get("unavailable"):
        return {
            "user_id": user_id,
            "username": username,
            "status": "unavailable",
            "unavailable_reason": node.get("message") or "",
            "data_source": DATA_SOURCE,
        }

    verified = bool(node.get("isBlueVerified") or node.get("isVerified")
                    or node.get("verified"))

    return {
        "user_id": user_id,
        "username": username,
        "name": node.get("name") or "",
        "created_at": parse_date(node.get("createdAt") or node.get("created_at")),
        "bio": node.get("description") or dig(node, "profile_bio.description",
                                              default="") or "",
        "location": node.get("location") or "",
        "followers": _int(node.get("followers") if node.get("followers") is not None
                          else node.get("followers_count")),
        "following": _int(node.get("following") if node.get("following") is not None
                          else (node.get("following_count")
                                or node.get("friends_count"))),
        "tweets": _int(node.get("statusesCount") or node.get("statuses_count")),
        "verified": verified,
        "verified_type": node.get("verifiedType") or node.get("verified_type") or "",
        "protected": bool(node.get("protected")),
        "website": _website(node),
        "bio_links": _expanded(dig(node, "entities.description.urls",
                                   "profile_bio.entities.description.urls",
                                   default=[])),
        # У twitterapi.io нет поля professional.category, которое отдаёт сам X,
        # поэтому этот признак в режиме api не участвует.
        "professional_categories": [],
        "avatar": node.get("profilePicture") or node.get("profile_image_url_https") or "",
        "pinned_tweet_ids": [str(x) for x in (node.get("pinnedTweetIds") or [])],
        "profile_url": f"https://x.com/{username}" if username else "",
        "status": "ok",
        "data_source": DATA_SOURCE,
    }


def parse_api_users(payload, field="followings") -> list:
    """Список пользователей из ответа followings / followers / batch."""
    if isinstance(payload, list):
        items = payload
    else:
        items = None
        for name in (field, "users", "followers", "followings", "data"):
            value = (payload or {}).get(name)
            if isinstance(value, list):
                items = value
                break
        items = items or []
    out = []
    for item in items:
        user = parse_api_user(item)
        if user:
            out.append(user)
    return out


def _tags(node, key, field="text") -> list:
    out = []
    for item in dig(node, f"entities.{key}", default=[]) or []:
        value = item.get(field)
        if value:
            out.append(value)
    return out


def parse_api_tweet(node, pinned_ids=()) -> dict:
    """Пост из /twitter/user/last_tweets → общий формат."""
    if not isinstance(node, dict):
        return None
    tweet_id = str(node.get("id") or "")
    if not tweet_id:
        return None

    repost = node.get("retweeted_tweet") or None
    quoted = node.get("quoted_tweet") or None

    text = node.get("text") or ""
    if repost:
        # У репоста в text лежит обрезанное "RT @user: …" — берём оригинал.
        original = repost.get("text") or ""
        if original:
            text = original

    urls = _expanded(dig(node, "entities.urls", default=[]))
    cashtags = _tags(node, "symbols")
    hashtags = _tags(node, "hashtags")
    if repost:
        urls += _expanded(dig(repost, "entities.urls", default=[]))
        cashtags += _tags(repost, "symbols")
        hashtags += _tags(repost, "hashtags")

    return {
        "tweet_id": tweet_id,
        "text": text,
        "created_at": node.get("createdAt") or "",
        "author": dig(node, "author.userName", default=""),
        "is_repost": bool(repost),
        "repost_of": dig(repost, "author.userName", default="") if repost else "",
        "is_reply": bool(node.get("isReply") or node.get("inReplyToId")),
        "is_quote": bool(quoted),
        "pinned": tweet_id in set(pinned_ids or ()),
        "urls": urls,
        "cashtags": cashtags,
        "hashtags": hashtags,
        "lang": node.get("lang") or "",
        "favorite_count": _int(node.get("likeCount")),
        "retweet_count": _int(node.get("retweetCount")),
    }


def parse_api_tweets(payload, pinned_ids=()) -> list:
    """
    Посты из ответа last_tweets, без дублей по id.

    Реальная структура ответа (проверено 10.09.2026): посты лежат в
    data.tweets, а закреплённый — отдельным полем data.pin_tweet. Часто оно
    приходит пустым, и тогда закреплённого поста в выдаче просто нет;
    задвоения при этом не возникает.
    """
    if isinstance(payload, list):
        items = list(payload)
    else:
        items = (payload or {}).get("tweets")
        if not isinstance(items, list):
            items = list(dig(payload, "data.tweets", default=[]) or [])
        else:
            items = list(items)
        pinned = (payload or {}).get("pin_tweet") or dig(payload, "data.pin_tweet")
        if isinstance(pinned, dict) and pinned.get("id"):
            pinned_ids = set(pinned_ids or ()) | {str(pinned["id"])}
            items.insert(0, pinned)

    out, seen = [], set()
    for item in items:
        tweet = parse_api_tweet(item, pinned_ids)
        if not tweet or tweet["tweet_id"] in seen:
            continue
        seen.add(tweet["tweet_id"])
        out.append(tweet)
    return out


def author_profile(tweet_node) -> dict:
    """
    Профиль автора, вложенный в пост. Он богаче, чем запись в списке подписок:
    там есть isBlueVerified и раскрытые ссылки. Используем, чтобы уточнить
    данные кандидата без отдельного запроса.
    """
    author = (tweet_node or {}).get("author")
    return parse_api_user(author) if isinstance(author, dict) else None
