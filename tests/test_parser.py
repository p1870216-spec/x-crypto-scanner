# -*- coding: utf-8 -*-
"""
Разбор сохранённых образцов ответов X (этап 2).

Фикстуры *_real.json — настоящие ответы X, снятые 10.09.2026.
synthetic_cases.json — случаи, которых в той выдаче не было: репосты,
длинные посты, реклама, удалённые твиты, старая схема legacy, битые ответы.
"""

import json
import os

import pytest

from scanner.capture import (
    bottom_cursor,
    dig,
    extract_tweets,
    extract_users,
    operation_of,
    parse_profile,
    timeline_terminated,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def profile_payload():
    return load("UserByScreenName_real.json")


@pytest.fixture(scope="module")
def following_payload():
    return load("Following_real.json")


@pytest.fixture(scope="module")
def tweets_payload():
    return load("UserOriginalsTimeline_real.json")


@pytest.fixture(scope="module")
def synthetic():
    return load("synthetic_cases.json")


# ------------------------------------------------------------ имя операции


@pytest.mark.parametrize("url,expected", [
    ("https://x.com/i/api/graphql/abc123/Following?variables=%7B%7D", "Following"),
    ("https://x.com/i/api/graphql/x-Y_9/UserByScreenName", "UserByScreenName"),
    ("https://x.com/i/api/graphql/h/UserOriginalsTimeline?a=1", "UserOriginalsTimeline"),
    ("https://x.com/home", ""),
    ("", ""),
])
def test_operation_of(url, expected):
    assert operation_of(url) == expected


# ------------------------------------------------------------------- dig


def test_dig_tries_paths_in_order():
    obj = {"legacy": {"name": "старое"}, "core": {"name": "новое"}}
    assert dig(obj, "core.name", "legacy.name") == "новое"
    assert dig(obj, "нет.такого", "legacy.name") == "старое"
    assert dig(obj, "нет.такого", default="—") == "—"


def test_dig_handles_lists_and_wrong_types():
    obj = {"urls": [{"expanded_url": "https://a.io"}]}
    assert dig(obj, "urls.0.expanded_url") == "https://a.io"
    assert dig(obj, "urls.5.expanded_url") is None
    assert dig(obj, "urls.0.expanded_url.deeper") is None
    assert dig("строка", "что.угодно") is None
    assert dig(None, "a.b") is None


# --------------------------------------------------------- профиль цели


def test_profile_real_fields(profile_payload):
    p = parse_profile(profile_payload)
    # Значения сверены с тем, что показано на странице x.com/Rus_Khairullin
    assert p["user_id"] == "967316062080348160"
    assert p["username"] == "Rus_Khairullin"
    assert p["name"] == "Ruslan Khairullin"
    assert p["followers"] == 126225
    assert p["following"] == 1487
    assert p["tweets"] == 9893
    assert p["verified"] is True
    assert p["protected"] is False
    assert p["location"] == "Dubai, United Arab Emirates"
    assert "Founder InvestZone" in p["bio"]
    # t.co раскрыт до настоящего адреса
    assert p["website"] == "https://t.me/invest_zonaa"
    assert p["profile_url"] == "https://x.com/Rus_Khairullin"
    assert p["professional_categories"] == ["Blockchain"]
    assert p["data_source"] == "graphql"


def test_profile_missing_user_returns_none():
    assert parse_profile({"data": {"user": {}}}) is None
    assert parse_profile({}) is None


# ------------------------------------------------------- список Following


def test_following_real_users(following_payload):
    users = extract_users(following_payload)
    assert len(users) == 5           # в фикстуре оставлено 5 пользователей
    by_name = {u["username"]: u for u in users}

    # Поля сверены с карточками на странице /following
    u = by_name["JohannKerbrat"]
    assert u["name"] == "Johann Kerbrat"
    assert u["followers"] == 23343
    assert u["following"] == 117
    assert u["tweets"] == 361
    assert "Robinhood" in u["bio"]
    assert u["status"] == "ok"

    u = by_name["fundstrat"]
    assert u["followers"] == 674183
    assert u["website"] == "https://fundstratdirect.com/tlee-services/"
    assert u["bio_links"] == ["http://bit.ly/3JkWhtw"]

    u = by_name["Binance_intern"]
    assert u["verified_type"] == "Business"


def test_following_skips_cursors(following_payload):
    users = extract_users(following_payload)
    assert all(u["user_id"] for u in users)
    assert all(not u["username"].startswith("cursor") for u in users)


def test_timeline_terminate_and_cursor(following_payload):
    # X отдаёт список одной страницей и сразу закрывает пагинацию вниз
    assert timeline_terminated(following_payload) is True
    assert bottom_cursor(following_payload)


def test_following_old_legacy_schema(synthetic):
    users = extract_users(synthetic["following_old_schema"])
    assert len(users) == 2
    old, unavailable = users[0], users[1]

    assert old["username"] == "old_schema_user"
    assert old["name"] == "Старая схема"
    assert old["followers"] == 12345
    assert old["following"] == 678
    assert old["tweets"] == 9012
    assert old["verified"] is True
    assert old["verified_type"] == "Business"
    assert old["location"] == "Москва"
    assert old["website"] == "https://etherscan.io/address/0x1"
    assert old["bio_links"] == ["https://dexscreener.com/solana"]

    assert unavailable["status"] == "unavailable"
    assert unavailable["user_id"] == "222"


# ------------------------------------------------------------------ посты


def test_tweets_real(tweets_payload):
    tweets = extract_tweets(tweets_payload)
    # в фикстуре 4 обычных поста плюс закреплённый из TimelinePinEntry
    assert len(tweets) == 5
    ids = [t["tweet_id"] for t in tweets]
    assert len(ids) == len(set(ids)), "дубли постов"

    pinned = [t for t in tweets if t["pinned"]]
    assert len(pinned) == 1
    assert pinned[0]["tweet_id"] == "2090734811769970964"
    assert all(t["author"] == "Rus_Khairullin" for t in tweets)
    assert all(t["text"] for t in tweets)


def test_tweets_module_and_cursors_skipped(tweets_payload):
    # блок «Кого читать» и курсоры не должны попасть в посты
    tweets = extract_tweets(tweets_payload)
    assert all(t["tweet_id"].isdigit() for t in tweets)


def test_tweets_cashtags_from_entities(tweets_payload):
    tweets = extract_tweets(tweets_payload)
    assert all(isinstance(t["cashtags"], list) for t in tweets)


def test_tweets_edge_cases(synthetic):
    tweets = extract_tweets(synthetic["tweets_edge_cases"])
    by_id = {t["tweet_id"]: t for t in tweets}

    # реклама и удалённый пост отсеяны
    assert "4000" not in by_id
    assert "5000" not in by_id

    # закреплённый пост не задвоился, хотя пришёл дважды
    assert len([t for t in tweets if t["tweet_id"] == "1000"]) == 1
    assert by_id["1000"]["pinned"] is True

    # репост: помечен, текст взят из оригинала, а не обрезанный "RT @…"
    rt = by_id["2000"]
    assert rt["is_repost"] is True
    assert rt["repost_of"] == "whale_alert"
    assert rt["text"].startswith("5000 $BTC transferred")
    assert "…" not in rt["text"]
    assert rt["cashtags"] == ["BTC"]
    assert rt["urls"] == ["https://etherscan.io/tx/0xabc"]

    # длинный пост: полный текст из note_tweet, обёртка развёрнута
    long_post = by_id["3000"]
    assert long_post["text"].endswith("потоки на биржи.")
    assert long_post["is_reply"] is True
    assert long_post["hashtags"] == ["onchain"]


def test_broken_payloads_do_not_crash(synthetic):
    for payload in synthetic["broken_payloads"]:
        assert extract_users(payload) == []
        assert extract_tweets(payload) == []
        assert timeline_terminated(payload) is False
        assert bottom_cursor(payload) == ""
