import os
import uuid
import time
import threading
import sqlite3
import json
import hashlib
from pathlib import Path
from datetime import date, datetime, timedelta, time as dt_time
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import pandas as pd
from core import load_table, get_columns, Mapping, Rules, preview_unpaid
from email_templates import SUBJECT_1, SUBJECT_2, BODY_1, BODY_2

app = FastAPI(title="Invoice Chaser")
templates = Jinja2Templates(directory="templates")

DB_PATH = Path("app.db")
UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)

UPLOADS: Dict[str, Dict[str, Any]] = {}
LAST_PREVIEW: Dict[str, Dict[str, Any]] = {}  # token -> {"rows": [], "stats": {}}
FILE_PATHS: Dict[str, str] = {}  # token -> file path


# -------------------------
# DB helpers + tables
# -------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables():
    conn = get_conn()
    cur = conn.cursor()

    # settings: SOLO 2 emails (day_1 y day_2). day_2 puede ser NULL o 0.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            signature_name TEXT NOT NULL,
            day_1 INTEGER NOT NULL,
            day_2 INTEGER,
            from_email TEXT NOT NULL,
            reply_to_email TEXT
        )
    """)

    # state: guardamos mapping + rules + token para "Actualizar archivo" sin volver a seleccionar columnas
    cur.execute("""
        CREATE TABLE IF NOT EXISTS state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token TEXT,
            email_col TEXT,
            amount_col TEXT,
            due_date_col TEXT,
            status_col TEXT,
            name_col TEXT,
            paid_value TEXT,
            min_days_late INTEGER
        )
    """)

    # --- NUEVO: cache de preview en DB (para que "atrás" no rompa nada)
    # SQLite no soporta IF NOT EXISTS en ADD COLUMN => try/except
    try:
        cur.execute("ALTER TABLE state ADD COLUMN preview_rows_json TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE state ADD COLUMN preview_stats_json TEXT")
    except Exception:
        pass

    # debts: cada “deuda” detectada (por fila / cliente)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            name TEXT,
            amount REAL NOT NULL,
            due_date TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(email, amount, due_date)
        )
    """)

    # reminders: 2 recordatorios por debt como máximo
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            debt_id INTEGER NOT NULL,
            stage INTEGER NOT NULL,                 -- 1 o 2
            send_at TEXT NOT NULL,
            sent_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending', -- pending/sent/failed/canceled
            last_error TEXT,
            FOREIGN KEY(debt_id) REFERENCES debts(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS send_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            stage INTEGER NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL,                   -- sent/failed/canceled
            error TEXT
        )
    """)

    # seed settings si está vacío
    cur.execute("SELECT COUNT(*) AS c FROM settings")
    if cur.fetchone()["c"] == 0:
        cur.execute("""
            INSERT INTO settings (company_name, signature_name, day_1, day_2, from_email, reply_to_email)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("Tu empresa", "Tu nombre", 7, 14, "cobros@tudominio.com", ""))

    # seed state (fila única id=1)
    cur.execute("SELECT COUNT(*) AS c FROM state")
    if cur.fetchone()["c"] == 0:
        cur.execute("""
            INSERT INTO state (id, token, email_col, amount_col, due_date_col, status_col, name_col, paid_value, min_days_late)
            VALUES (1, NULL, NULL, NULL, NULL, NULL, NULL, 'Pagado', 0)
        """)

    conn.commit()
    conn.close()


def get_settings() -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row)


def save_settings(company_name: str, signature_name: str, day_1: int, day_2: Optional[int], from_email: str, reply_to_email: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings (company_name, signature_name, day_1, day_2, from_email, reply_to_email)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (company_name, signature_name, int(day_1), (int(day_2) if day_2 is not None else None), from_email, reply_to_email))
    conn.commit()
    conn.close()


def save_state(token: str, mapping: Mapping, rules: Rules):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE state
        SET token = ?, email_col = ?, amount_col = ?, due_date_col = ?, status_col = ?, name_col = ?,
            paid_value = ?, min_days_late = ?
        WHERE id = 1
    """, (
        token,
        mapping.email_col,
        mapping.amount_col,
        mapping.due_date_col,
        mapping.status_col,
        mapping.name_col,
        rules.paid_value,
        int(rules.min_days_late),
    ))
    conn.commit()
    conn.close()


def save_preview_cache(token: str, rows: List[Dict[str, Any]], stats: Dict[str, Any]):
    """Guarda el preview en SQLite y en memoria por token."""
    # Guardar en memoria
    LAST_PREVIEW[token] = {"rows": rows, "stats": stats}
    
    # Guardar en SQLite (último token)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE state
        SET preview_rows_json = ?, preview_stats_json = ?
        WHERE id = 1
    """, (json.dumps(rows, ensure_ascii=False), json.dumps(stats, ensure_ascii=False)))
    conn.commit()
    conn.close()


def get_state() -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM state WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return dict(row)


def get_preview_rows_from_db(token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Obtiene preview rows desde DB o memoria. Si token es None, usa el último token."""
    if token and token in LAST_PREVIEW:
        return LAST_PREVIEW[token].get("rows", [])
    
    st = get_state()
    token_from_db = st.get("token")
    if token_from_db and token_from_db in LAST_PREVIEW:
        return LAST_PREVIEW[token_from_db].get("rows", [])
    
    try:
        if st.get("preview_rows_json"):
            return json.loads(st["preview_rows_json"])
    except Exception:
        pass
    return []


def get_preview_stats_from_db(token: Optional[str] = None) -> Dict[str, Any]:
    """Obtiene preview stats desde DB o memoria."""
    if token and token in LAST_PREVIEW:
        return LAST_PREVIEW[token].get("stats", {})
    
    st = get_state()
    token_from_db = st.get("token")
    if token_from_db and token_from_db in LAST_PREVIEW:
        return LAST_PREVIEW[token_from_db].get("stats", {})
    
    try:
        if st.get("preview_stats_json"):
            return json.loads(st["preview_stats_json"])
    except Exception:
        pass
    
    return {
        "total_rows": 0,
        "valid_rows": 0,
        "unpaid_rows": 0,
        "decision_reason": "Datos desde caché.",
    }


# -------------------------
# Utils
# -------------------------
def pick_send_datetime(base_date: date, seed: str) -> datetime:
    """
    Genera un datetime determinístico entre 09:00 y 18:00.
    Usa hash del seed para generar hora y minutos de forma consistente.
    """
    # Crear hash determinístico del seed
    hash_obj = hashlib.md5(seed.encode())
    hash_int = int(hash_obj.hexdigest(), 16)
    
    # Hora entre 9-18 (10 horas posibles)
    hour = 9 + (hash_int % 10)
    
    # Minutos 0-59
    minute = hash_int % 60
    
    return datetime.combine(base_date, dt_time(hour, minute))


def _parse_due_date(val) -> Optional[date]:
    if val is None or val == "" or str(val).lower() == "none":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    s = str(val).strip()
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        try:
            return datetime.fromisoformat(s.split(" ")[0]).date()
        except Exception:
            return None


def upsert_debt(email: str, name: str, amount: float, due_date_iso: Optional[str]) -> int:
    conn = get_conn()
    cur = conn.cursor()
    created_at = datetime.utcnow().isoformat()

    try:
        cur.execute("""
            INSERT INTO debts (email, name, amount, due_date, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (email, name, float(amount), due_date_iso, created_at))
        debt_id = cur.lastrowid
    except sqlite3.IntegrityError:
        cur.execute("""
            SELECT id FROM debts
            WHERE email = ? AND amount = ? AND (
              (due_date IS NULL AND ? IS NULL) OR (due_date = ?)
            )
            LIMIT 1
        """, (email, float(amount), due_date_iso, due_date_iso))
        debt_id = cur.fetchone()["id"]

    conn.commit()
    conn.close()
    return int(debt_id)


def cancel_pending_for_debts_not_in_current(current_debt_ids: List[int]):
    """
    Al actualizar el Excel: cancelamos recordatorios pending de deudas que ya no están como impago.
    """
    conn = get_conn()
    cur = conn.cursor()

    if not current_debt_ids:
        cur.execute("""
            UPDATE reminders
            SET status='canceled', last_error=NULL
            WHERE status='pending'
        """)
        conn.commit()
        conn.close()
        return

    placeholders = ",".join(["?"] * len(current_debt_ids))
    cur.execute(f"""
        UPDATE reminders
        SET status='canceled', last_error=NULL
        WHERE status='pending' AND debt_id NOT IN ({placeholders})
    """, current_debt_ids)

    conn.commit()
    conn.close()


def create_or_refresh_reminders_from_rows(settings: Dict[str, Any], rows: List[Dict[str, Any]], token: Optional[str] = None) -> int:
    """
    Crea recordatorios (stage 1 y stage 2 opcional) para las filas impagadas actuales.
    Cancela pendientes de las que ya no estén impagadas.
    """
    day_1 = int(settings["day_1"])
    day_2_raw = settings.get("day_2", None)
    day_2 = int(day_2_raw) if (day_2_raw is not None and str(day_2_raw) != "") else 0

    created = 0
    current_debt_ids: List[int] = []

    conn = get_conn()
    cur = conn.cursor()

    for r in rows:
        to_email = (r.get("email") or "").strip()
        if not to_email or "@" not in to_email:
            continue

        amount = r.get("amount")
        if amount is None or amount == "":
            continue

        name = (r.get("name") or "").strip()
        due = _parse_due_date(r.get("due_date"))
        base = due if due else date.today()
        due_iso = due.isoformat() if due else None

        debt_id = upsert_debt(to_email, name, float(amount), due_iso)
        current_debt_ids.append(debt_id)

        # Stage 1 - con horario distribuido
        send_day_1 = base + timedelta(days=day_1)
        seed_1 = f"{to_email}|1|{token or 'default'}"
        send_at_1 = pick_send_datetime(send_day_1, seed_1)
        send_at_1_iso = send_at_1.isoformat()

        cur.execute("""
            SELECT COUNT(*) AS c FROM reminders
            WHERE debt_id = ? AND stage = 1 AND send_at = ? AND status IN ('pending','sent')
        """, (debt_id, send_at_1_iso))
        if cur.fetchone()["c"] == 0:
            cur.execute("""
                INSERT INTO reminders (debt_id, stage, send_at, status)
                VALUES (?, 1, ?, 'pending')
            """, (debt_id, send_at_1_iso))
            created += 1

        # Stage 2 (opcional) - con horario distribuido
        if day_2 and day_2 > 0:
            send_day_2 = base + timedelta(days=day_2)
            seed_2 = f"{to_email}|2|{token or 'default'}"
            send_at_2 = pick_send_datetime(send_day_2, seed_2)
            send_at_2_iso = send_at_2.isoformat()

            cur.execute("""
                SELECT COUNT(*) AS c FROM reminders
                WHERE debt_id = ? AND stage = 2 AND send_at = ? AND status IN ('pending','sent')
            """, (debt_id, send_at_2_iso))
            if cur.fetchone()["c"] == 0:
                cur.execute("""
                    INSERT INTO reminders (debt_id, stage, send_at, status)
                    VALUES (?, 2, ?, 'pending')
                """, (debt_id, send_at_2_iso))
                created += 1

    conn.commit()
    conn.close()

    cancel_pending_for_debts_not_in_current(current_debt_ids)

    return created


# -------------------------
# Email (Reply-To del cliente)
# -------------------------
def build_email(stage: int, to_name: str, amount: float, settings: Dict[str, Any]) -> Tuple[str, str]:
    company = settings["company_name"]
    signature = settings["signature_name"]
    name = to_name.strip() if to_name and to_name.strip() else "Hola"
    amount_str = str(amount)

    if stage == 1:
        subject = SUBJECT_1
        body = BODY_1.format(name=name, amount=amount_str, company=f"{company}\n{signature}")
    else:
        subject = SUBJECT_2
        body = BODY_2.format(name=name, amount=amount_str, company=f"{company}\n{signature}")

    return subject, body


def send_email(to_email: str, subject: str, body: str, from_email: str, reply_to: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")

    if not smtp_host or not smtp_user or not smtp_pass:
        print("\n" + "=" * 60)
        print("SIMULACIÓN EMAIL (NO SE ENVÍA)")
        print(f"FROM: {from_email}")
        if reply_to:
            print(f"REPLY-TO: {reply_to}")
        print(f"TO: {to_email}")
        print(f"SUBJECT: {subject}")
        print("-" * 60)
        print(body)
        print("=" * 60 + "\n")
        return

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)


# -------------------------
# Worker: envía cuando toca
# -------------------------
def process_due_reminders_loop():
    while True:
        try:
            settings = get_settings()
            from_email = settings.get("from_email", "cobros@tudominio.com")
            reply_to = (settings.get("reply_to_email") or "").strip()

            conn = get_conn()
            cur = conn.cursor()
            now_iso = datetime.utcnow().isoformat()

            cur.execute("""
                SELECT r.id AS rid, r.stage, r.send_at, d.email, d.name, d.amount
                FROM reminders r
                JOIN debts d ON d.id = r.debt_id
                WHERE r.status = 'pending' AND r.send_at <= ?
                ORDER BY r.send_at ASC
                LIMIT 20
            """, (now_iso,))
            due = cur.fetchall()

            for row in due:
                rid = row["rid"]
                stage = int(row["stage"])
                to_email = row["email"]
                to_name = row["name"] or ""
                amount = float(row["amount"])

                subject, body = build_email(stage, to_name, amount, settings)

                try:
                    send_email(to_email, subject, body, from_email, reply_to)

                    sent_at = datetime.utcnow().isoformat()
                    cur.execute("""
                        UPDATE reminders
                        SET status = 'sent', sent_at = ?, last_error = NULL
                        WHERE id = ?
                    """, (sent_at, rid))

                    cur.execute("""
                        INSERT INTO send_log (to_email, subject, stage, sent_at, status, error)
                        VALUES (?, ?, ?, ?, 'sent', NULL)
                    """, (to_email, subject, stage, sent_at))

                except Exception as e:
                    sent_at = datetime.utcnow().isoformat()
                    cur.execute("""
                        UPDATE reminders
                        SET status = 'failed', sent_at = ?, last_error = ?
                        WHERE id = ?
                    """, (sent_at, str(e), rid))

                    cur.execute("""
                        INSERT INTO send_log (to_email, subject, stage, sent_at, status, error)
                        VALUES (?, ?, ?, ?, 'failed', ?)
                    """, (to_email, subject, stage, sent_at, str(e)))

            conn.commit()
            conn.close()

        except Exception as e:
            print(f"[WORKER] Error: {e}")

        time.sleep(30)


@app.on_event("startup")
def on_startup():
    ensure_tables()
    t = threading.Thread(target=process_due_reminders_loop, daemon=True)
    t.start()


# -------------------------
# Routes
# -------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Check if there's existing data to show "Ir al panel" button
    st = get_state()
    token = st.get("token")
    has_data = bool(token and (token in LAST_PREVIEW or st.get("preview_rows_json")))
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "has_panel_data": has_data,
        "token": token,
    })


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...)):
    b = await file.read()
    token = str(uuid.uuid4())
    UPLOADS[token] = {"filename": file.filename, "bytes": b}

    df = load_table(b, file.filename)
    cols = get_columns(df)

    st = get_state()

    return templates.TemplateResponse("map.html", {
        "request": request,
        "token": token,
        "columns": cols,
        "state": st,
    })


@app.get("/panel", response_class=HTMLResponse)
def panel_get(request: Request, token: Optional[str] = None):
    """Muestra el panel del último token o del token especificado."""
    st = get_state()
    if not token:
        token = st.get("token")
    
    if not token:
        return templates.TemplateResponse("preview.html", {
            "request": request,
            "stats": {
                "total_rows": 0,
                "valid_rows": 0,
                "unpaid_rows": 0,
                "decision_reason": "No hay datos. Sube un archivo primero.",
            },
            "rows": [],
            "token": None,
        })
    
    rows = get_preview_rows_from_db(token)
    stats = get_preview_stats_from_db(token)
    if not stats:
        stats = {
            "total_rows": 0,
            "valid_rows": 0,
            "unpaid_rows": len(rows),
            "decision_reason": "Datos desde caché.",
        }

    return templates.TemplateResponse("preview.html", {
        "request": request,
        "stats": stats,
        "rows": rows,
        "token": token,
    })


@app.get("/preview", response_class=HTMLResponse)
def preview_get(request: Request):
    """Alias para /panel."""
    return panel_get(request)


@app.post("/preview", response_class=HTMLResponse)
def preview(
    request: Request,
    token: str = Form(...),

    email_col: str = Form(...),
    amount_col: str = Form(...),
    due_date_col: Optional[str] = Form(None),
    status_col: Optional[str] = Form(None),
    name_col: Optional[str] = Form(None),

    paid_value: str = Form("Pagado"),
    min_days_late: int = Form(0),
):
    try:
        if token not in UPLOADS:
            return templates.TemplateResponse("preview.html", {
                "request": request,
                "stats": {
                    "total_rows": 0,
                    "valid_rows": 0,
                    "unpaid_rows": 0,
                    "decision_reason": "Token inválido. Vuelve a subir el archivo.",
                },
                "rows": [],
            })

        item = UPLOADS[token]
        df = load_table(item["bytes"], item["filename"])

        # Validar que las columnas existan
        available_cols = set(df.columns)
        required_cols = [email_col, amount_col]
        missing_cols = [c for c in required_cols if c not in available_cols]
        
        if missing_cols:
            return templates.TemplateResponse("preview.html", {
                "request": request,
                "stats": {
                    "total_rows": len(df),
                    "valid_rows": 0,
                    "unpaid_rows": 0,
                    "decision_reason": f"Error: Las columnas {', '.join(missing_cols)} no existen en el archivo.",
                },
                "rows": [],
            })

        mapping = Mapping(
            email_col=email_col,
            amount_col=amount_col,
            due_date_col=(due_date_col or None) if (due_date_col and due_date_col in available_cols) else None,
            status_col=(status_col or None) if (status_col and status_col in available_cols) else None,
            name_col=(name_col or None) if (name_col and name_col in available_cols) else None,
        )
        rules = Rules(
            paid_value=paid_value,
            min_days_late=int(min_days_late) if min_days_late >= 0 else 0,
        )

        unpaid_df, stats = preview_unpaid(df, mapping, rules)
        
        # Limpiar datos: convertir NaN a None y fechas a strings
        rows: List[Dict[str, Any]] = []
        for _, row in unpaid_df.head(200).iterrows():
            clean_row = {
                "email": str(row.get("email", "")),
                "name": str(row.get("name", "")),
                "amount": float(row.get("amount", 0)) if pd.notna(row.get("amount")) else 0.0,
            }
            # Fecha de vencimiento
            due_date = row.get("due_date")
            if pd.notna(due_date) and due_date is not None:
                if isinstance(due_date, date):
                    clean_row["due_date"] = due_date.isoformat()
                else:
                    clean_row["due_date"] = str(due_date)
            else:
                clean_row["due_date"] = None
            
            # Días de retraso (solo si existe)
            if "days_late" in row and pd.notna(row.get("days_late")):
                clean_row["days_late"] = int(row["days_late"])
            else:
                clean_row["days_late"] = None
            
            rows.append(clean_row)

        save_state(token, mapping, rules)
        save_preview_cache(token, rows, stats)

        return templates.TemplateResponse("preview.html", {
            "request": request,
            "stats": stats,
            "rows": rows,
            "token": token,
        })
    except Exception as e:
        # Manejo de errores generales
        return templates.TemplateResponse("preview.html", {
            "request": request,
            "stats": {
                "total_rows": 0,
                "valid_rows": 0,
                "unpaid_rows": 0,
                "decision_reason": f"Error al procesar el archivo: {str(e)[:100]}",
            },
            "rows": [],
        })


@app.post("/sequence", response_class=HTMLResponse)
def sequence(request: Request, token: str = Form(...)):
    settings = get_settings()
    return templates.TemplateResponse("sequence.html", {
        "request": request,
        "settings": settings,
        "token": token,
    })


@app.post("/sequence/save", response_class=HTMLResponse)
def sequence_save(
    request: Request,
    token: str = Form(...),
    company_name: str = Form(...),
    signature_name: str = Form(...),
    from_email: str = Form(...),
    reply_to_email: str = Form(...),
    day_1: str = Form(...),
    day_2: str = Form(""),
):
    try:
        d1 = int(str(day_1).strip())
    except Exception:
        return HTMLResponse("day_1 inválido. Debe ser un número.", status_code=400)

    d2 = None
    try:
        if str(day_2).strip() != "":
            d2 = int(str(day_2).strip())
            if d2 <= 0:
                d2 = None
    except Exception:
        d2 = None

    save_settings(company_name, signature_name, d1, d2, from_email, reply_to_email)

    settings_now = get_settings()
    rows = get_preview_rows_from_db(token)
    if not rows:
        return templates.TemplateResponse("activated.html", {
            "request": request,
            "created": 0,
            "error": "No hay impagos en el panel. Vuelve al panel y verifica los datos.",
        })
    
    created = create_or_refresh_reminders_from_rows(settings_now, rows, token)
    
    # Si created es 0 pero hay rows, contar los recordatorios pendientes existentes
    if created == 0 and len(rows) > 0:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM reminders WHERE status = 'pending'")
        count = cur.fetchone()["c"]
        conn.close()
        if count > 0:
            created = count

    return templates.TemplateResponse("activated.html", {
        "request": request,
        "created": created,
        "token": token,
    })


# --- Actualizar archivo sin pedir re-subir
@app.post("/refresh", response_class=HTMLResponse)
async def refresh(request: Request, token: str = Form(...)):
    """Recalcula impagos usando el archivo guardado, sin pedir re-subir."""
    if not token:
        st = get_state()
        token = st.get("token")
    
    if not token:
        return templates.TemplateResponse("preview.html", {
            "request": request,
            "stats": {
                "total_rows": 0,
                "valid_rows": 0,
                "unpaid_rows": 0,
                "decision_reason": "No hay archivo previo.",
            },
            "rows": [],
            "token": None,
        })
    
    # Cargar archivo desde disco
    file_path = FILE_PATHS.get(token)
    if not file_path or not Path(file_path).exists():
        # Fallback a memoria
        if token not in UPLOADS:
            return templates.TemplateResponse("preview.html", {
                "request": request,
                "stats": {
                    "total_rows": 0,
                    "valid_rows": 0,
                    "unpaid_rows": 0,
                    "decision_reason": "Archivo no encontrado. Sube el archivo de nuevo.",
                },
                "rows": [],
                "token": None,
            })
        b = UPLOADS[token]["bytes"]
        filename = UPLOADS[token]["filename"]
    else:
        b = Path(file_path).read_bytes()
        filename = Path(file_path).name.split("_", 1)[1] if "_" in Path(file_path).name else "file.xlsx"
    
    # Obtener mapping y rules desde state
    st = get_state()
    if not st.get("email_col") or not st.get("amount_col"):
        return templates.TemplateResponse("preview.html", {
            "request": request,
            "stats": {
                "total_rows": 0,
                "valid_rows": 0,
                "unpaid_rows": 0,
                "decision_reason": "No hay configuración de columnas. Vuelve a seleccionar las columnas.",
            },
            "rows": [],
            "token": token,
        })
    
    df = load_table(b, filename)
    available_cols = set(df.columns)
    
    mapping = Mapping(
        email_col=st["email_col"],
        amount_col=st["amount_col"],
        due_date_col=(st.get("due_date_col") or None) if (st.get("due_date_col") and st.get("due_date_col") in available_cols) else None,
        status_col=(st.get("status_col") or None) if (st.get("status_col") and st.get("status_col") in available_cols) else None,
        name_col=(st.get("name_col") or None) if (st.get("name_col") and st.get("name_col") in available_cols) else None,
    )
    rules = Rules(
        paid_value=st.get("paid_value") or "Pagado",
        min_days_late=int(st.get("min_days_late") or 0),
    )
    
    unpaid_df, stats = preview_unpaid(df, mapping, rules)
    
    # Limpiar datos
    rows: List[Dict[str, Any]] = []
    for _, row in unpaid_df.head(200).iterrows():
        clean_row = {
            "email": str(row.get("email", "")),
            "name": str(row.get("name", "")),
            "amount": float(row.get("amount", 0)) if pd.notna(row.get("amount")) else 0.0,
        }
        due_date = row.get("due_date")
        if pd.notna(due_date) and due_date is not None:
            if isinstance(due_date, date):
                clean_row["due_date"] = due_date.isoformat()
            else:
                clean_row["due_date"] = str(due_date)
        else:
            clean_row["due_date"] = None
        
        if "days_late" in row and pd.notna(row.get("days_late")):
            clean_row["days_late"] = int(row["days_late"])
        else:
            clean_row["days_late"] = None
        
        rows.append(clean_row)
    
    save_preview_cache(token, rows, stats)
    
    # Actualizar recordatorios
    settings_now = get_settings()
    created = create_or_refresh_reminders_from_rows(settings_now, rows, token)
    
    return templates.TemplateResponse("preview.html", {
        "request": request,
        "stats": {**stats, "updated_scheduled": created, "refreshed": True},
        "rows": rows,
        "token": token,
    })


@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request, token: Optional[str] = None):
    """Muestra recordatorios (reminders) con estado real basado en send_at vs now."""
    conn = get_conn()
    cur = conn.cursor()
    
    # Obtener recordatorios desde reminders (no send_log)
    cur.execute("""
        SELECT r.id, r.stage, r.send_at, r.sent_at, r.status, r.last_error,
               d.email, d.name, d.amount
        FROM reminders r
        JOIN debts d ON d.id = r.debt_id
        ORDER BY r.send_at DESC, r.id DESC
        LIMIT 50
    """)
    rows_raw = cur.fetchall()
    conn.close()

    now = datetime.utcnow()
    rows = []
    for r in rows_raw:
        send_at_str = r["send_at"]
        sent_at_str = r["sent_at"]
        
        try:
            send_at = datetime.fromisoformat(send_at_str.replace("Z", "+00:00"))
            send_at_formatted = send_at.strftime("%d/%m/%Y %H:%M")
        except Exception:
            send_at = None
            send_at_formatted = send_at_str[:16] if len(send_at_str) > 16 else send_at_str
        
        sent_at_formatted = None
        if sent_at_str:
            try:
                sent_at_dt = datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
                sent_at_formatted = sent_at_dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                sent_at_formatted = sent_at_str[:16] if len(sent_at_str) > 16 else sent_at_str

        # Estado real: solo "Enviado" si realmente se envió
        status_db = r["status"]
        if status_db == "sent" and sent_at_str:
            status_display = "Enviado"
        elif status_db == "failed":
            status_display = "Error"
        elif status_db == "canceled":
            status_display = "Cancelado"
        else:  # pending
            status_display = "Pendiente"

        rows.append({
            "send_at_formatted": send_at_formatted,
            "sent_at_formatted": sent_at_formatted,
            "to_email": r["email"],
            "to_name": r["name"] or "",
            "amount": r["amount"],
            "stage": r["stage"],
            "status": status_db,
            "status_display": status_display,
            "error": r["last_error"] if (status_db == "failed" and r["last_error"]) else None,
        })

    st = get_state()
    current_token = token or st.get("token")

    return templates.TemplateResponse("activity.html", {
        "request": request,
        "rows": rows,
        "token": current_token,
    })


@app.get("/logs", response_class=HTMLResponse)
def logs():
    return RedirectResponse(url="/activity")
