"""
Program + КБЖУ lookups. No longer a stub — these read from the
`client_profiles` table in booking_api.py's own database, which you fill
in yourself via /set_program and /set_kbju (see booking_handlers.py).
No dependency on your existing bot's schema at all.
"""

import sqlite3


def get_client_program(conn: sqlite3.Connection, telegram_user_id: int):
    row = conn.execute(
        "SELECT program_text, updated_at FROM client_profiles WHERE telegram_id = ?",
        (telegram_user_id,),
    ).fetchone()
    if not row or not row["program_text"]:
        return {"text": None, "updated_at": None}
    return {"text": row["program_text"], "updated_at": row["updated_at"]}


def get_client_kbju(conn: sqlite3.Connection, telegram_user_id: int):
    row = conn.execute(
        "SELECT calories, protein, fat, carbs, updated_at FROM client_profiles WHERE telegram_id = ?",
        (telegram_user_id,),
    ).fetchone()
    if not row:
        return {"calories": None, "protein": None, "fat": None, "carbs": None, "updated_at": None}
    return dict(row)
