# -*- coding: utf-8 -*-
"""
Crypto Score и объяснение оценки (разделы 8.2–8.5 ТЗ).

    crypto_score = P + T + S + F      (0–100)

    P — профиль (bio, имя, username, сайт и ссылки)      до 40
    T — доля крипто-постов среди последних постов        до 40
    S — особые сигналы в постах (торговля, on-chain…)    до 10
    F — подписчики, только при наличии крипто-базы       до 10

Каждый вклад в балл записывается причиной с числом, например:
    Bio: "crypto trader" (+30); Profile capped at 40;
    21/30 recent posts are crypto-related (+40); 184K followers (+6)
"""

import re

from .text_match import build_matcher, visible_length

# ---------------------------------------------------------------- статусы

STATUS_OK = "ok"
STATUS_NOT_ANALYZED = "not_analyzed"
STATUS_PROTECTED = "protected"
STATUS_SUSPENDED = "suspended"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"

# Статусы, при которых посты недоступны не по нашей вине.
POSTS_UNAVAILABLE = (STATUS_PROTECTED, STATUS_SUSPENDED, STATUS_NOT_FOUND,
                     STATUS_ERROR)


def _fmt(value: float) -> str:
    """Числа в причинах: без лишних нулей."""
    return f"{value:g}"


def human_count(n: int) -> str:
    """126225 → '126K', 1250000 → '1.2M' — для причин и отчёта."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n // 1000}K"
    return str(n)


# =============================================================================
#  Сырой балл профиля (8.2)
# =============================================================================


class ProfileScore:
    def __init__(self):
        self.raw = 0.0
        self.reasons = []
        self.keywords = []
        self.groups = set()
        self.has_main = False

    def __repr__(self):
        return f"ProfileScore(raw={self.raw:g}, keywords={self.keywords})"


def score_profile(user: dict, matcher=None, keywords=None) -> ProfileScore:
    """
    «Сырой» балл профиля: bio ×1, name ×1, username ×0.5, сайт и ссылки в bio,
    плюс особые признаки. Веса полей — в keywords.yaml.
    """
    matcher = matcher or build_matcher(keywords)
    kw = matcher.kw
    fw = kw.get("field_weights", {}) or {}
    out = ProfileScore()

    bio = user.get("bio") or ""
    name = user.get("name") or ""
    username = user.get("username") or ""

    fields = [
        ("Bio", bio, float(fw.get("bio", 1.0)), False),
        ("Name", name, float(fw.get("name", 1.0)), False),
        ("Username", username, float(fw.get("username", 0.5)), True),
    ]
    results = [(label, mult, matcher.analyze(text, substring=sub))
               for label, text, mult, sub in fields]

    links = [user.get("website") or ""] + list(user.get("bio_links") or [])
    link_res = matcher.analyze_urls(links)
    link_mult = float(fw.get("website", 1.0))

    # Профессиональная категория аккаунта (X отдаёт её отдельным полем;
    # в исходном ТЗ этого признака не было, найден при разведке ответов).
    cats = (kw.get("special", {}) or {}).get("professional_categories", {}) or {}
    category_hits = [(c, float(cats.get(c, 0) or 0))
                     for c in user.get("professional_categories") or []
                     if float(cats.get(c, 0) or 0) > 0]

    # Основной признак ищем по всему профилю: если крипто-термин стоит в имени,
    # контекстные слова из bio уже осмысленны.
    out.has_main = (any(r.has_main for _, _, r in results)
                    or bool(link_res.domains) or bool(category_hits))

    cash_known, cash_unknown = [], []
    for label, mult, res in results:
        for m in res.all_matches(with_context=out.has_main):
            value = m.weight * mult
            if value <= 0:
                continue
            out.raw += value
            out.keywords.append(m.term)
            out.reasons.append(f'{label}: "{m.term}" (+{_fmt(value)})')
        out.groups |= res.groups
        # кэштеги считаются один раз на весь профиль — ниже
        cash_known += res.cashtags_known
        cash_unknown += res.cashtags_unknown

    for domain, m in link_res.domains:
        value = m.weight * link_mult
        out.raw += value
        out.keywords.append(domain)
        out.reasons.append(f'Link: "{domain}" (+{_fmt(value)})')

    # Кэштег: +15 за первое совпадение известного тикера во всём профиле.
    # Неизвестный кэштег ($TSLA — возможно, акция) даёт слабый плюс.
    first_cashtag = (cash_known or cash_unknown)
    if first_cashtag:
        m = first_cashtag[0]
        out.raw += m.weight
        out.keywords.append(m.term)
        out.reasons.append(f'Cashtag: "{m.term}" (+{_fmt(m.weight)})')

    for cat, weight in category_hits:
        out.raw += weight
        out.keywords.append(cat)
        out.reasons.append(f'Category: "{cat}" (+{_fmt(weight)})')

    out.raw = round(out.raw, 2)
    return out


# =============================================================================
#  Разбор постов
# =============================================================================


class PostsScore:
    def __init__(self):
        self.analyzed = 0.0          # взвешенное число постов
        self.crypto = 0.0            # взвешенное число крипто-постов
        self.analyzed_count = 0      # штучно, для отчёта
        self.crypto_count = 0
        self.ratio = 0.0
        self.group_posts = {}        # группа → в скольких крипто-постах встретилась
        self.pattern_posts = {}      # шаблон (уровни, адреса) → во всех постах
        self.crypto_pattern_posts = {}   # он же, но только по крипто-постам
        self.keywords = []
        self.replies_ratio = None

    def __repr__(self):
        return (f"PostsScore({self.crypto_count}/{self.analyzed_count}, "
                f"ratio={self.ratio:.2f})")


def _post_weight(post, repost_weight) -> float:
    return float(repost_weight) if post.get("is_repost") else 1.0


def analyze_posts(posts, matcher=None, keywords=None, repost_weight=0.5,
                  include_replies=False, patterns=None) -> PostsScore:
    """
    Разбор постов кандидата.

    Крипто-пост — тот, где есть основной крипто-термин, крипто-кэштег или
    крипто-домен. Одни контекстные слова пост крипто-постом не делают.
    Ответы в основную долю не входят: для них считается отдельный показатель.
    """
    matcher = matcher or build_matcher(keywords)
    kw = matcher.kw
    patterns = patterns if patterns is not None else (kw.get("patterns", {}) or {})
    compiled = {}
    for name, pat in patterns.items():
        try:
            compiled[name] = re.compile(pat)
        except re.error:
            continue

    # Шаблоны, которые сами по себе делают пост крипто-постом (адрес контракта).
    crypto_patterns = [name for name in (kw.get("crypto_patterns") or [])
                       if name in compiled]

    out = PostsScore()
    replies_total = replies_crypto = 0.0
    seen_terms = []

    for post in posts or []:
        text = post.get("text") or ""
        # Ссылки поста (уже раскрытые из entities) дают крипто-домены.
        res = matcher.analyze(text)
        link_res = matcher.analyze_urls(post.get("urls") or [])
        # Кэштеги из entities: X отдаёт их отдельным полем, это надёжнее текста.
        entity_cashtags = [t.upper() for t in post.get("cashtags") or []]
        known_entity = [t for t in entity_cashtags if t in matcher.known_tickers]

        pattern_hit = next((name for name in crypto_patterns
                            if compiled[name].search(text)), "")
        is_crypto = bool(res.has_main or link_res.domains or known_entity
                         or pattern_hit)
        weight = _post_weight(post, repost_weight)

        if post.get("is_reply"):
            replies_total += weight
            if is_crypto:
                replies_crypto += weight
            if not include_replies:
                continue

        out.analyzed += weight
        out.analyzed_count += 1
        if is_crypto:
            out.crypto += weight
            out.crypto_count += 1
            seen_terms += res.terms(with_context=True)
            seen_terms += [d for d, _ in link_res.domains]
            seen_terms += ["$" + t for t in known_entity]
            if pattern_hit:
                seen_terms.append(pattern_hit)
            # группы контекстных слов считаем только по крипто-постам
            for group in res.groups:
                out.group_posts[group] = out.group_posts.get(group, 0) + 1

        # Шаблоны (уровни цен, адреса контрактов). Для компонента S нужны
        # только крипто-посты (8.3), для специализаций — все (9).
        for name, rx in compiled.items():
            if rx.search(text):
                out.pattern_posts[name] = out.pattern_posts.get(name, 0) + 1
                if is_crypto:
                    out.crypto_pattern_posts[name] = \
                        out.crypto_pattern_posts.get(name, 0) + 1

        # Аирдропы считаются по всем постам, а не только по крипто-постам.
        if "airdrop" in res.groups and not is_crypto:
            out.group_posts["airdrop"] = out.group_posts.get("airdrop", 0) + 1

    out.ratio = (out.crypto / out.analyzed) if out.analyzed else 0.0
    if replies_total:
        out.replies_ratio = round(replies_crypto / replies_total, 4)

    # частые термины вперёд, без повторов
    order = {}
    for t in seen_terms:
        order[t] = order.get(t, 0) + 1
    out.keywords = [t for t, _ in sorted(order.items(), key=lambda x: -x[1])]
    return out


# =============================================================================
#  Итоговый балл (8.3)
# =============================================================================


class Score:
    """Итог по одному аккаунту."""

    def __init__(self):
        self.crypto_score = 0
        self.profile_score = 0.0     # P
        self.tweet_score = 0.0       # T + S
        self.p = self.t = self.s = self.f = 0.0
        self.profile_raw = 0.0
        self.category = ""
        self.category_emoji = ""
        self.reasons = []
        self.keywords = []
        self.crypto_posts = 0
        self.analyzed_posts = 0
        self.crypto_posts_ratio = None
        self.replies_crypto_ratio = None
        self.status = STATUS_OK
        self.groups = set()
        self.pattern_posts = {}

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    @property
    def matched_keywords(self) -> str:
        seen, out = set(), []
        for k in self.keywords:
            low = k.lower()
            if low not in seen:
                seen.add(low)
                out.append(k)
        return ", ".join(out)

    def __repr__(self):
        return f"Score({self.crypto_score}, {self.category})"


def category_for(score, keywords) -> tuple:
    """Категория и эмодзи по баллу (8.4)."""
    cats = (keywords or {}).get("categories") or []
    for c in sorted(cats, key=lambda x: -int(x.get("min", 0))):
        if score >= int(c.get("min", 0)):
            return c.get("name", ""), c.get("emoji", "")
    return "NOT CRYPTO", "⚪"


def compute_score(user, profile=None, posts=None, matcher=None, keywords=None,
                  analyze_tweets=True, status=STATUS_OK) -> Score:
    """
    Собрать итоговый балл из компонентов.

    user     — словарь профиля (из capture.parse_user);
    profile  — готовый ProfileScore (иначе посчитается здесь);
    posts    — готовый PostsScore или None, если посты не анализировались;
    status   — ok | not_analyzed | protected | suspended | not_found | error.
    """
    matcher = matcher or build_matcher(keywords)
    kw = matcher.kw
    cfg = kw.get("scoring", {}) or {}

    profile = profile if profile is not None else score_profile(user, matcher)
    out = Score()
    out.status = status
    out.profile_raw = profile.raw
    out.reasons = list(profile.reasons)
    out.keywords = list(profile.keywords)
    out.groups = set(profile.groups)

    profile_cap = float(cfg.get("profile_cap", 40))
    p = min(profile.raw, profile_cap)
    if profile.raw > profile_cap:
        out.reasons.append(f"Profile capped at {_fmt(profile_cap)}")

    # --- аккаунт не попал в глубокий анализ ---
    if status == STATUS_NOT_ANALYZED:
        out.p = round(p, 2)
        out.profile_score = out.p
        out.crypto_score = max(0, min(100, int(round(profile.raw))))
        out.category, out.category_emoji = category_for(out.crypto_score, kw)
        return out

    posts_available = posts is not None and posts.analyzed_count > 0

    # --- T: доля крипто-постов ---
    t = 0.0
    if posts_available:
        tweets_cap = float(cfg.get("tweets_cap", 40))
        full = float(cfg.get("tweets_full_ratio", 0.6)) or 0.6
        min_posts = int(cfg.get("tweets_min_posts", 5))
        t = tweets_cap * min(posts.ratio / full, 1.0)
        if posts.analyzed_count < min_posts:
            t *= posts.analyzed_count / min_posts
            out.reasons.append(
                f"Few posts analyzed ({posts.analyzed_count}), tweet score reduced")
        out.crypto_posts = posts.crypto_count
        out.analyzed_posts = posts.analyzed_count
        out.crypto_posts_ratio = round(posts.ratio, 4)
        out.replies_crypto_ratio = posts.replies_ratio
        out.keywords += posts.keywords
        out.groups |= set(posts.group_posts)
        out.pattern_posts = dict(posts.pattern_posts)
        if t > 0:
            out.reasons.append(
                f"{posts.crypto_count}/{posts.analyzed_count} recent posts "
                f"are crypto-related (+{_fmt(round(t, 1))})")

        # --- P: штраф, если профиль крипто, а посты — нет ---
        pen_posts = int(cfg.get("profile_penalty_min_posts", 10))
        pen_ratio = float(cfg.get("profile_penalty_max_ratio", 0.1))
        if posts.analyzed_count >= pen_posts and posts.ratio < pen_ratio and p > 0:
            p *= float(cfg.get("profile_penalty_factor", 0.5))
            out.reasons.append(
                "Crypto profile but posts are not (changed topic?), profile halved")

    # --- S: особые сигналы в постах ---
    s = 0.0
    if posts_available:
        signals = cfg.get("signals", {}) or {}
        # торговля: контекстные торговые слова ИЛИ уровни цен, в крипто-постах
        trading_posts = max(posts.group_posts.get("trading", 0),
                            posts.crypto_pattern_posts.get("price_level", 0))
        checks = [
            ("trading", trading_posts, "Trading content"),
            ("onchain", posts.group_posts.get("onchain", 0), "On-chain content"),
            ("airdrop", posts.group_posts.get("airdrop", 0), "Airdrop / farming"),
        ]
        for key, count, label in checks:
            rule = signals.get(key, {}) or {}
            need = int(rule.get("min_posts", 0) or 0)
            bonus = float(rule.get("bonus", 0) or 0)
            if need and count >= need and bonus:
                s += bonus
                out.reasons.append(f"{label} in {count} posts (+{_fmt(bonus)})")
        signals_cap = float(cfg.get("signals_cap", 10))
        if s > signals_cap:
            s = signals_cap
            out.reasons.append(f"Post signals capped at {_fmt(signals_cap)}")

    # --- F: подписчики, только при наличии крипто-базы ---
    f = 0.0
    followers = int(user.get("followers") or 0)
    need_raw = float(cfg.get("followers_requires_profile_raw", 20))
    need_ratio = float(cfg.get("followers_requires_ratio", 0.2))
    has_base = profile.raw >= need_raw or (posts_available and posts.ratio >= need_ratio)
    if has_base:
        for tier in cfg.get("followers_tiers", []) or []:
            try:
                threshold, bonus = int(tier[0]), float(tier[1])
            except (TypeError, ValueError, IndexError):
                continue
            if followers >= threshold:
                f = min(bonus, float(cfg.get("followers_cap", 10)))
                out.reasons.append(
                    f"{human_count(followers)} followers (+{_fmt(f)})")
                break

    # --- особые случаи ---
    if not analyze_tweets:
        total = min(100.0, 2.0 * (p + f))
        out.reasons.append("Profile-only scoring (post analysis disabled)")
        t = s = 0.0
    elif status in POSTS_UNAVAILABLE or not posts_available:
        total = p + f
        label = {
            STATUS_PROTECTED: "Account is protected, posts unavailable",
            STATUS_SUSPENDED: "Account is suspended, posts unavailable",
            STATUS_NOT_FOUND: "Account not found, posts unavailable",
        }.get(status, "Posts unavailable")
        out.reasons.append(label)
        t = s = 0.0
    else:
        total = p + t + s + f

    out.p, out.t, out.s, out.f = round(p, 2), round(t, 2), round(s, 2), round(f, 2)
    out.profile_score = out.p
    out.tweet_score = round(out.t + out.s, 2)
    out.crypto_score = max(0, min(100, int(round(total))))
    out.category, out.category_emoji = category_for(out.crypto_score, kw)
    return out


# =============================================================================
#  Отбор кандидатов на глубокий анализ (7.3)
# =============================================================================


def is_candidate(user, profile_raw, deep_threshold=20, low_bio_check=True,
                 low_bio_max_length=30, low_bio_min_followers=5000):
    """
    Кандидат, если сырой балл профиля дотягивает до порога, либо bio почти
    пустое, но аудитория большая (за такими стоит посмотреть посты).

    Возвращает (bool, причина).
    """
    if profile_raw >= deep_threshold:
        return True, "profile"
    if low_bio_check:
        length = visible_length(user.get("bio") or "")
        followers = int(user.get("followers") or 0)
        if length < low_bio_max_length and followers >= low_bio_min_followers:
            return True, "short_bio"
    return False, ""
