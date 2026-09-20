import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
TRAINER_TG_ID = int(os.environ["TRAINER_TG_ID"])

app = FastAPI(title="Discipline Fitness Bot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Conn:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        return self.raw.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def get_conn(retries=4):
    delay = 0.5
    last = None
    for attempt in range(retries):
        try:
            raw = psycopg.connect(
                DATABASE_URL,
                row_factory=dict_row,
                connect_timeout=5,
            )
            return Conn(raw)
        except psycopg.OperationalError as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 4)
    raise last


def init_db():
    conn = get_conn()
    try:
        statements = [
            """CREATE TABLE IF NOT EXISTS slots (
                id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                duration INTEGER NOT NULL DEFAULT 60,
                capacity INTEGER NOT NULL DEFAULT 1
            )""",
            """CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                slot_id TEXT NOT NULL REFERENCES slots(id),
                client_tg_id BIGINT NOT NULL,
                client_name TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                reminded_24h INTEGER NOT NULL DEFAULT 0,
                reminded_2h INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS client_profiles (
                telegram_id BIGINT PRIMARY KEY,
                program_text TEXT,
                calories INTEGER,
                protein INTEGER,
                fat INTEGER,
                carbs INTEGER,
                weight_kg REAL,
                updated_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS known_users (
                telegram_id BIGINT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                first_seen TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS food_logs (
                id TEXT PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                log_date TEXT NOT NULL,
                description TEXT,
                calories INTEGER,
                protein INTEGER,
                fat INTEGER,
                carbs INTEGER,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS progress_logs (
                id TEXT PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                log_date TEXT NOT NULL,
                exercise TEXT NOT NULL,
                weight_kg REAL NOT NULL,
                reps INTEGER NOT NULL,
                sets INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS technique_reviews (
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
            )""",
            """CREATE TABLE IF NOT EXISTS weekly_template (
                id TEXT PRIMARY KEY,
                day_of_week INTEGER NOT NULL,
                time TEXT NOT NULL,
                duration INTEGER NOT NULL DEFAULT 60,
                capacity INTEGER NOT NULL DEFAULT 1
            )""",
            """CREATE TABLE IF NOT EXISTS water_logs (
                telegram_id BIGINT NOT NULL,
                log_date TEXT NOT NULL,
                ml INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (telegram_id, log_date)
            )""",
            """CREATE TABLE IF NOT EXISTS habit_logs (
                telegram_id BIGINT NOT NULL,
                log_date TEXT NOT NULL,
                workout INTEGER NOT NULL DEFAULT 0,
                nutrition INTEGER NOT NULL DEFAULT 0,
                sleep INTEGER NOT NULL DEFAULT 0,
                water INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (telegram_id, log_date)
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_active_booking_client_slot
               ON bookings(slot_id, client_tg_id)
               WHERE status = 'active'""",
            """CREATE INDEX IF NOT EXISTS idx_bookings_slot_status
               ON bookings(slot_id, status)""",
            """CREATE INDEX IF NOT EXISTS idx_food_logs_user_date
               ON food_logs(telegram_id, log_date)""",
            """CREATE INDEX IF NOT EXISTS idx_progress_logs_user_date
               ON progress_logs(telegram_id, log_date)""",
        ]
        for sql in statements:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
async def startup_db():
    last = None
    for attempt in range(1, 8):
        try:
            await asyncio.to_thread(init_db)
            return
        except Exception as exc:
            last = exc
            print(f"DB startup attempt {attempt}/7 failed: {exc}")
            await asyncio.sleep(min(attempt * 2, 10))
    raise last


@app.get("/")
def root():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "telegram_booking_miniapp.html"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/health")
def health():
    conn = get_conn()
    try:
        conn.execute("SELECT 1").fetchone()
        return {"ok": True, "service": "discipline-bot"}
    finally:
        conn.close()


def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "missing initData")
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(401, "invalid initData")

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "missing hash")

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )
    secret = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()
    expected = hmac.new(
        secret,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "invalid initData signature")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
        user = json.loads(parsed.get("user", "{}"))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(401, "invalid initData payload")

    if auth_date <= 0 or time.time() - auth_date > 86400:
        raise HTTPException(401, "initData expired")

    if not user.get("id"):
        raise HTTPException(401, "telegram user missing")

    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO known_users
               (telegram_id, first_name, username, first_seen)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (telegram_id) DO UPDATE SET
               first_name = EXCLUDED.first_name,
               username = EXCLUDED.username""",
            (
                user["id"],
                user.get("first_name"),
                user.get("username"),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return user


def require_trainer(init_data: str):
    user = validate_init_data(init_data)
    if user["id"] != TRAINER_TG_ID:
        raise HTTPException(403, "trainer only")
    return user


class SlotCreate(BaseModel):
    date: str
    time: str
    duration: int = Field(default=60, ge=15, le=240)
    capacity: int = Field(default=1, ge=1, le=50)
    init_data: str


class BookingRequest(BaseModel):
    slot_id: str
    init_data: str


class CancelRequest(BaseModel):
    init_data: str


class ProgramUpdate(BaseModel):
    init_data: str
    program_text: str = Field(max_length=20000)


class KbjuUpdate(BaseModel):
    init_data: str
    calories: int = Field(ge=500, le=10000)
    protein: int = Field(ge=0, le=1000)
    fat: int = Field(ge=0, le=500)
    carbs: int = Field(ge=0, le=1500)


class WeeklyUpdate(BaseModel):
    init_data: str
    days: list[str]
    times: list[str]


@app.get("/api/slots")
def slots(week_start: str):
    try:
        start = datetime.strptime(week_start, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "week_start must be YYYY-MM-DD")

    end = start + timedelta(days=7)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM slots
               WHERE date >= %s AND date < %s
               ORDER BY date, time""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

        result = {}
        for row in rows:
            booked = conn.execute(
                """SELECT COUNT(*) AS cnt FROM bookings
                   WHERE slot_id = %s AND status = 'active'""",
                (row["id"],),
            ).fetchone()["cnt"]
            result.setdefault(row["date"], []).append(
                {
                    "id": row["id"],
                    "time": row["time"],
                    "duration": row["duration"],
                    "capacity": row["capacity"],
                    "booked": booked,
                }
            )
        return result
    finally:
        conn.close()


@app.post("/api/slots")
def create_slot(req: SlotCreate):
    require_trainer(req.init_data)
    try:
        datetime.strptime(req.date, "%Y-%m-%d")
        datetime.strptime(req.time, "%H:%M")
    except ValueError:
        raise HTTPException(400, "invalid date/time")

    slot_id = f"{req.date}_{req.time}"
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO slots(id,date,time,duration,capacity)
               VALUES(%s,%s,%s,%s,%s)
               ON CONFLICT(id) DO UPDATE SET
               duration=EXCLUDED.duration,
               capacity=EXCLUDED.capacity""",
            (
                slot_id,
                req.date,
                req.time,
                req.duration,
                req.capacity,
            ),
        )
        conn.commit()
        return {"id": slot_id}
    finally:
        conn.close()


@app.post("/api/bookings")
async def create_booking(req: BookingRequest):
    user = validate_init_data(req.init_data)
    client_id = user["id"]
    client_name = user.get("first_name") or "Клиент"

    conn = get_conn()
    try:
        # Advisory transaction lock serializes bookings for this slot.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (req.slot_id,))
        slot = conn.execute(
            "SELECT * FROM slots WHERE id=%s", (req.slot_id,)
        ).fetchone()
        if not slot:
            raise HTTPException(404, "slot not found")

        existing = conn.execute(
            """SELECT id FROM bookings
               WHERE slot_id=%s AND client_tg_id=%s AND status='active'""",
            (req.slot_id, client_id),
        ).fetchone()
        if existing:
            raise HTTPException(409, "already booked")

        booked = conn.execute(
            """SELECT COUNT(*) AS cnt FROM bookings
               WHERE slot_id=%s AND status='active'""",
            (req.slot_id,),
        ).fetchone()["cnt"]
        if booked >= slot["capacity"]:
            raise HTTPException(409, "slot is full")

        booking_id = f"bk_{req.slot_id}_{client_id}_{time.time_ns()}"
        conn.execute(
            """INSERT INTO bookings
               (id,slot_id,client_tg_id,client_name,created_at,status)
               VALUES(%s,%s,%s,%s,%s,'active')""",
            (
                booking_id,
                req.slot_id,
                client_id,
                client_name,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()

        roster = conn.execute(
            """SELECT client_name FROM bookings
               WHERE slot_id=%s AND status='active'
               ORDER BY created_at""",
            (req.slot_id,),
        ).fetchall()
        roster_names = ", ".join(r["client_name"] for r in roster)
        capacity = slot["capacity"]
        slot_date, slot_time = slot["date"], slot["time"]
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    await notify_trainer(
        f"Новая запись: {client_name} — {slot_date} {slot_time}\n"
        f"Мест занято: {len(roster)}/{capacity}\n"
        f"{roster_names}"
    )
    return {"booking_id": booking_id}


@app.get("/api/my-bookings")
def my_bookings(init_data: str):
    user = validate_init_data(init_data)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT b.id,s.date,s.time,s.duration
               FROM bookings b JOIN slots s ON s.id=b.slot_id
               WHERE b.client_tg_id=%s AND b.status='active'
               ORDER BY s.date,s.time""",
            (user["id"],),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.delete("/api/bookings/{booking_id}")
async def cancel_booking(booking_id: str, req: CancelRequest):
    user = validate_init_data(req.init_data)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id=%s AND status='active'",
            (booking_id,),
        ).fetchone()
        if not row or row["client_tg_id"] != user["id"]:
            raise HTTPException(404, "booking not found")
        slot = conn.execute(
            "SELECT * FROM slots WHERE id=%s", (row["slot_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=%s",
            (booking_id,),
        )
        conn.commit()
    finally:
        conn.close()

    if slot:
        await notify_trainer(
            f"Отмена записи: {row['client_name']} — "
            f"{slot['date']} {slot['time']}"
        )
    return {"ok": True}


@app.get("/api/me")
def me(init_data: str):
    user = validate_init_data(init_data)
    conn = get_conn()
    try:
        profile = conn.execute(
            "SELECT * FROM client_profiles WHERE telegram_id=%s",
            (user["id"],),
        ).fetchone()
        today = date.today().isoformat()
        totals = conn.execute(
            """SELECT COALESCE(SUM(calories),0) calories,
                      COALESCE(SUM(protein),0) protein,
                      COALESCE(SUM(fat),0) fat,
                      COALESCE(SUM(carbs),0) carbs,
                      COUNT(*) entries
               FROM food_logs
               WHERE telegram_id=%s AND log_date=%s""",
            (user["id"], today),
        ).fetchone()
        return {
            "program": {
                "text": profile["program_text"] if profile else None,
                "updated_at": profile["updated_at"] if profile else None,
            },
            "kbju": {
                "calories": profile["calories"] if profile else None,
                "protein": profile["protein"] if profile else None,
                "fat": profile["fat"] if profile else None,
                "carbs": profile["carbs"] if profile else None,
                "updated_at": profile["updated_at"] if profile else None,
            },
            "today": dict(totals),
        }
    finally:
        conn.close()


@app.get("/api/slots/{slot_id}/roster")
def slot_roster(slot_id: str, init_data: str):
    require_trainer(init_data)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT client_name,client_tg_id
               FROM bookings WHERE slot_id=%s AND status='active'
               ORDER BY created_at""",
            (slot_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/session")
def session_info(init_data: str):
    user = validate_init_data(init_data)
    return {
        "telegram_id": user["id"],
        "first_name": user.get("first_name"),
        "username": user.get("username"),
        "is_trainer": user["id"] == TRAINER_TG_ID,
    }


@app.get("/api/admin/whoami")
def admin_whoami(init_data: str):
    try:
        user = validate_init_data(init_data)
    except HTTPException:
        return {"is_trainer": False}
    return {"is_trainer": user["id"] == TRAINER_TG_ID}


@app.get("/api/admin/clients")
def admin_clients(init_data: str):
    require_trainer(init_data)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT k.telegram_id,k.first_name,k.username,
                      p.program_text,p.calories,p.protein,p.fat,p.carbs
               FROM known_users k
               LEFT JOIN client_profiles p ON p.telegram_id=k.telegram_id
               WHERE k.telegram_id <> %s
               ORDER BY k.first_seen DESC""",
            (TRAINER_TG_ID,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/admin/clients/{client_id}/program")
def admin_program(client_id: int, req: ProgramUpdate):
    require_trainer(req.init_data)
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO client_profiles(telegram_id,program_text,updated_at)
               VALUES(%s,%s,%s)
               ON CONFLICT(telegram_id) DO UPDATE SET
               program_text=EXCLUDED.program_text,
               updated_at=EXCLUDED.updated_at""",
            (client_id, req.program_text.strip(), datetime.utcnow().isoformat()),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/admin/clients/{client_id}/kbju")
def admin_kbju(client_id: int, req: KbjuUpdate):
    require_trainer(req.init_data)
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO client_profiles
               (telegram_id,calories,protein,fat,carbs,updated_at)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(telegram_id) DO UPDATE SET
               calories=EXCLUDED.calories,protein=EXCLUDED.protein,
               fat=EXCLUDED.fat,carbs=EXCLUDED.carbs,
               updated_at=EXCLUDED.updated_at""",
            (
                client_id,
                req.calories,
                req.protein,
                req.fat,
                req.carbs,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


DAY_MAP = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def _valid_time(value):
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except ValueError:
        return False


@app.post("/api/admin/weekly")
def admin_weekly(req: WeeklyUpdate):
    require_trainer(req.init_data)
    days = [d.strip().lower() for d in req.days]
    times = [t.strip() for t in req.times]
    if not days or not times:
        raise HTTPException(400, "days and times are required")
    if any(d not in DAY_MAP for d in days):
        raise HTTPException(400, "invalid weekday")
    if any(not _valid_time(t) for t in times):
        raise HTTPException(400, "invalid time")

    conn = get_conn()
    try:
        for d in days:
            dow = DAY_MAP[d]
            for t in times:
                entry_id = f"{dow}_{t}"
                conn.execute(
                    """INSERT INTO weekly_template
                       (id,day_of_week,time,duration,capacity)
                       VALUES(%s,%s,%s,60,1)
                       ON CONFLICT(id) DO NOTHING""",
                    (entry_id, dow, t),
                )
        conn.commit()
    finally:
        conn.close()

    generate_slots()
    return {"ok": True}


@app.get("/api/admin/roster")
def admin_roster(date: str, init_data: str):
    require_trainer(init_data)
    conn = get_conn()
    try:
        slots = conn.execute(
            "SELECT * FROM slots WHERE date=%s ORDER BY time", (date,)
        ).fetchall()
        result = []
        for slot in slots:
            rows = conn.execute(
                """SELECT client_name FROM bookings
                   WHERE slot_id=%s AND status='active'
                   ORDER BY created_at""",
                (slot["id"],),
            ).fetchall()
            result.append(
                {
                    "time": slot["time"],
                    "capacity": slot["capacity"],
                    "booked": len(rows),
                    "names": [r["client_name"] for r in rows],
                }
            )
        return result
    finally:
        conn.close()


def generate_slots(horizon=21):
    conn = get_conn()
    try:
        template = conn.execute(
            "SELECT day_of_week,time,duration,capacity FROM weekly_template"
        ).fetchall()
        today = date.today()
        for offset in range(horizon):
            d = today + timedelta(days=offset)
            for item in template:
                if d.weekday() != item["day_of_week"]:
                    continue
                slot_id = f"{d.isoformat()}_{item['time']}"
                conn.execute(
                    """INSERT INTO slots
                       (id,date,time,duration,capacity)
                       VALUES(%s,%s,%s,%s,%s)
                       ON CONFLICT(id) DO NOTHING""",
                    (
                        slot_id,
                        d.isoformat(),
                        item["time"],
                        item["duration"],
                        item["capacity"],
                    ),
                )
        conn.commit()
    finally:
        conn.close()


async def notify_trainer(message):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": TRAINER_TG_ID, "text": message},
            )
            response.raise_for_status()
    except Exception:
        # Booking itself must not become a 500 just because Telegram
        # notification temporarily failed.
        return
