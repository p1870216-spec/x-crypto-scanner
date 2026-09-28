# -*- coding: utf-8 -*-
"""
Специализации аккаунта (раздел 9 ТЗ).

Назначаются только при crypto_score >= порога; у аккаунта их может быть
несколько. Правила и пороги живут в keywords.yaml, доли считаются от
проанализированных постов.

    TRADER, ANALYST, CRYPTO NEWS, DEFI, ONCHAIN, MEMECOIN, WEB3,
    INVESTOR / VC, AIRDROP, GENERAL CRYPTO
"""

import re

from .text_match import Matcher, build_matcher, normalize

GENERAL = "GENERAL CRYPTO"


def _term_regex(term: str):
    """Тот же принцип, что и в поиске терминов: целые слова, гибкие разделители."""
    term = str(term)
    if not re.search(r"\w", term):          # эмодзи и знаки ищем как есть
        return None
    core = r"[\s\-_]*".join(re.escape(p) for p in term.split())
    return re.compile(r"(?<![\w$])" + core + r"(?:es|s)?(?!\w)", re.IGNORECASE)


class _Rule:
    __slots__ = ("name", "profile_terms", "post_terms", "post_groups",
                 "patterns", "min_ratio", "min_pattern_posts")

    def __init__(self, name, cfg):
        self.name = name
        cfg = cfg or {}
        self.profile_terms = [(t, _term_regex(t)) for t in cfg.get("profile_terms", [])]
        self.post_terms = [(t, _term_regex(t)) for t in cfg.get("post_terms", [])]
        self.post_groups = list(cfg.get("post_groups", []) or [])
        self.patterns = list(cfg.get("patterns", []) or [])
        self.min_ratio = float(cfg.get("min_ratio", 1.01))
        self.min_pattern_posts = int(cfg.get("min_pattern_posts", 0) or 0)


def _contains(text, term, regex) -> bool:
    if regex is None:
        return term in text
    return bool(regex.search(text))


class SpecializationDetector:
    def __init__(self, keywords=None, matcher: Matcher = None):
        self.matcher = matcher or build_matcher(keywords)
        self.kw = self.matcher.kw
        self.rules = [_Rule(name, cfg)
                      for name, cfg in (self.kw.get("specializations", {}) or {}).items()]
        gc = self.kw.get("general_crypto", {}) or {}
        self.general_min_groups = int(gc.get("min_groups", 3) or 3)
        self.general_groups = {
            name: [(t, _term_regex(t)) for t in terms]
            for name, terms in (gc.get("groups", {}) or {}).items()
        }
        self.patterns = {}
        for name, pat in (self.kw.get("patterns", {}) or {}).items():
            try:
                self.patterns[name] = re.compile(pat)
            except re.error:
                pass

    # ------------------------------------------------------------- разбор

    def detect(self, user, posts=None, post_stats=None, crypto_score=100,
               threshold=20) -> list:
        """
        user        — словарь профиля;
        posts       — список постов (сырые тексты) или None;
        post_stats  — PostsScore из scoring.analyze_posts (для групп);
        crypto_score, threshold — специализации ставятся только от порога.
        """
        if crypto_score < threshold:
            return []

        profile_text = normalize(
            " ".join([user.get("bio") or "", user.get("name") or "",
                      user.get("username") or ""]))

        texts = [normalize(p.get("text") or "") for p in posts or []]
        total = len(texts)
        group_posts = dict(post_stats.group_posts) if post_stats else {}
        pattern_posts = dict(post_stats.pattern_posts) if post_stats else {}
        if not pattern_posts and texts:
            for name, rx in self.patterns.items():
                pattern_posts[name] = sum(1 for t in texts if rx.search(t))

        found = []
        for rule in self.rules:
            if self._matches(rule, profile_text, texts, total, group_posts,
                             pattern_posts):
                found.append(rule.name)

        if not found or self._touches_many_groups(texts):
            if GENERAL not in found:
                found.append(GENERAL)
        return found

    def _matches(self, rule, profile_text, texts, total, group_posts,
                 pattern_posts) -> bool:
        # признак в bio / name / username
        for term, rx in rule.profile_terms:
            if _contains(profile_text, term, rx):
                return True

        if not total:
            return False

        # доля постов с терминами специализации
        if rule.post_terms and rule.min_ratio <= 1.0:
            hits = 0
            for text in texts:
                if any(_contains(text, t, rx) for t, rx in rule.post_terms):
                    hits += 1
            if hits / total >= rule.min_ratio:
                return True

        # доля постов с контекстными словами нужной группы
        if rule.post_groups and rule.min_ratio <= 1.0:
            hits = max((group_posts.get(g, 0) for g in rule.post_groups),
                       default=0)
            if hits / total >= rule.min_ratio:
                return True

        # шаблоны: уровни цен, адреса контрактов
        if rule.patterns and rule.min_pattern_posts:
            for name in rule.patterns:
                if pattern_posts.get(name, 0) >= rule.min_pattern_posts:
                    return True
        return False

    def _touches_many_groups(self, texts) -> bool:
        """Посты затрагивают >= N разных тематических групп (BTC, ETH, DeFi…)."""
        if not texts:
            return False
        hit = set()
        for name, terms in self.general_groups.items():
            for text in texts:
                if any(_contains(text, t, rx) for t, rx in terms):
                    hit.add(name)
                    break
        return len(hit) >= self.general_min_groups


def detect_specializations(user, posts=None, post_stats=None, crypto_score=100,
                           threshold=20, keywords=None, detector=None) -> list:
    """Удобная обёртка: одна специализация или несколько, списком."""
    detector = detector or SpecializationDetector(keywords)
    return detector.detect(user, posts=posts, post_stats=post_stats,
                           crypto_score=crypto_score, threshold=threshold)
