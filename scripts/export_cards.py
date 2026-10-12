"""Read-only text helpers required by the bundled card model."""
import sqlite3


def read_db(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def localizations(db, table):
    if table not in {"Localizations_enUS", "Localizations_jaJP"}:
        raise ValueError("Unsupported localization table")
    result, ranks = {}, {}
    for row in db.execute(f'SELECT LocId, Formatted, Loc FROM "{table}"'):
        rank = 0 if row["Formatted"] == 1 else (row["Formatted"] or 999)
        if row["LocId"] not in ranks or rank < ranks[row["LocId"]]:
            result[row["LocId"]], ranks[row["LocId"]] = row["Loc"], rank
    return result


def ability_pairs(value):
    for part in (value or "").split(","):
        if part:
            left, right = part.split(":", 1)
            yield int(left), int(right)


def type_line(card, loc):
    main, sub = loc.get(card["TypeTextId"]), loc.get(card["SubtypeTextId"])
    return f"{main} — {sub}" if main and sub else main or sub
