# -*- coding: utf-8 -*-
"""
Нормализация текста и поиск терминов по словарям keywords.yaml (раздел 8.1 ТЗ).

Правила:
  * только целые слова: (?<![\\w$])термин(?!\\w) — в Python \\w покрывает кириллицу;
  * у английских терминов допускается окончание s / es;
  * фразы совпадают с любым разделителем: "on chain" находит "onchain",
    "on-chain", "on chain";
  * длинные термины поглощают короткие: "crypto trader" даёт +30, а не 30+25+15;
  * каждый термин засчитывается один раз в пределах поля;
  * CT / SOL / OP / ARB — только заглавными; "Hartford, CT" не считается;
  * русские термины ищутся по основе с любым окончанием;
  * контекстные слова (long, chart, wallet, эфир…) засчитываются, только если
    в тексте есть основной крипто-термин, крипто-кэштег или крипто-домен;
  * в username поиск по подстроке и только для терминов от 5 символов.

Регистронезависимость сделана через re.IGNORECASE, а не casefold(): так
позиции совпадений остаются в координатах исходной строки, что нужно для
поглощения фраз. На кириллице и латинице результат тот же.
"""

import os
import re
import unicodedata

import yaml

# --------------------------------------------------------------- регулярки

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
CASHTAG_RE = re.compile(r"(?<![\w])\$([A-Za-z][A-Za-z0-9_]{0,9})(?![\w])")
MENTION_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_]{1,15})(?![\w])")
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]+"
)

# Что считать «словом» при поиске целыми словами.
_BOUNDARY_LEFT = r"(?<![\w$])"
_BOUNDARY_RIGHT = r"(?!\w)"

_ASCII_RE = re.compile(r"^[a-z0-9 .\-_/]+$")


def normalize(text: str) -> str:
    """Unicode NFKC и схлопывание пробелов. Регистр не трогаем."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    return text.replace("​", "").replace("\xa0", " ")


def strip_urls(text: str):
    """Убрать URL из текста. Возвращает (текст_без_url, список_url)."""
    urls = URL_RE.findall(text)
    return URL_RE.sub(" ", text), urls


def visible_length(text: str) -> int:
    """
    Длина bio без ссылок и эмодзи — для отбора «коротких bio»
    (LOW_BIO_MAX_LENGTH в config.py).
    """
    text, _ = strip_urls(normalize(text))
    text = EMOJI_RE.sub("", text)
    return len(text.strip())


def domain_of(url: str) -> str:
    """Домен из адреса, без www и без протокола."""
    u = normalize(url).strip().lower()
    u = re.sub(r"^[a-z]+://", "", u)
    u = u.split("/")[0].split("?")[0].split("#")[0]
    if u.startswith("www."):
        u = u[4:]
    return u


# ----------------------------------------------------------------- находки


class Match:
    """Одно совпадение: что нашли, сколько это стоит и какого рода признак."""

    __slots__ = ("term", "weight", "kind", "group", "main")

    def __init__(self, term, weight, kind="term", group="", main=True):
        self.term = term
        self.weight = float(weight)
        self.kind = kind        # term | context | cashtag | domain | special | mention
        self.group = group      # для контекстных слов — имя группы
        self.main = main        # делает ли признак текст крипто-текстом

    def __repr__(self):
        return f"Match({self.term!r}, {self.weight:g}, {self.kind})"

    def __eq__(self, other):
        return (isinstance(other, Match) and self.term == other.term
                and self.weight == other.weight and self.kind == other.kind)


class MatchResult:
    """Итог разбора одного текста."""

    def __init__(self):
        self.main = []          # основные термины (делают текст крипто-текстом)
        self.weak = []          # термины, которые баллы дают, но крипто-текстом не делают
        self.context = []       # контекстные слова
        self.cashtags_known = []
        self.cashtags_unknown = []
        self.domains = []       # [(домен, вес)]
        self.ens = []
        self.symbols = []
        self.mentions = []      # признаки внутри @упоминаний
        self.urls = []

    # --- признаки ---

    @property
    def has_main(self) -> bool:
        """
        Есть ли в тексте основной крипто-признак: термин, известный кэштег,
        крипто-домен, ENS-имя или крипто-упоминание. Одни контекстные слова
        этого не дают.
        """
        return bool(self.main or self.cashtags_known or self.domains
                    or self.ens or any(m.main for m in self.mentions))

    @property
    def groups(self) -> set:
        return {m.group for m in self.context if m.group}

    def score(self, with_context: bool = None) -> float:
        """
        Сумма весов. Контекстные слова учитываются только при наличии
        основного признака (или если вызывающий код явно это разрешил —
        например, когда основной признак нашёлся в соседнем поле профиля).
        """
        if with_context is None:
            with_context = self.has_main
        total = sum(m.weight for m in self.main) + sum(m.weight for m in self.weak)
        total += sum(m.weight for _, m in self.domains)
        total += sum(m.weight for m in self.ens) + sum(m.weight for m in self.symbols)
        total += sum(m.weight for m in self.mentions)
        if with_context:
            total += sum(m.weight for m in self.context)
        return total

    def all_matches(self, with_context: bool = None) -> list:
        if with_context is None:
            with_context = self.has_main
        out = list(self.main) + list(self.weak)
        out += [m for _, m in self.domains] + list(self.ens) + list(self.symbols)
        out += list(self.mentions)
        if with_context:
            out += list(self.context)
        return out

    def terms(self, with_context: bool = None) -> list:
        return [m.term for m in self.all_matches(with_context)]


# ------------------------------------------------------------- маска текста


class _Mask:
    """Занятые участки текста — чтобы длинные фразы поглощали короткие слова."""

    def __init__(self):
        self.spans = []

    def free(self, start, end) -> bool:
        return all(end <= s or start >= e for s, e in self.spans)

    def take(self, start, end):
        self.spans.append((start, end))


# ----------------------------------------------------------------- словарь


class _Term:
    __slots__ = ("term", "weight", "main", "kind", "group", "regex",
                 "substring_regex", "not_after")

    def __init__(self, term, weight, main, kind, group, regex,
                 substring_regex=None, not_after=""):
        self.term = term
        self.weight = weight
        self.main = main
        self.kind = kind
        self.group = group
        self.regex = regex
        self.substring_regex = substring_regex
        self.not_after = not_after


def _as_dict(value) -> dict:
    """Значение словаря может быть числом или словарём с параметрами."""
    if isinstance(value, dict):
        return dict(value)
    return {"weight": value}


def _phrase(term: str) -> str:
    """Слова фразы разделяются пробелом, дефисом, подчёркиванием или ничем."""
    return r"[\s\-_]*".join(re.escape(p) for p in term.split())


class Matcher:
    """Поиск терминов по словарям keywords.yaml."""

    def __init__(self, keywords: dict):
        self.kw = keywords or {}
        self.terms = []
        self.exclusions = [re.compile(p, re.IGNORECASE)
                           for p in self.kw.get("exclusions", []) or []]

        cash = self.kw.get("cashtags", {}) or {}
        self.known_tickers = {t.upper() for t in cash.get("tickers", [])}
        self.cashtag_known_weight = float(cash.get("known_weight", 15))
        self.cashtag_unknown_weight = float(cash.get("unknown_weight", 3))

        self.domains = {d.lower(): float(w)
                        for d, w in (self.kw.get("domains", {}) or {}).items()}
        self._domain_re = self._build_domain_regex()

        special = self.kw.get("special", {}) or {}
        self.ens_weight = float(special.get("ens_weight", 20))
        self.ens_suffixes = [s.lower() for s in special.get("ens_suffixes", [])]
        self._ens_re = re.compile(
            r"(?<![\w])[\w-]{1,40}\.(" + "|".join(self.ens_suffixes) + r")(?![\w])",
            re.IGNORECASE) if self.ens_suffixes else None
        self.symbols = {s: float(w)
                        for s, w in (special.get("symbols", {}) or {}).items()
                        if float(w) > 0}

        self.username_min_length = int(
            self.kw.get("username_substring_min_length", 5))
        # Во сколько раз слабее признак внутри @упоминания, чем в самом тексте.
        self.mention_weight = float(
            (self.kw.get("field_weights", {}) or {}).get("mention", 0.5))

        self._load_terms()
        # Длинные термины проверяются первыми — так они поглощают короткие.
        self.terms.sort(key=lambda t: (-len(t.term), t.term))

    # ------------------------------------------------------ сборка словаря

    def _build_domain_regex(self):
        if not self.domains:
            return None
        parts = sorted((re.escape(d) for d in self.domains), key=len, reverse=True)
        return re.compile(r"(?<![\w.])(" + "|".join(parts) + r")(?![\w])",
                          re.IGNORECASE)

    def _add(self, term, cfg, kind="term", group="", case_sensitive=False,
             stem=False):
        weight = float(cfg.get("weight", 0))
        if weight <= 0 and kind != "context":
            return
        main = bool(cfg.get("main", True))
        flags = 0 if case_sensitive else re.IGNORECASE

        if stem:
            pattern = _BOUNDARY_LEFT + _phrase(term) + r"\w*"
        else:
            core = _phrase(term)
            plural = r"(?:es|s)?" if _ASCII_RE.match(term.lower()) else ""
            pattern = _BOUNDARY_LEFT + core + plural + _BOUNDARY_RIGHT

        substring = None
        if len(term) >= self.username_min_length:
            substring = re.compile(_phrase(term), flags)

        self.terms.append(_Term(
            term=term, weight=weight, main=main, kind=kind, group=group,
            regex=re.compile(pattern, flags), substring_regex=substring,
            not_after=str(cfg.get("not_after", "")),
        ))

    def _load_terms(self):
        for term, value in (self.kw.get("terms", {}) or {}).items():
            self._add(str(term), _as_dict(value))

        for term, value in (self.kw.get("case_sensitive_terms", {}) or {}).items():
            cfg = _as_dict(value)
            cfg.setdefault("not_after", "," if str(term) == "CT" else "")
            self._add(str(term), cfg, case_sensitive=True)

        for term, value in (self.kw.get("ru_terms", {}) or {}).items():
            cfg = _as_dict(value)
            self._add(str(term), cfg, stem=cfg.get("stem", True))

        for group, cfg in (self.kw.get("context_groups", {}) or {}).items():
            cfg = cfg or {}
            weight = cfg.get("weight", 2)
            for term in cfg.get("terms", []) or []:
                self._add(str(term), {"weight": weight, "main": False},
                          kind="context", group=str(group))

    # -------------------------------------------------------------- поиск

    def analyze(self, text: str, substring: bool = False) -> MatchResult:
        """
        Разобрать один текст (bio, name, пост).
        substring=True — режим username: поиск по подстроке, только термины
        длиной от username_substring_min_length.
        """
        res = MatchResult()
        if not text:
            return res

        text = normalize(text)

        # ENS-имена ищем до вырезания ссылок: vitalik.eth похоже на домен.
        if self._ens_re:
            for m in self._ens_re.finditer(text):
                res.ens.append(Match(m.group(0), self.ens_weight, "special"))
                if len(res.ens) >= 1:
                    break

        # Крипто-домены прямо в тексте (pump.fun, dexscreener.com в bio).
        seen_domains = set()
        if self._domain_re:
            for m in self._domain_re.finditer(text):
                d = m.group(1).lower()
                if d not in seen_domains:
                    seen_domains.add(d)
                    res.domains.append((d, Match(d, self.domains[d], "domain")))

        # Кэштеги — по исходному регистру: $BTC, но не "btc" в слове.
        seen_cash = set()
        for m in CASHTAG_RE.finditer(text):
            ticker = m.group(1).upper()
            if ticker in seen_cash:
                continue
            seen_cash.add(ticker)
            if ticker in self.known_tickers:
                res.cashtags_known.append(
                    Match("$" + ticker, self.cashtag_known_weight, "cashtag"))
            else:
                res.cashtags_unknown.append(
                    Match("$" + ticker, self.cashtag_unknown_weight, "cashtag"))

        # Символы-признаки: ₿, 🦇🔊
        for sym, weight in self.symbols.items():
            if sym in text:
                res.symbols.append(Match(sym, weight, "special"))

        # Ссылки убираем: домены уже учтены, слова внутри URL считать нельзя.
        clean, urls = strip_urls(text)
        res.urls = urls

        mask = _Mask()
        # Упоминания запоминаем, но НЕ маскируем: "@okx" и так совпадёт целым
        # словом с полным весом. Разбирать по подстроке будем только те,
        # внутри которых обычный поиск ничего не нашёл (см. ниже).
        mentions = []
        if not substring and self.mention_weight > 0:
            mentions = [(m.start(), m.end(), m.group(1))
                        for m in MENTION_RE.finditer(clean)]
        # Исключения ("криптография", "cryptography") маскируем до поиска,
        # чтобы основа "крипт" их не поймала.
        for rx in self.exclusions:
            for m in rx.finditer(clean):
                mask.take(m.start(), m.end())
        # Найденные домены тоже маскируем — иначе "pump.fun" даст ещё и "fun".
        if self._domain_re:
            for m in self._domain_re.finditer(clean):
                mask.take(m.start(), m.end())

        for term in self.terms:
            rx = term.substring_regex if substring else term.regex
            if rx is None:
                continue
            for m in rx.finditer(clean):
                if not mask.free(m.start(), m.end()):
                    continue
                if term.not_after and self._blocked_by_prefix(clean, m.start(),
                                                              term.not_after):
                    continue
                mask.take(m.start(), m.end())
                match = Match(term.term, term.weight, term.kind, term.group)
                if term.kind == "context":
                    res.context.append(match)
                elif term.main:
                    res.main.append(match)
                else:
                    res.weak.append(match)
                break        # каждый термин засчитывается один раз в поле

        # Теперь добираем признаки внутри @упоминаний — но только там, где
        # обычный поиск целыми словами ничего не нашёл. "@okx" уже посчитан
        # выше с полным весом, а "@a16zcrypto" не был бы найден никогда.
        for start, end, handle in mentions:
            if not mask.free(start, end):
                continue
            found = self._mention_matches(handle)
            if found:
                mask.take(start, end)
                res.mentions += found
        return res

    def _mention_matches(self, handle: str) -> list:
        """
        Крипто-признаки внутри одного @упоминания. Ищем по подстроке и только
        термины от username_substring_min_length символов — короткие вроде
        "eth" дали бы слишком много ложных срабатываний в именах аккаунтов.
        """
        out = []
        mask = _Mask()
        for rx in self.exclusions:
            for m in rx.finditer(handle):
                mask.take(m.start(), m.end())

        for term in self.terms:
            if term.kind == "context" or term.substring_regex is None:
                continue
            match = term.substring_regex.search(handle)
            if not match or not mask.free(match.start(), match.end()):
                continue
            mask.take(match.start(), match.end())
            out.append(Match(f"{term.term} (@{handle})",
                             term.weight * self.mention_weight,
                             kind="mention", main=term.main))
        return out

    @staticmethod
    def _blocked_by_prefix(text, start, chars) -> bool:
        """
        "Hartford, CT" — CT после запятой это штат, а не Crypto Twitter.
        Смотрим ближайший непробельный символ слева.
        """
        i = start - 1
        while i >= 0 and text[i].isspace():
            i -= 1
        return i >= 0 and text[i] in chars

    # ------------------------------------------------------------ ссылки

    def analyze_urls(self, urls) -> MatchResult:
        """Крипто-домены в сайте профиля и ссылках из bio."""
        res = MatchResult()
        seen = set()
        for url in urls or []:
            if not url:
                continue
            host = domain_of(url)
            for domain, weight in self.domains.items():
                if host == domain or host.endswith("." + domain):
                    if domain in seen:
                        break
                    seen.add(domain)
                    res.domains.append((domain, Match(domain, weight, "domain")))
                    break
        return res


# ------------------------------------------------------------- загрузка

_CACHE = {}


def load_keywords(path=None) -> dict:
    """Прочитать keywords.yaml (с кешем по пути и времени изменения)."""
    if path is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            import config
            path = os.path.join(root, config.KEYWORDS_FILE)
        except Exception:
            path = os.path.join(root, "keywords.yaml")
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0
    key = (path, stamp)
    if key not in _CACHE:
        with open(path, encoding="utf-8") as f:
            _CACHE[key] = yaml.safe_load(f) or {}
    return _CACHE[key]


def build_matcher(keywords=None, path=None) -> Matcher:
    return Matcher(keywords if keywords is not None else load_keywords(path))
