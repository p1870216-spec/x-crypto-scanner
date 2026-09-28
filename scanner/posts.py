# -*- coding: utf-8 -*-
"""
Глубокий анализ кандидатов: последние посты, оценка, специализации
(разделы 7.4 и 8–9 ТЗ).

В кеш кладутся СЫРЫЕ тексты постов, а не оценки: тогда `--rescore` пересчитает
всё после правки словарей, не потратив ни одного запроса.

Закрытый, заблокированный или удалённый аккаунт — не ошибка: ставится статус
и оценка считается только по профилю.
"""

import sys
import time

import config

from .api_client import ApiError
from .api_parse import parse_api_tweet
from .scoring import (
    STATUS_ERROR,
    STATUS_NOT_ANALYZED,
    STATUS_OK,
    STATUS_PROTECTED,
    STATUS_SUSPENDED,
    analyze_posts,
    compute_score,
    score_profile,
)
from .specializations import SpecializationDetector

STATUS_NO_POSTS = "no_posts"

# Размер страницы постов у twitterapi.io. Оплата идёт за страницу целиком.
PAGE_SIZE = 20


def _log(message, logger=None, level="info"):
    print(message)
    sys.stdout.flush()
    if logger:
        getattr(logger, level, logger.info)(message)


def human_eta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"~{seconds} sec"
    minutes = seconds // 60
    if minutes < 90:
        return f"~{minutes} min"
    return f"~{minutes // 60} h {minutes % 60} min"


def fetch_posts(client, user, max_tweets=None, include_replies=None):
    """
    Последние посты одного кандидата.

    Возвращает (посты, статус). Пустая выдача у сервиса неотличима от закрытого
    или удалённого аккаунта — статус в этом случае определяем по профилю,
    который уже лежит в кеше.
    """
    max_tweets = max_tweets or config.TWEETS_PER_USER
    if include_replies is None:
        include_replies = config.INCLUDE_REPLIES

    # Сервис отдаёт посты страницами по 20 и берёт деньги за всю страницу.
    # При TWEETS_PER_USER=30 оплачиваются две страницы, то есть 40 постов, —
    # так что забираем все 40, а не выбрасываем оплаченное.
    page = PAGE_SIZE
    max_tweets = -(-max_tweets // page) * page

    username = user.get("username") or ""
    user_id = user.get("user_id") or ""
    pinned = set(user.get("pinned_tweet_ids") or ())

    raw = list(client.iter_tweets(username=username, user_id=user_id,
                                  max_tweets=max_tweets,
                                  include_replies=include_replies))
    posts, seen = [], set()
    for item in raw:
        post = parse_api_tweet(item, pinned)
        if not post or post["tweet_id"] in seen:
            continue
        # Чужие посты в ленте кандидата нам не нужны: только его собственные
        # и его репосты.
        author = (post.get("author") or "").lower()
        if author and username and author != username.lower() \
                and not post.get("is_repost"):
            continue
        seen.add(post["tweet_id"])
        posts.append(post)

    if posts:
        return posts, STATUS_OK
    if user.get("protected"):
        return [], STATUS_PROTECTED
    if user.get("status") == "unavailable":
        return [], STATUS_SUSPENDED
    return [], STATUS_NO_POSTS


def build_record(user, score, specializations) -> dict:
    """Строка результата для кеша и выгрузок."""
    return {
        "user_id": user.get("user_id", ""),
        "username": user.get("username", ""),
        "crypto_score": score.crypto_score,
        "category": score.category,
        "category_emoji": score.category_emoji,
        "profile_score": score.profile_score,
        "tweet_score": score.tweet_score,
        "profile_raw": score.profile_raw,
        "specializations": list(specializations or []),
        "matched_keywords": score.matched_keywords,
        "reason": score.reason,
        "crypto_posts": score.crypto_posts,
        "analyzed_posts": score.analyzed_posts,
        "crypto_posts_ratio": score.crypto_posts_ratio,
        "replies_crypto_ratio": score.replies_crypto_ratio,
        "status": score.status,
        "data_source": user.get("data_source", ""),
    }


def score_user(user, posts, status, matcher, detector, analyze_tweets=True) -> dict:
    """Посчитать балл и специализации по профилю и (если есть) постам."""
    profile = score_profile(user, matcher)
    stats = None
    if analyze_tweets and posts:
        stats = analyze_posts(posts, matcher,
                              repost_weight=config.REPOST_WEIGHT,
                              include_replies=config.INCLUDE_REPLIES)
    score = compute_score(user, profile, stats, matcher,
                          analyze_tweets=analyze_tweets, status=status)
    specializations = detector.detect(
        user, posts=posts, post_stats=stats,
        crypto_score=score.crypto_score, threshold=config.CRYPTO_THRESHOLD)
    return build_record(user, score, specializations)


class DeepAnalyzer:
    """Этап 5: посты кандидатов и итоговая оценка."""

    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, client, cache, matcher, detector=None, logger=None,
                 errors_logger=None):
        self.client = client
        self.cache = cache
        self.matcher = matcher
        self.detector = detector or SpecializationDetector(matcher=matcher)
        self.log = logger
        self.errors = errors_logger
        self.consecutive_errors = 0
        self.stats = {"analyzed": 0, "from_cache": 0, "unavailable": 0,
                      "errors": 0, "skipped": 0}

    def _fail(self, username, exc):
        message = f"posts @{username}: {exc.__class__.__name__}: {exc}"
        if self.errors:
            self.errors.error(message)
        self.stats["errors"] += 1
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
            raise RuntimeError(
                f"{self.MAX_CONSECUTIVE_ERRORS} аккаунтов подряд завершились "
                f"ошибкой — похоже на общую проблему. Работа остановлена, "
                f"кеш сохранён. Последняя ошибка: {exc}")

    def analyze(self, candidates, limit=None, max_age_days=None):
        """
        Пройти по кандидатам: взять посты (из кеша или запросом), оценить,
        сохранить. Прогресс и оценка оставшегося времени — в консоль.
        """
        planned = list(candidates)
        if limit:
            planned = planned[:limit]
            self.stats["skipped"] = max(0, len(candidates) - limit)

        total = len(planned)
        if not total:
            return self.stats

        started = time.monotonic()
        durations = []

        for index, user in enumerate(planned, 1):
            username = user.get("username") or user.get("user_id") or "?"
            began = time.monotonic()
            try:
                posts, status = self._posts_for(user, max_age_days)
            except ApiError as exc:
                self._fail(username, exc)
                record = score_user(user, [], STATUS_ERROR, self.matcher,
                                    self.detector, config.ANALYZE_TWEETS)
                self.cache.put_analysis(user["user_id"], record)
                continue
            self.consecutive_errors = 0

            record = score_user(user, posts, status, self.matcher, self.detector,
                                config.ANALYZE_TWEETS)
            self.cache.put_analysis(user["user_id"], record)
            # Кеш сохраняется после каждого кандидата: прерывание не теряет
            # уже оплаченные запросы.
            self.cache.save_analysis()

            if status in (STATUS_PROTECTED, STATUS_SUSPENDED, STATUS_NO_POSTS):
                self.stats["unavailable"] += 1
            else:
                self.stats["analyzed"] += 1

            durations.append(time.monotonic() - began)
            note = "" if status == STATUS_OK else f" [{status}]"
            line = (f"  [{index}/{total}] @{username} - "
                    f"score {record['crypto_score']}{note}")
            if index % 10 == 0 or index == total:
                average = sum(durations[-20:]) / len(durations[-20:])
                line += f"   ETA: {human_eta(average * (total - index))}"
            _log(line, self.log)

        elapsed = time.monotonic() - started
        _log(f"  Готово за {human_eta(elapsed)}: "
             f"проанализировано {self.stats['analyzed']}, "
             f"из кеша {self.stats['from_cache']}, "
             f"недоступны {self.stats['unavailable']}, "
             f"ошибок {self.stats['errors']}", self.log)
        return self.stats

    def _posts_for(self, user, max_age_days):
        """Посты из кеша, если они свежие, иначе запросом."""
        cached = self.cache.posts_of(user["user_id"], max_age_days)
        if cached is not None:
            self.stats["from_cache"] += 1
            return cached.get("posts") or [], cached.get("status", STATUS_OK)

        posts, status = fetch_posts(self.client, user)
        self.cache.put_posts(user["user_id"], posts, status)
        return posts, status


def analyze_all(users, cache, matcher, detector=None, analyze_tweets=True):
    """
    Итоговые оценки по ВСЕМ аккаунтам, а не только по кандидатам.

    Не кандидат — оценка равна сырому баллу профиля, колонки постов пустые,
    статус not_analyzed (раздел 8.3). Функция ничего не запрашивает у сервиса,
    поэтому на ней же держится `--rescore`.
    """
    detector = detector or SpecializationDetector(matcher=matcher)
    records = []
    for user in users:
        uid = user.get("user_id")
        if not uid:
            continue
        cached = cache.posts_of(uid)
        if cached is not None:
            posts = cached.get("posts") or []
            status = cached.get("status", STATUS_OK)
        else:
            posts, status = [], STATUS_NOT_ANALYZED

        record = score_user(user, posts, status, matcher, detector,
                            analyze_tweets)
        cache.put_analysis(uid, record)
        records.append(record)
    cache.save_analysis()
    return records
