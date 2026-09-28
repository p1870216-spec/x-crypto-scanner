# -*- coding: utf-8 -*-
"""
Сбор списка с кешем и возобновлением (разделы 7.1–7.3, 10 ТЗ).

Сеть не используется: вместо клиента подставляется заглушка, которая
отдаёт страницы так же, как это делает twitterapi.io.
"""

import pytest

from scanner.api_client import ApiError
from scanner.collector import ApiCollector, CollectorError, TargetNotFound
from scanner.storage import STATUS_COMPLETE, STATUS_PARTIAL, TargetCache
from scanner.text_match import build_matcher


def make_user(n, bio="", followers=1000):
    return {
        "id": str(n),
        "userName": f"user{n}",
        "name": f"Имя {n}",
        "description": bio,
        "followers_count": followers,
        "following_count": 10,
        "statuses_count": 100,
        "created_at": "Sun Jul 25 00:20:01 +0000 2021",
        "protected": False,
        "url": None,
    }


class FakeClient:
    """Заглушка twitterapi.io: страницы по page_size, курсор = номер записи."""

    def __init__(self, users, profile=None, fail_after=None):
        self.users = users
        self.profile = profile or {"data": {
            "id": "1", "userName": "target", "name": "Цель",
            "description": "", "followers": 100, "following": len(users),
            "statusesCount": 10, "createdAt": "2018-02-24T08:31:35.000000Z",
        }}
        self.fail_after = fail_after
        self.last_cursor = ""
        self.requests_made = 0
        self.profiles_received = 0
        self.tweets_received = 0
        self.pages_served = []
        self.batches_served = []

    def get_profile(self, username):
        self.requests_made += 1
        return self.profile

    def iter_user_list(self, username, kind="followings", page_size=None,
                       max_users=None, on_page=None, start_cursor=""):
        page_size = page_size or 200
        start = int(start_cursor or 0)
        page = 0
        served = 0
        while start < len(self.users):
            if self.fail_after is not None and page >= self.fail_after:
                raise ApiError("сервис недоступен", retryable=False)
            chunk = self.users[start:start + page_size]
            page += 1
            self.requests_made += 1
            self.profiles_received += len(chunk)
            self.pages_served.append(len(chunk))
            start += len(chunk)
            self.last_cursor = str(start) if start < len(self.users) else ""
            if on_page:
                on_page(chunk, page)
            for item in chunk:
                yield item, page
                served += 1
                if max_users and served >= max_users:
                    return
            if not self.last_cursor:
                return

    def batch_user_info(self, user_ids, on_batch=None):
        ids = list(user_ids)
        self.batches_served.append(len(ids))
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            self.requests_made += 1
            self.profiles_received += len(chunk)
            enriched = []
            for uid in chunk:
                enriched.append({
                    "id": uid, "userName": f"user{uid}", "name": f"Имя {uid}",
                    "description": "", "followers": 1000, "following": 10,
                    "statusesCount": 100, "isBlueVerified": True,
                    "createdAt": "Sun Jul 25 00:20:01 +0000 2021",
                    "entities": {"url": {"urls": [
                        {"expanded_url": "https://etherscan.io/x"}]}},
                })
            if on_batch:
                on_batch(enriched, start // 100 + 1)
            for item in enriched:
                yield item


@pytest.fixture
def cache(tmp_path):
    return TargetCache("target", root=str(tmp_path))


# ------------------------------------------------------- 7.1 профиль цели


def test_fetch_target(cache):
    client = FakeClient([make_user(i) for i in range(5)])
    collector = ApiCollector(client, cache)
    profile = collector.fetch_target("target")
    assert profile["username"] == "target"
    assert profile["following"] == 5
    assert cache.load_target()["username"] == "target"


def test_fetch_target_uses_cache(cache):
    client = FakeClient([])
    ApiCollector(client, cache).fetch_target("target")
    calls = client.requests_made
    ApiCollector(client, cache).fetch_target("target", max_age_days=7)
    assert client.requests_made == calls, "второй раз не должен ходить в сеть"


def test_fetch_target_force_refresh(cache):
    client = FakeClient([])
    ApiCollector(client, cache).fetch_target("target")
    calls = client.requests_made
    ApiCollector(client, cache).fetch_target("target", force=True)
    assert client.requests_made == calls + 1


def test_target_not_found(cache):
    client = FakeClient([], profile={"data": {}})
    with pytest.raises(TargetNotFound):
        ApiCollector(client, cache).fetch_target("нет_такого")


def test_target_unavailable(cache):
    client = FakeClient([], profile={"data": {
        "id": "1", "userName": "x", "unavailable": True, "message": "suspended"}})
    with pytest.raises(TargetNotFound):
        ApiCollector(client, cache).fetch_target("x")


# --------------------------------------------------------- 7.2 сбор списка


def test_collect_all(cache):
    client = FakeClient([make_user(i) for i in range(450)])
    users = ApiCollector(client, cache).collect_user_list("target", declared=450)

    assert len(users) == 450
    state = cache.load_following()
    assert state["status"] == STATUS_COMPLETE
    assert len(state["ids"]) == 450
    assert len(set(state["ids"])) == 450, "дубликатов быть не должно"


def test_collect_deduplicates(cache):
    users = [make_user(i) for i in range(10)] + [make_user(3), make_user(4)]
    client = FakeClient(users)
    collected = ApiCollector(client, cache).collect_user_list("target")
    assert len(collected) == 10


def test_max_following_respected(cache):
    """Лимит должен резать внутри страницы, а не по её границе."""
    client = FakeClient([make_user(i) for i in range(450)])
    users = ApiCollector(client, cache).collect_user_list(
        "target", max_users=50, declared=450)

    assert len(users) == 50
    assert len(cache.load_following()["ids"]) == 50
    assert cache.load_following()["status"] == STATUS_PARTIAL
    # и лишних записей мы не выкачивали
    assert sum(client.pages_served) <= 50


def test_cache_written_after_every_page(cache):
    saved = []
    original = cache.save_following

    def spy(ids, *a, **kw):
        saved.append(len(ids))
        return original(ids, *a, **kw)

    cache.save_following = spy
    client = FakeClient([make_user(i) for i in range(450)])
    ApiCollector(client, cache).collect_user_list("target")
    # 3 страницы по 200/200/50 плюс итоговое сохранение
    assert saved[:3] == [200, 400, 450]


def test_resume_continues_from_cursor(cache):
    """Возобновление не должно перекачивать уже собранное."""
    all_users = [make_user(i) for i in range(450)]

    # первый заход обрывается после второй страницы
    client = FakeClient(all_users, fail_after=2)
    with pytest.raises(CollectorError):
        ApiCollector(client, cache).collect_user_list("target", declared=450)

    state = cache.load_following()
    assert len(state["ids"]) == 400
    assert state["status"] == STATUS_PARTIAL
    assert state["cursor"], "курсор продолжения должен быть сохранён"

    # второй заход добирает остаток
    client2 = FakeClient(all_users)
    users = ApiCollector(client2, cache).collect_user_list("target", declared=450)

    assert len(users) == 450
    assert cache.load_following()["status"] == STATUS_COMPLETE
    assert client2.profiles_received == 50, "перекачивать начало нельзя"


def test_resume_skipped_when_complete(cache):
    client = FakeClient([make_user(i) for i in range(10)])
    ApiCollector(client, cache).collect_user_list("target")
    calls = client.requests_made

    client2 = FakeClient([make_user(i) for i in range(10)])
    ApiCollector(client2, cache).collect_user_list("target")
    assert client2.requests_made == 0, "готовый список заново не собираем"
    assert calls > 0


def test_force_refresh_ignores_cache(cache):
    all_users = [make_user(i) for i in range(10)]
    ApiCollector(FakeClient(all_users), cache).collect_user_list("target")

    client = FakeClient(all_users)
    ApiCollector(client, cache).collect_user_list("target", resume=False)
    assert client.profiles_received == 10


def test_incomplete_list_warns(cache, capsys):
    """Собрано меньше 95% заявленного — предупреждение (раздел 7.2)."""
    client = FakeClient([make_user(i) for i in range(10)])
    ApiCollector(client, cache).collect_user_list("target", declared=100)
    assert "ВНИМАНИЕ" in capsys.readouterr().out


def test_api_error_keeps_collected(cache):
    client = FakeClient([make_user(i) for i in range(450)], fail_after=1)
    with pytest.raises(CollectorError) as exc:
        ApiCollector(client, cache).collect_user_list("target")
    assert "200" in str(exc.value)
    assert len(cache.load_following()["ids"]) == 200


# ------------------------------------------------------------ обогащение


def test_enrich_adds_missing_fields(cache):
    client = FakeClient([make_user(i) for i in range(5)])
    collector = ApiCollector(client, cache)
    users = collector.collect_user_list("target")
    assert users[0]["verified"] is False        # в списке признака нет

    collector.enrich_users(users)
    assert cache.users["0"]["verified"] is True
    assert cache.users["0"]["website"] == "https://etherscan.io/x"


def test_enrich_runs_once(cache):
    client = FakeClient([make_user(i) for i in range(5)])
    collector = ApiCollector(client, cache)
    users = collector.collect_user_list("target")
    collector.enrich_users(users)

    refreshed = [cache.users[u["user_id"]] for u in users]
    assert collector.enrich_users(refreshed) == 0, "второй раз платить не за что"


# ----------------------------------------------------- 7.3 первичный анализ


def test_initial_analysis_selects_candidates(cache):
    users = [
        {"user_id": "1", "username": "a", "name": "", "bio": "crypto trader",
         "followers": 100},
        {"user_id": "2", "username": "b", "name": "", "bio": "повар",
         "followers": 100},
        {"user_id": "3", "username": "c", "name": "", "bio": "🚀",
         "followers": 50000},
    ]
    cache.put_users(users)
    candidates = ApiCollector(FakeClient([]), cache).initial_analysis(
        users, build_matcher())

    ids = {u["user_id"] for u in candidates}
    assert ids == {"1", "3"}
    assert users[0]["candidate_reason"] == "profile"
    assert users[2]["candidate_reason"] == "short_bio"
    assert users[1]["candidate"] is False
    assert users[0]["profile_raw"] == 30


# --------------------------------------------------- 10. остановка по ошибкам


def test_five_failures_in_a_row_stop_the_run(cache):
    collector = ApiCollector(FakeClient([]), cache)
    for i in range(4):
        collector._fail("posts", f"user{i}", RuntimeError("сбой"))
    with pytest.raises(CollectorError) as exc:
        collector._fail("posts", "user5", RuntimeError("сбой"))
    assert "подряд" in str(exc.value)


def test_success_resets_error_counter(cache):
    collector = ApiCollector(FakeClient([]), cache)
    for i in range(4):
        collector._fail("posts", f"user{i}", RuntimeError("сбой"))
    collector._ok()
    collector._fail("posts", "again", RuntimeError("сбой"))
    assert collector.consecutive_errors == 1
