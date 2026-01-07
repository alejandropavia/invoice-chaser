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
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import logging

import pandas as pd
from core import load_table, get_columns, Mapping, Rules, preview_unpaid
from email_templates import get_email_template

app = FastAPI(title="Invoice Chaser")
templates = Jinja2Templates(directory="templates")

logger = logging.getLogger("uvicorn.error")

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

    # settings: email_language, chase_1_days, chase_2_days
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            signature_name TEXT NOT NULL,
            chase_1_days INTEGER NOT NULL,
            chase_2_days INTEGER,
            from_email TEXT NOT NULL,
            reply_to_email TEXT,
            email_language TEXT NOT NULL DEFAULT 'en'
        )
    """)
    
    # Add email_language column if missing
    try:
        cur.execute("ALTER TABLE settings ADD COLUMN email_language TEXT DEFAULT 'en'")
    except Exception:
        pass
    
    # Migrate old day_1/day_2 to chase_1_days/chase_2_days if needed
    try:
        cur.execute("ALTER TABLE settings ADD COLUMN chase_1_days INTEGER")
        cur.execute("ALTER TABLE settings ADD COLUMN chase_2_days INTEGER")
        # Copy old values
        cur.execute("UPDATE settings SET chase_1_days = day_1 WHERE chase_1_days IS NULL AND day_1 IS NOT NULL")
        cur.execute("UPDATE settings SET chase_2_days = day_2 WHERE chase_2_days IS NULL AND day_2 IS NOT NULL")
    except Exception:
        pass

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

    # reminders: stage 0 = due date, 1 = chase #1, 2 = chase #2
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            debt_id INTEGER NOT NULL,
            stage INTEGER NOT NULL,                 -- 0 = due date, 1 = chase #1, 2 = chase #2
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
        # Try new schema first
        try:
            cur.execute("""
                INSERT INTO settings (company_name, signature_name, chase_1_days, chase_2_days, from_email, reply_to_email, email_language)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ("Your Company", "Your Name", 2, 5, "notifications@invoicechaserapp.com", "", "en"))
        except Exception:
            # Fallback to old schema
            cur.execute("""
                INSERT INTO settings (company_name, signature_name, day_1, day_2, from_email, reply_to_email)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ("Your Company", "Your Name", 2, 5, "notifications@invoicechaserapp.com", ""))

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
    settings = dict(row)
    # Ensure email_language defaults to 'en'
    if not settings.get("email_language"):
        settings["email_language"] = "en"
    # Migrate old day_1/day_2 if present
    if "day_1" in settings and "chase_1_days" not in settings:
        settings["chase_1_days"] = settings.get("day_1", 2)
    if "day_2" in settings and "chase_2_days" not in settings:
        settings["chase_2_days"] = settings.get("day_2", 5)
    return settings


def save_settings(company_name: str, signature_name: str, chase_1_days: int, chase_2_days: Optional[int], from_email: str, reply_to_email: str, email_language: str = "en"):
    conn = get_conn()
    cur = conn.cursor()
    # Try new schema first
    try:
        cur.execute("""
            INSERT INTO settings (company_name, signature_name, chase_1_days, chase_2_days, from_email, reply_to_email, email_language)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (company_name, signature_name, int(chase_1_days), (int(chase_2_days) if chase_2_days is not None else None), from_email, reply_to_email, email_language))
    except Exception:
        # Fallback to old schema (for migration)
        cur.execute("""
            INSERT INTO settings (company_name, signature_name, day_1, day_2, from_email, reply_to_email)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (company_name, signature_name, int(chase_1_days), (int(chase_2_days) if chase_2_days is not None else None), from_email, reply_to_email))
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
def pick_send_datetime(base_date: date, seed: str, now: Optional[datetime] = None) -> datetime:
    """
    Generates a deterministic datetime between 09:10 and 11:40 in Europe/Madrid timezone.
    Never schedules in the past - if base_date is today and time has passed, schedules for tomorrow.
    Uses hash of seed for consistent time per invoice.
    Returns timezone-aware datetime.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Madrid")
        use_zoneinfo = True
    except ImportError:
        # Python < 3.9 fallback
        try:
            import pytz
            tz = pytz.timezone("Europe/Madrid")
            use_zoneinfo = False
        except ImportError:
            # No timezone support - use UTC and log warning
            logger.warning("No timezone support available. Using UTC. Install pytz for timezone support.")
            tz = None
            use_zoneinfo = False
    
    if now is None:
        if tz:
            if use_zoneinfo:
                now = datetime.now(tz)
            else:
                now = datetime.now(tz)
        else:
            now = datetime.utcnow()
    else:
        # Ensure now is in Madrid timezone
        if tz:
            if now.tzinfo is None:
                if use_zoneinfo:
                    now = now.replace(tzinfo=tz)
                else:
                    now = tz.localize(now)
            else:
                if use_zoneinfo:
                    now = now.astimezone(tz)
                else:
                    now = now.astimezone(tz)
    
    # Create deterministic hash from seed
    hash_obj = hashlib.md5(seed.encode())
    hash_int = int(hash_obj.hexdigest(), 16)
    
    # Hour between 9-11 (3 hours: 9, 10, 11)
    hour = 9 + (hash_int % 3)
    
    # Minutes between 10-40 (31 possible minutes)
    minute = 10 + (hash_int % 31)
    
    # Create datetime
    if tz:
        if use_zoneinfo:
            target_datetime = datetime.combine(base_date, dt_time(hour, minute))
            target_datetime = target_datetime.replace(tzinfo=tz)
        else:
            target_datetime = tz.localize(datetime.combine(base_date, dt_time(hour, minute)))
    else:
        target_datetime = datetime.combine(base_date, dt_time(hour, minute))
    
    # If target is in the past, move to next day
    if target_datetime <= now:
        target_datetime = target_datetime + timedelta(days=1)
        # Recalculate time for next day (keep same hour/minute pattern)
        if tz:
            if use_zoneinfo:
                target_datetime = datetime.combine(target_datetime.date(), dt_time(hour, minute))
                target_datetime = target_datetime.replace(tzinfo=tz)
            else:
                target_datetime = tz.localize(datetime.combine(target_datetime.date(), dt_time(hour, minute)))
        else:
            target_datetime = datetime.combine(target_datetime.date(), dt_time(hour, minute))
    
    return target_datetime


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
    Creates reminders: stage 0 = due date, 1 = chase #1, 2 = chase #2.
    For old invoices, schedules ONLY the next pending reminder (no flood).
    Cancels pending reminders for debts no longer in current list.
    """
    # Get chase days (migrate from old day_1/day_2 if needed)
    chase_1_days = int(settings.get("chase_1_days") or settings.get("day_1", 2))
    chase_2_days_raw = settings.get("chase_2_days") or settings.get("day_2")
    chase_2_days = int(chase_2_days_raw) if (chase_2_days_raw is not None and str(chase_2_days_raw) != "") else None

    created = 0
    current_debt_ids: List[int] = []
    
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Madrid")
        now = datetime.now(tz)
    except ImportError:
        import pytz
        tz = pytz.timezone("Europe/Madrid")
        now = datetime.now(tz)

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
        if not due:
            continue  # Skip if no due date
        
        due_iso = due.isoformat()
        debt_id = upsert_debt(to_email, name, float(amount), due_iso)
        current_debt_ids.append(debt_id)

        # Check existing reminders for this debt
        cur.execute("""
            SELECT stage, status, send_at FROM reminders
            WHERE debt_id = ? AND status IN ('pending', 'sent')
            ORDER BY stage ASC
        """, (debt_id,))
        existing = cur.fetchall()
        existing_stages = {int(row["stage"]): row["status"] for row in existing}
        
        # Calculate dates
        due_date = due
        chase_1_date = due_date + timedelta(days=chase_1_days)
        chase_2_date = due_date + timedelta(days=chase_2_days) if chase_2_days else None
        
        # Determine which reminder to schedule
        # For old invoices (due date in past), schedule ONLY the next pending one
        # For future invoices, schedule all appropriate stages
        stages_to_schedule = []
        today = date.today()
        
        is_old_invoice = due_date < today
        
        if is_old_invoice:
            # Old invoice: schedule ONLY the next pending reminder
            if 0 not in existing_stages:
                # Due date reminder not sent yet
                stages_to_schedule.append((0, due_date))
            elif existing_stages.get(0) == "sent":
                # Due date sent, check chase #1
                if 1 not in existing_stages:
                    stages_to_schedule.append((1, chase_1_date))
                elif existing_stages.get(1) == "sent":
                    # Chase #1 sent, check chase #2
                    if chase_2_days and 2 not in existing_stages:
                        stages_to_schedule.append((2, chase_2_date))
        else:
            # Future invoice: schedule all appropriate stages
            if 0 not in existing_stages:
                stages_to_schedule.append((0, due_date))
            if 1 not in existing_stages:
                stages_to_schedule.append((1, chase_1_date))
            if chase_2_days and 2 not in existing_stages:
                stages_to_schedule.append((2, chase_2_date))
        
        # Schedule reminders
        for stage, target_date in stages_to_schedule:
            seed = f"{to_email}|{stage}|{token or 'default'}"
            send_at = pick_send_datetime(target_date, seed, now)
            send_at_iso = send_at.isoformat()
            
            # Check if already exists
            cur.execute("""
                SELECT COUNT(*) AS c FROM reminders
                WHERE debt_id = ? AND stage = ? AND send_at = ? AND status IN ('pending','sent')
            """, (debt_id, stage, send_at_iso))
            if cur.fetchone()["c"] == 0:
                cur.execute("""
                    INSERT INTO reminders (debt_id, stage, send_at, status)
                    VALUES (?, ?, ?, 'pending')
                """, (debt_id, stage, send_at_iso))
                created += 1

    conn.commit()
    conn.close()

    cancel_pending_for_debts_not_in_current(current_debt_ids)

    return created


# -------------------------
# Email (Reply-To del cliente)
# -------------------------
def build_email(stage: int, to_name: str, amount: float, due_date: Optional[date], invoice_number: str, settings: Dict[str, Any]) -> Tuple[str, str]:
    """
    Builds email subject and HTML body using templates.
    stage: 0 = due date, 1 = chase #1, 2 = chase #2
    """
    company = settings["company_name"]
    signature = settings["signature_name"]
    name = to_name.strip() if to_name and to_name.strip() else ""
    amount_str = f"{amount:.2f}€" if amount else "0€"
    language = settings.get("email_language", "en")
    
    # Format due date
    if due_date:
        if language == "es":
            due_date_str = due_date.strftime("%d/%m/%Y")
        else:
            due_date_str = due_date.strftime("%B %d, %Y")
    else:
        due_date_str = "N/A"
    
    # Get template
    subject, body_template = get_email_template(stage, language)
    
    # Format body
    if language == "es":
        greeting = name if name else "Hola"
    else:
        greeting = name if name else "Hello"
    
    body = body_template.format(
        name=greeting,
        invoice_number=invoice_number,
        amount=amount_str,
        due_date=due_date_str,
        company=f"{company}<br/>{signature}"
    )
    
    return subject, body


def send_email_via_resend(to_email: str, subject: str, html_body: str, reply_to: Optional[str] = None) -> None:
    """Send email via Resend API."""
    from email_service import send_email as resend_send
    try:
        resend_send(to_email, subject, html_body, reply_to)
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        raise


# -------------------------
# Worker: envía cuando toca
# -------------------------
def process_due_reminders_loop():
    while True:
        try:
            settings = get_settings()
            reply_to = (settings.get("reply_to_email") or "").strip()

            conn = get_conn()
            cur = conn.cursor()
            now_iso = datetime.utcnow().isoformat()

            cur.execute("""
                SELECT r.id AS rid, r.stage, r.send_at, d.email, d.name, d.amount, d.due_date
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
                due_date_str = row.get("due_date")
                due_date = None
                if due_date_str:
                    try:
                        due_date = datetime.fromisoformat(due_date_str).date()
                    except:
                        try:
                            due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
                        except:
                            pass
                
                # Generate invoice number (simple hash of email + amount + due_date)
                invoice_seed = f"{to_email}{amount}{due_date_str or ''}"
                invoice_hash = hashlib.md5(invoice_seed.encode()).hexdigest()[:8].upper()
                invoice_number = f"INV-{invoice_hash}"

                subject, html_body = build_email(stage, to_name, amount, due_date, invoice_number, settings)

                try:
                    send_email_via_resend(to_email, subject, html_body, reply_to if reply_to else None)

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
                    error_msg = str(e)
                    cur.execute("""
                        UPDATE reminders
                        SET status = 'failed', sent_at = ?, last_error = ?
                        WHERE id = ?
                    """, (sent_at, error_msg, rid))

                    cur.execute("""
                        INSERT INTO send_log (to_email, subject, stage, sent_at, status, error)
                        VALUES (?, ?, ?, ?, 'failed', ?)
                    """, (to_email, subject, stage, sent_at, error_msg))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"[WORKER] Error: {e}")

        time.sleep(30)


@app.on_event("startup")
def on_startup():
    port = os.getenv("PORT", "10000")
    logger.info(f"Starting Invoice Chaser. PORT={port}")
    ensure_tables()
    logger.info("Database tables ensured")
    t = threading.Thread(target=process_due_reminders_loop, daemon=True)
    t.start()
    logger.info("Background worker thread started")
    
    # Temporary email test - only runs if TEST_EMAIL and RESEND_API_KEY are set
    # This will be removed after confirmation
    try:
        from email_service import send_email
        test_email = os.getenv("TEST_EMAIL")
        if test_email and os.getenv("RESEND_API_KEY"):
            try:
                send_email(
                    to_email=test_email,
                    subject="Invoice Chaser – Email Test",
                    html="""
                    <p>Hello,</p>
                    <p>This is a real test email sent from Invoice Chaser.</p>
                    <p>If you received this, the email system is working correctly.</p>
                    """
                )
                logger.info(f"Test email sent to {test_email}")
            except Exception as e:
                logger.warning(f"Test email failed (non-critical): {e}")
    except Exception as e:
        logger.warning(f"Email service import failed (non-critical): {e}")


# -------------------------
# Routes
# -------------------------
@app.get("/health")
def health():
    """Health check endpoint for Render."""
    return {"ok": True}


@app.get("/version")
def version():
    """Version endpoint for deployment verification."""
    commit_hash = os.getenv("RENDER_GIT_COMMIT", os.getenv("GIT_COMMIT", "unknown"))
    return {"version": commit_hash, "service": "Invoice Chaser"}


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
    reply_to_email: str = Form(...),
    chase_1_days: str = Form(...),
    chase_2_days: str = Form(""),
    email_language: str = Form("en"),
):
    try:
        c1 = int(str(chase_1_days).strip())
        if c1 < 0:
            return HTMLResponse("Chase #1 days must be >= 0.", status_code=400)
    except Exception:
        return HTMLResponse("Chase #1 days must be a number.", status_code=400)

    c2 = None
    try:
        if str(chase_2_days).strip() != "":
            c2 = int(str(chase_2_days).strip())
            if c2 <= 0:
                c2 = None
    except Exception:
        c2 = None

    # Validate language
    if email_language not in ["en", "es"]:
        email_language = "en"
    
    # FROM email is fixed (custom domain)
    from_email = f"notifications@{os.getenv('CUSTOM_DOMAIN', 'invoicechaserapp.com')}"
    
    save_settings(company_name, signature_name, c1, c2, from_email, reply_to_email, email_language)

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


