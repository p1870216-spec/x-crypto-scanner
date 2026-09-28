# -*- coding: utf-8 -*-
"""
Выгрузки: xlsx, csv, txt (раздел 12 ТЗ).

Всё складывается в output/<target>/.

Безопасность значений (иначе файл либо не открывается, либо Excel считает
текст формулой):
  * управляющие символы, недопустимые в XML, вырезаются — иначе openpyxl
    падает с IllegalCharacterError;
  * строки обрезаются до 32 000 символов;
  * текст, начинающийся с =, +, -, @, пишется строго как строка: в xlsx через
    data_type='s', в csv через ведущий апостроф.
"""

import csv
import datetime
import os
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Символы, которых не бывает в XML: openpyxl на них падает.
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_CELL_LENGTH = 32000
FORMULA_STARTS = ("=", "+", "-", "@")

# Колонки: сначала в порядке исходного ТЗ, затем дополнительные.
# (ключ, заголовок, ширина, тип)
COLUMNS = [
    ("username", "username", 20, "text"),
    ("name", "name", 26, "text"),
    ("followers", "followers", 12, "int"),
    ("following", "following", 11, "int"),
    ("tweets", "tweets", 10, "int"),
    ("verified", "verified", 9, "bool"),
    ("bio", "bio", 50, "wrap"),
    ("location", "location", 18, "text"),
    ("created_at", "created_at", 12, "text"),
    ("profile_url", "profile_url", 30, "link"),
    ("profile_score", "profile_score", 13, "num"),
    ("tweet_score", "tweet_score", 12, "num"),
    ("crypto_score", "crypto_score", 12, "int"),
    ("category", "category", 18, "text"),
    ("specializations", "specializations", 26, "text"),
    ("matched_keywords", "matched_keywords", 36, "wrap"),
    ("crypto_posts", "crypto_posts", 12, "int"),
    ("analyzed_posts", "analyzed_posts", 14, "int"),
    ("crypto_posts_ratio", "crypto_posts_ratio", 17, "percent"),
    ("reason", "reason", 60, "wrap"),
    # дополнительные
    ("user_id", "user_id", 20, "text"),
    ("website", "website", 30, "text"),
    ("status", "status", 13, "text"),
    ("replies_crypto_ratio", "replies_crypto_ratio", 18, "percent"),
    ("data_source", "data_source", 12, "text"),
]

CSV_COLUMNS = [key for key, *_ in COLUMNS]

# Заливка строки по категории.
CATEGORY_FILL = {
    "STRONG CRYPTO CT": "C6EFCE",
    "CRYPTO INFLUENCER": "FFE699",
    "CRYPTO RELATED": "E2EFDA",
    "POSSIBLE CRYPTO": "FFF2CC",
    "NOT CRYPTO": None,
}

HEADER_FILL = PatternFill("solid", fgColor="305496")
HEADER_FONT = Font(color="FFFFFF", bold=True)


# ---------------------------------------------------------------- значения


def clean_text(value) -> str:
    """Убрать запрещённые в XML символы и обрезать слишком длинное."""
    if value is None:
        return ""
    text = str(value)
    text = ILLEGAL_XML.sub("", text)
    if len(text) > MAX_CELL_LENGTH:
        text = text[:MAX_CELL_LENGTH - 1] + "…"
    return text


def looks_like_formula(value) -> bool:
    return isinstance(value, str) and value.startswith(FORMULA_STARTS)


def csv_safe(value) -> str:
    """Для CSV: перед опасным началом ставим апостроф, как требует ТЗ."""
    text = clean_text(value)
    return "'" + text if looks_like_formula(text) else text


def build_row(user: dict, record: dict) -> dict:
    """Одна строка выгрузки из профиля и результата скоринга."""
    user = user or {}
    record = record or {}
    specializations = record.get("specializations") or []
    return {
        "username": user.get("username") or record.get("username", ""),
        "name": user.get("name", ""),
        "followers": int(user.get("followers") or 0),
        "following": int(user.get("following") or 0),
        "tweets": int(user.get("tweets") or 0),
        "verified": bool(user.get("verified")),
        "bio": user.get("bio", ""),
        "location": user.get("location", ""),
        "created_at": user.get("created_at", ""),
        "profile_url": user.get("profile_url", ""),
        "profile_score": record.get("profile_score", 0),
        "tweet_score": record.get("tweet_score", 0),
        "crypto_score": int(record.get("crypto_score") or 0),
        "category": record.get("category", ""),
        "specializations": ", ".join(specializations),
        "matched_keywords": record.get("matched_keywords", ""),
        "crypto_posts": record.get("crypto_posts", 0),
        "analyzed_posts": record.get("analyzed_posts", 0),
        "crypto_posts_ratio": record.get("crypto_posts_ratio"),
        "reason": record.get("reason", ""),
        "user_id": user.get("user_id") or record.get("user_id", ""),
        "website": user.get("website", ""),
        "status": record.get("status", ""),
        "replies_crypto_ratio": record.get("replies_crypto_ratio"),
        "data_source": user.get("data_source", ""),
        "_specializations": list(specializations),
    }


def build_rows(users, analysis) -> list:
    """Строки по всем аккаунтам, отсортированные по баллу вниз."""
    rows = []
    for user in users:
        uid = str(user.get("user_id") or "")
        rows.append(build_row(user, (analysis or {}).get(uid)))
    rows.sort(key=lambda r: (-r["crypto_score"], -r["followers"]))
    return rows


# ------------------------------------------------------------------ Excel


def _write_cell(ws, row_index, col_index, value, kind, fill):
    cell = ws.cell(row=row_index, column=col_index)

    if kind in ("int", "num") and isinstance(value, (int, float)):
        cell.value = value
        cell.number_format = "#,##0" if kind == "int" else "0.0"
    elif kind == "percent":
        if value is None or value == "":
            cell.value = ""
        else:
            cell.value = float(value)
            cell.number_format = "0%"
    elif kind == "bool":
        cell.value = "да" if value else ""
    elif kind == "link":
        text = clean_text(value)
        cell.value = text
        if text.startswith("http"):
            cell.hyperlink = text
            cell.font = Font(color="0563C1", underline="single")
    else:
        text = clean_text(value)
        cell.value = text
        if looks_like_formula(text):
            # Иначе Excel примет "=cmd" или "-5" за формулу.
            cell.data_type = "s"
        if kind == "wrap":
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    if fill:
        cell.fill = fill
    return cell


def _write_sheet(wb, title, rows, sheet_index=None):
    ws = wb.create_sheet(title=title[:31])

    for col_index, (_, header, width, _kind) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=col_index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(col_index)].width = width

    fills = {}
    for row_index, row in enumerate(rows, 2):
        color = CATEGORY_FILL.get(row["category"])
        fill = None
        if color:
            fill = fills.get(color) or PatternFill("solid", fgColor=color)
            fills[color] = fill
        for col_index, (key, _h, _w, kind) in enumerate(COLUMNS, 1):
            _write_cell(ws, row_index, col_index, row.get(key), kind, fill)

    ws.freeze_panes = "A2"
    last_column = get_column_letter(len(COLUMNS))
    ws.auto_filter.ref = f"A1:{last_column}{max(1, len(rows) + 1)}"
    return ws


def _write_statistics(wb, stats):
    ws = wb.create_sheet(title="STATISTICS")
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 26

    row = 1
    for title, pairs in stats:
        cell = ws.cell(row=row, column=1, value=title)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
        ws.cell(row=row, column=2).fill = PatternFill("solid", fgColor="DDEBF7")
        row += 1
        for name, value in pairs:
            ws.cell(row=row, column=1, value=clean_text(name))
            target = ws.cell(row=row, column=2)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                target.value = value
                target.number_format = "#,##0"
            else:
                text = clean_text(value)
                target.value = text
                if looks_like_formula(text):
                    target.data_type = "s"
            row += 1
        row += 1
    return ws


def export_xlsx(path, rows, stats, crypto_threshold=20, strong_threshold=60):
    """Книга с шестью листами (раздел 12 ТЗ)."""
    wb = Workbook()
    wb.remove(wb.active)

    crypto = [r for r in rows if r["crypto_score"] >= crypto_threshold]
    strong = [r for r in rows if r["crypto_score"] >= strong_threshold]
    traders = [r for r in crypto if "TRADER" in r["_specializations"]]
    analysts = [r for r in crypto if "ANALYST" in r["_specializations"]]

    _write_sheet(wb, "ALL FOLLOWING", rows)
    _write_sheet(wb, "CRYPTO", crypto)
    _write_sheet(wb, "STRONG_CRYPTO", strong)
    _write_sheet(wb, "TRADERS", traders)
    _write_sheet(wb, "ANALYSTS", analysts)
    _write_statistics(wb, stats)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb.save(path)
    return path


# -------------------------------------------------------------------- CSV


def export_csv(path, rows):
    """
    UTF-8 с BOM (utf-8-sig): иначе Excel на Windows покажет кириллицу
    кракозябрами при двойном клике по файлу.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            out = []
            for key in CSV_COLUMNS:
                value = row.get(key)
                if isinstance(value, bool):
                    value = "да" if value else ""
                elif value is None:
                    value = ""
                out.append(csv_safe(value))
            writer.writerow(out)
    return path


# ------------------------------------------------------------------ отчёт


def _human(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def export_report(path, target, rows, stats, warnings=(), top=20,
                  crypto_threshold=20, strong_threshold=60):
    """Текстовый отчёт: шапка, сводка, предупреждения и TOP-20."""
    crypto = [r for r in rows if r["crypto_score"] >= crypto_threshold]
    strong = [r for r in rows if r["crypto_score"] >= strong_threshold]

    lines = []
    lines.append("=" * 78)
    lines.append("X CRYPTO FOLLOWING SCANNER")
    lines.append("=" * 78)
    lines.append(f"Target:            @{target}")
    lines.append(f"Дата:              "
                 f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Total Following:   {_human(len(rows))}")
    lines.append(f"Crypto Related:    {_human(len(crypto))} "
                 f"(score >= {crypto_threshold})")
    lines.append(f"Strong Crypto CT:  {_human(len(strong))} "
                 f"(score >= {strong_threshold})")
    lines.append("")

    for warning in warnings or ():
        lines.append(f"ВНИМАНИЕ: {warning}")
    if warnings:
        lines.append("")

    lines.append("-" * 78)
    lines.append(f"TOP {top}")
    lines.append("-" * 78)

    for index, row in enumerate(rows[:top], 1):
        specializations = row["specializations"] or "—"
        lines.append(f"{index:>3}. @{row['username']}")
        lines.append(f"     Score:      {row['crypto_score']}")
        lines.append(f"     Followers:  {_human(row['followers'])}")
        lines.append(f"     Category:   {row['category']}")
        lines.append(f"     Type:       {specializations}")
        lines.append(f"     Reason:     {row['reason'] or '—'}")
        lines.append("")

    lines.append("-" * 78)
    lines.append("СТАТИСТИКА")
    lines.append("-" * 78)
    for title, pairs in stats:
        lines.append(title)
        for name, value in pairs:
            shown = _human(value) if isinstance(value, int) else value
            lines.append(f"  {name:<42} {shown}")
        lines.append("")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ------------------------------------------------------------- статистика


def build_stats(target_profile, rows, counters, settings) -> list:
    """
    Числа для листа STATISTICS и текстового отчёта. Все значения — реальные,
    считаются по строкам выгрузки и счётчикам прогона.
    """
    collected = len(rows)
    declared = int((target_profile or {}).get("following") or 0)
    threshold = settings.get("crypto_threshold", 20)

    by_category = {}
    by_specialization = {}
    by_status = {}
    for row in rows:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
        by_status[row["status"] or "—"] = by_status.get(row["status"] or "—", 0) + 1
        for name in row["_specializations"]:
            by_specialization[name] = by_specialization.get(name, 0) + 1

    coverage = f"{collected / declared:.0%}" if declared else "—"

    return [
        ("СБОР", [
            ("Total Following (собрано)", collected),
            ("Заявлено в профиле цели", declared),
            ("Покрытие", coverage),
            ("Источник данных", settings.get("data_source", "")),
            ("Дата запуска", datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
            ("Режим оценки", settings.get("scoring_mode", "")),
        ]),
        ("ИТОГИ", [
            (f"Crypto-related (>= {threshold})",
             sum(1 for r in rows if r["crypto_score"] >= threshold)),
            ("Кандидатов на глубокий анализ", counters.get("candidates", 0)),
            ("Проанализировано постов успешно", counters.get("analyzed", 0)),
            ("Взято из кеша", counters.get("from_cache", 0)),
            ("Закрытых и недоступных", counters.get("unavailable", 0)),
            ("Ошибок", counters.get("errors", 0)),
            ("Кандидатов без анализа постов", counters.get("pending", 0)),
        ]),
        ("ПО КАТЕГОРИЯМ", sorted(by_category.items(), key=lambda x: -x[1])),
        ("ПО СПЕЦИАЛИЗАЦИЯМ",
         sorted(by_specialization.items(), key=lambda x: -x[1]) or [("—", 0)]),
        ("ПО СТАТУСАМ", sorted(by_status.items(), key=lambda x: -x[1])),
    ]


# ----------------------------------------------------------------- сборка


def export_all(out_dir, target, rows, stats, warnings=(), crypto_threshold=20,
               strong_threshold=60):
    """
    Собрать все файлы. Возвращает (пути, ошибки).

    Открытый в Excel файл заблокирован на запись — это самая частая помеха
    при повторном прогоне. Один такой файл не должен ронять всю выгрузку:
    остальные пишутся, а про заблокированные говорим отдельно.
    """
    os.makedirs(out_dir, exist_ok=True)
    crypto = [r for r in rows if r["crypto_score"] >= crypto_threshold]
    strong = [r for r in rows if r["crypto_score"] >= strong_threshold]

    jobs = [
        (f"{target}_analysis.xlsx",
         lambda p: export_xlsx(p, rows, stats, crypto_threshold, strong_threshold)),
        (f"{target}_all.csv", lambda p: export_csv(p, rows)),
        (f"{target}_crypto.csv", lambda p: export_csv(p, crypto)),
        (f"{target}_strong_crypto.csv", lambda p: export_csv(p, strong)),
        (f"{target}_report.txt",
         lambda p: export_report(p, target, rows, stats, warnings, 20,
                                 crypto_threshold, strong_threshold)),
    ]

    paths, failures = [], []
    for filename, write in jobs:
        path = os.path.join(out_dir, filename)
        try:
            paths.append(write(path))
        except PermissionError:
            failures.append((filename, "файл открыт в другой программе "
                                       "(закройте его в Excel и повторите)"))
        except OSError as exc:
            failures.append((filename, str(exc)))
    return paths, failures
