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
    # New schema with all required fields
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            send_at TEXT NOT NULL,
            sent_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            reminder_type TEXT NOT NULL,  -- due_date | chase_1 | chase_2
            to_email TEXT NOT NULL,
            reply_to TEXT,
            invoice_number TEXT NOT NULL,
            client_name TEXT,
            amount TEXT NOT NULL,
            currency TEXT DEFAULT 'EUR',
            due_date TEXT,
            days_overdue INTEGER,
            company_name TEXT,
            signature TEXT,
            invoice_key TEXT NOT NULL,  -- (to_email + invoice_number + due_date) for cooldown
            last_sent_at TEXT,  -- for cooldown enforcement
            debt_id INTEGER,  -- keep for backward compatibility
            stage INTEGER,  -- keep for backward compatibility (0=due_date, 1=chase_1, 2=chase_2)
            FOREIGN KEY(debt_id) REFERENCES debts(id)
        )
    """)
    
    # Migrate existing reminders if needed
    try:
        cur.execute("ALTER TABLE reminders ADD COLUMN id_new TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN created_at TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN reminder_type TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN to_email TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN reply_to TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN invoice_number TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN client_name TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN amount TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN currency TEXT DEFAULT 'EUR'")
        cur.execute("ALTER TABLE reminders ADD COLUMN due_date TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN days_overdue INTEGER")
        cur.execute("ALTER TABLE reminders ADD COLUMN company_name TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN signature TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN invoice_key TEXT")
        cur.execute("ALTER TABLE reminders ADD COLUMN last_sent_at TEXT")
    except Exception:
        pass  # Columns may already exist

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
    Works with both old and new schema.
    """
    conn = get_conn()
    cur = conn.cursor()

    if not current_debt_ids:
        # Cancel all pending reminders
        try:
            # Try new schema first (error field)
            cur.execute("""
                UPDATE reminders
                SET status='canceled', error=NULL
                WHERE status='pending'
            """)
        except Exception:
            # Fallback to old schema (last_error field)
            cur.execute("""
                UPDATE reminders
                SET status='canceled', last_error=NULL
                WHERE status='pending'
            """)
        conn.commit()
        conn.close()
        return

    placeholders = ",".join(["?"] * len(current_debt_ids))
    try:
        # Try new schema first
        cur.execute(f"""
            UPDATE reminders
            SET status='canceled', error=NULL
            WHERE status='pending' AND debt_id NOT IN ({placeholders})
        """, current_debt_ids)
    except Exception:
        # Fallback to old schema
        cur.execute(f"""
            UPDATE reminders
            SET status='canceled', last_error=NULL
            WHERE status='pending' AND debt_id NOT IN ({placeholders})
        """, current_debt_ids)

    conn.commit()
    conn.close()


def clamp_to_future(dt: datetime, now: datetime) -> datetime:
    """
    If dt is in the past, move it to now + 2 minutes.
    Returns timezone-aware datetime.
    """
    if dt <= now:
        return now + timedelta(minutes=2)
    return dt


def create_or_refresh_reminders_from_rows(settings: Dict[str, Any], rows: List[Dict[str, Any]], token: Optional[str] = None, include_due_date: bool = True) -> int:
    """
    Creates ALL 3 reminders per invoice (due_date, chase_1, chase_2) with new schema.
    Cooldown is enforced in scheduler (6 hours per invoice_key).
    All send_at times are clamped to future (now + 2 minutes minimum).
    """
    # Get chase days (migrate from old day_1/day_2 if needed)
    chase_1_days = int(settings.get("chase_1_days") or settings.get("day_1", 2))
    chase_2_days_raw = settings.get("chase_2_days") or settings.get("day_2")
    chase_2_days = int(chase_2_days_raw) if (chase_2_days_raw is not None and str(chase_2_days_raw) != "") else None

    logger.info(f"[REMINDERS] Creating reminders: chase_1_days={chase_1_days}, chase_2_days={chase_2_days}, include_due_date={include_due_date}")
    logger.info(f"[REMINDERS] Processing {len(rows)} invoices")

    created = 0
    current_debt_ids: List[int] = []
    
    # Get current time for comparison (timezone-aware)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Madrid")
        use_zoneinfo = True
    except ImportError:
        try:
            import pytz
            tz = pytz.timezone("Europe/Madrid")
            use_zoneinfo = False
        except ImportError:
            tz = None
            use_zoneinfo = False
    
    if tz:
        if use_zoneinfo:
            now = datetime.now(tz)
        else:
            now = datetime.now(tz)
    else:
        now = datetime.utcnow()

    # Get settings for email
    company_name = settings.get("company_name", "Your Company")
    signature = settings.get("signature_name", "")
    reply_to = (settings.get("reply_to_email") or "").strip()
    currency = "EUR"  # Default currency

    conn = get_conn()
    cur = conn.cursor()

    for r in rows:
        to_email = (r.get("email") or "").strip()
        if not to_email or "@" not in to_email:
            continue

        amount = r.get("amount")
        if amount is None or amount == "":
            continue

        client_name = (r.get("name") or "").strip()
        due = _parse_due_date(r.get("due_date"))
        if not due:
            continue  # Skip if no due date
        
        due_iso = due.isoformat()
        debt_id = upsert_debt(to_email, client_name, float(amount), due_iso)
        current_debt_ids.append(debt_id)

        # Generate invoice number (consistent hash)
        invoice_seed = f"{to_email}{amount}{due_iso}"
        invoice_hash = hashlib.md5(invoice_seed.encode()).hexdigest()[:8].upper()
        invoice_number = f"INV-{invoice_hash}"
        
        # Invoice key for cooldown (unique per invoice)
        invoice_key = f"{to_email}|{invoice_number}|{due_iso}"
        
        # Calculate days overdue
        today = date.today()
        days_overdue = (today - due).days if due < today else 0
        
        # Calculate candidate reminder dates
        due_date = due
        chase_1_date = due_date + timedelta(days=chase_1_days)
        chase_2_date = due_date + timedelta(days=chase_2_days) if chase_2_days and chase_2_days > 0 else None
        
        # Create all 3 reminders (cooldown enforced in scheduler)
        reminders_to_create = []
        
        # 1. Due date reminder (always if include_due_date)
        if include_due_date:
            reminders_to_create.append({
                "reminder_type": "due_date",
                "stage": 0,
                "target_date": due_date,
                "days_overdue": 0,
            })
        
        # 2. Chase 1 reminder (mandatory)
        reminders_to_create.append({
            "reminder_type": "chase_1",
            "stage": 1,
            "target_date": chase_1_date,
            "days_overdue": chase_1_days,
        })
        
        # 3. Chase 2 reminder (optional, only if chase_2_days > 0)
        if chase_2_date:
            reminders_to_create.append({
                "reminder_type": "chase_2",
                "stage": 2,
                "target_date": chase_2_date,
                "days_overdue": chase_2_days,
            })
        
        # Create each reminder
        for rem_info in reminders_to_create:
            reminder_type = rem_info["reminder_type"]
            stage = rem_info["stage"]
            target_date = rem_info["target_date"]
            days_overdue_for_reminder = rem_info["days_overdue"]
            
            # Check if reminder already exists (by invoice_key and reminder_type)
            cur.execute("""
                SELECT id FROM reminders
                WHERE invoice_key = ? AND reminder_type = ? AND status IN ('pending', 'sent')
                LIMIT 1
            """, (invoice_key, reminder_type))
            existing = cur.fetchone()
            if existing:
                continue  # Skip if already exists
            
            # Calculate send_at time
            seed = f"{to_email}|{stage}|{token or 'default'}"
            send_at = pick_send_datetime(target_date, seed, now)
            
            # NEVER schedule in the past - clamp to future (now + 2 minutes minimum)
            if send_at <= now:
                send_at = now + timedelta(minutes=2)
            send_at_iso = send_at.isoformat()
            
            # Create reminder ID
            reminder_id = f"{invoice_key}|{reminder_type}|{send_at_iso}"
            
            # Insert reminder with all fields
            try:
                cur.execute("""
                    INSERT INTO reminders (
                        id, created_at, send_at, status, reminder_type,
                        to_email, reply_to, invoice_number, client_name,
                        amount, currency, due_date, days_overdue,
                        company_name, signature, invoice_key,
                        debt_id, stage
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    reminder_id,
                    now.isoformat(),
                    send_at_iso,
                    reminder_type,
                    to_email,
                    reply_to if reply_to else None,
                    invoice_number,
                    client_name if client_name else None,
                    str(amount),
                    currency,
                    due_iso,
                    days_overdue_for_reminder,
                    company_name,
                    signature if signature else None,
                    invoice_key,
                    debt_id,
                    stage,
                ))
                created += 1
                logger.info(f"[REMINDER] Created {reminder_type} reminder for {to_email} (invoice {invoice_number}), send_at={send_at_iso}")
            except sqlite3.IntegrityError:
                # Already exists, skip
                logger.debug(f"[REMINDER] Reminder {reminder_id} already exists, skipping")
                continue

    conn.commit()
    conn.close()

    cancel_pending_for_debts_not_in_current(current_debt_ids)
    
    logger.info(f"[REMINDERS] Created {created} reminders total")

    return created


# -------------------------
# Email (Reply-To del cliente)
# -------------------------
def build_email(stage: int, to_name: str, amount: float, due_date: Optional[date], invoice_number: str, settings: Dict[str, Any]) -> Tuple[str, str]:
    """
    Builds email subject and HTML body using templates (legacy function for backward compatibility).
    stage: 0 = due date, 1 = chase #1, 2 = chase #2
    """
    company = settings["company_name"]
    signature = settings["signature_name"]
    name = to_name.strip() if to_name and to_name.strip() else ""
    amount_str = f"{amount:.2f}€" if amount else "0€"
    language = settings.get("email_language", "es")  # Default to Spanish
    
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


def build_email_new(
    reminder_type: str,
    client_name: str,
    amount: float,
    currency: str,
    due_date: Optional[date],
    days_overdue: int,
    invoice_number: str,
    company_name: str,
    signature: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Builds email subject and HTML body using new schema (Spanish only for MVP).
    reminder_type: 'due_date' | 'chase_1' | 'chase_2'
    """
    # Format amount with currency
    if currency == "EUR" or currency == "€":
        amount_str = f"{amount:.2f}€"
    else:
        amount_str = f"{amount:.2f} {currency}"
    
    # Format due date (Spanish format)
    if due_date:
        due_date_str = due_date.strftime("%d/%m/%Y")
    else:
        due_date_str = "N/A"
    
    # Format greeting
    greeting = client_name if client_name and client_name.strip() else "Hola"
    
    # Build signature
    signature_line = ""
    if signature and signature.strip():
        signature_line = f"<br/>{signature}"
    
    # Build email based on reminder type
    if reminder_type == "due_date":
        subject = f"Recordatorio: factura {invoice_number} vence hoy"
        body = f"""<p>Hola {greeting},</p>
<p>Te recordamos que hoy vence la factura {invoice_number} por {amount_str}.</p>
<p>Si ya has realizado el pago, puedes ignorar este mensaje.</p>
<p>Gracias,<br/>{company_name}{signature_line}</p>"""
    
    elif reminder_type == "chase_1":
        subject = f"Factura pendiente: {invoice_number} ({days_overdue} días de retraso)"
        body = f"""<p>Hola {greeting},</p>
<p>Según nuestros registros, la factura {invoice_number} por {amount_str} sigue pendiente.</p>
<p>Han pasado {days_overdue} días desde la fecha de vencimiento ({due_date_str}).</p>
<p>¿Podrías confirmarnos cuándo se realizará el pago?</p>
<p>Gracias,<br/>{company_name}{signature_line}</p>"""
    
    elif reminder_type == "chase_2":
        subject = f"Segundo recordatorio: factura {invoice_number} pendiente"
        body = f"""<p>Hola {greeting},</p>
<p>Este es un segundo recordatorio sobre la factura {invoice_number} por {amount_str}, que venció el {due_date_str}.</p>
<p>Han pasado {days_overdue} días desde la fecha de vencimiento.</p>
<p>Si ya has realizado el pago, puedes ignorar este mensaje. Si no, te agradeceríamos que nos confirmes cuándo se realizará el pago.</p>
<p>Gracias,<br/>{company_name}{signature_line}</p>"""
    
    else:
        # Fallback
        subject = f"Recordatorio: factura {invoice_number}"
        body = f"""<p>Hola {greeting},</p>
<p>Te recordamos que la factura {invoice_number} por {amount_str} sigue pendiente.</p>
<p>Gracias,<br/>{company_name}{signature_line}</p>"""
    
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
async def scheduler_loop():
    """Async scheduler that runs every 30 seconds, enforces cooldown (6 hours per invoice_key)."""
    import asyncio
    
    while True:
        try:
            await asyncio.sleep(30)  # Wait 30 seconds between checks
            
            settings = get_settings()
            
            # Get current time (timezone-aware)
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo("Europe/Madrid")
                use_zoneinfo = True
            except ImportError:
                try:
                    import pytz
                    tz = pytz.timezone("Europe/Madrid")
                    use_zoneinfo = False
                except ImportError:
                    tz = None
                    use_zoneinfo = False
            
            if tz:
                if use_zoneinfo:
                    now = datetime.now(tz)
                else:
                    now = datetime.now(tz)
            else:
                now = datetime.utcnow()
            
            now_iso = now.isoformat()
            
            conn = get_conn()
            cur = conn.cursor()
            
            # Fetch pending reminders that are due (try new schema first, fallback to old)
            try:
                cur.execute("""
                    SELECT id, reminder_type, to_email, client_name, amount, currency,
                           due_date, days_overdue, invoice_number, invoice_key, last_sent_at,
                           company_name, signature, reply_to, stage
                    FROM reminders
                    WHERE status = 'pending' AND send_at <= ?
                    ORDER BY send_at ASC
                    LIMIT 20
                """, (now_iso,))
                due = cur.fetchall()
            except Exception as e:
                # Fallback to old schema if new columns don't exist
                logger.warning(f"[SCHEDULER] New schema query failed, trying old schema: {e}")
                try:
                    cur.execute("""
                        SELECT r.id AS id, r.stage AS stage, r.send_at AS send_at,
                               d.email AS to_email, d.name AS client_name, d.amount AS amount,
                               d.due_date AS due_date
                        FROM reminders r
                        JOIN debts d ON d.id = r.debt_id
                        WHERE r.status = 'pending' AND r.send_at <= ?
                        ORDER BY r.send_at ASC
                        LIMIT 20
                    """, (now_iso,))
                    due_old = cur.fetchall()
                    # Convert old schema to new format (skip for now, just log)
                    logger.warning(f"[SCHEDULER] Found {len(due_old)} reminders in old schema format - migration needed")
                    due = []
                except Exception as e2:
                    logger.error(f"[SCHEDULER] Both schema queries failed: {e2}")
                    due = []
            
            sent_count = 0
            skipped_cooldown = 0
            
            for row in due:
                reminder_id = row["id"]
                invoice_key = row.get("invoice_key")
                if not invoice_key:
                    # Generate invoice_key from available data (backward compatibility)
                    to_email = row.get("to_email") or ""
                    invoice_number = row.get("invoice_number") or ""
                    due_date_str = row.get("due_date") or ""
                    invoice_key = f"{to_email}|{invoice_number}|{due_date_str}"
                last_sent_at_str = row.get("last_sent_at")
                
                # Enforce cooldown: 6 hours per invoice_key
                if last_sent_at_str:
                    try:
                        last_sent_at = datetime.fromisoformat(last_sent_at_str.replace("Z", "+00:00"))
                        if tz:
                            if use_zoneinfo:
                                last_sent_at = last_sent_at.astimezone(tz)
                            else:
                                last_sent_at = last_sent_at.astimezone(tz)
                        time_since_last = now - last_sent_at
                        if time_since_last.total_seconds() < 6 * 3600:  # 6 hours
                            skipped_cooldown += 1
                            logger.debug(f"[SCHEDULER] Skipping {reminder_id} due to cooldown (last sent {time_since_last.total_seconds()/3600:.1f}h ago)")
                            continue
                    except Exception as e:
                        logger.warning(f"[SCHEDULER] Error parsing last_sent_at: {e}")
                
                # Get reminder data
                to_email = row["to_email"]
                client_name = row.get("client_name") or ""
                amount_str = row["amount"]
                currency = row.get("currency") or "EUR"
                due_date_str = row.get("due_date")
                days_overdue = row.get("days_overdue") or 0
                invoice_number = row["invoice_number"]
                reminder_type = row["reminder_type"]
                stage = row.get("stage") or 0
                company_name = row.get("company_name") or settings.get("company_name", "Your Company")
                signature = row.get("signature") or settings.get("signature_name", "")
                reply_to = row.get("reply_to") or settings.get("reply_to_email") or ""
                
                # Parse due date
                due_date = None
                if due_date_str:
                    try:
                        due_date = datetime.fromisoformat(due_date_str).date()
                    except:
                        try:
                            due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
                        except:
                            pass
                
                # Parse amount
                try:
                    amount = float(amount_str)
                except:
                    amount = 0.0
                
                # Build email using new template system
                subject, html_body = build_email_new(
                    reminder_type=reminder_type,
                    client_name=client_name,
                    amount=amount,
                    currency=currency,
                    due_date=due_date,
                    days_overdue=days_overdue,
                    invoice_number=invoice_number,
                    company_name=company_name,
                    signature=signature,
                )
                
                try:
                    # Send email
                    send_email_via_resend(to_email, subject, html_body, reply_to if reply_to else None)
                    
                    sent_at = now.isoformat()
                    
                    # Update reminder status
                    cur.execute("""
                        UPDATE reminders
                        SET status = 'sent', sent_at = ?, error = NULL, last_sent_at = ?
                        WHERE id = ?
                    """, (sent_at, sent_at, reminder_id))
                    
                    # Update all reminders for this invoice_key with last_sent_at (for cooldown)
                    cur.execute("""
                        UPDATE reminders
                        SET last_sent_at = ?
                        WHERE invoice_key = ?
                    """, (sent_at, invoice_key))
                    
                    # Log to send_log
                    cur.execute("""
                        INSERT INTO send_log (to_email, subject, stage, sent_at, status, error)
                        VALUES (?, ?, ?, ?, 'sent', NULL)
                    """, (to_email, subject, stage, sent_at))
                    
                    sent_count += 1
                    logger.info(f"[SCHEDULER] Sent {reminder_type} email to {to_email} (invoice {invoice_number})")
                    
                except Exception as e:
                    sent_at = now.isoformat()
                    error_msg = str(e)
                    
                    cur.execute("""
                        UPDATE reminders
                        SET status = 'failed', sent_at = ?, error = ?
                        WHERE id = ?
                    """, (sent_at, error_msg, reminder_id))
                    
                    cur.execute("""
                        INSERT INTO send_log (to_email, subject, stage, sent_at, status, error)
                        VALUES (?, ?, ?, ?, 'failed', ?)
                    """, (to_email, subject, stage, sent_at, error_msg))
                    
                    logger.error(f"[SCHEDULER] Failed to send email to {to_email}: {error_msg}")
            
            conn.commit()
            conn.close()
            
            if sent_count > 0 or skipped_cooldown > 0:
                logger.info(f"[SCHEDULER] Processed: {sent_count} sent, {skipped_cooldown} skipped (cooldown)")
                
        except Exception as e:
            logger.error(f"[SCHEDULER] Error in scheduler loop: {e}")
            await asyncio.sleep(30)  # Wait before retrying


@app.on_event("startup")
async def on_startup():
    port = os.getenv("PORT", "10000")
    logger.info(f"Starting Invoice Chaser. PORT={port}")
    ensure_tables()
    logger.info("Database tables ensured")
    
    # Start async scheduler
    import asyncio
    asyncio.create_task(scheduler_loop())
    logger.info("Async scheduler started (runs every 30 seconds)")
    
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
    # Accept both old (day_1/day_2) and new (chase_1_days/chase_2_days) field names with defaults
    chase_1_days: Optional[str] = Form(None),  # Default to 2 in code
    day_1: Optional[str] = Form(None),  # Legacy support
    chase_2_days: Optional[str] = Form(None),  # Default to 0/None in code
    day_2: Optional[str] = Form(None),  # Legacy support
    email_language: str = Form("es"),
    include_due_date_email: str = Form("true"),  # Default to true
):
    # Parse chase_1_days (support both field names, default to 2)
    c1_str = (chase_1_days or "").strip() if chase_1_days else ""
    if not c1_str and day_1:
        c1_str = (day_1 or "").strip()
    if not c1_str:
        c1_str = "2"  # Default
    
    try:
        c1 = int(c1_str)
        if c1 < 0:
            c1 = 2  # Default if negative
    except Exception:
        c1 = 2  # Default on error
    
    # Parse chase_2_days (support both field names, default to 0/None)
    c2_str = (chase_2_days or "").strip() if chase_2_days else ""
    if not c2_str and day_2:
        c2_str = (day_2 or "").strip()
    
    c2 = None
    try:
        if c2_str and c2_str != "" and c2_str != "0":
            c2_val = int(c2_str)
            if c2_val > 0:
                c2 = c2_val
    except Exception:
        c2 = None  # Default: disabled (0 or empty)

    # Parse include_due_date_email
    include_due = str(include_due_date_email).lower() in ("true", "1", "yes", "on")
    
    # Validate language
    if email_language not in ["en", "es"]:
        email_language = "es"  # Default to Spanish for UI
    
    # FROM email is fixed (custom domain)
    from_email = f"notifications@{os.getenv('CUSTOM_DOMAIN', 'invoicechaserapp.com')}"
    
    logger.info(f"[SETTINGS] Saving: chase_1_days={c1}, chase_2_days={c2}, include_due_date={include_due}, language={email_language}")
    
    save_settings(company_name, signature_name, c1, c2, from_email, reply_to_email, email_language)

    settings_now = get_settings()
    rows = get_preview_rows_from_db(token)
    if not rows:
        return templates.TemplateResponse("activated.html", {
            "request": request,
            "created": 0,
            "error": "No hay impagos en el panel. Vuelve al panel y verifica los datos.",
            "token": token,
        })
    
    created = create_or_refresh_reminders_from_rows(settings_now, rows, token, include_due_date=include_due)
    
    # Si created es 0 pero hay rows, contar los recordatorios pendientes existentes
    if created == 0 and len(rows) > 0:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM reminders WHERE status = 'pending'")
        count = cur.fetchone()["c"]
        conn.close()
        if count > 0:
            created = count
    
    # Scheduler runs automatically every 30 seconds, no need to trigger manually

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
    created = create_or_refresh_reminders_from_rows(settings_now, rows, token, include_due_date=True)
    
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
    
    # Obtener recordatorios desde reminders (new schema with all fields)
    # Try new schema first, fallback to old schema for backward compatibility
    try:
        cur.execute("""
            SELECT id, reminder_type, stage, send_at, sent_at, status, error,
                   to_email, client_name, amount, invoice_number, days_overdue
            FROM reminders
            ORDER BY send_at DESC, id DESC
            LIMIT 50
        """)
        rows_raw = cur.fetchall()
        use_new_schema = True
    except Exception:
        # Fallback to old schema
        cur.execute("""
            SELECT r.id, r.stage, r.send_at, r.sent_at, r.status, r.last_error,
                   d.email, d.name, d.amount
            FROM reminders r
            JOIN debts d ON d.id = r.debt_id
            ORDER BY r.send_at DESC, r.id DESC
            LIMIT 50
        """)
        rows_raw = cur.fetchall()
        use_new_schema = False
    
    conn.close()

    now = datetime.utcnow()
    rows = []
    for r in rows_raw:
        send_at_str = r["send_at"]
        sent_at_str = r.get("sent_at") or r.get("sent_at")
        
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
        
        # Get reminder type display name
        if use_new_schema:
            reminder_type = r.get("reminder_type") or "unknown"
            if reminder_type == "due_date":
                reminder_display = "Vencimiento"
            elif reminder_type == "chase_1":
                reminder_display = "Recordatorio 1"
            elif reminder_type == "chase_2":
                reminder_display = "Recordatorio 2"
            else:
                reminder_display = f"#{r.get('stage', 0)}"
            to_email = r.get("to_email") or ""
            to_name = r.get("client_name") or ""
            amount = r.get("amount") or "0"
            stage = r.get("stage") or 0
            error = r.get("error") if (status_db == "failed") else None
        else:
            reminder_display = f"#{r.get('stage', 0)}"
            to_email = r.get("email") or ""
            to_name = r.get("name") or ""
            amount = r.get("amount") or "0"
            stage = r.get("stage") or 0
            error = r.get("last_error") if (status_db == "failed") else None

        rows.append({
            "send_at_formatted": send_at_formatted,
            "sent_at_formatted": sent_at_formatted,
            "to_email": to_email,
            "to_name": to_name,
            "amount": amount,
            "stage": stage,
            "reminder_display": reminder_display,
            "status": status_db,
            "status_display": status_display,
            "error": error,
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


@app.get("/debug/pending")
def debug_pending():
    """Debug endpoint to view pending reminders."""
    conn = get_conn()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT id, reminder_type, to_email, invoice_number, send_at, status,
               created_at, sent_at, error, invoice_key, last_sent_at
        FROM reminders
        WHERE status = 'pending'
        ORDER BY send_at ASC
        LIMIT 20
    """)
    rows = cur.fetchall()
    conn.close()
    
    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "reminder_type": row["reminder_type"],
            "to_email": row["to_email"],
            "invoice_number": row["invoice_number"],
            "send_at": row["send_at"],
            "status": row["status"],
            "created_at": row.get("created_at"),
            "sent_at": row.get("sent_at"),
            "error": row.get("error"),
            "invoice_key": row.get("invoice_key"),
            "last_sent_at": row.get("last_sent_at"),
        })
    
    return JSONResponse({
        "count": len(result),
        "reminders": result,
    })


