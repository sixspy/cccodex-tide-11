from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import authenticate, issue_token, read_user
from db import (
    SettingLocked,
    SettingSealed,
    connect,
    contest_detail,
    get_setting_today,
    init_db,
    list_contests,
    list_yielded,
    seal_setting,
    setting_write_in_progress,
)
from parser import FLAGS, YIELD_METHODS


class LoginBody(BaseModel):
    username: str
    password: str


class BatchBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1)
    yield_method: str | None = None


class YieldSettingBody(BaseModel):
    yield_method: str


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


def _date_text(value):
    if value is None:
        return None
    return value if isinstance(value, str) else value.isoformat()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def _user_or_401(authorization: str | None):
    user = read_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _writer_or_403(user):
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="档案员不能改动归集设置")
    return user


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "tide-telegram-intake"}


@app.post("/api/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_token(user),
        "username": user["username"],
        "role": user["role"],
        "title": user["title"],
    }


@app.get("/api/batches")
def list_batches(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.title, b.status, b.accepted_count, b.rejected_count,
                   b.created_by, b.created_at,
                   (SELECT COUNT(*) FROM yielded_rows yr WHERE yr.batch_id = b.id) AS yielded_count
            FROM intake_batches b
            ORDER BY b.id DESC
            """
        ).fetchall()
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
    return rows


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        batch = conn.execute(
            """
            SELECT b.*,
                   (SELECT COUNT(*) FROM yielded_rows yr WHERE yr.batch_id = b.id) AS yielded_count
            FROM intake_batches b WHERE b.id = %s
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="没有这一批报文")
        landed = conn.execute(
            """
            SELECT line_no, station_code, observed_at, tide_cm, quality_flag
            FROM landed_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        rejected = conn.execute(
            """
            SELECT line_no, raw_line, reason
            FROM reject_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        yielded = conn.execute(
            """
            SELECT yr.line_no, yr.station_code, yr.observed_at, yr.tide_cm,
                   yr.quality_flag, yr.reason, yr.contest_id
            FROM yielded_rows yr WHERE yr.batch_id = %s ORDER BY yr.line_no
            """,
            (batch_id,),
        ).fetchall()
    batch["created_at"] = _stamp(batch["created_at"])
    for row in landed:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    for row in yielded:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    return {"batch": batch, "landed": landed, "rejected": rejected, "yielded": yielded}


@app.post("/api/batches")
def create_batch(body: BatchBody, authorization: str | None = Header(default=None)):
    user = _user_or_401(authorization)
    _writer_or_403(user)
    from db import _insert_batch

    if body.yield_method is not None and body.yield_method not in YIELD_METHODS:
        raise HTTPException(status_code=400, detail="让位方式不认识")

    title = body.title.strip()
    raw_text = body.raw_text.strip()
    if not title or not raw_text:
        raise HTTPException(status_code=400, detail="标题和报文都不能空")
    with connect() as conn:
        try:
            batch_id = _insert_batch(conn, title, raw_text, user["username"], body.yield_method)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return batch_detail(batch_id, authorization)


@app.get("/api/yield-setting")
def get_yield_setting(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        setting = get_setting_today(conn)
        in_progress = setting_write_in_progress(conn)
    payload = {
        "methods": [
            {"value": value, "label": label} for value, label in YIELD_METHODS.items()
        ],
        "setting": None,
        "write_in_progress": in_progress,
    }
    if setting:
        payload["setting"] = {
            "yield_method": setting["yield_method"],
            "method_label": setting["method_label"],
            "sealed_by": setting["sealed_by"],
            "seal_date": _date_text(setting["seal_date"]),
            "sealed_at": _stamp(setting["sealed_at"]),
        }
    return payload


@app.put("/api/yield-setting")
def put_yield_setting(body: YieldSettingBody, authorization: str | None = Header(default=None)):
    user = _user_or_401(authorization)
    _writer_or_403(user)
    if body.yield_method not in YIELD_METHODS:
        raise HTTPException(status_code=400, detail="让位方式不认识")
    with connect() as conn:
        try:
            setting = seal_setting(conn, body.yield_method, user["username"])
            conn.commit()
        except SettingLocked as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=str(exc))
        except SettingSealed as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=str(exc))
    return {
        "yield_method": setting["yield_method"],
        "method_label": setting["method_label"],
        "sealed_by": setting["sealed_by"],
        "seal_date": _date_text(setting["seal_date"]),
        "sealed_at": _stamp(setting["sealed_at"]),
    }


@app.get("/api/flag-contests")
def flag_contests(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = list_contests(conn)
    for row in rows:
        row["observed_at"] = _stamp(row["observed_at"])
        row["created_at"] = _stamp(row["created_at"])
        row["winner_flag_label"] = FLAGS.get(row["winner_flag"], "无") if row["winner_flag"] else "无"
    return rows


@app.get("/api/flag-contests/{contest_id}")
def flag_contest_detail(contest_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        result = contest_detail(conn, contest_id)
        if not result:
            raise HTTPException(status_code=404, detail="没有这场旗争")
    contest = result["contest"]
    contest["observed_at"] = _stamp(contest["observed_at"])
    contest["created_at"] = _stamp(contest["created_at"])
    for row in result["losers"]:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    if contest["winner_flag"]:
        contest["winner_flag_label"] = FLAGS[contest["winner_flag"]]
    else:
        contest["winner_flag_label"] = "无"
    return result


@app.get("/api/yielded-queue")
def yielded_queue(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = list_yielded(conn)
    for row in rows:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    return rows


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
