# -*- coding: utf-8 -*-
"""
Разбор ответов twitterapi.io (раздел 6.0 ТЗ).

Фикстуры api_*_real.json — настоящие ответы сервиса, снятые 10.09.2026.
Проверяем, что записи всех трёх эндпоинтов сводятся к одному формату
и что значения совпадают с тем, что показывает X на страницах.
"""

import json
import os

import pytest

from scanner.api_client import estimate_cost, load_env_file, mask_key
from scanner.api_parse import (
    parse_api_tweet,
    parse_api_tweets,
    parse_api_user,
    parse_api_users,
    parse_date,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def profile_payload():
    return load("api_user_info_real.json")


@pytest.fixture(scope="module")
def followings_payload():
    return load("api_followings_real.json")


@pytest.fixture(scope="module")
def batch_payload():
    return load("api_batch_info_real.json")


@pytest.fixture(scope="module")
def tweets_payload():
    return load("api_last_tweets_real.json")


# ------------------------------------------------------------------- даты


@pytest.mark.parametrize("value,expected", [
    ("2018-02-24T08:31:35.000000Z", "2018-02-24"),      # /user/info
    ("Sun Jul 25 00:20:01 +0000 2021", "2021-07-25"),   # followings, batch
    ("", ""),
    (None, ""),
    ("что-то непонятное", "что-то непонятное"),
])
def test_parse_date(value, expected):
    assert parse_date(value) == expected


# --------------------------------------------------------------- профиль


def test_profile_from_user_info(profile_payload):
    u = parse_api_user(profile_payload)
    # Сверено со страницей x.com/Rus_Khairullin
    assert u["user_id"] == "967316062080348160"
    assert u["username"] == "Rus_Khairullin"
    assert u["name"] == "Ruslan Khairullin"
    assert u["following"] == 1487
    assert u["followers"] > 120000
    assert u["verified"] is True          # isBlueVerified
    assert u["protected"] is False
    assert u["location"] == "Dubai, United Arab Emirates"
    assert u["created_at"] == "2018-02-24"
    assert "Founder InvestZone" in u["bio"]
    assert u["profile_url"] == "https://x.com/Rus_Khairullin"
    assert u["data_source"] == "api"


# ------------------------------------------------------- список подписок


def test_followings_common_format(followings_payload):
    users = parse_api_users(followings_payload)
    assert len(users) == 5
    by_name = {u["username"]: u for u in users}

    u = by_name["JohannKerbrat"]
    assert u["user_id"] == "1419089826586918913"
    assert u["name"] == "Johann Kerbrat"
    assert u["following"] == 117          # following_count / friends_count
    assert u["followers"] > 20000
    assert u["tweets"] > 0
    assert u["created_at"] == "2021-07-25"
    assert "Robinhood" in u["bio"]
    assert u["status"] == "ok"


def test_followings_do_not_leak_tco_links(followings_payload):
    """
    В списке подписок сайт приходит нераскрытой ссылкой t.co — домена из неё
    не видно, поэтому в website такая ссылка попасть не должна.
    """
    for u in parse_api_users(followings_payload):
        assert "t.co/" not in u["website"]


def test_followings_have_no_premium_flag(followings_payload):
    """
    Признак Premium в списке подписок сервис не отдаёт (только устаревшее
    verified=false). Поэтому профили кандидатов обогащаются пакетным запросом.
    """
    users = {u["username"]: u for u in parse_api_users(followings_payload)}
    assert users["fundstrat"]["verified"] is False


# -------------------------------------------------- пакетное обогащение


def test_batch_info_adds_premium_and_links(batch_payload):
    users = {u["username"]: u for u in parse_api_users(batch_payload, "users")}

    assert users["fundstrat"]["verified"] is True
    assert users["Binance_intern"]["verified_type"] == "Business"

    # ссылки раскрыты: домен виден и его можно оценить
    assert users["fundstrat"]["website"] == "https://fundstratdirect.com/tlee-services/"
    assert users["fundstrat"]["bio_links"] == ["http://bit.ly/3JkWhtw"]
    assert users["guywuolletjr"]["bio_links"] == ["http://a16z.com/disclosures/"]
    assert users["kain"]["website"] == "https://infinex.xyz"


def test_batch_and_followings_agree_on_shared_fields(batch_payload,
                                                     followings_payload):
    """Один и тот же пользователь из разных эндпоинтов — одни и те же поля."""
    batch = {u["username"]: u for u in parse_api_users(batch_payload, "users")}
    listed = {u["username"]: u for u in parse_api_users(followings_payload)}
    for name in set(batch) & set(listed):
        for field in ("user_id", "name", "created_at", "bio", "location",
                      "following", "protected"):
            assert batch[name][field] == listed[name][field], f"{name}.{field}"


def test_unavailable_user():
    u = parse_api_user({"id": "1", "userName": "gone", "unavailable": True,
                        "message": "suspended"})
    assert u["status"] == "unavailable"


# ------------------------------------------------------------------ посты


def test_tweets_common_format(tweets_payload):
    tweets = parse_api_tweets(tweets_payload)
    assert len(tweets) == 3
    for t in tweets:
        assert t["tweet_id"].isdigit()
        assert t["author"] == "Rus_Khairullin"
        assert t["text"]
        assert isinstance(t["cashtags"], list)


def test_tweets_cashtags_and_urls(tweets_payload):
    tweets = {t["tweet_id"]: t for t in parse_api_tweets(tweets_payload)}
    assert tweets["2097946784051970179"]["cashtags"] == ["LAPTOP"]
    assert tweets["2097742332212699606"]["cashtags"] == ["STRK"]
    # ссылка раскрыта из entities, а не оставлена как t.co
    with_urls = tweets["2098018947933094349"]
    assert with_urls["urls"] == ["https://app.nado.xyz/?join=investzona"]


def test_tweets_no_duplicates(tweets_payload):
    ids = [t["tweet_id"] for t in parse_api_tweets(tweets_payload)]
    assert len(ids) == len(set(ids))


def test_repost_uses_original_text():
    payload = {"tweets": [{
        "id": "1",
        "text": "RT @whale_alert: 5000 $BTC transferred…",
        "createdAt": "Thu Sep 04 12:00:00 +0000 2026",
        "author": {"userName": "reposter"},
        "retweeted_tweet": {
            "id": "0",
            "text": "5000 $BTC transferred from unknown wallet to Binance",
            "author": {"userName": "whale_alert"},
            "entities": {"symbols": [{"text": "BTC"}],
                         "urls": [{"expanded_url": "https://etherscan.io/tx/0x1"}]},
        },
    }]}
    t = parse_api_tweets(payload)[0]
    assert t["is_repost"] is True
    assert t["repost_of"] == "whale_alert"
    assert t["text"].startswith("5000 $BTC transferred from unknown")
    assert "…" not in t["text"]
    assert t["cashtags"] == ["BTC"]
    assert t["urls"] == ["https://etherscan.io/tx/0x1"]


def test_pinned_tweet_from_separate_field():
    """Закреплённый пост приходит отдельным полем data.pin_tweet."""
    payload = {"data": {
        "pin_tweet": {"id": "100", "text": "закреплённый",
                      "author": {"userName": "someone"}},
        "tweets": [{"id": "200", "text": "обычный",
                    "author": {"userName": "someone"}}],
    }}
    tweets = parse_api_tweets(payload)
    assert len(tweets) == 2
    pinned = [t for t in tweets if t["pinned"]]
    assert len(pinned) == 1 and pinned[0]["tweet_id"] == "100"


def test_pinned_tweet_not_duplicated():
    """Если закреплённый пост есть и в списке — он не должен задвоиться."""
    tweet = {"id": "100", "text": "закреплённый", "author": {"userName": "x"}}
    payload = {"data": {"pin_tweet": tweet, "tweets": [tweet]}}
    tweets = parse_api_tweets(payload)
    assert len(tweets) == 1
    assert tweets[0]["pinned"] is True


def test_reply_flag():
    payload = {"tweets": [{"id": "1", "text": "ответ", "isReply": True,
                           "inReplyToId": "999", "author": {"userName": "x"}}]}
    assert parse_api_tweets(payload)[0]["is_reply"] is True


def test_broken_payloads_do_not_crash():
    for payload in ({}, {"data": {}}, {"tweets": None}, {"followings": None},
                    {"users": "не список"}):
        assert parse_api_users(payload) == []
        assert parse_api_tweets(payload) == []
    assert parse_api_user(None) is None
    assert parse_api_user({}) is None
    assert parse_api_tweet({}) is None


# ------------------------------------------------------- клиент и деньги


def test_estimate_cost():
    # 1487 профилей по $0.01 за 1000
    assert estimate_cost(profiles=1487) == pytest.approx(0.01487, abs=1e-6)
    # 15000 постов по $0.15 за 1000
    assert estimate_cost(tweets=15000) == pytest.approx(2.25, abs=1e-6)
    assert estimate_cost() == 0


def test_mask_key_hides_secret():
    key = "abcdef1234567890"
    masked = mask_key(key)
    assert key not in masked
    assert masked.startswith("…7890")
    assert mask_key("") == "—"


def test_load_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# комментарий\nTWITTERAPI_KEY = secret \nПУСТО\n",
                    encoding="utf-8")
    values = load_env_file(str(path))
    assert values["TWITTERAPI_KEY"] == "secret"


def test_load_env_file_missing(tmp_path):
    assert load_env_file(str(tmp_path / "нет.env")) == {}
