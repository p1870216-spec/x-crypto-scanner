# -*- coding: utf-8 -*-
"""
Профиль цели и сбор списков (разделы 7.1–7.3 ТЗ).

Сбор вынесен в общую функцию `collect_user_list(username, kind)`, чтобы
позже добавить Followers и пересечение аудиторий без переделки (раздел 15).

Кеш пишется после каждой страницы, поэтому прерывание (Ctrl+C) не теряет
собранное, а следующий запуск продолжает с сохранённого курсора.
"""

import sys

import config

from .api_client import ApiError
from .api_parse import parse_api_user, parse_api_users
from .scoring import is_candidate, score_profile
from .storage import STAGE_FOLLOWING, STATUS_COMPLETE, STATUS_PARTIAL


class CollectorError(RuntimeError):
    """Работа остановлена: продолжать нельзя, кеш сохранён."""


class TargetNotFound(CollectorError):
    pass


def _log(message, logger=None, level="info"):
    print(message)
    sys.stdout.flush()
    if logger:
        getattr(logger, level, logger.info)(message)


class ApiCollector:
    """Сбор данных через twitterapi.io."""

    # Столько подряд упавших аккаунтов считаем общей поломкой (раздел 10).
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, client, cache, logger=None, errors_logger=None):
        self.client = client
        self.cache = cache
        self.log = logger
        self.errors = errors_logger
        self.consecutive_errors = 0

    def _fail(self, stage, username, exc):
        """Ошибка одного аккаунта работу не останавливает (раздел 10)."""
        message = f"{stage} @{username}: {exc.__class__.__name__}: {exc}"
        if self.errors:
            self.errors.error(message)
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
            raise CollectorError(
                f"{self.MAX_CONSECUTIVE_ERRORS} аккаунтов подряд завершились "
                f"ошибкой — похоже на общую проблему. Работа остановлена, "
                f"кеш сохранён. Последняя ошибка: {exc}")

    def _ok(self):
        self.consecutive_errors = 0

    # ------------------------------------------------------- 7.1 профиль цели

    def fetch_target(self, username, max_age_days=None, force=False) -> dict:
        """Профиль цели: из кеша, если свежий, иначе запросом."""
        from .storage import is_fresh

        cached = self.cache.load_target()
        if cached and not force and is_fresh(cached.get("collected_at", ""),
                                             max_age_days):
            _log(f"[2/5] Opening @{username}... из кеша "
                 f"(following: {cached.get('following', 0):,})", self.log)
            return cached

        try:
            payload = self.client.get_profile(username)
        except ApiError as exc:
            raise CollectorError(f"не удалось получить профиль @{username}: {exc}")

        profile = parse_api_user(payload)
        if not profile or not profile.get("username"):
            raise TargetNotFound(f"ERROR: Target user not found: @{username}")
        if profile.get("status") == "unavailable":
            raise TargetNotFound(
                f"ERROR: аккаунт @{username} недоступен "
                f"({profile.get('unavailable_reason') or 'без причины'})")

        self.cache.save_target(profile)
        _log(f"[2/5] Opening @{username}... OK "
             f"(following: {profile.get('following', 0):,})", self.log)
        if profile.get("protected"):
            _log("  ВНИМАНИЕ: аккаунт закрытый, список подписок может быть "
                 "недоступен.", self.log, "warning")
        return profile

    # ---------------------------------------------------- 7.2 сбор списка

    def collect_user_list(self, username, kind="followings", max_users=None,
                          declared=None, resume=True):
        """
        Собрать список подписок (или подписчиков) с сохранением после каждой
        страницы. Возвращает список профилей в общем формате.

        kind — "followings" | "followers".
        """
        state = self.cache.load_following()
        known_ids = list(state.get("ids") or []) if resume else []
        cursor = state.get("cursor", "") if resume else ""
        pages_done = int(state.get("pages", 0)) if resume else 0
        seen = set(known_ids)

        if state.get("status") == STATUS_COMPLETE and resume and not max_users:
            _log(f"[3/5] Collecting {kind}... уже собрано ранее: "
                 f"{len(known_ids):,}", self.log)
            return [self.cache.users[uid] for uid in known_ids
                    if uid in self.cache.users]

        if known_ids:
            _log(f"[3/5] Collecting {kind}... продолжаем: "
                 f"уже есть {len(known_ids):,}, страниц пройдено {pages_done}",
                 self.log)
        else:
            _log(f"[3/5] Collecting {kind}...", self.log)

        last_report = len(seen)
        last_page = pages_done
        status = STATUS_PARTIAL

        def on_page(items, page):
            """После каждой страницы: разобрать, положить в кеш, сохранить."""
            nonlocal last_report, last_page
            last_page = pages_done + page
            users = parse_api_users(items if isinstance(items, list) else [])
            fresh = []
            for user in users:
                # Лимит проверяем здесь, а не только при выдаче: страница
                # приходит целиком, и без этого --max-following 50 сохранил бы
                # все 200 записей страницы.
                if max_users and len(known_ids) >= max_users:
                    break
                uid = user.get("user_id")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                known_ids.append(uid)
                fresh.append(user)
            if fresh:
                self.cache.put_users(fresh)
            self.cache.save_following(known_ids, STATUS_PARTIAL,
                                      cursor=self.client.last_cursor,
                                      pages=last_page, declared=declared)
            if len(seen) - last_report >= 50:
                last_report = len(seen)
                _log(f"  Found: {len(seen):,}", self.log)

        # Сколько ещё нужно добрать с учётом уже собранного при возобновлении.
        remaining = (max_users - len(known_ids)) if max_users else None
        if remaining is not None and remaining <= 0:
            _log(f"  Лимит {max_users} уже набран: {len(known_ids)}", self.log)
            self.cache.save_following(known_ids, STATUS_PARTIAL, cursor=cursor,
                                      pages=pages_done, declared=declared)
            return [self.cache.users[uid] for uid in known_ids
                    if uid in self.cache.users]

        # Маленький лимит — маленькая страница: не платим за записи,
        # которые всё равно отбросим.
        page_size = None
        if remaining:
            page_size = max(20, min(int(config.TWITTERAPI_PAGE_SIZE), remaining))

        try:
            for _ in self.client.iter_user_list(
                    username, kind=kind, max_users=remaining,
                    page_size=page_size, start_cursor=cursor, on_page=on_page):
                pass
            status = STATUS_COMPLETE if not max_users else STATUS_PARTIAL
        except ApiError as exc:
            self.cache.save_following(known_ids, STATUS_PARTIAL,
                                      cursor=self.client.last_cursor,
                                      pages=last_page, declared=declared)
            raise CollectorError(
                f"сбор списка прерван: {exc}\n"
                f"Собрано {len(known_ids):,} аккаунтов, они сохранены. "
                f"Следующий запуск продолжит с этого места.")

        self.cache.save_following(known_ids, status,
                                  cursor=self.client.last_cursor,
                                  pages=last_page, declared=declared)
        self.cache.save_state(STAGE_FOLLOWING, collected=len(known_ids))

        _log(f"  Found: {len(seen):,}"
             + (f" / {declared:,}" if declared else ""), self.log)

        if declared and not max_users and len(seen) < declared * 0.95:
            _log(f"  ВНИМАНИЕ: собрано {len(seen):,} из заявленных "
                 f"{declared:,} ({len(seen) / declared:.0%}). "
                 f"Список может быть неполным.", self.log, "warning")
        _log(f"  {kind.capitalize()} collection completed.", self.log)

        return [self.cache.users[uid] for uid in known_ids
                if uid in self.cache.users]

    # ------------------------------------------------------ обогащение

    def enrich_users(self, users, only_missing=True):
        """
        Дополнить профили пакетным запросом.

        Зачем: в списке подписок сервис не отдаёт признак Premium, а сайт
        присылает нераскрытой ссылкой t.co — домен из неё не виден, а на
        доменах держится часть скоринга. Стоит примерно $0.0001 за профиль.
        """
        targets = []
        for user in users:
            if not user.get("user_id"):
                continue
            if only_missing and user.get("enriched"):
                continue
            targets.append(user["user_id"])
        if not targets:
            return 0

        _log(f"  Обогащение профилей: {len(targets):,} "
             f"(~${len(targets) * 0.0001:.2f})", self.log)

        updated = 0
        batch = []

        def on_batch(items, number):
            nonlocal updated
            enriched = parse_api_users(items, "users")
            for user in enriched:
                user["enriched"] = True
            self.cache.put_users(enriched, save=False)
            updated += len(enriched)
            if number % 3 == 0:
                self.cache.save_users()
                _log(f"    обогащено: {updated:,}/{len(targets):,}", self.log)

        try:
            for _ in self.client.batch_user_info(targets, on_batch=on_batch):
                pass
        except ApiError as exc:
            self.cache.save_users()
            _log(f"  Обогащение прервано: {exc}. "
                 f"Продолжаем с тем, что есть.", self.log, "warning")
        self.cache.save_users()
        return updated

    # ------------------------------------------------ 7.3 первичный анализ

    def initial_analysis(self, users, matcher):
        """
        Посчитать сырой балл профиля для всех и отобрать кандидатов.
        Профили при этом не открываются — это офлайн-этап.
        """
        _log("[4/5] Initial analysis...", self.log)
        candidates, short_bio = [], 0
        total = len(users)

        for i, user in enumerate(users, 1):
            profile = score_profile(user, matcher)
            ok, why = is_candidate(
                user, profile.raw,
                deep_threshold=config.DEEP_ANALYSIS_THRESHOLD,
                low_bio_check=config.LOW_BIO_DEEP_CHECK,
                low_bio_max_length=config.LOW_BIO_MAX_LENGTH,
                low_bio_min_followers=config.LOW_BIO_MIN_FOLLOWERS)
            user["profile_raw"] = profile.raw
            user["candidate"] = ok
            user["candidate_reason"] = why
            if ok:
                candidates.append(user)
                if why == "short_bio":
                    short_bio += 1
            if i % 500 == 0 or i == total:
                _log(f"  Processed: {i}/{total}", self.log)

        self.cache.put_users(users)
        _log(f"  Candidates: {len(candidates)} (short bio: {short_bio})", self.log)
        return candidates
