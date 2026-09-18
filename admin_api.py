"""
Trainer-only HTTP endpoints for the Mini App's "Тренер" tab: client list,
program/КБЖУ assignment, weekly schedule presets, roster. Every write here
does exactly what /admin (the bot's button panel) and the text commands
already do — same tables, same validation — just reachable from the app's
UI instead of the bot's chat.

Registers its routes onto the FastAPI app already created in booking_api.
Import this module ONCE, after booking_api is fully loaded, so booking_api
never has to import admin_api or schedule_handlers itself (that would be
a circular import — schedule_handlers already imports get_conn from
booking_api). In main.py:

    from booking_api import app as fastapi_app
    import admin_api  # noqa: F401 -- registers /api/admin/* routes
"""
from datetime import datetime

from fastapi import HTTPException
from pydantic import BaseModel

from booking_api import app, get_conn, validate_init_data, TRAINER_TG_ID
from schedule_handlers import generate_upcoming_slots, DAY_MAP, HORIZON_DAYS


def _require_trainer(init_data: str) -> dict:
    user = validate_init_data(init_data)
    if user.get("id") != TRAINER_TG_ID:
        raise HTTPException(403, "trainer only")
    return user


@app.get("/api/admin/whoami")
def admin_whoami(init_data: str):
    """Lets the frontend decide whether to show the Тренер tab at all.
    Never raises — worst case the tab just doesn't appear; every actual
    write endpoint below still checks TRAINER_TG_ID independently."""
    try:
        user = validate_init_data(init_data)
    except HTTPException:
        return {"is_trainer": False}
    return {"is_trainer": user.get("id") == TRAINER_TG_ID}


@app.get("/api/admin/clients")
def admin_list_clients(init_data: str):
    _require_trainer(init_data)
    conn = get_conn()
    rows = conn.execute(
        "SELECT k.telegram_id, k.first_name, k.username, p.program_text, "
        "p.calories, p.protein, p.fat, p.carbs "
        "FROM known_users k LEFT JOIN client_profiles p ON p.telegram_id = k.telegram_id "
        "ORDER BY k.first_seen DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class ProgramUpdate(BaseModel):
    init_data: str
    program_text: str


@app.post("/api/admin/clients/{client_id}/program")
def admin_set_program(client_id: int, req: ProgramUpdate):
    _require_trainer(req.init_data)
    conn = get_conn()
    conn.execute(
        "INSERT INTO client_profiles (telegram_id, program_text, updated_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (telegram_id) DO UPDATE SET program_text = EXCLUDED.program_text, "
        "updated_at = EXCLUDED.updated_at",
        (client_id, req.program_text, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


class KbjuUpdate(BaseModel):
    init_data: str
    calories: int
    protein: int
    fat: int
    carbs: int


@app.post("/api/admin/clients/{client_id}/kbju")
def admin_set_kbju(client_id: int, req: KbjuUpdate):
    _require_trainer(req.init_data)
    conn = get_conn()
    conn.execute(
        "INSERT INTO client_profiles (telegram_id, calories, protein, fat, carbs, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (telegram_id) DO UPDATE SET calories = EXCLUDED.calories, "
        "protein = EXCLUDED.protein, fat = EXCLUDED.fat, carbs = EXCLUDED.carbs, "
        "updated_at = EXCLUDED.updated_at",
        (client_id, req.calories, req.protein, req.fat, req.carbs, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


class WeeklyUpdate(BaseModel):
    init_data: str
    days: list[str]
    times: list[str]


@app.post("/api/admin/weekly")
async def admin_set_weekly(req: WeeklyUpdate):
    _require_trainer(req.init_data)
    unknown = [d for d in req.days if d not in DAY_MAP]
    if unknown:
        raise HTTPException(400, f"unknown days: {', '.join(unknown)}")
    conn = get_conn()
    for d in req.days:
        dow = DAY_MAP[d]
        for t in req.times:
            entry_id = f"{dow}_{t}"
            conn.execute(
                "INSERT INTO weekly_template (id, day_of_week, time, duration, capacity) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET duration = EXCLUDED.duration, "
                "capacity = EXCLUDED.capacity",
                (entry_id, dow, t, 60, 1),
            )
    conn.commit()
    conn.close()
    await generate_upcoming_slots()
    return {"ok": True, "horizon_days": HORIZON_DAYS}


@app.get("/api/admin/roster")
def admin_roster(date: str, init_data: str):
    _require_trainer(init_data)
    conn = get_conn()
    slots = conn.execute(
        "SELECT * FROM slots WHERE date = %s ORDER BY time", (date,)
    ).fetchall()
    result = []
    for s in slots:
        names = conn.execute(
            "SELECT client_name FROM bookings WHERE slot_id = %s AND status = 'active'",
            (s["id"],),
        ).fetchall()
        result.append({
            "time": s["time"], "capacity": s["capacity"],
            "booked": len(names), "names": [n["client_name"] for n in names],
        })
    conn.close()
    return result
