# -*- coding: utf-8 -*-
"""
Глубокий анализ кандидатов (разделы 7.4, 8.3 ТЗ).
Сеть не используется: клиент подменён заглушкой.
"""

import pytest

import config
from scanner.api_client import ApiError
from scanner.posts import (
    PAGE_SIZE,
    STATUS_NO_POSTS,
    DeepAnalyzer,
    analyze_all,
    fetch_posts,
    human_eta,
    score_user,
)
from scanner.scoring import STATUS_NOT_ANALYZED, STATUS_OK, STATUS_PROTECTED
from scanner.specializations import SpecializationDetector
from scanner.storage import TargetCache
from scanner.text_match import build_matcher


@pytest.fixture(scope="module")
def matcher():
    return build_matcher()


@pytest.fixture(scope="module")
def detector(matcher):
    return SpecializationDetector(matcher=matcher)


@pytest.fixture
def cache(tmp_path):
    return TargetCache("target", root=str(tmp_path))


def user(uid="1", username="someone", bio="crypto trader", **extra):
    data = {"user_id": uid, "username": username, "name": "", "bio": bio,
            "followers": 1000, "following": 10, "tweets": 100,
            "website": "", "bio_links": [], "professional_categories": [],
            "protected": False, "status": "ok", "pinned_tweet_ids": []}
    data.update(extra)
    return data


def api_tweet(tid, text, author="someone", **extra):
    data = {"id": str(tid), "text": text, "author": {"userName": author},
            "createdAt": "Wed Sep 09 17:26:09 +0000 2026"}
    data.update(extra)
    return data


class FakeClient:
    """Отдаёт посты страницами по 20, как настоящий сервис."""

    def __init__(self, tweets_by_user=None, fail_for=()):
        self.tweets_by_user = tweets_by_user or {}
        self.fail_for = set(fail_for)
        self.calls = []
        self.tweets_received = 0

    def iter_tweets(self, username=None, user_id=None, max_tweets=30,
                    include_replies=False):
        self.calls.append({"username": username, "max_tweets": max_tweets,
                           "include_replies": include_replies})
        if username in self.fail_for:
            raise ApiError("сервис недоступен")
        items = self.tweets_by_user.get(username, [])
        if not include_replies:
            items = [t for t in items if not t.get("isReply")]
        served = 0
        for item in items:
            yield item
            served += 1
            self.tweets_received += 1
            if max_tweets and served >= max_tweets:
                return


# ------------------------------------------------------------------- ETA


@pytest.mark.parametrize("seconds,expected", [
    (5, "~5 sec"), (89, "~89 sec"), (120, "~2 min"),
    (3600, "~60 min"), (5400, "~1 h 30 min"), (7800, "~2 h 10 min"),
    (-5, "~0 sec"),
])
def test_human_eta(seconds, expected):
    assert human_eta(seconds) == expected


# ------------------------------------------------------------ сбор постов


def test_fetch_posts_rounds_up_to_paid_page(matcher):
    """
    Сервис берёт деньги за страницу целиком. При TWEETS_PER_USER=30
    оплачиваются 40 постов, значит и забирать надо 40.
    """
    tweets = [api_tweet(i, f"пост {i}") for i in range(60)]
    client = FakeClient({"someone": tweets})
    posts, status = fetch_posts(client, user(), max_tweets=30)

    assert client.calls[0]["max_tweets"] == 40
    assert len(posts) == 40
    assert status == STATUS_OK
    assert 40 % PAGE_SIZE == 0


def test_fetch_posts_drops_duplicates():
    tweets = [api_tweet(1, "первый"), api_tweet(1, "он же"), api_tweet(2, "второй")]
    posts, _ = fetch_posts(FakeClient({"someone": tweets}), user())
    assert [p["tweet_id"] for p in posts] == ["1", "2"]


def test_fetch_posts_drops_foreign_posts():
    """Чужие посты в ленте не наши — кроме репостов."""
    tweets = [
        api_tweet(1, "мой пост"),
        api_tweet(2, "чужой пост", author="stranger"),
        api_tweet(3, "репост", author="stranger",
                  retweeted_tweet={"id": "30", "text": "оригинал",
                                   "author": {"userName": "stranger"}}),
    ]
    posts, _ = fetch_posts(FakeClient({"someone": tweets}), user())
    ids = [p["tweet_id"] for p in posts]
    assert ids == ["1", "3"]
    assert posts[1]["is_repost"] is True
    assert posts[1]["repost_of"] == "stranger"


def test_fetch_posts_marks_pinned():
    tweets = [api_tweet(1, "обычный"), api_tweet(99, "закреплённый")]
    posts, _ = fetch_posts(FakeClient({"someone": tweets}),
                           user(pinned_tweet_ids=["99"]))
    pinned = [p for p in posts if p["pinned"]]
    assert len(pinned) == 1 and pinned[0]["tweet_id"] == "99"


def test_fetch_posts_excludes_replies_by_default():
    tweets = [api_tweet(1, "пост"), api_tweet(2, "ответ", isReply=True)]
    posts, _ = fetch_posts(FakeClient({"someone": tweets}), user())
    assert [p["tweet_id"] for p in posts] == ["1"]


def test_fetch_posts_status_for_protected():
    posts, status = fetch_posts(FakeClient({}), user(protected=True))
    assert posts == [] and status == STATUS_PROTECTED


def test_fetch_posts_status_when_empty():
    """Пустая выдача у сервиса неотличима от удалённого аккаунта."""
    posts, status = fetch_posts(FakeClient({}), user())
    assert posts == [] and status == STATUS_NO_POSTS


# --------------------------------------------------------------- оценка


def test_score_user_combines_profile_and_posts(matcher, detector):
    tweets = [{"tweet_id": str(i), "text": "$BTC long setup", "is_repost": False,
               "is_reply": False, "urls": [], "cashtags": ["BTC"]}
              for i in range(20)]
    record = score_user(user(bio="Crypto trader | BTC | ETH | DeFi"),
                        tweets, STATUS_OK, matcher, detector)
    assert record["crypto_score"] > 80
    assert record["analyzed_posts"] == 20
    assert record["crypto_posts"] == 20
    assert "TRADER" in record["specializations"]
    assert record["reason"]


def test_score_user_protected_uses_profile_only(matcher, detector):
    record = score_user(user(bio="crypto trader", followers=150_000),
                        [], STATUS_PROTECTED, matcher, detector)
    assert record["status"] == STATUS_PROTECTED
    assert record["analyzed_posts"] == 0
    assert "protected" in record["reason"].lower()


def test_score_user_specializations_need_threshold(matcher, detector):
    record = score_user(user(bio="повар"), [], STATUS_OK, matcher, detector)
    assert record["crypto_score"] < config.CRYPTO_THRESHOLD
    assert record["specializations"] == []


# ------------------------------------------------------------- проход


def test_analyzer_saves_after_each_candidate(cache, matcher):
    tweets = {f"user{i}": [api_tweet(f"{i}0", "bitcoin", author=f"user{i}")]
              for i in range(3)}
    candidates = [user(str(i), f"user{i}") for i in range(3)]
    analyzer = DeepAnalyzer(FakeClient(tweets), cache, matcher)
    analyzer.analyze(candidates)

    assert analyzer.stats["analyzed"] == 3
    assert len(cache.analysis) == 3
    # посты в кеше лежат сырыми текстами — для --rescore
    assert cache.posts_of("0")["posts"][0]["text"] == "bitcoin"


def test_analyzer_limit_for_trial_run(cache, matcher):
    tweets = {f"user{i}": [api_tweet(f"{i}0", "bitcoin", author=f"user{i}")]
              for i in range(10)}
    candidates = [user(str(i), f"user{i}") for i in range(10)]
    client = FakeClient(tweets)
    analyzer = DeepAnalyzer(client, cache, matcher)
    analyzer.analyze(candidates, limit=3)

    assert len(client.calls) == 3
    assert analyzer.stats["skipped"] == 7


def test_analyzer_reuses_cached_posts(cache, matcher):
    tweets = {"user0": [api_tweet("1", "bitcoin", author="user0")]}
    candidates = [user("0", "user0")]

    client = FakeClient(tweets)
    DeepAnalyzer(client, cache, matcher).analyze(candidates)
    assert len(client.calls) == 1

    client2 = FakeClient(tweets)
    analyzer = DeepAnalyzer(client2, cache, matcher)
    analyzer.analyze(candidates, max_age_days=7)
    assert client2.calls == [], "повторно платить за те же посты нельзя"
    assert analyzer.stats["from_cache"] == 1


def test_analyzer_single_failure_does_not_stop(cache, matcher):
    tweets = {"user1": [api_tweet("10", "bitcoin", author="user1")]}
    candidates = [user("0", "user0"), user("1", "user1")]
    analyzer = DeepAnalyzer(FakeClient(tweets, fail_for=["user0"]), cache, matcher)
    analyzer.analyze(candidates)

    assert analyzer.stats["errors"] == 1
    assert analyzer.stats["analyzed"] == 1
    assert cache.analysis["0"]["status"] == "error"


def test_analyzer_stops_after_five_failures(cache, matcher):
    candidates = [user(str(i), f"user{i}") for i in range(6)]
    client = FakeClient({}, fail_for=[f"user{i}" for i in range(6)])
    analyzer = DeepAnalyzer(client, cache, matcher)
    with pytest.raises(RuntimeError) as exc:
        analyzer.analyze(candidates)
    assert "подряд" in str(exc.value)


# -------------------------------------------- итоговая оценка по всем


def test_analyze_all_marks_non_candidates(cache, matcher):
    users = [user("1", "analyzed"), user("2", "skipped", bio="повар")]
    cache.put_posts("1", [{"tweet_id": "5", "text": "bitcoin", "is_repost": False,
                           "is_reply": False, "urls": [], "cashtags": []}])
    records = {r["username"]: r for r in analyze_all(users, cache, matcher)}

    assert records["analyzed"]["status"] == STATUS_OK
    assert records["analyzed"]["analyzed_posts"] == 1
    assert records["skipped"]["status"] == STATUS_NOT_ANALYZED
    assert records["skipped"]["analyzed_posts"] == 0
    assert records["skipped"]["crypto_posts_ratio"] is None


def test_analyze_all_is_offline(cache, matcher):
    """--rescore не должен требовать ни сети, ни клиента."""
    users = [user("1", "a"), user("2", "b")]
    cache.put_posts("1", [{"tweet_id": "5", "text": "$BTC long", "is_repost": False,
                           "is_reply": False, "urls": [], "cashtags": ["BTC"]}])
    records = analyze_all(users, cache, matcher)
    assert len(records) == 2
    assert len(cache.analysis) == 2


def test_analyze_all_respects_no_tweets(cache, matcher):
    users = [user("1", "a", bio="crypto trader", followers=150_000)]
    cache.put_posts("1", [{"tweet_id": "5", "text": "нейтральный пост",
                           "is_repost": False, "is_reply": False,
                           "urls": [], "cashtags": []}])
    record = analyze_all(users, cache, matcher, analyze_tweets=False)[0]
    assert "Profile-only" in record["reason"]
