from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd


# -------------------------
# Configuración de columnas
# -------------------------
@dataclass
class Mapping:
    email_col: str
    amount_col: str
    due_date_col: Optional[str] = None
    status_col: Optional[str] = None
    name_col: Optional[str] = None


# -------------------------
# Reglas de negocio
# -------------------------
@dataclass
class Rules:
    paid_value: str = "Pagado"
    min_days_late: int = 0


# -------------------------
# Normalizadores
# -------------------------
def _to_float(x) -> Optional[float]:
    if pd.isna(x):
        return None
    try:
        s = str(x).strip()
        s = s.replace("€", "").replace(" ", "")
        # 1.234,56 -> 1234.56
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        return float(s)
    except Exception:
        return None


def _to_date(x) -> Optional[date]:
    if pd.isna(x):
        return None
    try:
        dt = pd.to_datetime(x, dayfirst=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.date()
    except Exception:
        return None


def _norm_text(x: Any) -> str:
    return str(x).strip().lower() if x is not None else ""


# -------------------------
# Carga de archivo
# -------------------------
def load_table(file_bytes: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    bio = pd.io.common.BytesIO(file_bytes)
    if name.endswith(".csv"):
        df = pd.read_csv(bio)
    else:
        df = pd.read_excel(bio)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def get_columns(df: pd.DataFrame) -> List[str]:
    return list(df.columns)


# -------------------------
# Lógica principal
# -------------------------
def preview_unpaid(
    df: pd.DataFrame,
    mapping: Mapping,
    rules: Rules,
    today: Optional[date] = None
) -> Tuple[pd.DataFrame, Dict[str, Any]]:

    today = today or date.today()

    out = pd.DataFrame()
    out["email"] = df[mapping.email_col].astype(str).str.strip()
    out["amount"] = df[mapping.amount_col].apply(_to_float)

    # Nombre
    if mapping.name_col and mapping.name_col in df.columns:
        out["name"] = df[mapping.name_col].astype(str).str.strip()
    else:
        out["name"] = ""

    # Estado
    if mapping.status_col and mapping.status_col in df.columns:
        out["status"] = df[mapping.status_col].astype(str)
    else:
        out["status"] = ""

    # Fecha vencimiento
    if mapping.due_date_col and mapping.due_date_col in df.columns:
        out["due_date"] = df[mapping.due_date_col].apply(_to_date)
    else:
        out["due_date"] = None

    # Filas mínimas válidas
    out = out[out["email"].str.contains("@", na=False)]
    out = out[out["amount"].notna()]

    has_status = out["status"].astype(str).str.len().gt(0).any()
    has_due = out["due_date"].notna().any()

    decision_reason = ""
    unpaid_mask = None

    # --- Regla 1: estado ---
    if has_status:
        paid_norm = _norm_text(rules.paid_value)
        unpaid_mask = out["status"].apply(lambda x: _norm_text(x) != paid_norm)
        decision_reason = f"Se considera impago cuando el estado es distinto de “{rules.paid_value}”."

    # --- Regla 2: fecha ---
    elif has_due:
        days_late = out["due_date"].apply(
            lambda d: (today - d).days if d else None
        )
        unpaid_mask = days_late.apply(
            lambda x: x is not None and x > rules.min_days_late
        )
        decision_reason = (
            f"Se considera impago cuando el vencimiento supera "
            f"{rules.min_days_late} días de retraso."
        )

    # --- Sin reglas posibles ---
    else:
        unpaid_mask = pd.Series([False] * len(out))
        decision_reason = (
            "No se detectó columna de estado ni fecha de vencimiento. "
            "Para detectar impagos necesitas al menos una de ellas."
        )

    unpaid = out[unpaid_mask].copy()

    # Días de retraso solo si hay fecha
    if has_due:
        unpaid["days_late"] = unpaid["due_date"].apply(
            lambda d: (today - d).days if d else None
        )

    stats = {
        "total_rows": int(len(df)),
        "valid_rows": int(len(out)),
        "unpaid_rows": int(len(unpaid)),
        "decision_reason": decision_reason,
    }

    return unpaid, stats
