"""
Booking API for the training-session Mini App — Postgres-backed.

Owns the full database schema (every CREATE TABLE for this project lives
here, in one place, so there is exactly one source of truth for table
and column names — every other file only reads/writes through get_conn()).

ENV VARS REQUIRED:
    BOT_TOKEN       - same token your bot already uses
    DATABASE_URL    - Postgres connection string (Railway injects this
                      automatically once you reference it from the
                      Postgres service into this service's Variables)
    TRAINER_TG_ID   - your personal Telegram user id, for booking notifications
"""

import os
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, date
from urllib.parse import parse_qsl

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from client_data import get_client_program, get_client_kbju

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
TRAINER_TG_ID = int(os.environ["TRAINER_TG_ID"])

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def serve_miniapp():
    """Serves the Mini App itself, so bot + API + frontend are one Railway
    service on one domain — MINIAPP_URL is just that domain's root."""
    return FileResponse("telegram_booking_miniapp.html")


# ---------- DB connection ----------
class _ConnWrapper:
    """Lets every call site use conn.execute(sql, params).fetchone()/
    fetchall() the way sqlite3 allowed. psycopg needs an explicit
    cursor — this hides that difference in exactly one place."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def get_conn():
    raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return _ConnWrapper(raw)


# ---------- Schema (single source of truth for every table) ----------
def init_db():
    conn = get_conn()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            duration INTEGER NOT NULL DEFAULT 60,
            capacity INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY,
            slot_id TEXT NOT NULL REFERENCES slots(id),
            client_tg_id BIGINT NOT NULL,
            client_name TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            reminded_24h INTEGER NOT NULL DEFAULT 0,
            reminded_2h INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            telegram_id BIGINT PRIMARY KEY,
            program_text TEXT,
            calories INTEGER,
            protein INTEGER,
            fat INTEGER,
            carbs INTEGER,
            weight_kg REAL,
            updated_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS known_users (
            telegram_id BIGINT PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            first_seen TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS food_logs (
            id TEXT PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            log_date TEXT NOT NULL,
            description TEXT,
            calories INTEGER,
            protein INTEGER,
            fat INTEGER,
            carbs INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS progress_logs (
            id TEXT PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            log_date TEXT NOT NULL,
            exercise TEXT NOT NULL,
            weight_kg REAL,
            reps INTEGER,
            sets INTEGER,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS technique_reviews (
            id TEXT PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            exercise TEXT,
            video_file_id TEXT,
            trainer_message_id BIGINT,
            score INTEGER,
            feedback TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_template (
            id TEXT PRIMARY KEY,
            day_of_week INTEGER NOT NULL,
            time TEXT NOT NULL,
            duration INTEGER NOT NULL DEFAULT 60,
            capacity INTEGER NOT NULL DEFAULT 1
        )
    """)


    conn.execute('''
        CREATE TABLE IF NOT EXISTS water_logs (
            telegram_id BIGINT NOT NULL,
            log_date TEXT NOT NULL,
            ml INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (telegram_id, log_date)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS habit_logs (
            telegram_id BIGINT NOT NULL,
            log_date TEXT NOT NULL,
            workout INTEGER NOT NULL DEFAULT 0,
            nutrition INTEGER NOT NULL DEFAULT 0,
            sleep INTEGER NOT NULL DEFAULT 0,
            water INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (telegram_id, log_date)
        )
    ''')

    conn.commit()
    conn.close()


init_db()


# ---------- Telegram initData validation ----------
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "missing initData")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "no hash in initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(401, "invalid initData signature")

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > 86400:
        raise HTTPException(401, "initData expired (older than 24h)")

    user = json.loads(parsed.get("user", "{}"))
    _record_known_user(user)
    return user


def _record_known_user(user: dict):
    """Every time someone opens the mini app, note their id/name so the
    trainer can find it later with /clients."""
    if not user.get("id"):
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO known_users (telegram_id, first_name, username, first_seen) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (telegram_id) DO UPDATE SET first_name = EXCLUDED.first_name, username = EXCLUDED.username",
        (user["id"], user.get("first_name"), user.get("username"), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ---------- Request models ----------
class SlotCreate(BaseModel):
    date: str
    time: str
    duration: int = 60
    capacity: int = 1
    init_data: str


class BookingRequest(BaseModel):
    slot_id: str
    init_data: str


class CancelRequest(BaseModel):
    init_data: str


# ---------- Slots ----------
@app.get("/api/slots")
def get_slots(week_start: str):
    conn = get_conn()
    start = datetime.strptime(week_start, "%Y-%m-%d")
    end = start + timedelta(days=7)
    rows = conn.execute(
        "SELECT * FROM slots WHERE date >= %s AND date < %s ORDER BY date, time",
        (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    ).fetchall()

    result = {}
    for r in rows:
        booked = conn.execute(
            "SELECT COUNT(*) AS cnt FROM bookings WHERE slot_id = %s AND status = 'active'",
            (r["id"],),
        ).fetchone()["cnt"]
        result.setdefault(r["date"], []).append({
            "id": r["id"], "time": r["time"], "duration": r["duration"],
            "capacity": r["capacity"], "booked": booked,
        })
    conn.close()
    return result


@app.post("/api/slots")
def create_slot(req: SlotCreate):
    user = validate_init_data(req.init_data)
    if user.get("id") != TRAINER_TG_ID:
        raise HTTPException(403, "only the trainer can create slots")

    slot_id = f"{req.date}_{req.time}"
    conn = get_conn()
    conn.execute(
        "INSERT INTO slots (id, date, time, duration, capacity) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET date = EXCLUDED.date, time = EXCLUDED.time, "
        "duration = EXCLUDED.duration, capacity = EXCLUDED.capacity",
        (slot_id, req.date, req.time, req.duration, req.capacity),
    )
    conn.commit()
    conn.close()
    return {"id": slot_id}


# ---------- Bookings ----------
@app.post("/api/bookings")
async def create_booking(req: BookingRequest):
    user = validate_init_data(req.init_data)
    client_tg_id = user.get("id")
    if not client_tg_id:
        raise HTTPException(400, "no user id in initData")

    conn = get_conn()
    slot = conn.execute("SELECT * FROM slots WHERE id = %s", (req.slot_id,)).fetchone()
    if not slot:
        conn.close()
        raise HTTPException(404, "slot not found")

    booked = conn.execute(
        "SELECT COUNT(*) AS cnt FROM bookings WHERE slot_id = %s AND status = 'active'",
        (req.slot_id,),
    ).fetchone()["cnt"]
    if booked >= slot["capacity"]:
        conn.close()
        raise HTTPException(409, "slot is full")

    booking_id = f"bk_{req.slot_id}_{client_tg_id}_{int(time.time())}"
    client_name = user.get("first_name", "Клиент")
    conn.execute(
        "INSERT INTO bookings (id, slot_id, client_tg_id, client_name, created_at, status) "
        "VALUES (%s, %s, %s, %s, %s, 'active')",
        (booking_id, req.slot_id, client_tg_id, client_name, datetime.utcnow().isoformat()),
    )
    conn.commit()

    roster_rows = conn.execute(
        "SELECT client_name FROM bookings WHERE slot_id = %s AND status = 'active'",
        (req.slot_id,),
    ).fetchall()
    conn.close()

    roster_names = ", ".join(r["client_name"] for r in roster_rows)
    await notify_trainer(
        f"Новая запись: {client_name} — {slot['date']} в {slot['time']}\n"
        f"Записано на это время ({len(roster_rows)}/{slot['capacity']}): {roster_names}"
    )
    return {"booking_id": booking_id}


@app.get("/api/me")
def get_me(init_data: str):
    """Program + КБЖУ + today's logged nutrition for the calling client
    only — scoped to their own validated Telegram id."""
    user = validate_init_data(init_data)
    conn = get_conn()
    program = get_client_program(conn, user["id"])
    kbju = get_client_kbju(conn, user["id"])
    today = date.today().isoformat()
    totals = conn.execute(
        "SELECT COALESCE(SUM(calories),0) AS calories, COALESCE(SUM(protein),0) AS protein, "
        "COALESCE(SUM(fat),0) AS fat, COALESCE(SUM(carbs),0) AS carbs, COUNT(*) AS entries "
        "FROM food_logs WHERE telegram_id = %s AND log_date = %s",
        (user["id"], today),
    ).fetchone()
    conn.close()
    return {"program": program, "kbju": kbju, "today": dict(totals)}


@app.get("/api/slots/{slot_id}/roster")
def get_roster(slot_id: str, init_data: str):
    user = validate_init_data(init_data)
    if user.get("id") != TRAINER_TG_ID:
        raise HTTPException(403, "trainer only")
    conn = get_conn()
    rows = conn.execute(
        "SELECT client_name, client_tg_id FROM bookings WHERE slot_id = %s AND status = 'active'",
        (slot_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/my-bookings")
def get_my_bookings(init_data: str):
    user = validate_init_data(init_data)
    client_tg_id = user.get("id")
    conn = get_conn()
    rows = conn.execute("""
        SELECT b.id, s.date, s.time, s.duration
        FROM bookings b JOIN slots s ON b.slot_id = s.id
        WHERE b.client_tg_id = %s AND b.status = 'active'
        ORDER BY s.date, s.time
    """, (client_tg_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/bookings/{booking_id}")
async def cancel_booking(booking_id: str, req: CancelRequest):
    user = validate_init_data(req.init_data)
    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id = %s", (booking_id,)).fetchone()
    if not row or row["client_tg_id"] != user.get("id"):
        conn.close()
        raise HTTPException(404, "booking not found")
    slot = conn.execute("SELECT * FROM slots WHERE id = %s", (row["slot_id"],)).fetchone()
    conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = %s", (booking_id,))
    conn.commit()
    conn.close()

    await notify_trainer(f"Отмена записи: {row['client_name']} — {slot['date']} в {slot['time']}")
    return {"ok": True}


# ---------- Trainer notification ----------
async def notify_trainer(text: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": TRAINER_TG_ID, "text": text},
        )
