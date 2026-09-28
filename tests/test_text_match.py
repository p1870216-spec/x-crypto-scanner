# -*- coding: utf-8 -*-
"""Поиск терминов (раздел 8.1 ТЗ): целые слова, фразы, русские основы,
кэштеги, контекстные слова, ложные срабатывания."""

import pytest

from scanner.text_match import (
    Matcher,
    build_matcher,
    domain_of,
    normalize,
    visible_length,
)


@pytest.fixture(scope="module")
def matcher():
    return build_matcher()


def terms_of(res, with_context=None):
    return [m.term for m in res.all_matches(with_context)]


# ------------------------------------------------------------ нормализация


def test_normalize_nfkc():
    # NFKC приводит «широкие» символы к обычным
    assert normalize("ｃｒｙｐｔｏ") == "crypto"
    assert normalize("a\xa0b") == "a b"


def test_visible_length_drops_links_and_emoji():
    assert visible_length("https://t.co/abc") == 0
    assert visible_length("🚀🔥") == 0
    assert visible_length("Trader 📈 https://x.com/a") == len("Trader")


@pytest.mark.parametrize("url,expected", [
    ("https://www.Etherscan.io/address/0x1", "etherscan.io"),
    ("http://pump.fun/coin?x=1", "pump.fun"),
    ("dexscreener.com", "dexscreener.com"),
])
def test_domain_of(url, expected):
    assert domain_of(url) == expected


# -------------------------------------------------------- контрольные ложные


@pytest.mark.parametrize("text", [
    "method",           # содержит eth
    "solution",         # содержит sol
    "stop",
    "прямой эфир",      # «эфир» — только контекстное слово
    "Hartford, CT",     # штат, а не Crypto Twitter
    "cryptography",     # целое слово crypto сюда не подходит
    "криптография",     # исключение из keywords.yaml
    "Actor. Dad. Coffee.",
])
def test_no_false_positives(matcher, text):
    res = matcher.analyze(text)
    assert res.score() == 0, f"{text!r} дал {terms_of(res)}"


def test_ct_counted_when_standalone(matcher):
    res = matcher.analyze("CT is my home")
    assert "CT" in terms_of(res)


def test_sol_only_uppercase(matcher):
    assert "SOL" in terms_of(matcher.analyze("SOL maxi"))
    assert "SOL" not in terms_of(matcher.analyze("sol maxi"))


# ------------------------------------------------------ контрольные попадания


@pytest.mark.parametrize("text,expected", [
    ("криптовалютой", "крипт"),
    ("биткоина", "биткоин"),
    ("#Bitcoin", "bitcoin"),
    ("cryptocurrencies", "cryptocurrencies"),
    ("Мой блокчейн", "блокчейн"),
    ("аирдропы каждый день", "аирдроп"),
])
def test_matches_found(matcher, text, expected):
    assert expected in terms_of(matcher.analyze(text))


def test_cashtag_known(matcher):
    res = matcher.analyze("$ETH looks good")
    assert res.cashtags_known and res.cashtags_known[0].term == "$ETH"
    assert res.has_main is True


def test_cashtag_unknown_is_weak(matcher):
    res = matcher.analyze("$TSLA earnings")
    assert not res.cashtags_known
    assert res.cashtags_unknown[0].term == "$TSLA"
    assert res.cashtags_unknown[0].weight == 3


def test_ens_name(matcher):
    res = matcher.analyze("vitalik.eth")
    assert res.ens and res.ens[0].weight == 20
    assert res.has_main is True


def test_word_boundary_dollar(matcher):
    """$SOL не должен ещё раз засчитаться как слово SOL."""
    res = matcher.analyze("$SOL")
    assert "SOL" not in [m.term for m in res.main]
    assert res.cashtags_known


# ----------------------------------------------------------- фразы и поглощение


def test_phrase_absorbs_parts(matcher):
    res = matcher.analyze("crypto trader")
    assert terms_of(res) == ["crypto trader"]
    assert res.score() == 30


def test_phrase_separators(matcher):
    for text in ("on-chain analysis", "onchain analysis", "on chain analysis"):
        assert "on chain" in terms_of(matcher.analyze(text)), text


def test_term_counted_once_per_field(matcher):
    res = matcher.analyze("crypto crypto crypto")
    assert terms_of(res) == ["crypto"]
    assert res.score() == 25


def test_plural_forms(matcher):
    assert "altcoin" in terms_of(matcher.analyze("altcoins season"))
    assert "airdrop" in terms_of(matcher.analyze("airdrops are back"))


# ------------------------------------------------------------ контекстные слова


def test_context_alone_is_ignored(matcher):
    res = matcher.analyze("long weekend, nice chart and a good setup")
    assert res.has_main is False
    assert res.score() == 0
    assert res.context, "контекстные слова должны найтись, но не считаться"


def test_context_counts_with_main_term(matcher):
    res = matcher.analyze("$BTC long setup, chart looks good")
    assert res.has_main is True
    assert res.score() > 0
    assert "trading" in res.groups


def test_maxi_needs_crypto_context(matcher):
    assert matcher.analyze("maxi dress").score() == 0
    assert matcher.analyze("bitcoin maxi").score() > 25


# ------------------------------------------------------------------ username


def test_mention_gives_signal(matcher):
    """«GP @a16zcrypto» — крипто-признак, хотя целым словом его не найти."""
    res = matcher.analyze("GP @a16zcrypto. Common sense will not help.")
    assert res.has_main is True
    assert res.mentions and "crypto" in res.mentions[0].term
    assert res.score() == 12.5           # 25 x 0.5


def test_exact_handle_keeps_full_weight(matcher):
    """
    "@okx" и раньше совпадал целым словом с полным весом — правка про
    упоминания не должна была это отнять.
    """
    assert matcher.analyze("creator @okx").score() == 15
    assert matcher.analyze("okx").score() == 15


def test_mention_weight_is_half_for_substring(matcher):
    """А вот "crypto" внутри "@a16zcrypto" находится только по подстроке."""
    assert matcher.analyze("@a16zcrypto").score() == 12.5
    assert matcher.analyze("crypto").score() == 25


def test_mention_ignores_exclusions(matcher):
    assert matcher.analyze("Follow @cryptography_daily").score() == 0


def test_mention_without_crypto_gives_nothing(matcher):
    assert matcher.analyze("@HunterBiden you are going to jail").score() == 0


def test_mention_counted_once_not_twice(matcher):
    """Признак внутри упоминания не должен сложиться сам с собой."""
    res = matcher.analyze("@crypto")
    assert res.score() == 25, "точное совпадение считается целым словом"
    assert res.mentions == [], "и не дублируется как упоминание"


def test_mentions_off_when_weight_zero():
    m = Matcher({"terms": {"crypto": 25}, "field_weights": {"mention": 0}})
    assert m.analyze("GP @a16zcrypto").score() == 0


def test_username_substring_mode(matcher):
    res = matcher.analyze("cryptoguy", substring=True)
    assert "crypto" in terms_of(res)


def test_username_short_terms_not_substring(matcher):
    """Короткие термины (btc, eth) по подстроке не ищутся — слишком шумно."""
    res = matcher.analyze("something", substring=True)
    assert res.score() == 0


# --------------------------------------------------------------- домены


def test_domains_in_text(matcher):
    res = matcher.analyze("Check dexscreener.com for charts")
    assert [d for d, _ in res.domains] == ["dexscreener.com"]
    assert res.has_main is True


def test_domains_in_urls(matcher):
    res = matcher.analyze_urls(["https://etherscan.io/address/0x1",
                                "https://example.com"])
    assert [d for d, _ in res.domains] == ["etherscan.io"]


def test_domain_not_matched_inside_other_domain(matcher):
    res = matcher.analyze_urls(["https://stargate.io"])
    assert res.domains == []


def test_urls_are_stripped_before_word_search():
    """Слова внутри ссылки не должны засчитываться как термины."""
    m = Matcher({"terms": {"trading": 15}})
    res = m.analyze("https://example.com/trading-course")
    assert res.score() == 0


# ------------------------------------------------------------ пустые входы


@pytest.mark.parametrize("value", ["", None, "   "])
def test_empty_input(matcher, value):
    res = matcher.analyze(value)
    assert res.score() == 0
    assert res.has_main is False
