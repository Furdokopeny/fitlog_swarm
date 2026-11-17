"""Edzésnapló – REST API (Flask + PostgreSQL)."""
import os
import socket
import time
from contextlib import contextmanager
from datetime import date

import psycopg2
from flask import Flask, jsonify, request
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.json.ensure_ascii = False
HOST = socket.gethostname()  # a konténer azonosítója: a skálázás bemutatásához


def secret(name, default=""):
    """Érték beolvasása Docker secretből (NAME_FILE), egyébként környezeti változóból."""
    path = os.environ.get(f"{name}_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get(name, default)


DB = {
    "host": os.environ.get("DB_HOST", "db"),  # a Swarm service neve
    "dbname": os.environ.get("DB_NAME", "fitlog"),
    "user": os.environ.get("DB_USER", "fitlog"),
    "password": secret("DB_PASSWORD"),
    "connect_timeout": 3,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS workouts (
    id           SERIAL PRIMARY KEY,
    performed_on DATE         NOT NULL DEFAULT CURRENT_DATE,
    exercise     VARCHAR(100) NOT NULL,
    sets         INT          NOT NULL DEFAULT 1 CHECK (sets > 0),
    reps         INT          NOT NULL DEFAULT 1 CHECK (reps > 0),
    weight_kg    NUMERIC(7,2) NOT NULL DEFAULT 0 CHECK (weight_kg >= 0),
    duration_min INT          NOT NULL DEFAULT 0 CHECK (duration_min >= 0),
    notes        TEXT         NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
"""


@contextmanager
def db():
    """Kapcsolat kérésenként: az adatbázis újraindulását is túléli."""
    conn = psycopg2.connect(cursor_factory=RealDictCursor, **DB)
    try:
        with conn, conn.cursor() as cur:  # sikeres blokk végén commit
            yield cur
    finally:
        conn.close()


def init_db():
    """Séma létrehozása; az advisory lock megakadályozza a replikák versenyhelyzetét."""
    for _ in range(10):
        try:
            with db() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(42)")
                cur.execute(SCHEMA)
            return
        except psycopg2.OperationalError:
            time.sleep(2)
    raise RuntimeError("Az adatbázis nem érhető el")  # a Swarm újraindítja a taskot


def number(data, key, cast, default, minimum):
    try:
        value = cast(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"Érvénytelen érték: {key}")
    if not (minimum <= value <= 10000):
        raise ValueError(f"Érvénytelen érték: {key}")
    return value


def validate(data):
    exercise = str(data.get("exercise", "")).strip()[:100]
    if not exercise:
        raise ValueError("A gyakorlat neve kötelező.")
    try:
        performed_on = date.fromisoformat(data.get("performed_on") or date.today().isoformat())
    except ValueError:
        raise ValueError("Érvénytelen dátum.")
    return (
        performed_on,
        exercise,
        number(data, "sets", int, 1, 1),
        number(data, "reps", int, 1, 1),
        number(data, "weight_kg", float, 0, 0),
        number(data, "duration_min", int, 0, 0),
        str(data.get("notes", "")).strip()[:500],
    )


@app.errorhandler(psycopg2.Error)
def db_error(err):
    app.logger.error("Adatbázis-hiba: %s", err)
    return jsonify(error="Az adatbázis jelenleg nem érhető el. Próbáld újra egy perc múlva."), 503


@app.get("/api/health")  # liveness: szándékosan nem függ az adatbázistól
def health():
    return jsonify(status="ok", host=HOST)


@app.get("/api/info")
def info():
    try:
        with db() as cur:
            cur.execute("SELECT 1")
        ok = True
    except psycopg2.Error:
        ok = False
    return jsonify(host=HOST, db=ok)


@app.get("/api/workouts")
def list_workouts():
    with db() as cur:
        cur.execute(
            """SELECT id, performed_on::text AS performed_on, exercise, sets, reps,
                      weight_kg::float AS weight_kg, duration_min, notes
               FROM workouts ORDER BY performed_on DESC, id DESC LIMIT 100"""
        )
        return jsonify(cur.fetchall())


@app.post("/api/workouts")
def add_workout():
    try:
        row = validate(request.get_json(silent=True) or {})
    except ValueError as err:
        return jsonify(error=str(err)), 400
    with db() as cur:
        cur.execute(
            """INSERT INTO workouts (performed_on, exercise, sets, reps, weight_kg, duration_min, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            row,
        )
        return jsonify(id=cur.fetchone()["id"]), 201


@app.delete("/api/workouts/<int:wid>")
def delete_workout(wid):
    with db() as cur:
        cur.execute("DELETE FROM workouts WHERE id = %s", (wid,))
        deleted = cur.rowcount
    if not deleted:
        return jsonify(error="Nincs ilyen bejegyzés."), 404
    return jsonify(deleted=wid)


@app.get("/api/stats")
def stats():
    with db() as cur:
        cur.execute(
            """SELECT count(*)::int AS total_workouts,
                      count(*) FILTER (WHERE performed_on >= CURRENT_DATE - 6)::int AS last7_days,
                      COALESCE(sum(sets * reps * weight_kg), 0)::float AS total_volume_kg,
                      COALESCE(sum(duration_min), 0)::int AS total_minutes
               FROM workouts"""
        )
        return jsonify(cur.fetchone())


init_db()
