# -*- coding: utf-8 -*-
"""
Crypto Score (разделы 8.2–8.5 ТЗ) и специализации (раздел 9).
Включает все контрольные примеры из 8.3.
"""

import pytest

from scanner.scoring import (
    STATUS_NOT_ANALYZED,
    STATUS_PROTECTED,
    analyze_posts,
    compute_score,
    human_count,
    is_candidate,
    score_profile,
)
from scanner.specializations import GENERAL, SpecializationDetector
from scanner.text_match import build_matcher, load_keywords


@pytest.fixture(scope="module")
def matcher():
    return build_matcher()


@pytest.fixture(scope="module")
def kw():
    return load_keywords()


def user(bio="", name="", username="user", followers=0, **extra):
    u = {"bio": bio, "name": name, "username": username,
         "followers": followers, "following": 100, "tweets": 100,
         "website": "", "bio_links": [], "professional_categories": []}
    u.update(extra)
    return u


def posts(texts, **flags):
    out = []
    for i, t in enumerate(texts):
        p = {"text": t, "is_repost": False, "is_reply": False,
             "urls": [], "cashtags": [], "tweet_id": str(i)}
        p.update(flags)
        out.append(p)
    return out


# =============================================================================
#  8.2 — сырой балл профиля
# =============================================================================


def test_control_bitcoin_vs_crypto_trader(matcher):
    """Контрольный пример из ТЗ: разный profile_raw и разный P."""
    simple = score_profile(user(bio="I love Bitcoin"), matcher)
    rich = score_profile(user(bio="Crypto trader | BTC | ETH | DeFi"), matcher)

    assert simple.raw == 25
    assert rich.raw >= 90

    s1 = compute_score(user(bio="I love Bitcoin"), simple, None, matcher,
                       status=STATUS_NOT_ANALYZED)
    s2 = compute_score(user(bio="Crypto trader | BTC | ETH | DeFi"), rich, None,
                       matcher)
    assert s1.p == 25
    assert s2.p == 40           # потолок профиля


def test_profile_fields_weighting(matcher):
    """username весит вдвое меньше, чем bio."""
    in_bio = score_profile(user(bio="crypto"), matcher).raw
    in_username = score_profile(user(username="cryptoguy"), matcher).raw
    assert in_bio == 25
    assert in_username == 12.5


def test_profile_links_and_symbols(matcher):
    p = score_profile(user(bio="charts ₿", website="https://etherscan.io/x"),
                      matcher)
    # ₿ 20 + etherscan.io 15 + контекстное "chart" 2:
    # контекст засчитан, потому что основной признак в профиле есть (₿)
    assert p.raw == 37
    assert "etherscan.io" in p.keywords

    # без основного признака то же самое "charts" не даёт ничего
    assert score_profile(user(bio="charts"), matcher).raw == 0


def test_profile_reason_format(matcher):
    p = score_profile(user(bio="crypto trader"), matcher)
    assert p.reasons == ['Bio: "crypto trader" (+30)']


def test_professional_category_bonus(matcher):
    """Признак, найденный при разведке ответов X: professional.category."""
    plain = score_profile(user(bio="Founder"), matcher).raw
    with_cat = score_profile(
        user(bio="Founder", professional_categories=["Blockchain"]), matcher).raw
    assert with_cat - plain == 15


# =============================================================================
#  Разбор постов
# =============================================================================


def test_crypto_post_needs_main_term(matcher):
    stats = analyze_posts(posts([
        "$BTC breaking out",                 # крипто
        "bitcoin is fine",                   # крипто
        "long weekend, nice chart",          # только контекст — не крипто
        "coffee and rain",                   # не крипто
    ]), matcher)
    assert stats.analyzed_count == 4
    assert stats.crypto_count == 2
    assert stats.ratio == 0.5


def test_memecoin_posts_are_crypto(matcher):
    """
    Посты про мемкоины раньше проваливались: тикер вроде $LAPTOP неизвестен,
    других признаков в тексте нет. Добавлены явные фразы и адрес контракта.
    """
    stats = analyze_posts(posts([
        "Turns out my sniper did buy $LAPTOP at around a $188M mcap",
        "everyone was watching the $LAPTOP rug pull",
        "нашёл контракт 0x" + "a" * 40,
        "shitcoin season is back",
    ]), matcher)
    assert stats.crypto_count == 4


def test_unknown_cashtag_alone_is_not_crypto(matcher):
    """$TSLA и другие неизвестные тикеры сами по себе крипто-постом не делают."""
    stats = analyze_posts(posts(["$TSLA earnings today",
                                 "Anyone's buying $LAPTOP?"]), matcher)
    assert stats.crypto_count == 0


def test_contract_address_marks_post_crypto(matcher):
    stats = analyze_posts(posts(["CA: 0x" + "b" * 40]), matcher)
    assert stats.crypto_count == 1
    assert "evm_address" in stats.keywords


def test_random_hex_is_not_an_address(matcher):
    """Короткий hex адресом не считается — иначе поймаем любой мусор."""
    stats = analyze_posts(posts(["цвет 0xff00aa и число 0x1234"]), matcher)
    assert stats.crypto_count == 0


def test_mentions_count_in_profile(matcher):
    """Признак в @упоминании должен доходить до сырого балла профиля."""
    plain = score_profile(user(bio="GP at a fund"), matcher).raw
    with_mention = score_profile(user(bio="GP @a16zcrypto"), matcher).raw
    assert plain == 0
    assert with_mention == 12.5


def test_repost_weight(matcher):
    """Репост считается с весом REPOST_WEIGHT и в числителе, и в знаменателе."""
    items = posts(["bitcoin news"]) + posts(["bitcoin news"], is_repost=True)
    stats = analyze_posts(items, matcher, repost_weight=0.5)
    assert stats.analyzed == 1.5
    assert stats.crypto == 1.5
    assert stats.ratio == 1.0


def test_replies_excluded_by_default(matcher):
    items = posts(["bitcoin"]) + posts(["bitcoin reply"], is_reply=True)
    stats = analyze_posts(items, matcher, include_replies=False)
    assert stats.analyzed_count == 1
    assert stats.replies_ratio == 1.0        # отдельный показатель

    stats2 = analyze_posts(items, matcher, include_replies=True)
    assert stats2.analyzed_count == 2


def test_crypto_domain_makes_post_crypto(matcher):
    stats = analyze_posts(
        [{"text": "look at this", "urls": ["https://dexscreener.com/solana/x"],
          "cashtags": [], "is_repost": False, "is_reply": False}], matcher)
    assert stats.crypto_count == 1


def test_entity_cashtags_used(matcher):
    """Кэштеги X отдаёт отдельным полем — на них тоже опираемся."""
    stats = analyze_posts(
        [{"text": "nice move today", "urls": [], "cashtags": ["SOL"],
          "is_repost": False, "is_reply": False}], matcher)
    assert stats.crypto_count == 1


# =============================================================================
#  8.3 — итоговый балл
# =============================================================================


def test_control_strong_ct(matcher):
    """
    Контрольный пример из ТЗ: P=40, 21/30 крипто-постов (T=40),
    торговые сигналы (S=5), 184K подписчиков (F=6) → 91, STRONG CRYPTO CT.
    """
    u = user(bio="Crypto trader | BTC | ETH | DeFi", followers=184_000)
    items = posts(["$BTC long setup"] * 21 + ["random thought about life"] * 9)
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)

    assert score.p == 40
    assert score.t == 40
    assert score.s == 5
    assert score.f == 6
    assert score.crypto_score == 91
    assert score.category == "STRONG CRYPTO CT"


def test_control_actor_with_huge_audience(matcher):
    """Актёр с 5M подписчиков и без крипто-признаков получает 0."""
    u = user(bio="Actor", followers=5_000_000)
    items = posts(["new movie out", "thanks for the support"] * 15)
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)

    assert score.f == 0
    assert score.crypto_score == 0
    assert score.category == "NOT CRYPTO"


def test_tweets_component_caps_at_60_percent(matcher):
    """Доля 60% и выше даёт максимум по T."""
    u = user(bio="crypto")
    for crypto_n, expected in ((18, 40), (30, 40), (9, 20)):
        items = posts(["bitcoin"] * crypto_n + ["hello"] * (30 - crypto_n))
        stats = analyze_posts(items, matcher)
        score = compute_score(u, score_profile(u, matcher), stats, matcher)
        assert score.t == expected, f"{crypto_n}/30"


def test_few_posts_reduce_tweet_score(matcher):
    u = user(bio="crypto")
    items = posts(["bitcoin", "bitcoin"])          # всего 2 поста, все крипто
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)
    assert score.t == pytest.approx(40 * (2 / 5))
    assert any("Few posts" in r for r in score.reasons)


def test_profile_halved_when_posts_are_not_crypto(matcher):
    """Профиль крипто, а посты — нет: P режется вдвое."""
    u = user(bio="Crypto trader | BTC | ETH | DeFi")
    items = posts(["cooking dinner"] * 20)
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)
    assert score.p == 20
    assert any("changed topic" in r for r in score.reasons)


def test_not_analyzed_uses_profile_raw(matcher):
    u = user(bio="finance investor")              # 3 + 3 = 6
    p = score_profile(u, matcher)
    score = compute_score(u, p, None, matcher, status=STATUS_NOT_ANALYZED)
    assert score.crypto_score == int(round(p.raw))
    assert score.analyzed_posts == 0
    assert score.crypto_posts_ratio is None


def test_protected_account_scores_profile_plus_followers(matcher):
    u = user(bio="Crypto trader | BTC", followers=150_000)
    p = score_profile(u, matcher)
    score = compute_score(u, p, None, matcher, status=STATUS_PROTECTED)
    assert score.crypto_score == int(round(score.p + score.f))
    assert any("protected" in r.lower() for r in score.reasons)


def test_profile_only_mode_doubles(matcher):
    u = user(bio="crypto trader", followers=120_000)
    p = score_profile(u, matcher)
    score = compute_score(u, p, None, matcher, analyze_tweets=False)
    assert score.crypto_score == min(100, int(round(2 * (score.p + score.f))))
    assert any("Profile-only" in r for r in score.reasons)


def test_score_clamped_to_100(matcher):
    u = user(bio="crypto trader bitcoin ethereum defi web3 blockchain solana",
             followers=5_000_000)
    items = posts(["$BTC long setup, on-chain wallet flows, airdrop points"] * 30)
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)
    assert score.crypto_score <= 100
    assert score.s <= 10


def test_signals_thresholds(matcher):
    """S: торговля >=3 постов (+5), on-chain >=3 (+5), аирдропы >=2 (+3)."""
    u = user(bio="crypto")
    items = posts(["$BTC long setup"] * 3 + ["bitcoin wallet whale flows"] * 3
                  + ["bitcoin airdrop points farming"] * 2)
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)
    assert score.s == 10          # 5 + 5 + 3 = 13, но потолок 10
    assert any("capped at 10" in r for r in score.reasons)


def test_price_levels_count_as_trading(matcher):
    u = user(bio="crypto")
    items = posts(["$BTC TP1 65,000", "$BTC SL 60,000", "$BTC target 70,000"])
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)
    assert score.s >= 5


def test_price_levels_only_count_in_crypto_posts(matcher):
    """Уровни цен в постах про акции сигнала не дают (8.3: «в крипто-постах»)."""
    u = user(bio="crypto")
    items = posts(["$TSLA TP1 420,000", "$AAPL SL 180,000", "$NVDA 900,000"])
    stats = analyze_posts(items, matcher)
    assert stats.pattern_posts.get("price_level") == 3
    assert stats.crypto_pattern_posts.get("price_level", 0) == 0
    score = compute_score(u, score_profile(u, matcher), stats, matcher)
    assert score.s == 0


def test_followers_tiers(matcher):
    u_base = dict(bio="crypto trader")
    expected = [(1_500_000, 10), (600_000, 8), (150_000, 6),
                (60_000, 4), (20_000, 2), (5_000, 0)]
    for followers, bonus in expected:
        u = user(followers=followers, **u_base)
        score = compute_score(u, score_profile(u, matcher), None, matcher,
                              status=STATUS_PROTECTED)
        assert score.f == bonus, followers


def test_followers_need_crypto_base(matcher):
    """Без крипто-базы подписчики не дают ничего."""
    u = user(bio="Chef", followers=2_000_000)
    score = compute_score(u, score_profile(u, matcher), None, matcher,
                          status=STATUS_PROTECTED)
    assert score.f == 0


# =============================================================================
#  8.4 — категории
# =============================================================================


@pytest.mark.parametrize("score,expected", [
    (0, "NOT CRYPTO"), (19, "NOT CRYPTO"),
    (20, "POSSIBLE CRYPTO"), (39, "POSSIBLE CRYPTO"),
    (40, "CRYPTO RELATED"), (59, "CRYPTO RELATED"),
    (60, "CRYPTO INFLUENCER"), (79, "CRYPTO INFLUENCER"),
    (80, "STRONG CRYPTO CT"), (100, "STRONG CRYPTO CT"),
])
def test_categories(kw, score, expected):
    from scanner.scoring import category_for
    assert category_for(score, kw)[0] == expected


# =============================================================================
#  8.5 — объяснимость
# =============================================================================


def test_reasons_are_explainable(matcher):
    u = user(bio="crypto trader | BTC", followers=184_000)
    items = posts(["$BTC long setup"] * 21 + ["hello"] * 9)
    stats = analyze_posts(items, matcher)
    score = compute_score(u, score_profile(u, matcher), stats, matcher)

    text = score.reason
    assert 'Bio: "crypto trader" (+30)' in text
    assert "Profile capped at 40" in text
    assert "21/30 recent posts are crypto-related (+40)" in text
    assert "184K followers (+6)" in text
    assert "crypto trader" in score.matched_keywords


def test_matched_keywords_deduplicated(matcher):
    u = user(bio="bitcoin", name="bitcoin")
    score = compute_score(u, score_profile(u, matcher), None, matcher,
                          status=STATUS_NOT_ANALYZED)
    assert score.matched_keywords == "bitcoin"


@pytest.mark.parametrize("n,expected", [
    (0, "0"), (999, "999"), (5_000, "5K"), (184_000, "184K"),
    (1_200_000, "1.2M"), (5_000_000, "5M"),
])
def test_human_count(n, expected):
    assert human_count(n) == expected


# =============================================================================
#  7.3 — отбор кандидатов
# =============================================================================


def test_candidate_by_profile_score():
    assert is_candidate(user(bio="crypto"), 25)[0] is True
    assert is_candidate(user(bio="chef"), 3)[0] is False


def test_candidate_by_short_bio_and_audience():
    ok, why = is_candidate(user(bio="🚀", followers=50_000), 0)
    assert ok is True and why == "short_bio"

    ok, _ = is_candidate(user(bio="🚀", followers=100), 0)
    assert ok is False

    long_bio = "x" * 40
    ok, _ = is_candidate(user(bio=long_bio, followers=50_000), 0)
    assert ok is False


def test_candidate_short_bio_ignores_links_and_emoji():
    """Длина bio считается без ссылок и эмодзи."""
    u = user(bio="🚀🔥 https://t.co/abcdefghij", followers=10_000)
    assert is_candidate(u, 0)[0] is True


# =============================================================================
#  9 — специализации
# =============================================================================


@pytest.fixture(scope="module")
def detector():
    return SpecializationDetector()


def test_specializations_need_threshold(detector):
    assert detector.detect(user(bio="crypto trader"), crypto_score=10) == []


def test_trader_by_profile(detector):
    assert "TRADER" in detector.detect(user(bio="crypto trader"), crypto_score=50)


def test_trader_by_price_levels(detector, matcher):
    items = posts(["$BTC TP1 65,000", "$BTC SL 60,000", "$BTC entry 61,500"])
    stats = analyze_posts(items, matcher)
    found = detector.detect(user(bio="crypto"), posts=items, post_stats=stats,
                            crypto_score=50)
    assert "TRADER" in found


def test_multiple_specializations(detector):
    found = detector.detect(user(bio="DeFi researcher and NFT builder"),
                            crypto_score=60)
    assert {"DEFI", "ANALYST", "WEB3"} <= set(found)


def test_memecoin_by_contract_address(detector, matcher):
    items = posts([
        "CA: 0x" + "a" * 40,
        "new one 0x" + "b" * 40,
    ])
    stats = analyze_posts(items, matcher)
    found = detector.detect(user(bio="crypto"), posts=items, post_stats=stats,
                            crypto_score=50)
    assert "MEMECOIN" in found


def test_general_crypto_fallback(detector):
    found = detector.detect(user(bio="crypto"), crypto_score=50)
    assert found == [GENERAL]


def test_general_crypto_when_many_topics(detector, matcher):
    items = posts(["bitcoin is up", "ethereum staking yield",
                   "solana altcoin season", "nft mint today"])
    stats = analyze_posts(items, matcher)
    found = detector.detect(user(bio="crypto trader"), posts=items,
                            post_stats=stats, crypto_score=70)
    assert "TRADER" in found and GENERAL in found


def test_investor_vc_profile_only(detector):
    found = detector.detect(user(bio="Partner at ventures, angel investor"),
                            crypto_score=50)
    assert "INVESTOR / VC" in found
