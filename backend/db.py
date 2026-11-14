import hashlib
import os
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row

from parser import CONTEST_FLAGS, YIELD_METHODS

SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_batches (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS landed_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    UNIQUE (station_code, observed_at)
);

CREATE TABLE IF NOT EXISTS reject_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL
);

-- 当天封存的让位方式，一天只有一行，封存后当天不得更换
CREATE TABLE IF NOT EXISTS yield_settings (
    id BIGSERIAL PRIMARY KEY,
    seal_date DATE NOT NULL UNIQUE,
    yield_method TEXT NOT NULL,
    method_label TEXT NOT NULL,
    sealed_by TEXT NOT NULL,
    sealed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 同一站同一时刻实测旗与插补旗对撞的旗争，每个争用点只留一种结局
CREATE TABLE IF NOT EXISTS flag_contests (
    id BIGSERIAL PRIMARY KEY,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    yield_method TEXT NOT NULL,
    method_label TEXT NOT NULL,
    winner_batch_id BIGINT,
    winner_line_no INTEGER,
    winner_flag TEXT,
    yield_count INTEGER NOT NULL DEFAULT 0,
    created_batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (station_code, observed_at)
);

-- 落选队列：旗争中没有入库的旗，逐行留底
CREATE TABLE IF NOT EXISTS yielded_rows (
    id BIGSERIAL PRIMARY KEY,
    contest_id BIGINT NOT NULL REFERENCES flag_contests(id),
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yielded_batch ON yielded_rows (batch_id);
CREATE INDEX IF NOT EXISTS idx_yielded_contest ON yielded_rows (contest_id);
"""

SEED_CLEAN = """吴淞口 11-12 早班
WG01|202611120830|186|A
WG01|202611120900|191|A
BG02|202611120830|142|M
"""

SEED_BROKEN = """外高桥 11-12 错报
LG04|202611121000|205|A
WG01|202611120830|186|A
WG0X|202611121000|10|A
"""

# 让位设置写入互斥锁：让位尚未写完时不许改让位设置
ADV_GLOBAL_LOCK = (7351001, 11)


class SettingLocked(RuntimeError):
    """让位正在写入，设置暂时不能改。"""


class SettingSealed(RuntimeError):
    """当天让位方式已封存。"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def _is_postgres(conn) -> bool:
    return hasattr(conn, "info")


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                count = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _advisory_xact_lock(conn, key1: int, key2: int) -> None:
    # 顺序锁：事务级咨询锁，提交或回滚后自动释放
    if _is_postgres(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (key1, key2))


def _try_advisory_xact_lock(conn, key1: int, key2: int) -> bool:
    if not _is_postgres(conn):
        return True
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s) AS ok", (key1, key2)
    ).fetchone()
    return row["ok"]


def _contest_lock_keys(station_code: str, observed) -> tuple[int, int]:
    digest = hashlib.md5(
        f"{station_code}|{observed.isoformat()}".encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16), int(digest[8:16], 16)


def get_setting_today(conn):
    return conn.execute(
        "SELECT * FROM yield_settings WHERE seal_date = %s", (date.today(),)
    ).fetchone()


def setting_write_in_progress(conn) -> bool:
    # 试探全局锁能否拿到；拿不到说明正有批次在写让位
    return not _try_advisory_xact_lock(conn, *ADV_GLOBAL_LOCK)


def seal_setting(conn, yield_method: str, sealed_by: str):
    if yield_method not in YIELD_METHODS:
        raise ValueError("让位方式不认识")
    if not _try_advisory_xact_lock(conn, *ADV_GLOBAL_LOCK):
        raise SettingLocked("让位尚未写完，不许改让位设置")
    if get_setting_today(conn):
        raise SettingSealed("让位方式已封存，当天不得更换")
    return conn.execute(
        """
        INSERT INTO yield_settings (seal_date, yield_method, method_label, sealed_by)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (date.today(), yield_method, YIELD_METHODS[yield_method], sealed_by),
    ).fetchone()


def _land(conn, batch_id: int, line_no: int, parsed: dict) -> None:
    conn.execute(
        """
        INSERT INTO landed_rows (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            batch_id,
            line_no,
            parsed["station_code"],
            parsed["observed_at"],
            parsed["tide_cm"],
            parsed["quality_flag"],
        ),
    )


def _insert_yielded(conn, contest_id: int, batch_id: int, line_no: int, parsed: dict, reason: str) -> None:
    conn.execute(
        """
        INSERT INTO yielded_rows
            (contest_id, batch_id, line_no, station_code, observed_at, tide_cm, quality_flag, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            contest_id,
            batch_id,
            line_no,
            parsed["station_code"],
            parsed["observed_at"],
            parsed["tide_cm"],
            parsed["quality_flag"],
            reason,
        ),
    )


def _resolve_contest(conn, batch_id, key, rows, requested_method, sealed_method, reject):
    """裁决一个争用点，返回本点实际入库行数。"""
    station, observed = key

    # 顺序锁：同一站同一时刻的旗争串行裁决，只放行一种结局
    _advisory_xact_lock(conn, *_contest_lock_keys(station, observed))

    # 请求里的让位方式与当天封存设置不一致
    if requested_method is not None and requested_method not in YIELD_METHODS:
        for line_no, raw_line, _ in rows:
            reject(line_no, raw_line, "让位方式不符")
        return 0
    if sealed_method is None:
        for line_no, raw_line, _ in rows:
            reject(line_no, raw_line, "让位方式尚未封存")
        return 0
    if requested_method is not None and requested_method != sealed_method:
        for line_no, raw_line, _ in rows:
            reject(line_no, raw_line, "让位方式不符")
        return 0
    method = sealed_method

    contest = conn.execute(
        "SELECT * FROM flag_contests WHERE station_code = %s AND observed_at = %s",
        key,
    ).fetchone()
    if contest:
        # 结局已经被前一批拿走，本批全部记让位争用
        for line_no, _raw_line, parsed in rows:
            _insert_yielded(conn, contest["id"], batch_id, line_no, parsed, "让位争用")
        conn.execute(
            "UPDATE flag_contests SET yield_count = yield_count + 1 WHERE id = %s",
            (contest["id"],),
        )
        return 0

    contest = conn.execute(
        """
        INSERT INTO flag_contests
            (station_code, observed_at, yield_method, method_label,
             winner_batch_id, winner_line_no, winner_flag, yield_count, created_batch_id)
        VALUES (%s, %s, %s, %s, NULL, NULL, NULL, 0, %s)
        RETURNING *
        """,
        (station, observed, method, YIELD_METHODS[method], batch_id),
    ).fetchone()

    # 防御：历史数据已占住该时刻，本批评为让位争用
    landed = conn.execute(
        "SELECT id FROM landed_rows WHERE station_code = %s AND observed_at = %s",
        key,
    ).fetchone()
    if landed:
        for line_no, _raw_line, parsed in rows:
            _insert_yielded(conn, contest["id"], batch_id, line_no, parsed, "让位争用")
        conn.execute(
            "UPDATE flag_contests SET yield_count = yield_count + 1 WHERE id = %s",
            (contest["id"],),
        )
        return 0

    if method == "measured_keep":
        measured = [r for r in rows if r[2]["quality_flag"] == "A"]
        interp = [r for r in rows if r[2]["quality_flag"] == "M"]
        winner_line_no, _winner_raw, winner = measured[0]
        _land(conn, batch_id, winner_line_no, winner)
        conn.execute(
            """
            UPDATE flag_contests
            SET winner_batch_id = %s, winner_line_no = %s, winner_flag = %s
            WHERE id = %s
            """,
            (batch_id, winner_line_no, "A", contest["id"]),
        )
        for line_no, _raw_line, parsed in interp:
            _insert_yielded(conn, contest["id"], batch_id, line_no, parsed, "插补让位")
        for line_no, _raw_line, parsed in measured[1:]:
            _insert_yielded(conn, contest["id"], batch_id, line_no, parsed, "让位争用")
        return 1

    # drop_both：两行都不入库，全部进落选队列
    for line_no, _raw_line, parsed in rows:
        _insert_yielded(conn, contest["id"], batch_id, line_no, parsed, "两行都不入库")
    return 0


def _insert_batch(conn, title: str, raw_text: str, created_by: str, yield_method: str | None = None):
    from parser import parse_telegram_line

    # 全局顺序锁：本批让位写入期间，让位设置不许改（事务结束自动释放）
    _advisory_xact_lock(conn, *ADV_GLOBAL_LOCK)

    batch = conn.execute(
        """
        INSERT INTO intake_batches (title, raw_text, status, accepted_count, rejected_count, created_by)
        VALUES (%s, %s, '处理中', 0, 0, %s)
        RETURNING id
        """,
        (title, raw_text, created_by),
    ).fetchone()
    batch_id = batch["id"]
    accepted = 0
    rejected = 0

    def reject(line_no, raw_line, reason):
        nonlocal rejected
        conn.execute(
            "INSERT INTO reject_rows (batch_id, line_no, raw_line, reason) VALUES (%s, %s, %s, %s)",
            (batch_id, line_no, raw_line, reason),
        )
        rejected += 1

    # 第一遍：整批先解析，实测旗、插补旗都先解析完；坏行不参与旗争比较
    valid_rows = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        text = raw_line.strip()
        parsed, reason = parse_telegram_line(text)
        if reason:
            reject(line_no, text, reason)
        else:
            valid_rows.append((line_no, text, parsed))

    groups = {}
    order = []
    for item in valid_rows:
        parsed = item[2]
        key = (parsed["station_code"], parsed["observed_at"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    sealed = get_setting_today(conn)
    sealed_method = sealed["yield_method"] if sealed else None

    for key in order:
        rows = groups[key]
        # 只有实测旗 A、插补旗 M 参与旗争；可疑旗 B 等不参与这场比较
        contest_rows = [r for r in rows if r[2]["quality_flag"] in CONTEST_FLAGS]
        other_rows = [r for r in rows if r[2]["quality_flag"] not in CONTEST_FLAGS]
        flag_set = {parsed["quality_flag"] for _, _, parsed in contest_rows}

        # 同一站同一时刻同时来了实测旗和插补旗，进入旗争
        if set(CONTEST_FLAGS).issubset(flag_set):
            accepted += _resolve_contest(
                conn, batch_id, key, contest_rows, yield_method, sealed_method, reject
            )
            # 非争用旗不参与比较，按普通去重单独处理
            rows = other_rows
            if not rows:
                continue

        # 非旗争行维持原去重：同批重复先挡，库里已有再挡
        existing = conn.execute(
            "SELECT id FROM landed_rows WHERE station_code = %s AND observed_at = %s",
            key,
        ).fetchone()
        if existing:
            for line_no, raw_line, _ in rows:
                reject(line_no, raw_line, "这个站这个时刻已经落过库")
            continue
        first_line_no, _first_raw, first_parsed = rows[0]
        _land(conn, batch_id, first_line_no, first_parsed)
        accepted += 1
        for line_no, raw_line, _ in rows[1:]:
            reject(line_no, raw_line, "同一批里观测时刻重复")

    if accepted and not rejected:
        status = "已归集"
    elif accepted:
        status = "部分接纳"
    else:
        status = "未接纳"
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = %s, rejected_count = %s
        WHERE id = %s
        """,
        (status, accepted, rejected, batch_id),
    )
    return batch_id


def list_contests(conn):
    return conn.execute(
        """
        SELECT fc.id, fc.station_code, fc.observed_at, fc.yield_method, fc.method_label,
               fc.winner_batch_id, fc.winner_line_no, fc.winner_flag, fc.yield_count,
               fc.created_batch_id, fc.created_at,
               lr.tide_cm AS winner_tide_cm,
               (SELECT COUNT(*) FROM yielded_rows yr WHERE yr.contest_id = fc.id) AS loser_count
        FROM flag_contests fc
        LEFT JOIN landed_rows lr
          ON lr.batch_id = fc.winner_batch_id AND lr.line_no = fc.winner_line_no
        ORDER BY fc.id DESC
        """
    ).fetchall()


def contest_detail(conn, contest_id: int):
    contest = conn.execute(
        """
        SELECT fc.*, lr.tide_cm AS winner_tide_cm
        FROM flag_contests fc
        LEFT JOIN landed_rows lr
          ON lr.batch_id = fc.winner_batch_id AND lr.line_no = fc.winner_line_no
        WHERE fc.id = %s
        """,
        (contest_id,),
    ).fetchone()
    if not contest:
        return None
    losers = conn.execute(
        """
        SELECT id, batch_id, line_no, station_code, observed_at, tide_cm, quality_flag, reason
        FROM yielded_rows WHERE contest_id = %s ORDER BY id
        """,
        (contest_id,),
    ).fetchall()
    return {"contest": contest, "losers": losers}


def list_yielded(conn):
    return conn.execute(
        """
        SELECT yr.id, yr.contest_id, yr.batch_id, b.title AS batch_title, yr.line_no,
               yr.station_code, yr.observed_at, yr.tide_cm, yr.quality_flag, yr.reason, yr.created_at
        FROM yielded_rows yr
        JOIN intake_batches b ON b.id = yr.batch_id
        ORDER BY yr.id DESC
        """
    ).fetchall()


def _seed(conn):
    title, _, body = SEED_CLEAN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")
    title, _, body = SEED_BROKEN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin")
