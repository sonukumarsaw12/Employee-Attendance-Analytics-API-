"""Employee Attendance & Analytics API (HROne assignment, contract v2.0).
Run from project root: python -m uvicorn app.main:app --port 8000
All API/application logic is intentionally kept in this single file.
"""
from __future__ import annotations

import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load values from .env for local development, but never override real
# environment variables supplied by the OS/deployment platform.
load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

if not MONGO_URI or not MONGO_DB:
    raise RuntimeError(
        "Missing required environment variables: MONGO_URI and MONGO_DB. "
        "Set them in the environment or in a local .env file."
    )

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2500, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- validation ---------------------------------
def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, detail=f"{field} must use YYYY-MM-DD format")
    if parsed.isoformat() != value:
        raise HTTPException(422, detail=f"{field} must use YYYY-MM-DD format")
    return parsed


def parse_month(value: str) -> tuple[int, int, date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, detail="month must use YYYY-MM format")
    year, month = map(int, value.split("-"))
    if not 2000 <= year <= 2100:
        raise HTTPException(422, detail="month year must be between 2000 and 2100")
    return year, month, date(year, month, 1), date(year, month, monthrange(year, month)[1])


def epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def from_epoch_ms(value: int | None) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    # API instants are truncated to whole seconds before storage/calculation.
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def rounded(value: Any, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def round_int(value: Any) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def time_parts(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Time must use 24-hour HH:MM format")
    h, m = value.split(":")
    return int(h), int(m)


def shift_datetime(day: date, clock: str) -> datetime:
    h, m = time_parts(clock)
    return datetime.combine(day, time(h, m), tzinfo=IST)


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = utc_aware(punch_in).astimezone(IST)
    day = local.date()
    if shift_end <= shift_start:
        eh, em = time_parts(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime | None, shift_start: str, shift_end: str | None = None) -> int:
    if punch_in is None:
        return 0
    local = utc_aware(punch_in).astimezone(IST)
    shift_day = local.date()
    if shift_end is not None and shift_end <= shift_start:
        eh, em = time_parts(shift_end)
        if (local.hour, local.minute, local.second) < (eh, em, 0):
            shift_day -= timedelta(days=1)
    start = shift_datetime(shift_day, shift_start)
    seconds = (local - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in: datetime | None, punch_out: datetime | None) -> float | None:
    if punch_in is None or punch_out is None:
        return None
    seconds = Decimal(str((utc_aware(punch_out) - utc_aware(punch_in)).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out: datetime | None, shift_start: str, shift_end: str, record_date: str) -> int:
    if punch_out is None:
        return 0
    day = parse_date(record_date)
    start_hm = time_parts(shift_start)
    end_hm = time_parts(shift_end)
    end_day = day + timedelta(days=1) if end_hm <= start_hm else day
    shift_end_dt = shift_datetime(end_day, shift_end)
    seconds = (utc_aware(punch_out).astimezone(IST) - shift_end_dt).total_seconds()
    return int(seconds // 60) if seconds >= 1800 else 0


def serialize_history(history: list[dict] | None) -> list[dict]:
    output = []
    for entry in history or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            changes[key] = {
                "from": epoch_ms(change.get("from")) if isinstance(change.get("from"), datetime) else change.get("from"),
                "to": epoch_ms(change.get("to")) if isinstance(change.get("to"), datetime) else change.get("to"),
            }
        output.append({"at": epoch_ms(entry.get("at")), "by": entry.get("by", ""), "reason": entry.get("reason", ""), "changes": changes})
    return output


def attendance_response(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")), "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)), "history": serialize_history(doc.get("history", [])),
    }


def employee_response(doc: dict) -> dict:
    return {"emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
            "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
            "joined_on": doc["joined_on"], "created_at": epoch_ms(doc["created_at"])}


# ---------------------------------- models ----------------------------------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")  # derived fields sent by clients are ignored by contract


class EmployeeCreate(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, v: str) -> str:
        date.fromisoformat(v)
        if date.fromisoformat(v).isoformat() != v:
            raise ValueError("joined_on must use YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class EpochInput(StrictModel):
    punched_at: int | None = Field(default=None, strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class PunchInRequest(EpochInput):
    emp_code: str = Field(min_length=1)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(EpochInput):
    emp_code: str = Field(min_length=1)


class RegularizeRequest(StrictModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: int | None = Field(default=None, strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: int | None = Field(default=None, strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be blank")
        return v.strip()

    @field_validator("regularized_by")
    @classmethod
    def by_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("regularized_by must not be blank")
        return v.strip()


# ---------------------------------- indexes ---------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    # Indexes are idempotent. Compound keys support the contract's primary filters and sort orders.
    employees.create_index([("emp_code", ASCENDING)], name="ux_employees_emp_code", unique=True)
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")
    employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employees_joined_department")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], name="ux_attendance_emp_date", unique=True)
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")
    logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_emp")


# -------------------------------- system ------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, detail="MongoDB unavailable")


# -------------------------------- employees ---------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    except PyMongoError:
        raise HTTPException(503, detail="Database operation failed")
    return employee_response(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# ------------------------------- attendance ---------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    ts = from_epoch_ms(body.punched_at)
    day = attendance_day(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")
    return attendance_response(doc)


def attendance_filter(emp_code: str | None, date_from: str | None, date_to: str | None, status: str | None) -> dict:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, detail="Invalid status")
        query["status"] = status
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    if date_from or date_to:
        query["date"] = {**({"$gte": date_from} if date_from else {}), **({"$lte": date_to} if date_to else {})}
    return query


def attendance_find_query(query: dict, page: int, page_size: int):
    return logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                   status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(query)
    docs = attendance_find_query(query, page, page_size)
    return {"items": [attendance_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    ts = from_epoch_ms(body.punched_at)
    record = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if record is None:
        future = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$gt": ts}}, sort=[("punch_in", ASCENDING)])
        if future:
            raise HTTPException(422, detail="Punch-out must be after punch-in and within 24 hours")
        raise HTTPException(404, detail="No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, detail="Already punched out")
    pin = utc_aware(record.get("punch_in"))
    elapsed = (ts - pin).total_seconds()
    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(422, detail="Punch-out must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(pin, ts)
    overtime = compute_overtime_minutes(ts, emp["shift_start"], emp["shift_end"], record["date"])
    result = logs.update_one({"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
                             {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": work_hours < 4.5}})
    if result.modified_count != 1:
        raise HTTPException(409, detail="Already punched out")
    return attendance_response(logs.find_one({"emp_code": body.emp_code, "date": record["date"]}))


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    record_day = parse_date(date)
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")
    record = logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, detail="Attendance record not found")
    fields_set = body.model_fields_set
    if "status" not in fields_set and "punch_in" not in fields_set and "punch_out" not in fields_set:
        raise HTTPException(422, detail="Request must change at least one field")
    if body.status is None and "status" in fields_set:
        raise HTTPException(422, detail="status cannot be null")
    if "punch_in" in fields_set and body.punch_in is None:
        raise HTTPException(422, detail="punch_in cannot be null; use ABSENT or LEAVE to clear punch times")
    if "punch_out" in fields_set and body.punch_out is None:
        raise HTTPException(422, detail="punch_out cannot be null")

    old = {k: record.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    new_status = body.status if "status" in fields_set else record["status"]
    pin = from_epoch_ms(body.punch_in) if "punch_in" in fields_set else utc_aware(record.get("punch_in"))
    pout = from_epoch_ms(body.punch_out) if "punch_out" in fields_set else utc_aware(record.get("punch_out"))
    if new_status in {"ABSENT", "LEAVE"}:
        if ("punch_in" in fields_set or "punch_out" in fields_set):
            raise HTTPException(422, detail="ABSENT or LEAVE cannot be supplied with punch times")
        pin = pout = None
    else:
        if pin is None:
            raise HTTPException(422, detail="A presence status requires punch_in")
        pin = utc_aware(pin).replace(microsecond=0)
        pout = utc_aware(pout).replace(microsecond=0) if pout is not None else None
        if attendance_day(pin, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise HTTPException(422, detail="punch_in must belong to the record attendance date")
        if pout is not None:
            elapsed = (pout - pin).total_seconds()
            if elapsed <= 0 or elapsed > 86400:
                raise HTTPException(422, detail="punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(pin, emp["shift_start"], emp["shift_end"]) if new_status in PRESENCE else 0
    work = compute_work_hours(pin, pout) if new_status in PRESENCE else None
    overtime = compute_overtime_minutes(pout, emp["shift_start"], emp["shift_end"], date) if new_status in PRESENCE else 0
    half = bool(work is not None and work < 4.5) if new_status in PRESENCE else False
    new = {"status": new_status, "punch_in": pin, "punch_out": pout, "work_hours": work,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half}

    def comparable(v):
        if isinstance(v, datetime):
            return epoch_ms(v)
        return v
    changes = {key: {"from": comparable(old.get(key)), "to": comparable(new.get(key))}
               for key in new if comparable(old.get(key)) != comparable(new.get(key))}
    if not changes:
        raise HTTPException(422, detail="Request does not change the record")
    at = datetime.now(UTC).replace(microsecond=0)
    hist = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: match the original history snapshot, so concurrent edits cannot overwrite history.
    history_match = record.get("history", [])
    query = {"emp_code": emp_code, "date": date, "history": history_match}
    if "history" not in record:
        query = {"emp_code": emp_code, "date": date, "history": {"$exists": False}}
    result = logs.update_one(query, {"$set": new, "$push": {"history": hist}})
    if result.modified_count != 1:
        raise HTTPException(409, detail="Attendance record changed concurrently; retry the request")
    return attendance_response(logs.find_one({"emp_code": emp_code, "date": date}))


# ------------------------------ aggregation helpers -------------------------
def month_bounds(month: str) -> tuple[date, date, str, str]:
    _, _, first, last = parse_month(month)
    return first, last, first.isoformat(), last.isoformat()


def working_days_expr(start_expr: Any, end_date: date) -> dict:
    # $dateDiff counts whole days; $range includes start and excludes end, so add one day.
    return {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_expr, "endDate": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": end_date.isoformat()}}, "unit": "day", "amount": 1}}, "unit": "day"}}, 0]}]}, "as": "i", "in": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$i"}}}}, "as": "d", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}}}}


def present_contribution(status_expr: Any = "$status", half_expr: Any = {"$ifNull": ["$half_day", False]}) -> dict:
    return {"$cond": [{"$in": [status_expr, ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [half_expr, 0.5, 1]}, 0]}


# ------------------------------- monthly analytics --------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first, last, first_s, last_s = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, detail="Employee not found")
    joined = max(first, parse_date(emp["joined_on"], "joined_on"))
    working_days = 0
    if joined <= last:
        cursor = joined
        while cursor <= last:
            if cursor.weekday() < 5:
                working_days += 1
            cursor += timedelta(days=1)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$gte": [{"$dateFromString": {"dateString": "$date"}}, {"$dateFromString": {"dateString": joined.isoformat()}}]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }}]
    agg = list(logs.aggregate(pipeline))
    row = agg[0] if agg else {}
    present = rounded(row.get("present_days", 0))
    pct = rounded(present / working_days * 100, 4) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present,
            "leave_days": int(row.get("leave_days", 0)), "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    first, last, first_s, last_s = month_bounds(month)
    pipeline = [
        {"$match": {"joined_on": {"$lte": last_s}, **({"department": department} if department is not None else {})}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}, {"$gte": ["$date", "$$joined"]}]}}}], "as": "month_logs"}},
        {"$unwind": {"path": "$month_logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENCE)]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$month_logs.date", first_s]}}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$month_logs.date", first_s]}}}}, 6]}]}, {"$cond": [{"$ifNull": ["$month_logs.half_day", False]}, 0.5, 1]}, 0]}},
            "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$month_logs.work_hours", None]}, None]}]}, "$month_logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$month_logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$month_logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$eq": ["$avg_work_hours", None]}, None, {"$round": ["$avg_work_hours", 2]}]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$match": {"headcount": {"$gt": 0}}}, {"$sort": {"department": 1}},
    ]
    rows = list(employees.aggregate(pipeline, allowDiskUse=True))
    for row in rows:
        row["present_days"] = rounded(row.get("present_days", 0))
        if row.get("avg_work_hours") is not None:
            row["avg_work_hours"] = rounded(row["avg_work_hours"])
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: str | None = None):
    _, _, first_s, last_s = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                     "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                       "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"month": month, "items": list(logs.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    days = (end - start).days + 1
    if days <= 0:
        raise HTTPException(422, detail="to must be on or after from")
    if days > 92:
        raise HTTPException(422, detail="Date range cannot exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, detail="Department not found")
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", from_date]}, {"$lte": ["$date", to_date]}, {"$gte": ["$date", "$$joined"]}]}}}], "as": "day_logs"}},
        {"$unwind": {"path": "$day_logs", "preserveNullAndEmptyArrays": True}},
        # Seed each department with at least the first requested date, even if there are no logs.
        {"$group": {"_id": {"$ifNull": ["$day_logs.date", from_date]},
            "present_count": {"$sum": {"$cond": [{"$in": ["$day_logs.status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$day_logs.half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$day_logs.late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$set": {"date_obj": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "date_obj", "range": {"step": 1, "unit": "day", "bounds": [
            datetime.combine(start, time.min, tzinfo=UTC), datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)]}}},
        {"$set": {"date_str": {"$dateToString": {"date": "$date_obj", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"}], "as": "headcount_docs"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_docs.n", 0]}, 0]},
                   "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date_obj"}, 2]}, {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]}]},
                   "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date_obj": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                       "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$round": ["$moving_avg_7d", 4]}]}}},
        {"$sort": {"date": 1}},
    ]
    # The pipeline uses $densify in MongoDB; no Python date-loop is used to fill gaps.
    rows = list(employees.aggregate(pipeline, allowDiskUse=True))
    # The pipeline seeds from_date via preserveNullAndEmptyArrays, then $densify fills every calendar day.
    # Keep rates and moving average at four decimals using half-up rounding at the API boundary.
    for row in rows:
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = rounded(row["attendance_rate"], 4)
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = rounded(row["moving_avg_7d"], 4)
        row["present_count"] = rounded(row.get("present_count", 0))
    return {"department": department, "items": rows}


# ------------------------------ explain endpoint ----------------------------
def explain_command(collection, pipeline_or_query, endpoint: str, kind: str, sort=None, skip=None, limit=None):
    if kind == "find":
        find_command: dict[str, Any] = {"find": collection.name, "filter": pipeline_or_query,
                                         "sort": sort or {}, "projection": {"_id": 0}}
        if skip is not None:
            find_command["skip"] = skip
        if limit is not None:
            find_command["limit"] = limit
        explanation = db.command("explain", find_command, verbosity="executionStats")
    else:
        explanation = db.command("explain", {"aggregate": collection.name, "pipeline": pipeline_or_query, "cursor": {}, "allowDiskUse": True}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection.name, "explain": explanation}


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str,
                  emp_code: str | None = None, month: str | None = None, department: str | None = None,
                  limit: int = Query(10, ge=1, le=50), date_from: str | None = None, date_to: str | None = None,
                  status: str | None = None, from_date: str | None = Query(None, alias="from"),
                  to_date: str | None = Query(None, alias="to"), page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100)):
    """Explain the real query shape for one of the five contract-defined endpoints."""
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        return explain_command(logs, query, endpoint, "find", sort={"date": -1, "emp_code": 1},
                               skip=(page - 1) * page_size, limit=page_size)

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        _, _, first, last = month_bounds(month)
        joined_doc = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
        if joined_doc is None:
            raise HTTPException(404, detail="Employee not found")
        joined = max(
            parse_date(str(first)[:10], "month start"),
            parse_date(str(joined_doc["joined_on"])[:10], "joined_on")
        )
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": str(first)[:10], "$lte": str(last)[:10]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                    {"$gte": [{"$dateFromString": {"dateString": "$date"}}, {"$dateFromString": {"dateString": joined.isoformat()}}]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }}]
        return explain_command(logs, pipeline, endpoint, "aggregate")

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, _, first, last = month_bounds(month)
        first_s, last_s = str(first)[:10], str(last)[:10]
        pipeline = [
            {"$match": {"joined_on": {"$lte": last_s}, **({"department": department} if department is not None else {})}},
            {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]},
                    {"$lte": ["$date", last_s]}, {"$gte": ["$date", "$$joined"]}]}}}], "as": "month_logs"}},
            {"$unwind": {"path": "$month_logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENCE)]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$month_logs.date", first_s]}}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$month_logs.date", first_s]}}}}, 6]}]},
                    {"$cond": [{"$ifNull": ["$month_logs.half_day", False]}, 0.5, 1]}, 0]}},
                "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENCE)]},
                    {"$ne": [{"$ifNull": ["$month_logs.work_hours", None]}, None]}]}, "$month_logs.work_hours", None]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$month_logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$month_logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "ON_DUTY"]}, 1, 0]}},
            }},
            {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
                "avg_work_hours": {"$cond": [{"$eq": ["$avg_work_hours", None]}, None, {"$round": ["$avg_work_hours", 2]}]},
                "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
            {"$match": {"headcount": {"$gt": 0}}}, {"$sort": {"department": 1}},
        ]
        return explain_command(employees, pipeline, endpoint, "aggregate")

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, _, first, last = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": str(first)[:10], "$lte": str(last)[:10]}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline += [
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        ]
        return explain_command(logs, pipeline, endpoint, "aggregate")

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from and to are required")
        start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, detail="Invalid trend date range")
        if not employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(404, detail="Department not found")
        pipeline = [
            {"$match": {"department": department}},
            {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", from_date]},
                    {"$lte": ["$date", to_date]}, {"$gte": ["$date", "$$joined"]}]}}}], "as": "day_logs"}},
            {"$unwind": {"path": "$day_logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {"_id": {"$ifNull": ["$day_logs.date", from_date]},
                "present_count": {"$sum": {"$cond": [{"$in": ["$day_logs.status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$day_logs.half_day", False]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$day_logs.late_minutes", 0]}, 0]}, 1, 0]}}}},
            {"$set": {"date_obj": {"$dateFromString": {"dateString": "$_id"}}}},
            {"$densify": {"field": "date_obj", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time.min, tzinfo=UTC), datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)]}}},
            {"$set": {"date_str": {"$dateToString": {"date": "$date_obj", "format": "%Y-%m-%d"}}}},
            {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}], "as": "headcount_docs"}},
            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_docs.n", 0]}, 0]},
                "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date_obj"}, 2]}, {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]}]},
                "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
            {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4]}, None]}}},
            {"$setWindowFields": {"sortBy": {"date_obj": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$round": ["$moving_avg_7d", 4]}]}}},
            {"$sort": {"date": 1}},
        ]
        return explain_command(employees, pipeline, endpoint, "aggregate")
    raise HTTPException(422, detail="endpoint must be one of attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend")
