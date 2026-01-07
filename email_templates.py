# Email templates for Invoice Chaser
# Supports English (en) and Spanish (es)
# 3 reminder types: due_date, chase_1, chase_2

# ============================================
# DUE DATE REMINDER (Stage 0)
# ============================================

DUE_DATE_SUBJECT_EN = "Invoice Due Today"
DUE_DATE_SUBJECT_ES = "Factura vence hoy"

DUE_DATE_BODY_EN = """<p>Hello {name},</p>
<p>This is a reminder that invoice #{invoice_number} for {amount} is due today.</p>
<p>If you have already made the payment, please disregard this message.</p>
<p>If you have any questions, please reply directly to this email.</p>
<p>Best regards,<br/>{company}</p>"""

DUE_DATE_BODY_ES = """<p>Hola {name},</p>
<p>Este es un recordatorio de que la factura #{invoice_number} por {amount} vence hoy.</p>
<p>Si ya has realizado el pago, por favor ignora este mensaje.</p>
<p>Si tienes alguna pregunta, puedes responder directamente a este correo.</p>
<p>Un saludo,<br/>{company}</p>"""

# ============================================
# CHASE #1 (Stage 1)
# ============================================

CHASE_1_SUBJECT_EN = "Reminder: Outstanding Invoice"
CHASE_1_SUBJECT_ES = "Recordatorio: factura pendiente"

CHASE_1_BODY_EN = """<p>Hello {name},</p>
<p>According to our records, invoice #{invoice_number} for {amount} is still unpaid.</p>
<p>This invoice was due on {due_date}.</p>
<p>If you have already made the payment, please disregard this message.</p>
<p>If you have any questions, please reply directly to this email.</p>
<p>Best regards,<br/>{company}</p>"""

CHASE_1_BODY_ES = """<p>Hola {name},</p>
<p>Según nuestros registros, la factura #{invoice_number} por {amount} sigue pendiente.</p>
<p>Esta factura venció el {due_date}.</p>
<p>Si ya has realizado el pago, por favor ignora este mensaje.</p>
<p>Si tienes alguna pregunta, puedes responder directamente a este correo.</p>
<p>Un saludo,<br/>{company}</p>"""

# ============================================
# CHASE #2 (Stage 2)
# ============================================

CHASE_2_SUBJECT_EN = "Final Reminder: Outstanding Invoice"
CHASE_2_SUBJECT_ES = "Recordatorio final: factura pendiente"

CHASE_2_BODY_EN = """<p>Hello {name},</p>
<p>We are following up on invoice #{invoice_number} for {amount}, which was due on {due_date}.</p>
<p>If you have already made the payment, please disregard this message.</p>
<p>If possible, please confirm when payment will be made. You can reply directly to this email.</p>
<p>Thank you,<br/>{company}</p>"""

CHASE_2_BODY_ES = """<p>Hola {name},</p>
<p>Estamos haciendo seguimiento de la factura #{invoice_number} por {amount}, que venció el {due_date}.</p>
<p>Si ya has realizado el pago, por favor ignora este mensaje.</p>
<p>Si es posible, confírmanos cuándo se realizará el pago. Puedes responder directamente a este correo.</p>
<p>Gracias,<br/>{company}</p>"""


def get_email_template(stage: int, language: str = "en"):
    """
    Returns (subject, body_html) for given stage and language.
    stage: 0 = due date, 1 = chase #1, 2 = chase #2
    language: "en" or "es"
    """
    lang = language.lower() if language else "en"
    if lang not in ["en", "es"]:
        lang = "en"
    
    if stage == 0:
        subject = DUE_DATE_SUBJECT_EN if lang == "en" else DUE_DATE_SUBJECT_ES
        body = DUE_DATE_BODY_EN if lang == "en" else DUE_DATE_BODY_ES
    elif stage == 1:
        subject = CHASE_1_SUBJECT_EN if lang == "en" else CHASE_1_SUBJECT_ES
        body = CHASE_1_BODY_EN if lang == "en" else CHASE_1_BODY_ES
    elif stage == 2:
        subject = CHASE_2_SUBJECT_EN if lang == "en" else CHASE_2_SUBJECT_ES
        body = CHASE_2_BODY_EN if lang == "en" else CHASE_2_BODY_ES
    else:
        # Fallback to chase #1
        subject = CHASE_1_SUBJECT_EN if lang == "en" else CHASE_1_SUBJECT_ES
        body = CHASE_1_BODY_EN if lang == "en" else CHASE_1_BODY_ES
    
    return subject, body
