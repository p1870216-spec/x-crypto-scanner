# -*- coding: utf-8 -*-
"""
Выгрузки (раздел 12 ТЗ) и защита от «опасных» значений (раздел 16):
управляющие символы, =cmd, кириллица, эмодзи, очень длинные строки.
"""

import csv
import os

import pytest
from openpyxl import load_workbook

from scanner.export import (
    COLUMNS,
    MAX_CELL_LENGTH,
    build_row,
    build_rows,
    build_stats,
    clean_text,
    csv_safe,
    export_all,
    export_csv,
    export_report,
    export_xlsx,
)


def user(uid="1", username="user1", **extra):
    data = {"user_id": uid, "username": username, "name": "Имя",
            "bio": "crypto trader", "location": "Дубай", "created_at": "2021-07-25",
            "followers": 12345, "following": 100, "tweets": 500,
            "verified": True, "website": "https://etherscan.io",
            "profile_url": f"https://x.com/{username}", "data_source": "api"}
    data.update(extra)
    return data


def record(uid="1", username="user1", score=75, **extra):
    data = {"user_id": uid, "username": username, "crypto_score": score,
            "category": "CRYPTO INFLUENCER", "profile_score": 40.0,
            "tweet_score": 29.0, "profile_raw": 90.0,
            "specializations": ["TRADER"], "matched_keywords": "crypto, btc",
            "reason": 'Bio: "crypto trader" (+30)', "crypto_posts": 12,
            "analyzed_posts": 30, "crypto_posts_ratio": 0.4,
            "replies_crypto_ratio": None, "status": "ok", "data_source": "api"}
    data.update(extra)
    return data


@pytest.fixture
def rows():
    users = [user("1", "alpha"), user("2", "beta"), user("3", "gamma")]
    analysis = {
        "1": record("1", "alpha", 85, category="STRONG CRYPTO CT",
                    specializations=["TRADER", "ANALYST"]),
        "2": record("2", "beta", 45, category="CRYPTO RELATED",
                    specializations=[]),
        "3": record("3", "gamma", 5, category="NOT CRYPTO",
                    specializations=[], status="not_analyzed",
                    analyzed_posts=0, crypto_posts=0, crypto_posts_ratio=None),
    }
    return build_rows(users, analysis)


@pytest.fixture
def stats(rows):
    return build_stats({"following": 3}, rows,
                       {"candidates": 2, "analyzed": 2, "unavailable": 0,
                        "errors": 0, "pending": 0},
                       {"crypto_threshold": 20, "data_source": "api",
                        "scoring_mode": "профиль + посты"})


# ------------------------------------------------------- опасные значения


def test_clean_text_removes_illegal_xml():
    """Управляющие символы валят openpyxl с IllegalCharacterError."""
    assert clean_text("до\x00\x01\x08после") == "допосле"
    # переводы строки и табуляция допустимы и должны остаться
    assert clean_text("строка\nдругая\tтаб") == "строка\nдругая\tтаб"


def test_clean_text_truncates():
    assert len(clean_text("я" * (MAX_CELL_LENGTH + 500))) == MAX_CELL_LENGTH


def test_clean_text_keeps_cyrillic_and_emoji():
    text = "Крипто-трейдер 🚀₿ и мемы"
    assert clean_text(text) == text


@pytest.mark.parametrize("value", ["=cmd|' /c calc'!A1", "+1+1", "-5", "@user"])
def test_csv_safe_prefixes_formulas(value):
    assert csv_safe(value).startswith("'")


def test_csv_safe_keeps_normal_text():
    assert csv_safe("обычный текст") == "обычный текст"


def test_xlsx_writes_formulas_as_text(tmp_path, stats):
    danger = build_row(user("9", "danger", name="=cmd|' /c calc'!A1",
                            bio="-5 минус в начале\x07"),
                       record("9", "danger", 10, reason="@упоминание",
                              category="NOT CRYPTO", specializations=[]))
    path = str(tmp_path / "danger.xlsx")
    export_xlsx(path, [danger], stats)

    ws = load_workbook(path)["ALL FOLLOWING"]
    headers = [c.value for c in ws[1]]
    name_cell = ws.cell(row=2, column=headers.index("name") + 1)
    reason_cell = ws.cell(row=2, column=headers.index("reason") + 1)
    bio_cell = ws.cell(row=2, column=headers.index("bio") + 1)

    assert name_cell.data_type == "s", "Excel принял бы это за формулу"
    assert name_cell.value == "=cmd|' /c calc'!A1"
    assert reason_cell.data_type == "s"
    assert "\x07" not in bio_cell.value


def test_xlsx_survives_control_characters(tmp_path, stats):
    """Без очистки openpyxl падает с IllegalCharacterError."""
    row = build_row(user("9", "ctl", bio="плохо\x00\x0b\x1f хорошо"),
                    record("9", "ctl", 30, category="POSSIBLE CRYPTO",
                           specializations=[]))
    path = str(tmp_path / "ctl.xlsx")
    export_xlsx(path, [row], stats)      # не должно бросить исключение

    ws = load_workbook(path)["ALL FOLLOWING"]
    headers = [c.value for c in ws[1]]
    bio = ws.cell(row=2, column=headers.index("bio") + 1).value
    assert bio == "плохо хорошо"


# ------------------------------------------------------------------ Excel


def test_xlsx_sheets_and_filters(tmp_path, rows, stats):
    path = str(tmp_path / "a.xlsx")
    export_xlsx(path, rows, stats, crypto_threshold=20, strong_threshold=60)
    wb = load_workbook(path)

    assert wb.sheetnames == ["ALL FOLLOWING", "CRYPTO", "STRONG_CRYPTO",
                             "TRADERS", "ANALYSTS", "STATISTICS"]
    assert wb["ALL FOLLOWING"].max_row == 4        # 3 строки + шапка
    assert wb["CRYPTO"].max_row == 3               # 85 и 45
    assert wb["STRONG_CRYPTO"].max_row == 2        # только 85
    assert wb["TRADERS"].max_row == 2
    assert wb["ANALYSTS"].max_row == 2

    ws = wb["ALL FOLLOWING"]
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref
    assert [c.value for c in ws[1]] == [title for _k, title, _w, _t in COLUMNS]


def test_xlsx_sorted_by_score(tmp_path, rows, stats):
    path = str(tmp_path / "b.xlsx")
    export_xlsx(path, rows, stats)
    ws = load_workbook(path)["ALL FOLLOWING"]
    scores = [ws.cell(row=r, column=13).value for r in range(2, ws.max_row + 1)]
    assert scores == sorted(scores, reverse=True)


def test_xlsx_formatting(tmp_path, rows, stats):
    path = str(tmp_path / "c.xlsx")
    export_xlsx(path, rows, stats)
    ws = load_workbook(path)["ALL FOLLOWING"]
    headers = [c.value for c in ws[1]]

    followers = ws.cell(row=2, column=headers.index("followers") + 1)
    assert followers.number_format == "#,##0"
    assert followers.value == 12345

    ratio = ws.cell(row=2, column=headers.index("crypto_posts_ratio") + 1)
    assert ratio.number_format == "0%"

    link = ws.cell(row=2, column=headers.index("profile_url") + 1)
    assert link.hyperlink is not None

    bio = ws.cell(row=2, column=headers.index("bio") + 1)
    assert bio.alignment.wrap_text is True

    # заливка строки по категории
    assert ws.cell(row=2, column=1).fill.fgColor.rgb.endswith("C6EFCE")


def test_xlsx_empty_ratio_is_blank(tmp_path, rows, stats):
    path = str(tmp_path / "d.xlsx")
    export_xlsx(path, rows, stats)
    ws = load_workbook(path)["ALL FOLLOWING"]
    headers = [c.value for c in ws[1]]
    # у не проанализированного аккаунта доля пустая, а не ноль
    last = ws.cell(row=ws.max_row, column=headers.index("crypto_posts_ratio") + 1)
    assert last.value in ("", None)


def test_xlsx_with_no_rows(tmp_path, stats):
    path = str(tmp_path / "empty.xlsx")
    export_xlsx(path, [], stats)
    wb = load_workbook(path)
    assert wb["ALL FOLLOWING"].max_row == 1


# -------------------------------------------------------------------- CSV


def test_csv_has_bom_for_excel(tmp_path, rows):
    """Без BOM Excel на Windows покажет кириллицу кракозябрами."""
    path = str(tmp_path / "a.csv")
    export_csv(path, rows)
    with open(path, "rb") as f:
        assert f.read(3) == b"\xef\xbb\xbf"


def test_csv_roundtrip_cyrillic_and_emoji(tmp_path):
    rows = build_rows([user("1", "u", name="Крипто 🚀", bio="биткоин ₿")],
                      {"1": record("1", "u")})
    path = str(tmp_path / "b.csv")
    export_csv(path, rows)
    with open(path, encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f, delimiter=";"))
    assert data[1][1] == "Крипто 🚀"
    assert data[1][6] == "биткоин ₿"


def test_csv_columns_match_headers(tmp_path, rows):
    path = str(tmp_path / "c.csv")
    export_csv(path, rows)
    with open(path, encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f, delimiter=";"))
    assert len(data) == len(rows) + 1
    assert len(data[0]) == len(COLUMNS)


# ------------------------------------------------------------------ отчёт


def test_report_contents(tmp_path, rows, stats):
    path = str(tmp_path / "r.txt")
    export_report(path, "Rus_Khairullin", rows, stats,
                  warnings=["список может быть неполным"], top=20)
    text = open(path, encoding="utf-8").read()

    assert "@Rus_Khairullin" in text
    assert "Total Following:   3" in text
    assert "Crypto Related:    2" in text
    assert "Strong Crypto CT:  1" in text
    assert "ВНИМАНИЕ: список может быть неполным" in text
    assert "TOP 20" in text
    assert "@alpha" in text
    assert 'Bio: "crypto trader" (+30)' in text


def test_report_top_limit(tmp_path, stats):
    many = build_rows([user(str(i), f"u{i}") for i in range(30)],
                      {str(i): record(str(i), f"u{i}", 90 - i) for i in range(30)})
    path = str(tmp_path / "r.txt")
    export_report(path, "t", many, stats, top=5)
    text = open(path, encoding="utf-8").read()
    assert "  5. @u4" in text
    assert "@u5" not in text


# -------------------------------------------------------------- статистика


def test_stats_numbers_are_real(rows):
    stats = build_stats({"following": 10}, rows,
                        {"candidates": 2, "analyzed": 2, "unavailable": 1,
                         "errors": 3, "pending": 4, "from_cache": 5},
                        {"crypto_threshold": 20, "data_source": "api",
                         "scoring_mode": "профиль + посты"})
    flat = {name: value for _title, pairs in stats for name, value in pairs}

    assert flat["Total Following (собрано)"] == 3
    assert flat["Заявлено в профиле цели"] == 10
    assert flat["Покрытие"] == "30%"
    assert flat["Crypto-related (>= 20)"] == 2
    assert flat["Кандидатов на глубокий анализ"] == 2
    assert flat["Ошибок"] == 3
    assert flat["Кандидатов без анализа постов"] == 4
    assert flat["Источник данных"] == "api"
    assert flat["STRONG CRYPTO CT"] == 1
    assert flat["TRADER"] == 1


def test_stats_without_declared_count(rows):
    stats = build_stats({}, rows, {}, {"crypto_threshold": 20})
    flat = {name: value for _title, pairs in stats for name, value in pairs}
    assert flat["Покрытие"] == "—"


# ------------------------------------------------------------------ сборка


def test_export_all_creates_every_file(tmp_path, rows, stats):
    out = str(tmp_path / "out")
    paths, failures = export_all(out, "Target", rows, stats)
    names = sorted(os.path.basename(p) for p in paths)
    assert names == ["Target_all.csv", "Target_analysis.xlsx",
                     "Target_crypto.csv", "Target_report.txt",
                     "Target_strong_crypto.csv"]
    assert failures == []
    for path in paths:
        assert os.path.getsize(path) > 0


def test_export_all_survives_locked_file(tmp_path, rows, stats, monkeypatch):
    """
    Открытый в Excel файл заблокирован на запись. Один такой файл не должен
    ронять всю выгрузку — остальные обязаны записаться.
    """
    import scanner.export as export_module

    real_csv = export_module.export_csv

    def locked(path, data):
        if path.endswith("_all.csv"):
            raise PermissionError(13, "Permission denied", path)
        return real_csv(path, data)

    monkeypatch.setattr(export_module, "export_csv", locked)

    out = str(tmp_path / "out")
    paths, failures = export_module.export_all(out, "Target", rows, stats)

    assert len(paths) == 4
    assert len(failures) == 1
    assert failures[0][0] == "Target_all.csv"
    assert "открыт в другой программе" in failures[0][1]
