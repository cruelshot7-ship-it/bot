import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

BASE = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(BASE / "discipline.db"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TRAINER_TG_ID = int(os.getenv("TRAINER_TG_ID", "8144320404"))
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "8000"))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discipline")

app = FastAPI(title="Discipline Fitness API", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


def db():
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            role TEXT NOT NULL DEFAULT 'client', water_goal INTEGER NOT NULL DEFAULT 2500,
            protein REAL, fat REAL, carbs REAL, calories REAL, coach_note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, starts_at TEXT NOT NULL,
            duration_min INTEGER NOT NULL DEFAULT 60, title TEXT NOT NULL DEFAULT 'Персональная тренировка',
            capacity INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open'
        );
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
            tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
            UNIQUE(slot_id,tg_id)
        );
        CREATE TABLE IF NOT EXISTS programs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            title TEXT NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS workouts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            title TEXT NOT NULL, performed_at TEXT NOT NULL, duration_min INTEGER, notes TEXT, data_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS training_days(
            id INTEGER PRIMARY KEY AUTOINCREMENT, program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
            day_order INTEGER NOT NULL, title TEXT NOT NULL, weekday TEXT, body TEXT NOT NULL DEFAULT '', exercises_json TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(program_id, day_order)
        );
        CREATE TABLE IF NOT EXISTS day_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT, day_id INTEGER NOT NULL REFERENCES training_days(id) ON DELETE CASCADE, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            completed INTEGER NOT NULL DEFAULT 0, load_feel INTEGER, energy INTEGER, soreness INTEGER, pain INTEGER, comment TEXT DEFAULT '', created_at TEXT NOT NULL,
            UNIQUE(day_id,tg_id)
        );
        CREATE TABLE IF NOT EXISTS water(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            amount INTEGER NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS habits(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            title TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS habit_done(
            id INTEGER PRIMARY KEY AUTOINCREMENT, habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            day TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(habit_id,day)
        );
        CREATE TABLE IF NOT EXISTS measurements(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            weight REAL, waist REAL, chest REAL, hips REAL, thigh REAL, arm REAL, body_fat REAL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS progress_photos(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            caption TEXT, mime TEXT NOT NULL, data BLOB NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            week_start TEXT NOT NULL, weight REAL, adherence INTEGER, energy INTEGER, sleep INTEGER,
            comment TEXT, status TEXT NOT NULL DEFAULT 'submitted', created_at TEXT NOT NULL,
            UNIQUE(tg_id,week_start)
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            kind TEXT NOT NULL, text TEXT NOT NULL, remind_at TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS exercises(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, muscle TEXT
        );
        """)
        c.execute("INSERT OR IGNORE INTO users(tg_id,role,first_name,created_at) VALUES(?,?,?,?,?)", (TRAINER_TG_ID,"trainer","Тренер",now())) if False else None
        c.execute("INSERT OR IGNORE INTO users(tg_id,role,first_name,created_at) VALUES(?,?,?,?)", (TRAINER_TG_ID,"trainer","Евгений",now()))
        c.commit()


def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def day(): return datetime.now(timezone.utc).date().isoformat()
def rowdict(r): return dict(r) if r else None


def ensure_user(tg_id: int, username="", first_name="", last_name=""):
    role = "trainer" if tg_id == TRAINER_TG_ID else "client"
    with closing(db()) as c:
        c.execute("""INSERT INTO users(tg_id,username,first_name,last_name,role,created_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_name=excluded.last_name,role=excluded.role""",
                  (tg_id, username, first_name, last_name, role, now()))
        c.commit()


def validate_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        raise HTTPException(401, "Telegram авторизация отсутствует")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise HTTPException(401, "Некорректный initData")
    auth_date = int(pairs.get("auth_date", "0"))
    if time.time() - auth_date > 86400:
        raise HTTPException(401, "Сессия Telegram устарела")
    data_check = "\n".join(f"{k}={v}" for k,v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise HTTPException(401, "Неверная подпись Telegram")
    user = json.loads(pairs.get("user", "{}"))
    if not user.get("id"):
        raise HTTPException(401, "Пользователь Telegram не найден")
    ensure_user(user["id"], user.get("username",""), user.get("first_name",""), user.get("last_name",""))
    return user


def current_user(x_telegram_init_data: Optional[str]):
    u = validate_init_data(x_telegram_init_data or "")
    with closing(db()) as c:
        r = c.execute("SELECT * FROM users WHERE tg_id=?", (u["id"],)).fetchone()
    return rowdict(r)


def require_trainer(user):
    if user["role"] != "trainer": raise HTTPException(403, "Только для тренера")


class BookingIn(BaseModel): slot_id: int
class WaterIn(BaseModel): amount: int = Field(ge=50, le=2000)
class HabitIn(BaseModel): title: str = Field(min_length=1, max_length=100)
class MeasurementIn(BaseModel):
    weight: Optional[float]=None; waist: Optional[float]=None; chest: Optional[float]=None; hips: Optional[float]=None; thigh: Optional[float]=None; arm: Optional[float]=None; body_fat: Optional[float]=None
class ReportIn(BaseModel):
    weight: Optional[float]=None; adherence: Optional[int]=Field(default=None,ge=0,le=100); energy: Optional[int]=Field(default=None,ge=1,le=10); sleep: Optional[int]=Field(default=None,ge=1,le=10); comment: str=""
class SlotIn(BaseModel): starts_at: str; duration_min: int=Field(default=60,ge=15,le=240); title: str="Персональная тренировка"; capacity: int=Field(default=1,ge=1,le=20)
class ProgramIn(BaseModel): tg_id: int; title: str; body: str
class NutritionIn(BaseModel): tg_id: int; calories: Optional[float]=None; protein: Optional[float]=None; fat: Optional[float]=None; carbs: Optional[float]=None; water_goal: Optional[int]=Field(default=None,ge=500,le=10000)
class WorkoutIn(BaseModel): title: str; performed_at: str; duration_min: Optional[int]=None; notes: str=""; exercises: list[dict]=[]
class TrainingDayIn(BaseModel):
    tg_id: int; day_order: int = Field(ge=1,le=14); title: str; weekday: str = ""; body: str = ""; exercises: list[dict] = []
class DayReportIn(BaseModel):
    completed: bool = False; load_feel: Optional[int] = Field(default=None,ge=1,le=10); energy: Optional[int] = Field(default=None,ge=1,le=10); soreness: Optional[int] = Field(default=None,ge=1,le=10); pain: Optional[int] = Field(default=None,ge=0,le=10); comment: str = ""
class NotificationIn(BaseModel): tg_id: int; text: str; remind_at: str; kind: str="custom"

init_db()

@app.get("/")
def root(): return FileResponse(BASE/"index.html")
@app.get("/health")
def health(): return {"ok":True,"service":"discipline","time":now()}

@app.get("/api/me")
def me(x_telegram_init_data: Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); return {"user":u}

@app.get("/api/slots")
def slots(x_telegram_init_data: Optional[str]=Header(default=None), include_closed: bool=False):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        q="""SELECT s.*, COUNT(CASE WHEN b.status='active' THEN 1 END) booked,
        MAX(CASE WHEN b.tg_id=? AND b.status='active' THEN b.id END) my_booking_id
        FROM slots s LEFT JOIN bookings b ON b.slot_id=s.id WHERE s.starts_at>=?"""
        args=[u["tg_id"],now()]
        if not include_closed: q += " AND s.status='open'"
        q += " GROUP BY s.id ORDER BY s.starts_at"
        out=[]
        for r in c.execute(q,args):
            d=rowdict(r); d["free"] = d["status"]=="open" and d["booked"] < d["capacity"]; out.append(d)
    return out

@app.post("/api/bookings")
def book(x: BookingIn, x_telegram_init_data: Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        s=c.execute("SELECT * FROM slots WHERE id=?",(x.slot_id,)).fetchone()
        if not s or s["status"]!="open": raise HTTPException(400,"Слот недоступен")
        booked=c.execute("SELECT COUNT(*) n FROM bookings WHERE slot_id=? AND status='active'",(x.slot_id,)).fetchone()["n"]
        if booked>=s["capacity"]: raise HTTPException(409,"Слот уже заполнен")
        existing=c.execute("SELECT id FROM bookings WHERE slot_id=? AND tg_id=? AND status='active'",(x.slot_id,u["tg_id"])).fetchone()
        if existing: return {"ok":True,"id":existing["id"]}
        cur=c.execute("INSERT INTO bookings(slot_id,tg_id,created_at) VALUES(?,?,?)",(x.slot_id,u["tg_id"],now()))
        c.commit(); bid=cur.lastrowid
    return {"ok":True,"id":bid}

@app.delete("/api/bookings/{booking_id}")
def cancel(booking_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        r=c.execute("SELECT * FROM bookings WHERE id=?",(booking_id,)).fetchone()
        if not r or (r["tg_id"]!=u["tg_id"] and u["role"]!="trainer"): raise HTTPException(404,"Запись не найдена")
        c.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(booking_id,)); c.commit()
    return {"ok":True}

@app.get("/api/my-bookings")
def my_bookings(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        return [rowdict(r) for r in c.execute("SELECT b.*,s.starts_at,s.duration_min,s.title FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.tg_id=? AND b.status='active' ORDER BY s.starts_at",(u["tg_id"],))]

@app.get("/api/program")
def program(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        p=c.execute("SELECT * FROM programs WHERE tg_id=? AND active=1 ORDER BY updated_at DESC LIMIT 1",(u["tg_id"],)).fetchone()
    return rowdict(p) or {"title":"Программа пока не назначена","body":"Тренер добавит её здесь."}

@app.get("/api/nutrition")
def nutrition(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    return {k:u[k] for k in ["calories","protein","fat","carbs","water_goal"]}

@app.get("/api/water")
def get_water(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        total=c.execute("SELECT COALESCE(SUM(amount),0) n FROM water WHERE tg_id=? AND day=?",(u["tg_id"],day())).fetchone()["n"]
        rows=[rowdict(r) for r in c.execute("SELECT * FROM water WHERE tg_id=? AND day=? ORDER BY id DESC",(u["tg_id"],day()))]
    return {"total":total,"goal":u["water_goal"],"items":rows}

@app.post("/api/water")
def add_water(x:WaterIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        c.execute("INSERT INTO water(tg_id,amount,day,created_at) VALUES(?,?,?,?)",(u["tg_id"],x.amount,day(),now())); c.commit()
    return get_water(x_telegram_init_data)

@app.get("/api/habits")
def get_habits(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        return [dict(rowdict(r),done=bool(r["done"])) for r in c.execute("""SELECT h.*,CASE WHEN hd.id IS NULL THEN 0 ELSE 1 END done FROM habits h LEFT JOIN habit_done hd ON hd.habit_id=h.id AND hd.day=? WHERE h.tg_id=? AND h.active=1 ORDER BY h.id""",(day(),u["tg_id"]))]

@app.post("/api/habits")
def add_habit(x:HabitIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        cur=c.execute("INSERT INTO habits(tg_id,title) VALUES(?,?)",(u["tg_id"],x.title.strip())); c.commit()
    return {"ok":True,"id":cur.lastrowid}

@app.post("/api/habits/{habit_id}/complete")
def complete_habit(habit_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        h=c.execute("SELECT * FROM habits WHERE id=? AND tg_id=?",(habit_id,u["tg_id"])).fetchone()
        if not h: raise HTTPException(404,"Привычка не найдена")
        d=c.execute("SELECT id FROM habit_done WHERE habit_id=? AND day=?",(habit_id,day())).fetchone()
        if d: c.execute("DELETE FROM habit_done WHERE id=?",(d["id"],)); done=False
        else: c.execute("INSERT INTO habit_done(habit_id,day,created_at) VALUES(?,?,?)",(habit_id,day(),now())); done=True
        c.commit()
    return {"ok":True,"done":done}

@app.get("/api/measurements")
def get_measurements(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c: return [rowdict(r) for r in c.execute("SELECT * FROM measurements WHERE tg_id=? ORDER BY created_at DESC LIMIT 30",(u["tg_id"],))]

@app.post("/api/measurements")
def add_measurement(x:MeasurementIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        c.execute("INSERT INTO measurements(tg_id,weight,waist,chest,hips,thigh,arm,body_fat,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(u["tg_id"],x.weight,x.waist,x.chest,x.hips,x.thigh,x.arm,x.body_fat,now())); c.commit()
    return {"ok":True}

@app.post("/api/photos")
async def upload_photo(caption:str="",file:UploadFile=File(...),x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    data=await file.read()
    if len(data)>8*1024*1024: raise HTTPException(413,"Фото больше 8 МБ")
    if not (file.content_type or "").startswith("image/"): raise HTTPException(400,"Нужен файл изображения")
    with closing(db()) as c:
        cur=c.execute("INSERT INTO progress_photos(tg_id,caption,mime,data,created_at) VALUES(?,?,?,?,?)",(u["tg_id"],caption,file.content_type,data,now())); c.commit()
    return {"ok":True,"id":cur.lastrowid}

@app.get("/api/photos")
def photos(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c: return [dict(rowdict(r),url=f"/api/photos/{r['id']}") for r in c.execute("SELECT id,tg_id,caption,mime,created_at FROM progress_photos WHERE tg_id=? ORDER BY created_at DESC",(u["tg_id"],))]

@app.get("/api/photos/{photo_id}")
def photo(photo_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c: r=c.execute("SELECT * FROM progress_photos WHERE id=? AND tg_id=?",(photo_id,u["tg_id"])).fetchone()
    if not r: raise HTTPException(404,"Фото не найдено")
    from fastapi.responses import Response
    return Response(r["data"],media_type=r["mime"])

@app.get("/api/program/days")
def program_days(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        p=c.execute("SELECT id FROM programs WHERE tg_id=? AND active=1 ORDER BY updated_at DESC LIMIT 1",(u["tg_id"],)).fetchone()
        if not p: return []
        rows=[]
        for r in c.execute("SELECT * FROM training_days WHERE program_id=? AND active=1 ORDER BY day_order",(p["id"],)):
            d=rowdict(r); d["exercises"]=json.loads(d.pop("exercises_json") or "[]")
            rep=c.execute("SELECT * FROM day_reports WHERE day_id=? AND tg_id=?",(d["id"],u["tg_id"])).fetchone()
            d["report"]=rowdict(rep)
            rows.append(d)
    return rows

@app.post("/api/program/days/{day_id}/report")
def save_day_report(day_id:int,x:DayReportIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        exists=c.execute("SELECT id FROM training_days WHERE id=?",(day_id,)).fetchone()
        if not exists: raise HTTPException(404,"День программы не найден")
        c.execute("""INSERT INTO day_reports(day_id,tg_id,completed,load_feel,energy,soreness,pain,comment,created_at) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(day_id,tg_id) DO UPDATE SET completed=excluded.completed,load_feel=excluded.load_feel,energy=excluded.energy,soreness=excluded.soreness,pain=excluded.pain,comment=excluded.comment,created_at=excluded.created_at""",(day_id,u["tg_id"],int(x.completed),x.load_feel,x.energy,x.soreness,x.pain,x.comment,now())); c.commit()
    return {"ok":True}

@app.get("/api/workouts")
def workouts(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        rows=[]
        for r in c.execute("SELECT * FROM workouts WHERE tg_id=? ORDER BY performed_at DESC LIMIT 50",(u["tg_id"],)):
            d=rowdict(r); d["exercises"]=json.loads(d.pop("data_json") or "[]"); rows.append(d)
    return rows

@app.post("/api/workouts")
def add_workout(x:WorkoutIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c:
        cur=c.execute("INSERT INTO workouts(tg_id,title,performed_at,duration_min,notes,data_json) VALUES(?,?,?,?,?,?)",(u["tg_id"],x.title,x.performed_at,x.duration_min,x.notes,json.dumps(x.exercises,ensure_ascii=False))); c.commit()
    return {"ok":True,"id":cur.lastrowid}

@app.get("/api/reports")
def reports(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data)
    with closing(db()) as c: return [rowdict(r) for r in c.execute("SELECT * FROM reports WHERE tg_id=? ORDER BY week_start DESC LIMIT 20",(u["tg_id"],))]

@app.post("/api/reports")
def add_report(x:ReportIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); ws=(datetime.now(timezone.utc).date()-timedelta(days=datetime.now(timezone.utc).weekday())).isoformat()
    with closing(db()) as c:
        c.execute("""INSERT INTO reports(tg_id,week_start,weight,adherence,energy,sleep,comment,created_at) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(tg_id,week_start) DO UPDATE SET weight=excluded.weight,adherence=excluded.adherence,energy=excluded.energy,sleep=excluded.sleep,comment=excluded.comment,created_at=excluded.created_at""",(u["tg_id"],ws,x.weight,x.adherence,x.energy,x.sleep,x.comment,now())); c.commit()
    return {"ok":True,"week_start":ws}

# Trainer API
@app.get("/api/trainer/clients")
def trainer_clients(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        return [rowdict(r) for r in c.execute("SELECT u.*, (SELECT COUNT(*) FROM bookings b WHERE b.tg_id=u.tg_id AND b.status='active') bookings FROM users u WHERE u.role='client' ORDER BY u.first_name,u.last_name")]

@app.get("/api/trainer/bookings")
def trainer_bookings(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        return [rowdict(r) for r in c.execute("""SELECT b.id,b.status,b.created_at,s.id slot_id,s.starts_at,s.duration_min,s.title,u.tg_id,u.first_name,u.last_name,u.username FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN users u ON u.tg_id=b.tg_id ORDER BY s.starts_at DESC""")]

@app.post("/api/trainer/slots")
def trainer_slot(x:SlotIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        cur=c.execute("INSERT INTO slots(starts_at,duration_min,title,capacity) VALUES(?,?,?,?)",(x.starts_at,x.duration_min,x.title,x.capacity)); c.commit()
    return {"ok":True,"id":cur.lastrowid}

@app.patch("/api/trainer/slots/{slot_id}/close")
def close_slot(slot_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c: c.execute("UPDATE slots SET status='closed' WHERE id=?",(slot_id,)); c.commit()
    return {"ok":True}

@app.post("/api/trainer/program")
def set_program(x:ProgramIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        c.execute("UPDATE programs SET active=0 WHERE tg_id=?",(x.tg_id,))
        c.execute("INSERT INTO programs(tg_id,title,body,updated_at) VALUES(?,?,?,?)",(x.tg_id,x.title,x.body,now())); c.commit()
    return {"ok":True}

@app.post("/api/trainer/program/day")
def set_training_day(x:TrainingDayIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        p=c.execute("SELECT id FROM programs WHERE tg_id=? AND active=1 ORDER BY updated_at DESC LIMIT 1",(x.tg_id,)).fetchone()
        if not p: raise HTTPException(400,"Сначала назначьте программу клиенту")
        c.execute("""INSERT INTO training_days(program_id,day_order,title,weekday,body,exercises_json) VALUES(?,?,?,?,?,?)
        ON CONFLICT(program_id,day_order) DO UPDATE SET title=excluded.title,weekday=excluded.weekday,body=excluded.body,exercises_json=excluded.exercises_json,active=1""",(p["id"],x.day_order,x.title,x.weekday,x.body,json.dumps(x.exercises,ensure_ascii=False))); c.commit()
    return {"ok":True}

@app.get("/api/trainer/program/days/{tg_id}")
def trainer_program_days(tg_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        p=c.execute("SELECT id FROM programs WHERE tg_id=? AND active=1 ORDER BY updated_at DESC LIMIT 1",(tg_id,)).fetchone()
        if not p:return []
        return [dict(rowdict(r),exercises=json.loads(r["exercises_json"] or "[]")) for r in c.execute("SELECT * FROM training_days WHERE program_id=? AND active=1 ORDER BY day_order",(p["id"],))]

@app.post("/api/trainer/nutrition")
def set_nutrition(x:NutritionIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        c.execute("UPDATE users SET calories=?,protein=?,fat=?,carbs=?,water_goal=? WHERE tg_id=?",(x.calories,x.protein,x.fat,x.carbs,x.water_goal,x.tg_id)); c.commit()
    return {"ok":True}

@app.get("/api/trainer/reports")
def trainer_reports(x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c: return [rowdict(r) for r in c.execute("SELECT r.*,u.first_name,u.last_name,u.username FROM reports r JOIN users u ON u.tg_id=r.tg_id ORDER BY r.created_at DESC LIMIT 100")]

@app.get("/api/trainer/measurements/{tg_id}")
def trainer_measurements(tg_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c: return [rowdict(r) for r in c.execute("SELECT * FROM measurements WHERE tg_id=? ORDER BY created_at DESC LIMIT 50",(tg_id,))]

@app.get("/api/trainer/photos/{tg_id}")
def trainer_photos(tg_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c: return [dict(rowdict(r),url=f"/api/trainer/photos/{tg_id}/{r['id']}") for r in c.execute("SELECT id,tg_id,caption,mime,created_at FROM progress_photos WHERE tg_id=? ORDER BY created_at DESC",(tg_id,))]

@app.get("/api/trainer/photos/{tg_id}/{photo_id}")
def trainer_photo(tg_id:int,photo_id:int,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c: r=c.execute("SELECT * FROM progress_photos WHERE id=? AND tg_id=?",(photo_id,tg_id)).fetchone()
    if not r: raise HTTPException(404,"Фото не найдено")
    from fastapi.responses import Response
    return Response(r["data"],media_type=r["mime"])

@app.post("/api/trainer/notifications")
def trainer_notification(x:NotificationIn,x_telegram_init_data:Optional[str]=Header(default=None)):
    u=current_user(x_telegram_init_data); require_trainer(u)
    with closing(db()) as c:
        cur=c.execute("INSERT INTO notifications(tg_id,kind,text,remind_at) VALUES(?,?,?,?)",(x.tg_id,x.kind,x.text,x.remind_at)); c.commit()
    return {"ok":True,"id":cur.lastrowid}

async def bot_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return
    tu=update.effective_user; ensure_user(tu.id,tu.username or "",tu.first_name or "",tu.last_name or "")
    role="тренер" if tu.id==TRAINER_TG_ID else "клиент"
    text=f"Привет, {tu.first_name or 'друг'}.\nРоль: {role}."
    if MINIAPP_URL:
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть приложение",web_app=WebAppInfo(url=MINIAPP_URL))]])
        await update.message.reply_text(text,kb)
    else: await update.message.reply_text(text+"\nMINIAPP_URL пока не настроен.")

async def bot_help(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start  открыть приложение\n/help  помощь")

async def reminder_loop(application:Application):
    while True:
        try:
            due=[]
            with closing(db()) as c:
                rows=c.execute("SELECT * FROM notifications WHERE sent=0 AND remind_at<=? ORDER BY id LIMIT 50",(now(),)).fetchall()
                for r in rows: due.append(rowdict(r))
            for r in due:
                try:
                    await application.bot.send_message(r["tg_id"], r["text"])
                    with closing(db()) as c: c.execute("UPDATE notifications SET sent=1 WHERE id=?",(r["id"],)); c.commit()
                except Exception as e: log.warning("notification %s failed: %s",r["id"],e)
        except Exception: log.exception("reminder loop")
        await asyncio.sleep(30)


def run_bot():
    async def runner():
        application=Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start",bot_start)); application.add_handler(CommandHandler("help",bot_help))
        await application.initialize()
        if MINIAPP_URL:
            try: await application.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Приложение",web_app=WebAppInfo(url=MINIAPP_URL)))
            except Exception: log.exception("menu button")
        await application.start(); await application.updater.start_polling(drop_pending_updates=True)
        asyncio.create_task(reminder_loop(application))
        await asyncio.Event().wait()
    asyncio.run(runner())

if __name__=="__main__":
    if not BOT_TOKEN: raise SystemExit("BOT_TOKEN is required")
    threading.Thread(target=run_bot,daemon=True).start()
    import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=PORT)
