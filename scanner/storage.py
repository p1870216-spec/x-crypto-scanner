# -*- coding: utf-8 -*-
"""
Кеш, атомарная запись и возобновление работы (раздел 11 ТЗ).

    cache/<target>/
      target.json      профиль цели
      following.json   упорядоченный список user_id + статус + курсор
      users.json       user_id -> профиль (+ дата сбора)
      posts.json       user_id -> сырые посты (+ дата сбора)
      analysis.json    user_id -> результат скоринга
      state.json       текущий этап, время, версия схемы

Запись атомарная: сначала во временный файл, потом os.replace — файл не
повредится, если программу прервать посреди сохранения.

Профили пользователей лежат отдельно от связи «цель → подписки»: это нужно,
чтобы позже добавить Followers и пересечение аудиторий без переделки (раздел 15).
"""

import datetime
import json
import os
import re
import tempfile

SCHEMA_VERSION = 1

STAGE_NEW = "new"
STAGE_TARGET = "target"
STAGE_FOLLOWING = "following"
STAGE_INITIAL = "initial_analysis"
STAGE_POSTS = "posts"
STAGE_EXPORT = "export"
STAGE_DONE = "done"

STATUS_PARTIAL = "partial"
STATUS_COMPLETE = "complete"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def safe_name(name: str) -> str:
    """
    Имя цели → безопасное имя папки.

    Отдельно отбрасываем "." и "..": точка входит в разрешённые символы,
    и без этой проверки кеш ушёл бы в родительскую папку.
    """
    cleaned = _SAFE_NAME.sub("_", (name or "").strip().lstrip("@"))
    if not cleaned or cleaned.strip(".") == "":
        return "unknown"
    return cleaned


def now_iso() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def age_days(stamp: str) -> float:
    """Сколько дней прошло с момента stamp. Пустая метка = бесконечно давно."""
    if not stamp:
        return float("inf")
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    return (datetime.datetime.now() - moment).total_seconds() / 86400.0


def is_fresh(stamp: str, max_age_days) -> bool:
    if max_age_days is None:
        return bool(stamp)
    return age_days(stamp) <= float(max_age_days)


def atomic_write_json(path: str, data) -> None:
    """Записать JSON так, чтобы падение не оставило битый файл."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str, default=None):
    """Прочитать JSON. Битый или отсутствующий файл — это не ошибка."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class TargetCache:
    """Кеш одной цели."""

    def __init__(self, target: str, root: str = None, cache_dir: str = "cache"):
        self.target = (target or "").lstrip("@")
        project = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.dir = os.path.join(project, cache_dir, safe_name(self.target))
        self._users = None
        self._posts = None
        self._analysis = None

    # ------------------------------------------------------------- пути

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def exists(self) -> bool:
        return os.path.exists(self.path("state.json"))

    def clear(self):
        """Полный сброс кеша цели (--force-refresh)."""
        for name in ("target.json", "following.json", "users.json",
                     "posts.json", "analysis.json", "state.json"):
            try:
                os.unlink(self.path(name))
            except OSError:
                pass
        self._users = self._posts = self._analysis = None

    # ------------------------------------------------------------ состояние

    def load_state(self) -> dict:
        state = read_json(self.path("state.json"), None)
        if not isinstance(state, dict):
            return {"stage": STAGE_NEW, "schema": SCHEMA_VERSION,
                    "updated_at": "", "target": self.target}
        if state.get("schema") != SCHEMA_VERSION:
            # Схема кеша изменилась — старые данные не используем.
            return {"stage": STAGE_NEW, "schema": SCHEMA_VERSION,
                    "updated_at": "", "target": self.target,
                    "schema_reset": True}
        return state

    def save_state(self, stage: str = None, **extra):
        state = self.load_state()
        if stage:
            state["stage"] = stage
        state["schema"] = SCHEMA_VERSION
        state["target"] = self.target
        state["updated_at"] = now_iso()
        state.pop("schema_reset", None)
        state.update(extra)
        atomic_write_json(self.path("state.json"), state)
        return state

    # -------------------------------------------------------------- цель

    def load_target(self) -> dict:
        return read_json(self.path("target.json"), None)

    def save_target(self, profile: dict):
        data = dict(profile or {})
        data["collected_at"] = now_iso()
        atomic_write_json(self.path("target.json"), data)

    # -------------------------------------------------- список подписок

    def load_following(self) -> dict:
        data = read_json(self.path("following.json"), None)
        if not isinstance(data, dict):
            return {"kind": "followings", "ids": [], "status": STATUS_PARTIAL,
                    "cursor": "", "pages": 0, "updated_at": ""}
        data.setdefault("ids", [])
        data.setdefault("status", STATUS_PARTIAL)
        data.setdefault("cursor", "")
        data.setdefault("pages", 0)
        return data

    def save_following(self, ids, status=STATUS_PARTIAL, cursor="", pages=0,
                       kind="followings", declared=None):
        atomic_write_json(self.path("following.json"), {
            "kind": kind,
            "ids": list(ids),
            "status": status,
            "cursor": cursor or "",
            "pages": int(pages or 0),
            "declared": declared,
            "updated_at": now_iso(),
        })

    # ---------------------------------------------------------- профили

    @property
    def users(self) -> dict:
        if self._users is None:
            data = read_json(self.path("users.json"), None)
            self._users = data if isinstance(data, dict) else {}
        return self._users

    def put_users(self, users, save=True):
        """Добавить или обновить профили. Ключ — user_id."""
        stamp = now_iso()
        for user in users or []:
            uid = str(user.get("user_id") or "")
            if not uid:
                continue
            record = dict(user)
            record["collected_at"] = stamp
            self.users[uid] = record
        if save:
            self.save_users()

    def save_users(self):
        atomic_write_json(self.path("users.json"), self.users)

    def fresh_user_ids(self, max_age_days) -> set:
        return {uid for uid, user in self.users.items()
                if is_fresh(user.get("collected_at", ""), max_age_days)}

    # ------------------------------------------------------------- посты

    @property
    def posts(self) -> dict:
        if self._posts is None:
            data = read_json(self.path("posts.json"), None)
            self._posts = data if isinstance(data, dict) else {}
        return self._posts

    def put_posts(self, user_id, posts, status="ok", save=True):
        """
        В кеш кладём СЫРЫЕ тексты постов, а не оценки: тогда --rescore
        пересчитает всё после правки словарей, не тратя запросов.
        """
        self.posts[str(user_id)] = {
            "posts": list(posts or []),
            "status": status,
            "collected_at": now_iso(),
        }
        if save:
            self.save_posts()

    def save_posts(self):
        atomic_write_json(self.path("posts.json"), self.posts)

    def posts_of(self, user_id, max_age_days=None):
        record = self.posts.get(str(user_id))
        if not record:
            return None
        if max_age_days is not None and not is_fresh(record.get("collected_at", ""),
                                                     max_age_days):
            return None
        return record

    # ---------------------------------------------------------- скоринг

    @property
    def analysis(self) -> dict:
        if self._analysis is None:
            data = read_json(self.path("analysis.json"), None)
            self._analysis = data if isinstance(data, dict) else {}
        return self._analysis

    def put_analysis(self, user_id, result, save=False):
        self.analysis[str(user_id)] = dict(result or {})
        if save:
            self.save_analysis()

    def save_analysis(self):
        atomic_write_json(self.path("analysis.json"), self.analysis)

    def flush(self):
        """Сохранить всё, что держится в памяти."""
        if self._users is not None:
            self.save_users()
        if self._posts is not None:
            self.save_posts()
        if self._analysis is not None:
            self.save_analysis()
