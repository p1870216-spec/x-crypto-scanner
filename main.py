# -*- coding: utf-8 -*-
"""
X Crypto Following Scanner — точка входа.

Запуск:
    python main.py                     цели из targets.txt или TARGET_USERNAME
    python main.py --target NAME       одна конкретная цель
    python main.py --check-connection  только проверка подключения к Chrome
    python main.py --rescore           пересчёт из кеша, без браузера
    python main.py --max-following 50 --no-tweets

Программа работает строго в режиме чтения: не кликает, не нажимает клавиши,
не использует X API и не спрашивает пароли.
"""

import argparse
import asyncio
import logging
import os
import sys

# Windows-консоль: без этого падает вывод кириллицы и символов вроде ₿.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="x_scanner",
        description="Анализ подписок аккаунта X на принадлежность к Crypto Twitter",
    )
    p.add_argument("--target", metavar="NAME",
                   help="username цели без @ (переопределяет targets.txt)")
    p.add_argument("--force-refresh", action="store_true",
                   help="игнорировать кеш и собрать всё заново")
    p.add_argument("--no-tweets", action="store_true",
                   help="не анализировать посты, оценка только по профилю")
    p.add_argument("--max-following", type=int, metavar="N",
                   help="собрать не более N подписок (для проверок: 50)")
    p.add_argument("--rescore", action="store_true",
                   help="пересчитать оценки и выгрузки из кеша, без браузера")
    p.add_argument("--check-connection", action="store_true",
                   help="проверить подключение к Chrome и выйти (режим browser)")
    p.add_argument("--probe-api", action="store_true",
                   help="проверить ключ twitterapi.io, сохранить образцы ответов "
                        "в cache/_debug/ и показать структуру полей")
    p.add_argument("--source", choices=["api", "browser"],
                   help="источник данных (по умолчанию из config.py)")
    p.add_argument("--speed", choices=["safe", "normal"],
                   help="профиль скорости (по умолчанию из config.py)")
    p.add_argument("--trial", type=int, metavar="N",
                   help="анализировать посты не более чем у N кандидатов "
                        "(пробный прогон)")
    p.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждение расходов")
    return p.parse_args(argv)


ROOT = os.path.dirname(os.path.abspath(__file__))


def setup_logging(target: str):
    """
    Два журнала на цель: run.log с подробным ходом работы и errors.log
    с ошибками отдельных аккаунтов (раздел 12 ТЗ).
    """
    from scanner.storage import safe_name

    out_dir = os.path.join(ROOT, config.OUTPUT_DIR, safe_name(target))
    os.makedirs(out_dir, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    run = logging.getLogger(f"run.{target}")
    errors = logging.getLogger(f"errors.{target}")
    for logger, filename, level in ((run, "run.log", logging.INFO),
                                    (errors, "errors.log", logging.WARNING)):
        logger.setLevel(level)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        handler = logging.FileHandler(os.path.join(out_dir, filename),
                                      encoding="utf-8")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return run, errors, out_dir


def confirm_cost(estimate: float, what: str, args) -> bool:
    """Спросить подтверждение, если смета выше порога из config.py."""
    limit = float(getattr(config, "CONFIRM_COST_ABOVE", 1.0) or 0)
    shown = f"${estimate:.2f}" if estimate >= 0.01 else f"${estimate:.4f}"
    print(f"  Оценка стоимости ({what}): {shown}")
    if args.yes or estimate <= limit:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print(f"  Смета выше ${limit:.2f}. Запустите с --yes, чтобы продолжить.")
        return False
    try:
        answer = input(f"  Это дороже ${limit:.2f}. Продолжить? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes", "д", "да")


def apply_args(args):
    """Аргументы командной строки перекрывают значения из config.py."""
    if args.force_refresh:
        config.FORCE_REFRESH = True
    if args.no_tweets:
        config.ANALYZE_TWEETS = False
    if args.max_following is not None:
        config.MAX_FOLLOWING = args.max_following
    if args.speed:
        config.SPEED = args.speed
    if args.source:
        config.DATA_SOURCE = args.source


def read_targets(args) -> list:
    """
    Порядок выбора целей:
      1) --target NAME;
      2) targets.txt, если файл есть и не пуст;
      3) config.TARGET_USERNAME.
    """
    if args.target:
        return [args.target.lstrip("@").strip()]

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        config.TARGETS_FILE)
    if os.path.exists(path):
        names = []
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.split("#", 1)[0].strip().lstrip("@")
                if line:
                    names.append(line)
        if names:
            return names

    return [config.TARGET_USERNAME.lstrip("@").strip()]


async def cmd_check_connection() -> int:
    from scanner.browser import check_connection
    print("[1/1] Connecting to Chrome...")
    ok = await check_connection()
    print("Connection check: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def _describe(value, indent=4, max_items=3):
    """Короткое описание структуры ответа: имена полей и типы."""
    pad = " " * indent
    if isinstance(value, dict):
        for k, v in list(value.items())[:60]:
            if isinstance(v, (dict, list)):
                kind = f"{type(v).__name__}({len(v)})"
                print(f"{pad}{k}: {kind}")
                if indent < 10:
                    _describe(v, indent + 4, max_items)
            else:
                text = repr(v)
                if len(text) > 60:
                    text = text[:60] + "…"
                print(f"{pad}{k}: {text}")
    elif isinstance(value, list):
        for item in value[:1]:
            _describe(item, indent + 2, max_items)


def cmd_probe_api(target: str) -> int:
    """
    Проверка ключа и разведка реальных ответов twitterapi.io.
    Парсер строим по этим образцам, а не по документации.
    """
    from scanner.api_client import (ApiError, ApiKeyMissing, TwitterApiClient,
                                    estimate_cost, mask_key)

    debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             config.CACHE_DIR, "_debug")
    try:
        client = TwitterApiClient(debug_dir=debug_dir, samples_per_endpoint=2)
    except ApiKeyMissing as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Ключ найден: {mask_key(client.api_key)}")
    print(f"Сервис: {client.base}")
    print(f"Цель: @{target}\n")

    try:
        print("[1/3] Профиль цели...")
        profile = client.get_profile(target)
        print("  поля ответа:")
        _describe(profile)

        print("\n[2/3] Первая страница подписок (pageSize=20 для пробы)...")
        page = client.get(client.ENDPOINT_FOLLOWINGS,
                          {"userName": target, "cursor": "", "pageSize": 20})
        items = page.get("followings") or []
        print(f"  получено записей: {len(items)}")
        print(f"  has_next_page: {page.get('has_next_page')}, "
              f"next_cursor: {'есть' if page.get('next_cursor') else 'нет'}")
        if items:
            print("  поля одной записи:")
            _describe(items[0])

        first = ""
        if items:
            first = (items[0].get("userName") or items[0].get("screen_name")
                     or items[0].get("username") or "")
        print(f"\n[3/3] Посты пользователя @{first or target}...")
        tweets = list(client.iter_tweets(username=first or target, max_tweets=5))
        print(f"  получено постов: {len(tweets)}")
        if tweets:
            print("  поля одного поста:")
            _describe(tweets[0])
    except ApiError as exc:
        print(f"\nERROR: {exc}")
        print(client.spent_report())
        return 1

    print("\n" + client.spent_report())
    print(f"Образцы сохранены в {debug_dir}")

    following = 0
    for key in ("following", "followingCount", "friends_count"):
        value = (profile.get("data") or profile).get(key)
        if isinstance(value, int):
            following = value
            break
    if following:
        print(f"\nСмета на полный сбор подписок @{target} ({following:,}): "
              f"${estimate_cost(profiles=following):.4f}")
    return 0


def write_exports(target, cache, target_profile, counters, declared=0,
                  run_log=None) -> list:
    """Собрать xlsx, csv и текстовый отчёт из кеша (раздел 12 ТЗ)."""
    from scanner.export import build_rows, build_stats, export_all
    from scanner.storage import safe_name

    out_dir = os.path.join(ROOT, config.OUTPUT_DIR, safe_name(target))
    rows = build_rows(list(cache.users.values()), cache.analysis)

    warnings = []
    collected = len(rows)
    if declared and collected < declared * 0.95:
        warnings.append(
            f"собрано {collected:,} из заявленных {declared:,} "
            f"({collected / declared:.0%}) — список может быть неполным")
    if counters.get("pending"):
        warnings.append(
            f"у {counters['pending']:,} кандидатов посты ещё не анализировались, "
            f"их балл посчитан только по профилю")
    if not config.ANALYZE_TWEETS:
        warnings.append("оценка только по профилю (анализ постов выключен)")

    stats = build_stats(target_profile, rows, counters, {
        "crypto_threshold": config.CRYPTO_THRESHOLD,
        "data_source": config.DATA_SOURCE,
        "scoring_mode": ("профиль + посты" if config.ANALYZE_TWEETS
                         else "только профиль"),
    })

    paths, failures = export_all(out_dir, safe_name(target), rows, stats,
                                 warnings, config.CRYPTO_THRESHOLD, 60)

    print("\nФайлы:")
    for path in paths:
        print(f"  {os.path.relpath(path, ROOT)}")
    for filename, reason in failures:
        print(f"  НЕ ЗАПИСАН {filename}: {reason}")
        if run_log:
            run_log.warning("не записан %s: %s", filename, reason)
    if run_log:
        run_log.info("выгрузки: %s", ", ".join(os.path.basename(p) for p in paths))
    return paths


def cmd_rescore(target: str, args) -> int:
    """
    Пересчёт оценок из кеша, без единого запроса и без браузера.
    Нужен, чтобы подбирать словари в keywords.yaml: правите файл — считаете
    заново — смотрите, что изменилось.
    """
    from scanner.posts import analyze_all
    from scanner.storage import TargetCache
    from scanner.text_match import build_matcher

    cache = TargetCache(target, root=ROOT, cache_dir=config.CACHE_DIR)
    if not cache.exists():
        print(f"ERROR: кеша для @{target} нет. Сначала запустите сбор: "
              f"python main.py --target {target}")
        return 1

    users = list(cache.users.values())
    if not users:
        print(f"ERROR: в кеше @{target} нет профилей.")
        return 1

    before = {uid: rec.get("crypto_score", 0)
              for uid, rec in cache.analysis.items()}

    print(f"=== Пересчёт из кеша: @{target} ===")
    print(f"  аккаунтов: {len(users)}, с постами: {len(cache.posts)}")
    records = analyze_all(users, cache, build_matcher(),
                          analyze_tweets=config.ANALYZE_TWEETS)
    records.sort(key=lambda r: -r["crypto_score"])

    changed = [r for r in records
               if before.get(r["user_id"]) is not None
               and before[r["user_id"]] != r["crypto_score"]]
    crypto = [r for r in records if r["crypto_score"] >= config.CRYPTO_THRESHOLD]

    print(f"  крипто (>= {config.CRYPTO_THRESHOLD}): {len(crypto)}")
    if before:
        print(f"  изменилось оценок: {len(changed)}")
        for record in sorted(changed, key=lambda r: -abs(
                r["crypto_score"] - before[r["user_id"]]))[:10]:
            was = before[record["user_id"]]
            print(f"    @{record['username']:<20} {was} -> "
                  f"{record['crypto_score']}")
    counters = {
        "candidates": sum(1 for u in users if u.get("candidate")),
        "analyzed": sum(1 for r in records if r["analyzed_posts"]),
        "pending": sum(1 for r in records
                       if r["status"] == "not_analyzed" and r["profile_raw"] >= 20),
        "unavailable": sum(1 for r in records
                           if r["status"] in ("protected", "suspended",
                                              "no_posts", "not_found")),
        "errors": sum(1 for r in records if r["status"] == "error"),
    }
    target_profile = cache.load_target() or {}
    write_exports(target, cache, target_profile, counters,
                  declared=int(target_profile.get("following") or 0))
    print("  Запросов к сервису: 0, потрачено $0.00")
    return 0


def run_target_api(target: str, args) -> int:
    """Проход по одной цели через twitterapi.io."""
    from scanner.api_client import (ApiError, ApiKeyMissing, TwitterApiClient,
                                    estimate_cost, mask_key)
    from scanner.collector import ApiCollector, CollectorError, TargetNotFound
    from scanner.posts import PAGE_SIZE, DeepAnalyzer, analyze_all
    from scanner.storage import (STAGE_EXPORT, STAGE_INITIAL, STAGE_POSTS,
                                 STAGE_TARGET, TargetCache, STATUS_COMPLETE)
    from scanner.text_match import build_matcher

    run_log, errors_log, out_dir = setup_logging(target)
    run_log.info("=== Запуск для @%s, источник api ===", target)

    cache = TargetCache(target, root=ROOT, cache_dir=config.CACHE_DIR)
    if config.FORCE_REFRESH:
        print("  --force-refresh: кеш очищен")
        cache.clear()

    debug_dir = (os.path.join(ROOT, config.CACHE_DIR, "_debug")
                 if config.DEBUG_SAVE_RAW else None)

    print("[1/5] Connecting to twitterapi.io...", end=" ")
    try:
        client = TwitterApiClient(debug_dir=debug_dir)
    except ApiKeyMissing as exc:
        print("FAILED")
        print(f"ERROR: {exc}")
        return 1
    print(f"OK (ключ {mask_key(client.api_key)})")

    collector = ApiCollector(client, cache, logger=run_log,
                             errors_logger=errors_log)

    try:
        profile = collector.fetch_target(
            target, max_age_days=config.CACHE_MAX_AGE_DAYS,
            force=config.FORCE_REFRESH)
        cache.save_state(STAGE_TARGET)

        declared = int(profile.get("following") or 0)
        to_collect = min(declared, config.MAX_FOLLOWING) if config.MAX_FOLLOWING \
            else declared
        state = cache.load_following()
        if state.get("status") != STATUS_COMPLETE:
            cost = estimate_cost(profiles=to_collect * 2)   # список + обогащение
            if not confirm_cost(cost, f"список подписок @{target}", args):
                print("Отменено пользователем.")
                return 1

        users = collector.collect_user_list(
            target, kind="followings", max_users=config.MAX_FOLLOWING,
            declared=declared, resume=not config.FORCE_REFRESH)

        if users:
            collector.enrich_users(users)
            users = [cache.users[u["user_id"]] for u in users
                     if u.get("user_id") in cache.users]

        matcher = build_matcher()
        candidates = collector.initial_analysis(users, matcher)
        cache.save_state(STAGE_INITIAL, users=len(users),
                         candidates=len(candidates))
        cache.flush()

        trial = args.trial if args.trial is not None else config.TRIAL_CANDIDATES
        analyzer_stats = {}
        print("[5/5] Deep analysis...")

        if not config.ANALYZE_TWEETS:
            print("  Посты не анализируются (--no-tweets): "
                  "оценка только по профилю.")
        elif not candidates:
            print("  Кандидатов нет.")
        else:
            # Кандидатов, у которых посты уже лежат в кеше, считать не нужно.
            fresh = sum(1 for u in candidates
                        if cache.posts_of(u["user_id"],
                                          config.CACHE_MAX_AGE_DAYS) is not None)
            planned = min(len(candidates), trial) if trial else len(candidates)
            to_fetch = max(0, planned - fresh)
            if trial and len(candidates) > trial:
                print(f"  Пробный прогон: {planned} из {len(candidates)} "
                      f"кандидатов (снять ограничение: --trial 0)")
            # Оплата идёт за страницы по 20 постов, поэтому считаем округлённо
            # вверх — иначе смета выйдет заниженной.
            per_user = -(-config.TWEETS_PER_USER // PAGE_SIZE) * PAGE_SIZE
            cost = estimate_cost(tweets=to_fetch * per_user)
            if to_fetch and not confirm_cost(cost, f"посты {to_fetch} аккаунтов",
                                             args):
                print("Отменено пользователем.")
                cache.flush()
                return 1

            analyzer = DeepAnalyzer(client, cache, matcher, logger=run_log,
                                    errors_logger=errors_log)
            analyzer.analyze(candidates, limit=planned,
                             max_age_days=config.CACHE_MAX_AGE_DAYS)
            analyzer_stats = analyzer.stats
            cache.save_state(STAGE_POSTS, analyzed=analyzer.stats["analyzed"])

        # Итоговые оценки по всем аккаунтам: не кандидаты получают балл,
        # равный сырому баллу профиля, и статус not_analyzed.
        records = analyze_all(users, cache, matcher,
                              analyze_tweets=config.ANALYZE_TWEETS)
        cache.save_state(STAGE_EXPORT, scored=len(records))
        cache.flush()

        records.sort(key=lambda r: -r["crypto_score"])
        crypto = [r for r in records if r["crypto_score"] >= config.CRYPTO_THRESHOLD]
        pending = [r for r in records
                   if r["status"] == "not_analyzed" and r["profile_raw"] >= 20]

        print(f"\nОценено аккаунтов: {len(records)}, "
              f"крипто (>= {config.CRYPTO_THRESHOLD}): {len(crypto)}")
        if pending:
            print(f"ВНИМАНИЕ: у {len(pending)} кандидатов посты ещё не "
                  f"анализировались — их балл посчитан только по профилю "
                  f"и может измениться. Снять ограничение: --trial 0")
        print("TOP-5:")
        for record in records[:5]:
            specs = ", ".join(record["specializations"]) or "—"
            mark = " (по профилю)" if record["status"] == "not_analyzed" else ""
            print(f"  {record['crypto_score']:>3}  @{record['username']:<20} "
                  f"{record['category']:<18} {specs}{mark}")

        counters = dict(analyzer_stats)
        counters["candidates"] = len(candidates)
        counters["pending"] = len(pending)
        write_exports(target, cache, profile, counters, declared, run_log)

    except TargetNotFound as exc:
        print(str(exc))
        errors_log.error(str(exc))
        return 1
    except CollectorError as exc:
        print(f"\nОСТАНОВКА: {exc}")
        errors_log.error("остановка: %s", exc)
        cache.flush()
        print(client.spent_report())
        return 2
    except ApiError as exc:
        print(f"\nОШИБКА twitterapi.io: {exc}")
        errors_log.error("api: %s", exc)
        cache.flush()
        print(client.spent_report())
        return 2
    except RuntimeError as exc:
        # Пять ошибок подряд на этапе постов — общая поломка.
        print(f"\nОСТАНОВКА: {exc}")
        errors_log.error("остановка: %s", exc)
        cache.flush()
        print(client.spent_report())
        return 2
    except KeyboardInterrupt:
        cache.flush()
        print("\nПрервано пользователем. Собранное сохранено в кеше, "
              "следующий запуск продолжит с этого места.")
        print(client.spent_report())
        return 130

    print()
    print(client.spent_report())
    run_log.info(client.spent_report())
    print(f"Журналы: {os.path.join(config.OUTPUT_DIR, os.path.basename(out_dir))}")
    return 0


async def run_target(target: str, args) -> int:
    """Полный проход по одной цели. Источник выбирается в config.DATA_SOURCE."""
    print(f"=== Target: @{target} (источник: {config.DATA_SOURCE}) ===")
    if config.DATA_SOURCE == "api":
        return run_target_api(target, args)

    print("Режим browser: сбор списка пока не подключён.")
    print("X отдаёт только около 50 подписок из чужого списка — "
          "основной режим api. Проверить браузер: --check-connection")
    return 1


async def main_async(argv=None) -> int:
    args = parse_args(argv)
    apply_args(args)

    if args.check_connection:
        return await cmd_check_connection()

    if args.probe_api:
        return cmd_probe_api(read_targets(args)[0])

    if args.rescore:
        rc = 0
        for target in read_targets(args):
            rc = max(rc, cmd_rescore(target, args))
        return rc

    targets = read_targets(args)
    print(f"Targets: {', '.join('@' + t for t in targets)}")
    rc = 0
    for target in targets:
        rc = max(rc, await run_target(target, args))
    return rc


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nПрервано пользователем. Прогресс сохранён в кеше.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
