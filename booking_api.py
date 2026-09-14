"""
Booking API for the training-session Mini App.

Deploy this in the SAME process/container as your existing python-telegram-bot,
so both read/write the same SQLite file directly. Running it as a second,
separate Railway service will NOT share the file unless you attach a shared
volume — two processes pointing at two different local SQLite files is the
most common way this silently breaks. If you'd rather keep it as a separate
service for isolation, move to Postgres (Railway has a one-click addon) so
both services talk to the same database over the network instead.

ENV VARS REQUIRED:
    BOT_TOKEN       - same token your bot already uses
    DB_PATH         - path to your existing SQLite file (default: clients.db)
    TRAINER_TG_ID   - your personal Telegram user id, for booking notifications

ASSUMPTIONS TO CHECK AGAINST YOUR REAL SCHEMA:
    - Nothing here reads your existing `clients` table — it only needs the
      Telegram user id, which comes from validated initData, not your DB.
      If you want to reject bookings from people who aren't your clients,
      add that check in create_booking() where marked below.
"""

import os
import sqlite3
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from client_data import get_client_program, get_client_kbju

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "clients.db")
TRAINER_TG_ID = int(os.environ["TRAINER_TG_ID"])

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def serve_miniapp():
    """Serves the Mini App itself, so bot + API + frontend are one Railway
    service on one domain — MINIAPP_URL is just that domain's root."""
    return FileResponse("telegram_booking_miniapp.html")


# ---------- DB setup ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
            slot_id TEXT NOT NULL,
            client_tg_id INTEGER NOT NULL,
            client_name TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            FOREIGN KEY (slot_id) REFERENCES slots(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            telegram_id INTEGER PRIMARY KEY,
            program_text TEXT,
            calories INTEGER,
            protein INTEGER,
            fat INTEGER,
            carbs INTEGER,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS known_users (
            telegram_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            first_seen TEXT
        )
    """)
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
    trainer can find it later with /clients — without this there'd be no
    way to know which telegram_id belongs to which client."""
    if not user.get("id"):
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO known_users (telegram_id, first_name, username, first_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username",
        (user["id"], user.get("first_name"), user.get("username"), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ---------- Models ----------
class SlotCreate(BaseModel):
    date: str          # 'YYYY-MM-DD'
    time: str          # 'HH:MM'
    duration: int = 60
    capacity: int = 1
    init_data: str      # must be the trainer's own initData


class BookingRequest(BaseModel):
    slot_id: str
    init_data: str


class CancelRequest(BaseModel):
    init_data: str


# ---------- Slots ----------
@app.get("/api/slots")
def get_slots(week_start: str):
    """week_start = 'YYYY-MM-DD' (Monday). Returns {date: [slot,...]} for 7 days."""
    conn = get_conn()
    start = datetime.strptime(week_start, "%Y-%m-%d")
    end = start + timedelta(days=7)
    rows = conn.execute(
        "SELECT * FROM slots WHERE date >= ? AND date < ? ORDER BY date, time",
        (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    ).fetchall()

    result = {}
    for r in rows:
        booked = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status = 'active'",
            (r["id"],),
        ).fetchone()[0]
        result.setdefault(r["date"], []).append({
            "id": r["id"], "time": r["time"], "duration": r["duration"],
            "capacity": r["capacity"], "booked": booked,
        })
    conn.close()
    return result


@app.post("/api/slots")
def create_slot(req: SlotCreate):
    """Trainer-only: add one open slot. No admin UI yet — call this directly
    (curl/Postman) until a proper admin screen exists (see roadmap)."""
    user = validate_init_data(req.init_data)
    if user.get("id") != TRAINER_TG_ID:
        raise HTTPException(403, "only the trainer can create slots")

    slot_id = f"{req.date}_{req.time}"
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO slots (id, date, time, duration, capacity) VALUES (?, ?, ?, ?, ?)",
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

    # OPTIONAL: reject non-clients here, e.g.
    #   if not is_known_client(client_tg_id): raise HTTPException(403, "not a client")

    conn = get_conn()
    slot = conn.execute("SELECT * FROM slots WHERE id = ?", (req.slot_id,)).fetchone()
    if not slot:
        conn.close()
        raise HTTPException(404, "slot not found")

    booked = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status = 'active'",
        (req.slot_id,),
    ).fetchone()[0]
    if booked >= slot["capacity"]:
        conn.close()
        raise HTTPException(409, "slot is full")

    booking_id = f"bk_{req.slot_id}_{client_tg_id}_{int(time.time())}"
    client_name = user.get("first_name", "Клиент")
    conn.execute(
        "INSERT INTO bookings (id, slot_id, client_tg_id, client_name, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'active')",
        (booking_id, req.slot_id, client_tg_id, client_name, datetime.utcnow().isoformat()),
    )
    conn.commit()

    # Roster is what makes group slots visible to the trainer — pulled fresh
    # right after the insert so the notification always reflects who's in.
    roster_rows = conn.execute(
        "SELECT client_name FROM bookings WHERE slot_id = ? AND status = 'active'",
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
    """Program + КБЖУ for the calling client only — scoped to their own
    validated Telegram id. Never returns another client's data."""
    user = validate_init_data(init_data)
    conn = get_conn()
    program = get_client_program(conn, user["id"])
    kbju = get_client_kbju(conn, user["id"])
    conn.close()
    return {"program": program, "kbju": kbju}


@app.get("/api/slots/{slot_id}/roster")
def get_roster(slot_id: str, init_data: str):
    """Trainer-only: who's booked into a given slot."""
    user = validate_init_data(init_data)
    if user.get("id") != TRAINER_TG_ID:
        raise HTTPException(403, "trainer only")
    conn = get_conn()
    rows = conn.execute(
        "SELECT client_name, client_tg_id FROM bookings WHERE slot_id = ? AND status = 'active'",
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
        WHERE b.client_tg_id = ? AND b.status = 'active'
        ORDER BY s.date, s.time
    """, (client_tg_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/bookings/{booking_id}")
async def cancel_booking(booking_id: str, req: CancelRequest):
    user = validate_init_data(req.init_data)
    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not row or row["client_tg_id"] != user.get("id"):
        conn.close()
        raise HTTPException(404, "booking not found")
    slot = conn.execute("SELECT * FROM slots WHERE id = ?", (row["slot_id"],)).fetchone()
    conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
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


# Local test run: `uvicorn booking_api:app --reload`
# On Railway, add to the SAME service as your bot (see deployment note above).
