# -*- coding: utf-8 -*-
"""Кеш, атомарная запись и возобновление (раздел 11 ТЗ)."""

import json
import os

import pytest

from scanner.storage import (
    SCHEMA_VERSION,
    STAGE_FOLLOWING,
    STAGE_NEW,
    STATUS_COMPLETE,
    STATUS_PARTIAL,
    TargetCache,
    age_days,
    atomic_write_json,
    is_fresh,
    now_iso,
    read_json,
    safe_name,
)


@pytest.fixture
def cache(tmp_path):
    return TargetCache("Rus_Khairullin", root=str(tmp_path))


# --------------------------------------------------------------- утилиты


@pytest.mark.parametrize("name,expected", [
    ("Rus_Khairullin", "Rus_Khairullin"),
    ("@user", "user"),
    ("../evil/path", ".._evil_path"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_safe_name(name, expected):
    assert safe_name(name) == expected


def test_safe_name_strips_unsafe_characters():
    """Имя цели не должно уводить запись за пределы папки кеша."""
    for name in ("имя с пробелом", "a/b\\c", "..", "con:name"):
        result = safe_name(name)
        assert "/" not in result and "\\" not in result
        assert result not in ("", ".", "..")


def test_atomic_write_replaces_file(tmp_path):
    path = str(tmp_path / "d" / "f.json")
    atomic_write_json(path, {"a": 1})
    atomic_write_json(path, {"a": 2, "кириллица": "да"})
    assert read_json(path) == {"a": 2, "кириллица": "да"}
    # временных файлов после записи не остаётся
    assert [n for n in os.listdir(os.path.dirname(path))
            if n.startswith(".tmp_")] == []


def test_read_json_survives_broken_file(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{это не json", encoding="utf-8")
    assert read_json(str(path), default="цел") == "цел"
    assert read_json(str(tmp_path / "нет.json"), default=None) is None


def test_freshness():
    assert is_fresh(now_iso(), 7) is True
    assert is_fresh("2000-01-01T00:00:00", 7) is False
    assert is_fresh("", 7) is False
    assert is_fresh("мусор", 7) is False
    assert age_days("") == float("inf")
    # без ограничения возраста свежим считается всё, у чего есть метка
    assert is_fresh("2000-01-01T00:00:00", None) is True
    assert is_fresh("", None) is False


# ------------------------------------------------------------ состояние


def test_new_cache_state(cache):
    assert cache.exists() is False
    state = cache.load_state()
    assert state["stage"] == STAGE_NEW
    assert state["schema"] == SCHEMA_VERSION


def test_save_and_load_state(cache):
    cache.save_state(STAGE_FOLLOWING, collected=42)
    assert cache.exists() is True
    state = cache.load_state()
    assert state["stage"] == STAGE_FOLLOWING
    assert state["collected"] == 42
    assert state["target"] == "Rus_Khairullin"
    assert state["updated_at"]


def test_schema_change_resets_state(cache):
    cache.save_state(STAGE_FOLLOWING)
    data = read_json(cache.path("state.json"))
    data["schema"] = SCHEMA_VERSION + 99
    atomic_write_json(cache.path("state.json"), data)

    state = cache.load_state()
    assert state["stage"] == STAGE_NEW, "старый кеш нельзя использовать"
    assert state["schema_reset"] is True


# ---------------------------------------------------------------- данные


def test_target_roundtrip(cache):
    cache.save_target({"username": "Rus_Khairullin", "following": 1487})
    loaded = cache.load_target()
    assert loaded["following"] == 1487
    assert loaded["collected_at"]


def test_following_roundtrip(cache):
    cache.save_following(["1", "2", "3"], STATUS_PARTIAL, cursor="КУРСОР",
                         pages=3, declared=1487)
    state = cache.load_following()
    assert state["ids"] == ["1", "2", "3"]
    assert state["status"] == STATUS_PARTIAL
    assert state["cursor"] == "КУРСОР"
    assert state["pages"] == 3
    assert state["declared"] == 1487


def test_following_defaults_on_empty(cache):
    state = cache.load_following()
    assert state["ids"] == [] and state["status"] == STATUS_PARTIAL
    assert state["cursor"] == ""


def test_users_upsert_and_persist(cache, tmp_path):
    cache.put_users([{"user_id": "1", "username": "a"},
                     {"user_id": "2", "username": "b"},
                     {"username": "без_id"}])
    assert len(cache.users) == 2

    cache.put_users([{"user_id": "1", "username": "a", "bio": "обновлено"}])
    assert cache.users["1"]["bio"] == "обновлено"
    assert cache.users["1"]["collected_at"]

    # данные переживают перезапуск
    again = TargetCache("Rus_Khairullin", root=str(tmp_path))
    assert set(again.users) == {"1", "2"}


def test_posts_store_raw_texts(cache):
    """В кеше лежат сырые тексты, чтобы --rescore пересчитал без запросов."""
    cache.put_posts("1", [{"tweet_id": "10", "text": "про биткоин"}])
    record = cache.posts_of("1")
    assert record["posts"][0]["text"] == "про биткоин"
    assert record["status"] == "ok"
    assert cache.posts_of("нет такого") is None


def test_posts_freshness(cache):
    cache.put_posts("1", [{"tweet_id": "10", "text": "x"}])
    assert cache.posts_of("1", max_age_days=7) is not None
    data = read_json(cache.path("posts.json"))
    data["1"]["collected_at"] = "2000-01-01T00:00:00"
    atomic_write_json(cache.path("posts.json"), data)
    cache._posts = None
    assert cache.posts_of("1", max_age_days=7) is None


def test_fresh_user_ids(cache):
    cache.put_users([{"user_id": "1"}, {"user_id": "2"}])
    data = read_json(cache.path("users.json"))
    data["2"]["collected_at"] = "2000-01-01T00:00:00"
    atomic_write_json(cache.path("users.json"), data)
    cache._users = None
    assert cache.fresh_user_ids(7) == {"1"}


def test_analysis_roundtrip(cache):
    cache.put_analysis("1", {"crypto_score": 91}, save=True)
    assert read_json(cache.path("analysis.json"))["1"]["crypto_score"] == 91


def test_clear_removes_everything(cache):
    cache.save_state(STAGE_FOLLOWING)
    cache.save_target({"username": "x"})
    cache.put_users([{"user_id": "1"}])
    cache.put_posts("1", [])
    cache.clear()

    assert cache.exists() is False
    assert cache.load_target() is None
    assert cache.users == {}
    assert cache.load_state()["stage"] == STAGE_NEW


def test_flush_writes_memory_state(cache, tmp_path):
    cache.put_users([{"user_id": "1"}], save=False)
    cache.put_posts("1", [{"tweet_id": "5", "text": "t"}], save=False)
    cache.put_analysis("1", {"crypto_score": 10})
    cache.flush()

    again = TargetCache("Rus_Khairullin", root=str(tmp_path))
    assert "1" in again.users
    assert again.posts_of("1") is not None
    assert again.analysis["1"]["crypto_score"] == 10


def test_two_targets_are_independent(tmp_path):
    a = TargetCache("first", root=str(tmp_path))
    b = TargetCache("second", root=str(tmp_path))
    a.put_users([{"user_id": "1"}])
    b.put_users([{"user_id": "2"}])
    assert set(a.users) == {"1"}
    assert set(b.users) == {"2"}
    assert a.dir != b.dir


def test_complete_status_marks_done(cache):
    cache.save_following(["1"], STATUS_COMPLETE, cursor="", pages=8)
    assert cache.load_following()["status"] == STATUS_COMPLETE
