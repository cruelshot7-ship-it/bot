import os
import json
import hmac
import hashlib
import urllib.parse
import time
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Fitness Fraction Booking API")

DATABASE_URL = os.environ["DATABASE_URL"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
TRAINER_TG_ID = int(os.environ.get("TRAINER_TG_ID", "0"))
TRAINER_TZ = os.environ.get("TRAINER_TZ", "Europe/Minsk")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "telegram_booking_miniapp.html")


def get_conn():
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def parse_init_data(init_data: str) -> dict[str, Any]:
    if not init_data:
        raise HTTPException(401, "Откройте приложение из Telegram")

    try:
        pairs = urllib.parse.parse_qs(init_data, keep_blank_values=True)
        received_hash = pairs.get("hash", [""])[0]
        auth_date = int(pairs.get("auth_date", ["0"])[0])

        if not received_hash or not auth_date:
            raise ValueError

        if time.time() - auth_date > 86400:
            raise HTTPException(401, "Сессия Telegram устарела")

        data_check = "\n".join(
            f"{key}={values[0]}"
            for key, values in sorted(pairs.items())
            if key != "hash"
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()

        calculated = hmac.new(
            secret_key,
            data_check.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            raise HTTPException(401, "Недействительная Telegram-сессия")

        user = json.loads(pairs["user"][0])
        return user

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(401, f"Ошибка авторизации: {exc}")


def require_user(init_data: str) -> dict[str, Any]:
    user = parse_init_data(init_data)
    tg_id = int(user["id"])

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO known_users(
                    telegram_id,
                    first_name,
                    username
                )
                VALUES (%s,%s,%s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    first_name=EXCLUDED.first_name,
                    username=EXCLUDED.username
                """,
                (
                    tg_id,
                    user.get("first_name", ""),
                    user.get("username", ""),
                ),
            )
        conn.commit()

    return user


def require_trainer(init_data: str) -> dict[str, Any]:
    user = require_user(init_data)

    if int(user["id"]) != TRAINER_TG_ID:
        raise HTTPException(403, "Доступ только для тренера")

    return user


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS slots(
                    id TEXT PRIMARY KEY,
                    date DATE NOT NULL,
                    time TEXT NOT NULL,
                    duration INTEGER NOT NULL DEFAULT 60,
                    capacity INTEGER NOT NULL DEFAULT 1
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bookings(
                    id BIGSERIAL PRIMARY KEY,
                    slot_id TEXT NOT NULL
                        REFERENCES slots(id)
                        ON DELETE CASCADE,
                    client_tg_id BIGINT NOT NULL,
                    client_name TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    status TEXT NOT NULL DEFAULT 'active',
                    reminded_24h BOOLEAN NOT NULL DEFAULT FALSE,
                    reminded_2h BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS client_profiles(
                    telegram_id BIGINT PRIMARY KEY,
                    program_text TEXT NOT NULL DEFAULT '',
                    calories INTEGER,
                    protein INTEGER,
                    fat INTEGER,
                    carbs INTEGER,
                    weight_kg NUMERIC(6,2),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS known_users(
                    telegram_id BIGINT PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS food_logs(
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    log_date DATE NOT NULL,
                    calories INTEGER,
                    protein INTEGER,
                    fat INTEGER,
                    carbs INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS progress_logs(
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    log_date DATE NOT NULL,
                    weight_kg NUMERIC(6,2),
                    waist_cm NUMERIC(6,2),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS technique_reviews(
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    exercise TEXT NOT NULL DEFAULT '',
                    video_url TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS weekly_template(
                    id TEXT PRIMARY KEY,
                    day_of_week INTEGER NOT NULL,
                    time TEXT NOT NULL,
                    duration INTEGER NOT NULL DEFAULT 60,
                    capacity INTEGER NOT NULL DEFAULT 1
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS water_logs(
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    log_date DATE NOT NULL,
                    amount_ml INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS habit_logs(
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    log_date DATE NOT NULL,
                    habit TEXT NOT NULL,
                    value BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(telegram_id, log_date, habit)
                )
                """
            )

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS bookings_active_unique
                ON bookings(slot_id, client_tg_id)
                WHERE status='active'
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS bookings_slot_idx
                ON bookings(slot_id)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS bookings_client_idx
                ON bookings(client_tg_id, status)
                """
            )

        conn.commit()


def normalize_days(value):
    if isinstance(value, str):
        value = [
            x.strip()
            for x in value.replace(";", ",").split(",")
            if x.strip()
        ]

    aliases = {
        "понедельник": "пн",
        "вторник": "вт",
        "среда": "ср",
        "четверг": "чт",
        "пятница": "пт",
        "суббота": "сб",
        "воскресенье": "вс",
    }

    valid = {
        "пн": 0,
        "вт": 1,
        "ср": 2,
        "чт": 3,
        "пт": 4,
        "сб": 5,
        "вс": 6,
    }

    result = []

    for item in value or []:
        key = aliases.get(
            str(item).strip().lower(),
            str(item).strip().lower(),
        )

        if key not in valid:
            raise HTTPException(400, f"Неверный день: {item}")

        if key not in result:
            result.append(key)

    return result


def normalize_times(value):
    if isinstance(value, str):
        value = [
            x.strip()
            for x in value.replace(";", ",").split(",")
            if x.strip()
        ]

    result = []

    for item in value or []:
        text = str(item).strip()

        try:
            parsed = datetime.strptime(text, "%H:%M")
        except ValueError:
            raise HTTPException(
                400,
                f"Неверное время: {item}. Формат ЧЧ:ММ",
            )

        normalized = parsed.strftime("%H:%M")

        if normalized not in result:
            result.append(normalized)

    return result


def generate_slots(horizon=35):
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id,day_of_week,time,duration,capacity
                FROM weekly_template
                """
            )

            templates = cur.fetchall()

            for offset in range(horizon):
                current_date = date.today() + timedelta(days=offset)
                dow = current_date.weekday()

                for _, template_dow, tm, duration, capacity in templates:

                    if template_dow != dow:
                        continue

                    slot_id = f"{current_date.isoformat()}_{tm}"

                    cur.execute(
                        """
                        INSERT INTO slots(
                            id,
                            date,
                            time,
                            duration,
                            capacity
                        )
                        VALUES (%s,%s,%s,%s,%s)

                        ON CONFLICT(id) DO UPDATE SET
                            duration=EXCLUDED.duration,
                            capacity=EXCLUDED.capacity

                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM bookings b
                            WHERE b.slot_id=slots.id
                              AND b.status='active'
                        )
                        """,
                        (
                            slot_id,
                            current_date,
                            tm,
                            duration,
                            capacity,
                        ),
                    )

        conn.commit()


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
    program_text: str = Field(default="", max_length=20000)


class KbjuUpdate(BaseModel):
    init_data: str
    calories: int = Field(ge=500, le=10000)
    protein: int = Field(ge=0, le=1000)
    fat: int = Field(ge=0, le=500)
    carbs: int = Field(ge=0, le=1500)


class WeeklyUpdate(BaseModel):
    init_data: str
    days: list[str] | str = []
    times: list[str] | str = []
    duration: int = Field(default=60, ge=15, le=240)
    capacity: int = Field(default=1, ge=1, le=50)


class SlotAdminUpdate(BaseModel):
    init_data: str
    date: str
    time: str
    duration: int = Field(default=60, ge=15, le=240)
    capacity: int = Field(default=1, ge=1, le=50)


@app.on_event("startup")
def startup():
    last_error = None

    for _ in range(5):
        try:
            init_db()
            generate_slots()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(2)

    raise last_error


@app.get("/")
def index():
    return FileResponse(
        HTML_FILE,
        headers={
            "Cache-Control":
                "no-store, no-cache, must-revalidate, max-age=0"
        },
    )


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/session")
def session(init_data: str = Query(default="")):
    user = require_user(init_data)

    return {
        "ok": True,
        "user": user,
        "is_trainer": int(user["id"]) == TRAINER_TG_ID,
    }


@app.get("/api/me")
def me(init_data: str = Query(default="")):
    user = require_user(init_data)
    tg_id = int(user["id"])

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    program_text,
                    calories,
                    protein,
                    fat,
                    carbs,
                    weight_kg,
                    updated_at
                FROM client_profiles
                WHERE telegram_id=%s
                """,
                (tg_id,),
            )

            row = cur.fetchone()

    if not row:
        return {
            "telegram_id": tg_id,
            "program_text": "",
            "kbju": None,
        }

    return {
        "telegram_id": tg_id,
        "program_text": row[0],
        "kbju": {
            "calories": row[1],
            "protein": row[2],
            "fat": row[3],
            "carbs": row[4],
        },
        "weight_kg": (
            float(row[5])
            if row[5] is not None
            else None
        ),
        "updated_at": (
            row[6].isoformat()
            if row[6]
            else None
        ),
    }


@app.get("/api/slots")
def slots(week_start: str):
    try:
        start = date.fromisoformat(week_start)
    except ValueError:
        raise HTTPException(400, "Неверная дата недели")

    end = start + timedelta(days=6)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.id,
                    s.date,
                    s.time,
                    s.duration,
                    s.capacity,
                    COUNT(b.id)
                        FILTER (WHERE b.status='active') AS booked
                FROM slots s
                LEFT JOIN bookings b
                    ON b.slot_id=s.id
                WHERE s.date BETWEEN %s AND %s
                GROUP BY s.id
                ORDER BY s.date,s.time
                """,
                (start, end),
            )

            rows = cur.fetchall()

    result = {}

    for sid, d, tm, duration, capacity, booked in rows:
        result.setdefault(d.isoformat(), []).append(
            {
                "id": sid,
                "date": d.isoformat(),
                "time": tm,
                "duration": duration,
                "capacity": capacity,
                "booked": booked,
                "available": max(0, capacity - booked),
            }
        )

    return {
        "week_start": start.isoformat(),
        "days": result,
    }


@app.post("/api/slots")
def create_slot(payload: SlotCreate):
    require_trainer(payload.init_data)

    try:
        d = date.fromisoformat(payload.date)
        tm = datetime.strptime(
            payload.time,
            "%H:%M",
        ).strftime("%H:%M")
    except ValueError:
        raise HTTPException(
            400,
            "Неверная дата или время",
        )

    sid = f"{d.isoformat()}_{tm}"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slots(
                    id,date,time,duration,capacity
                )
                VALUES (%s,%s,%s,%s,%s)

                ON CONFLICT(id) DO UPDATE SET
                    duration=EXCLUDED.duration,
                    capacity=EXCLUDED.capacity
                """,
                (
                    sid,
                    d,
                    tm,
                    payload.duration,
                    payload.capacity,
                ),
            )

        conn.commit()

    return {
        "ok": True,
        "slot_id": sid,
    }


@app.post("/api/bookings")
def create_booking(payload: BookingRequest):
    user = require_user(payload.init_data)
    tg_id = int(user["id"])

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    date,
                    time,
                    duration,
                    capacity
                FROM slots
                WHERE id=%s
                FOR UPDATE
                """,
                (payload.slot_id,),
            )

            slot = cur.fetchone()

            if not slot:
                raise HTTPException(
                    404,
                    "Слот не найден",
                )

            cur.execute(
                """
                SELECT COUNT(*)
                FROM bookings
                WHERE slot_id=%s
                  AND status='active'
                """,
                (payload.slot_id,),
            )

            booked = cur.fetchone()[0]

            if booked >= slot[4]:
                raise HTTPException(
                    409,
                    "Слот уже заполнен",
                )

            cur.execute(
                """
                SELECT id
                FROM bookings
                WHERE slot_id=%s
                  AND client_tg_id=%s
                  AND status='active'
                """,
                (
                    payload.slot_id,
                    tg_id,
                ),
            )

            if cur.fetchone():
                raise HTTPException(
                    409,
                    "Вы уже записаны на этот слот",
                )

            cur.execute(
                """
                INSERT INTO bookings(
                    slot_id,
                    client_tg_id,
                    client_name
                )
                VALUES (%s,%s,%s)
                RETURNING id
                """,
                (
                    payload.slot_id,
                    tg_id,
                    user.get("first_name", ""),
                ),
            )

            booking_id = cur.fetchone()[0]

        conn.commit()

    return {
        "ok": True,
        "booking_id": booking_id,
    }


@app.get("/api/my-bookings")
def my_bookings(
    init_data: str = Query(default=""),
):
    user = require_user(init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    b.id,
                    s.id,
                    s.date,
                    s.time,
                    s.duration,
                    b.status
                FROM bookings b
                JOIN slots s
                    ON s.id=b.slot_id
                WHERE b.client_tg_id=%s
                  AND b.status='active'
                ORDER BY s.date,s.time
                """,
                (int(user["id"]),),
            )

            rows = cur.fetchall()

    return {
        "bookings": [
            {
                "id": r[0],
                "slot_id": r[1],
                "date": r[2].isoformat(),
                "time": r[3],
                "duration": r[4],
                "status": r[5],
            }
            for r in rows
        ]
    }


@app.delete("/api/bookings/{booking_id}")
def cancel_booking(
    booking_id: int,
    init_data: str = Query(default=""),
):
    user = require_user(init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bookings
                SET status='cancelled'
                WHERE id=%s
                  AND client_tg_id=%s
                  AND status='active'
                RETURNING id
                """,
                (
                    booking_id,
                    int(user["id"]),
                ),
            )

            row = cur.fetchone()

        conn.commit()

    if not row:
        raise HTTPException(
            404,
            "Запись не найдена",
        )

    return {"ok": True}


@app.get("/api/slots/{slot_id}/roster")
def slot_roster(
    slot_id: str,
    init_data: str = Query(default=""),
):
    require_trainer(init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    b.id,
                    b.client_tg_id,
                    b.client_name,
                    b.created_at
                FROM bookings b
                WHERE b.slot_id=%s
                  AND b.status='active'
                ORDER BY b.created_at
                """,
                (slot_id,),
            )

            rows = cur.fetchall()

    return {
        "slot_id": slot_id,
        "roster": [
            {
                "booking_id": r[0],
                "telegram_id": r[1],
                "name": r[2],
                "created_at": r[3].isoformat(),
            }
            for r in rows
        ],
    }


@app.get("/api/admin/whoami")
def admin_whoami(
    init_data: str = Query(default=""),
):
    user = require_trainer(init_data)

    return {
        "ok": True,
        "telegram_id": int(user["id"]),
        "is_trainer": True,
    }


@app.get("/api/admin/clients")
def admin_clients(
    init_data: str = Query(default=""),
):
    require_trainer(init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ku.telegram_id,
                    ku.first_name,
                    ku.username,
                    cp.program_text,
                    cp.calories,
                    cp.protein,
                    cp.fat,
                    cp.carbs,
                    cp.weight_kg
                FROM known_users ku
                LEFT JOIN client_profiles cp
                    ON cp.telegram_id=ku.telegram_id
                WHERE ku.telegram_id <> %s
                ORDER BY LOWER(ku.first_name),
                         ku.telegram_id
                """,
                (TRAINER_TG_ID,),
            )

            rows = cur.fetchall()

    return {
        "clients": [
            {
                "telegram_id": r[0],
                "first_name": r[1],
                "username": r[2],
                "program_text": r[3] or "",
                "kbju": {
                    "calories": r[4],
                    "protein": r[5],
                    "fat": r[6],
                    "carbs": r[7],
                }
                if r[4] is not None
                else None,
                "weight_kg": (
                    float(r[8])
                    if r[8] is not None
                    else None
                ),
            }
            for r in rows
        ]
    }


@app.post("/api/admin/clients/{client_id}/program")
def admin_program(
    client_id: int,
    payload: ProgramUpdate,
):
    require_trainer(payload.init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO client_profiles(
                    telegram_id,
                    program_text
                )
                VALUES (%s,%s)

                ON CONFLICT(telegram_id)
                DO UPDATE SET
                    program_text=EXCLUDED.program_text,
                    updated_at=NOW()
                """,
                (
                    client_id,
                    payload.program_text,
                ),
            )

        conn.commit()

    return {"ok": True}


@app.post("/api/admin/clients/{client_id}/kbju")
def admin_kbju(
    client_id: int,
    payload: KbjuUpdate,
):
    require_trainer(payload.init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO client_profiles(
                    telegram_id,
                    calories,
                    protein,
                    fat,
                    carbs
                )
                VALUES (%s,%s,%s,%s,%s)

                ON CONFLICT(telegram_id)
                DO UPDATE SET
                    calories=EXCLUDED.calories,
                    protein=EXCLUDED.protein,
                    fat=EXCLUDED.fat,
                    carbs=EXCLUDED.carbs,
                    updated_at=NOW()
                """,
                (
                    client_id,
                    payload.calories,
                    payload.protein,
                    payload.fat,
                    payload.carbs,
                ),
            )

        conn.commit()

    return {"ok": True}


@app.get("/api/admin/weekly")
def get_weekly(
    init_data: str = Query(default=""),
):
    require_trainer(init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    day_of_week,
                    time,
                    duration,
                    capacity
                FROM weekly_template
                ORDER BY day_of_week,time
                """
            )

            rows = cur.fetchall()

    names = [
        "пн",
        "вт",
        "ср",
        "чт",
        "пт",
        "сб",
        "вс",
    ]

    return {
        "templates": [
            {
                "id": r[0],
                "day": names[r[1]],
                "day_of_week": r[1],
                "time": r[2],
                "duration": r[3],
                "capacity": r[4],
            }
            for r in rows
        ]
    }


@app.post("/api/admin/weekly")
def save_weekly(payload: WeeklyUpdate):
    require_trainer(payload.init_data)

    days = normalize_days(payload.days)
    times = normalize_times(payload.times)

    if not days or not times:
        raise HTTPException(
            400,
            "Укажите хотя бы один день и одно время",
        )

    day_map = {
        "пн": 0,
        "вт": 1,
        "ср": 2,
        "чт": 3,
        "пт": 4,
        "сб": 5,
        "вс": 6,
    }

    wanted = {
        (day_map[d], t)
        for d in days
        for t in times
    }

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id,day_of_week,time
                FROM weekly_template
                """
            )

            existing = cur.fetchall()

            for template_id, dow, tm in existing:

                if (dow, tm) not in wanted:
                    cur.execute(
                        """
                        DELETE FROM weekly_template
                        WHERE id=%s
                        """,
                        (template_id,),
                    )

            for dow, tm in wanted:

                template_id = f"{dow}_{tm}"

                cur.execute(
                    """
                    INSERT INTO weekly_template(
                        id,
                        day_of_week,
                        time,
                        duration,
                        capacity
                    )
                    VALUES (%s,%s,%s,%s,%s)

                    ON CONFLICT(id)
                    DO UPDATE SET
                        duration=EXCLUDED.duration,
                        capacity=EXCLUDED.capacity
                    """,
                    (
                        template_id,
                        dow,
                        tm,
                        payload.duration,
                        payload.capacity,
                    ),
                )

        conn.commit()

    generate_slots(35)

    return {
        "ok": True,
        "templates": len(wanted),
    }


@app.delete("/api/admin/weekly/{template_id}")
def delete_weekly(
    template_id: str,
    init_data: str = Query(default=""),
):
    require_trainer(init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT day_of_week,time
                FROM weekly_template
                WHERE id=%s
                """,
                (template_id,),
            )

            row = cur.fetchone()

            if not row:
                raise HTTPException(
                    404,
                    "Шаблон не найден",
                )

            dow, tm = row

            cur.execute(
                """
                DELETE FROM weekly_template
                WHERE id=%s
                """,
                (template_id,),
            )

            pg_dow = (dow + 1) % 7

            cur.execute(
                """
                DELETE FROM slots s
                WHERE EXTRACT(DOW FROM s.date)::int=%s
                  AND s.time=%s
                  AND s.date>=CURRENT_DATE
                  AND NOT EXISTS (
                      SELECT 1
                      FROM bookings b
                      WHERE b.slot_id=s.id
                        AND b.status='active'
                  )
                """,
                (
                    pg_dow,
                    tm,
                ),
            )

        conn.commit()

    return {"ok": True}


@app.post("/api/admin/slots")
def admin_create_slot(
    payload: SlotAdminUpdate,
):
    require_trainer(payload.init_data)

    try:
        d = date.fromisoformat(payload.date)
        tm = datetime.strptime(
            payload.time,
            "%H:%M",
        ).strftime("%H:%M")
    except ValueError:
        raise HTTPException(
            400,
            "Неверная дата или время",
        )

    sid = f"{d.isoformat()}_{tm}"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slots(
                    id,
                    date,
                    time,
                    duration,
                    capacity
                )
                VALUES (%s,%s,%s,%s,%s)

                ON CONFLICT(id)
                DO UPDATE SET
                    duration=EXCLUDED.duration,
                    capacity=EXCLUDED.capacity
                """,
                (
                    sid,
                    d,
                    tm,
                    payload.duration,
                    payload.capacity,
                ),
            )

        conn.commit()

    return {
        "ok": True,
        "slot_id": sid,
    }


@app.patch("/api/admin/slots/{slot_id}")
def admin_update_slot(
    slot_id: str,
    payload: SlotAdminUpdate,
):
    require_trainer(payload.init_data)

    try:
        new_date = date.fromisoformat(payload.date)
        new_time = datetime.strptime(
            payload.time,
            "%H:%M",
        ).strftime("%H:%M")
    except ValueError:
        raise HTTPException(
            400,
            "Неверная дата или время",
        )

    new_id = f"{new_date.isoformat()}_{new_time}"

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id
                FROM slots
                WHERE id=%s
                FOR UPDATE
                """,
                (slot_id,),
            )

            if not cur.fetchone():
                raise HTTPException(
                    404,
                    "Слот не найден",
                )

            cur.execute(
                """
                SELECT 1
                FROM bookings
                WHERE slot_id=%s
                  AND status='active'
                """,
                (slot_id,),
            )

            has_bookings = cur.fetchone() is not None

            if new_id != slot_id and has_bookings:
                raise HTTPException(
                    409,
                    "Нельзя перенести слот с активными записями",
                )

            if new_id != slot_id:
                cur.execute(
                    """
                    DELETE FROM slots
                    WHERE id=%s
                    """,
                    (slot_id,),
                )

            cur.execute(
                """
                INSERT INTO slots(
                    id,
                    date,
                    time,
                    duration,
                    capacity
                )
                VALUES (%s,%s,%s,%s,%s)

                ON CONFLICT(id)
                DO UPDATE SET
                    duration=EXCLUDED.duration,
                    capacity=EXCLUDED.capacity
                """,
                (
                    new_id,
                    new_date,
                    new_time,
                    payload.duration,
                    payload.capacity,
                ),
            )

        conn.commit()

    return {
        "ok": True,
        "slot_id": new_id,
    }


@app.delete("/api/admin/slots/{slot_id}")
def admin_delete_slot(
    slot_id: str,
    init_data: str = Query(default=""),
):
    require_trainer(init_data)

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*)
                FROM bookings
                WHERE slot_id=%s
                  AND status='active'
                """,
                (slot_id,),
            )

            if cur.fetchone()[0] > 0:
                raise HTTPException(
                    409,
                    "Нельзя удалить слот с активной записью",
                )

            cur.execute(
                """
                DELETE FROM slots
                WHERE id=%s
                RETURNING id
                """,
                (slot_id,),
            )

            if not cur.fetchone():
                raise HTTPException(
                    404,
                    "Слот не найден",
                )

        conn.commit()

    return {"ok": True}


@app.get("/api/admin/roster")
def admin_roster(
    date_value: str = Query(default=""),
    init_data: str = Query(default=""),
):
    require_trainer(init_data)

    target = date_value or date.today().isoformat()

    try:
        d = date.fromisoformat(target)
    except ValueError:
        raise HTTPException(
            400,
            "Неверная дата",
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.id,
                    s.time,
                    s.duration,
                    s.capacity,
                    b.id,
                    b.client_tg_id,
                    b.client_name,
                    b.status
                FROM slots s
                LEFT JOIN bookings b
                    ON b.slot_id=s.id
                    AND b.status='active'
                WHERE s.date=%s
                ORDER BY s.time,b.created_at
                """,
                (d,),
            )

            rows = cur.fetchall()

    result = {}

    for r in rows:
        sid = r[0]

        result.setdefault(
            sid,
            {
                "slot_id": sid,
                "time": r[1],
                "duration": r[2],
                "capacity": r[3],
                "bookings": [],
            },
        )

        if r[4] is not None:
            result[sid]["bookings"].append(
                {
                    "booking_id": r[4],
                    "telegram_id": r[5],
                    "name": r[6],
                    "status": r[7],
                }
            )

    return {
        "date": d.isoformat(),
        "slots": list(result.values()),
    }
