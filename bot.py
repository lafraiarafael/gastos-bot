import os
import json
import logging
import tempfile
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
gc = gspread.authorize(creds)

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1MZeWj4emurdqv4YG1eusCIXop1rI7qru3UHHIcqqMaA")
AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")

# ─── Retry decorator for Google Sheets API calls ─────────────────────────────
import time

def with_retry(max_attempts=3, delay=2):
    """Retry decorator for Google API calls that may fail with 503."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()
                    if any(x in error_str for x in ["503", "unavailable", "quota", "rate limit", "timeout", "500"]):
                        if attempt < max_attempts - 1:
                            logger.warning(f"Google API error (attempt {attempt+1}/{max_attempts}): {e}. Retrying in {delay}s...")
                            time.sleep(delay * (attempt + 1))
                            continue
                    raise  # Not a retryable error, raise immediately
            raise last_error
        return wrapper
    return decorator

# In-memory cache for expenses pending confirmation (keyed by a short id)
PENDING_EXPENSES = {}
_pending_counter = 0

def store_pending(expense: dict) -> str:
    global _pending_counter
    _pending_counter += 1
    key = str(_pending_counter % 100000)
    PENDING_EXPENSES[key] = expense
    return key

CATEGORIES = [
    "Alimentação", "Transporte", "Moradia", "Utilidades",
    "Saúde", "Lazer", "Educação", "Roupas",
    "Farmácia", "Impostos", "Outros", "Poupança"
]

# ─── Identify sender ──────────────────────────────────────────────────────────
def identify_sender(update: Update) -> str:
    user = update.effective_user
    combined = ((user.full_name or "") + " " + (user.username or "")).lower()
    if "renata" in combined:
        return "Renata"
    if any(w in combined for w in ["rafael", "rafa"]):
        return "Rafa"
    return (user.first_name or "Desconhecido").capitalize()

# ─── Progress bar ─────────────────────────────────────────────────────────────
def progress_bar(pct: float) -> str:
    pct_capped = min(pct, 100)
    filled = round(pct_capped / 10)
    empty = 10 - filled
    emoji = "🔴" if pct >= 100 else "🟡" if pct >= 75 else "🟢"
    return f"{'█' * filled}{'░' * empty} {pct:.0f}% {emoji}"

# ─── Transcribe ───────────────────────────────────────────────────────────────
async def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, language="pt"
        )
    return transcript.text

# ─── Parse expense ────────────────────────────────────────────────────────────
def parse_expense(text: str, sender: str) -> dict:
    today = datetime.now(AMSTERDAM_TZ).strftime("%d/%m/%y")
    week_num = datetime.now(AMSTERDAM_TZ).isocalendar()[1]
    cats = ", ".join(CATEGORIES)

    summary_keywords = ["resumo", "summary", "relatório", "relatorio", "como estamos", "situação", "situacao"]
    if any(kw in text.lower() for kw in summary_keywords):
        return {"is_summary_request": True}

    delete_keywords = ["remove", "remover", "apaga", "apagar", "deleta", "deletar", "exclui", "excluir", "cancela esse lançamento", "cancelar lançamento", "errei", "lancei errado", "lancei por engano"]
    if any(kw in text.lower() for kw in delete_keywords):
        return {"is_delete_request": True, "delete_query": text}

    prompt = f"""Você é um assistente de controle financeiro de casal que mora na Holanda.
A moeda usada é o Euro (€).

O texto abaixo foi dito por **{sender}** e descreve um gasto ou poupança.
Hoje: {today} | Semana ISO: {week_num}

Extraia e responda APENAS com JSON válido (sem markdown, sem explicação):
{{
  "is_summary_request": false,
  "data": "DD/MM/AA",
  "semana": <número inteiro da semana ISO>,
  "quem_pagou": "<Renata ou Rafa>",
  "categoria": "<categoria>",
  "descricao": "<descrição curta, máx 30 chars>",
  "valor": <número decimal, ex: 25.90>,
  "pago_com": "<Débito ou Crédito>",
  "observacao": "<texto livre ou string vazia>"
}}

Categorias válidas: {cats}

Regras:
- Se mencionar poupança, guardar, economizar, reservar, depositar → categoria = "Poupança", valor POSITIVO
- Se mencionar retirar, tirar, sacar, usar da poupança, pegar da poupança → categoria = "Poupança", valor NEGATIVO (ex: -5.00)
- quem_pagou: use "{sender}" se não mencionado
- pago_com: "Débito" se não mencionado
- valor: número puro sem símbolo (ex: 32.50 ou -5.00 para retiradas da poupança)
- data: hoje se não mencionada ({today})
- descricao: nome do estabelecimento ou produto

Texto: "{text}"
"""

    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"}
    )
    result = json.loads(resp.choices[0].message.content)

    # ── Validation (auditoria de erros silenciosos) ──
    if not result.get("is_summary_request") and not result.get("is_delete_request"):
        warnings = []

        if result.get("categoria") not in CATEGORIES:
            warnings.append(f"⚠️ Categoria \"{result.get('categoria')}\" não existe na lista oficial.")
            result["categoria"] = "Outros"
            warnings.append("Usei \"Outros\" como categoria padrão.")

        try:
            valor = float(result.get("valor", 0))
            if valor == 0:
                warnings.append("⚠️ Não consegui identificar um valor válido (ficou 0).")
            result["valor"] = valor
        except (ValueError, TypeError):
            warnings.append(f"⚠️ Valor \"{result.get('valor')}\" inválido, ficou 0.")
            result["valor"] = 0.0

        if result.get("quem_pagou") not in ["Rafa", "Renata"]:
            warnings.append(f"⚠️ Pessoa \"{result.get('quem_pagou')}\" não reconhecida, usei \"{sender}\".")
            result["quem_pagou"] = sender

        if warnings:
            result["_warnings"] = warnings

    return result

# ─── Insert lancamento ────────────────────────────────────────────────────────
@with_retry(max_attempts=3, delay=2)
def insert_lancamento(expense: dict) -> int:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    all_vals = ws.get_all_values()

    target_row = None
    for idx, row in enumerate(all_vals):
        if idx < 2:
            continue
        date_cell  = row[0].strip() if len(row) > 0 else ""
        who_cell   = row[2].strip() if len(row) > 2 else ""
        cat_cell   = row[3].strip() if len(row) > 3 else ""
        value_cell = row[5].strip() if len(row) > 5 else ""

        if date_cell and who_cell and not cat_cell and not value_cell:
            if who_cell.lower() == expense["quem_pagou"].lower():
                target_row = idx + 1
                break

    if target_row is None:
        for idx in range(len(all_vals) - 1, 1, -1):
            if any(cell.strip() for cell in all_vals[idx]):
                target_row = idx + 2
                break
        if target_row is None:
            target_row = len(all_vals) + 1

    valor_float = float(expense["valor"])

    row_data = [
        expense["data"], expense["semana"], expense["quem_pagou"],
        expense["categoria"], expense.get("descricao", ""),
        valor_float, expense["pago_com"], expense.get("observacao", "")
    ]

    row_vals = all_vals[target_row - 1] if target_row <= len(all_vals) else []
    date_existing = row_vals[0].strip() if row_vals else ""

    if date_existing:
        ws.update(f"A{target_row}:H{target_row}", [row_data], value_input_option='USER_ENTERED')
    else:
        ws.insert_row(row_data, target_row, value_input_option='USER_ENTERED')

    logger.info(f"Row {target_row}: {row_data}")
    return target_row

# ─── Find rows matching a delete request ─────────────────────────────────────
@with_retry(max_attempts=3, delay=2)
def find_matching_lancamentos(query: str, sender: str, max_results: int = 5) -> list[dict]:
    """Search the last ~60 days of Lançamentos for rows matching the query text."""
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    all_rows = ws.get_all_values()

    candidates = []
    for idx, row in enumerate(all_rows):
        if idx < 2:
            continue
        if len(row) < 6:
            continue
        date_cell, week_cell, who_cell, cat_cell, desc_cell, val_cell = row[0], row[1], row[2], row[3], row[4], row[5]
        if not date_cell.strip() or not cat_cell.strip():
            continue
        candidates.append({
            "row_number": idx + 1,
            "data": date_cell,
            "quem_pagou": who_cell,
            "categoria": cat_cell,
            "descricao": desc_cell,
            "valor_raw": val_cell,
            "observacao": row[7] if len(row) > 7 else ""
        })

    if not candidates:
        return []

    # Use GPT to rank/match candidates against the query (most recent 40 rows considered)
    recent = candidates[-40:]
    candidates_str = "\n".join([
        f"{c['row_number']}: {c['data']} | {c['quem_pagou']} | {c['categoria']} | {c['descricao']} | {c['valor_raw']} | {c['observacao']}"
        for c in recent
    ])

    prompt = f"""O usuário {sender} quer apagar um lançamento de despesa. Aqui está o pedido dele:
"{query}"

Aqui está a lista de lançamentos recentes (formato: row_number: data | quem_pagou | categoria | descrição | valor | observação):
{candidates_str}

Responda APENAS com um JSON contendo os números das linhas (row_number) que melhor correspondem ao pedido, ordenados do mais provável ao menos provável, no máximo {max_results}:
{{"matches": [<row_number>, ...]}}

Se nenhuma linha corresponder bem, retorne {{"matches": []}}.
"""
    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"}
    )
    result = json.loads(resp.choices[0].message.content)
    matched_rows = result.get("matches", [])

    by_row = {c["row_number"]: c for c in candidates}
    return [by_row[r] for r in matched_rows if r in by_row][:max_results]

# ─── List expenses by category (current month) ──────────────────────────────
@with_retry(max_attempts=3, delay=2)
def list_expenses_by_category(categoria: str) -> list[dict]:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    all_rows = ws.get_all_values()

    now = datetime.now(AMSTERDAM_TZ)
    current_month = now.month
    current_year = now.year

    # Poupança is a cumulative fund — show ALL entries regardless of month
    is_savings = (categoria == "Poupança")

    results = []
    for row in all_rows[2:]:
        if len(row) < 6:
            continue
        cat_cell = row[3].strip() if len(row) > 3 else ""
        if cat_cell != categoria:
            continue
        date_str = row[0].strip()
        if not date_str:
            continue
        try:
            parts = date_str.split("/")
            if len(parts) == 3:
                day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                year = 2000 + year if year < 100 else year
                if year < 2020:
                    continue
                # For regular categories: filter by current month only
                # For Poupança: include all months (cumulative fund)
                if not is_savings and (month != current_month or year != current_year):
                    continue
                results.append({
                    "data": date_str,
                    "quem_pagou": row[2],
                    "descricao": row[4],
                    "valor_raw": row[5],
                    "pago_com": row[6] if len(row) > 6 else "",
                    "observacao": row[7] if len(row) > 7 else ""
                })
        except Exception as e:
            logger.warning(f"List expenses parse error: {row} → {e}")

    return results

# ─── Get all months that have lancamentos ─────────────────────────────────────
@with_retry(max_attempts=3, delay=2)
def get_available_months() -> list[dict]:
    """Returns list of {year, month, label} for all months that have entries."""
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    all_rows = ws.get_all_values()

    months_seen = {}
    for row in all_rows[2:]:
        if len(row) < 4 or not row[0].strip() or not row[3].strip():
            continue
        date_str = row[0].strip()
        try:
            parts = date_str.split("/")
            if len(parts) == 3:
                month = int(parts[1])
                year = int(parts[2])
                year = 2000 + year if year < 100 else year
                # Skip entries with clearly wrong years (before 2020)
                if year < 2020:
                    continue
                key = (year, month)
                if key not in months_seen:
                    months_seen[key] = True
        except Exception:
            continue

    result = []
    for (year, month) in sorted(months_seen.keys(), reverse=True):
        from datetime import date
        dt = date(year, month, 1)
        label = dt.strftime("%B %Y").capitalize()
        result.append({"year": year, "month": month, "label": label})
    return result

# ─── Summarize expenses for a specific month ──────────────────────────────────
@with_retry(max_attempts=3, delay=2)
def get_month_summary(year: int, month: int) -> dict:
    """Read Lançamentos and aggregate by category for a specific month."""
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws_lanc = sh.worksheet("Lançamentos")
    ws_metas = sh.worksheet("Metas")
    all_rows = ws_lanc.get_all_values()
    meta_rows = ws_metas.get_all_values()

    # Read metas from Metas tab
    metas = {}
    for row in meta_rows[2:]:  # skip title + total row
        if len(row) < 2 or not row[0].strip():
            continue
        cat = row[0].strip()
        val_str = re.sub(r'[€ \-]', '', row[1]).replace(".", "").replace(",", ".").strip()
        try:
            metas[cat] = float(val_str) if val_str and val_str not in ["-", ""] else 0.0
        except ValueError:
            metas[cat] = 0.0

    # Aggregate gastos by category for target month ONLY
    # Poupança is cumulative (all time), so we sum it separately without month filter
    gastos = {}
    savings = 0.0
    for row in all_rows[2:]:
        if len(row) < 6 or not row[0].strip():
            continue
        date_str = row[0].strip()
        cat = row[3].strip() if len(row) > 3 else ""
        val_str = row[5].strip() if len(row) > 5 else ""
        if not cat or not val_str:
            continue
        try:
            val_clean = re.sub(r'[€ ]', '', val_str).replace(".", "").replace(",", ".").strip()
            valor = float(val_clean) if val_clean else 0.0

            # Poupança: sum ALL entries regardless of month (cumulative fund)
            if cat == "Poupança":
                savings += valor
                continue

            # Regular expenses: filter by target month/year only
            parts = date_str.split("/")
            if len(parts) != 3:
                continue
            r_month = int(parts[1])
            r_year = int(parts[2])
            r_year = 2000 + r_year if r_year < 100 else r_year
            if r_year < 2020:
                continue
            if r_month != month or r_year != year:
                continue
            gastos[cat] = gastos.get(cat, 0.0) + valor
        except Exception:
            continue

    # Build insights list matching format used in build_full_summary
    insights = []
    all_cats = set(list(metas.keys()) + list(gastos.keys()))
    for cat in all_cats:
        if cat == "Poupança":
            continue
        meta = metas.get(cat, 0.0)
        gasto = gastos.get(cat, 0.0)
        if meta == 0 and gasto == 0:
            continue
        saldo = meta - gasto
        pct = (gasto / meta * 100) if meta > 0 else 0.0
        insights.append({"categoria": cat, "meta": meta, "gasto": gasto, "saldo": saldo, "pct": pct})

    return {"insights": insights, "savings": savings}

# ─── Delete a specific row (clear its contents) ──────────────────────────────
def delete_lancamento_row(row_number: int):
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    ws.batch_clear([f"A{row_number}:H{row_number}"])
    logger.info(f"Cleared row {row_number}")

# ─── Get insights (expenses + savings) ───────────────────────────────────────
def get_category_insights() -> tuple[list[dict], float]:
    """Returns (category_insights, total_cumulative_savings) for the CURRENT month.
    Uses get_month_summary() so it always filters by month correctly."""
    now = datetime.now(AMSTERDAM_TZ)
    result = get_month_summary(now.year, now.month)
    return result["insights"], result["savings"]

# ─── Build confirmation (category only) ──────────────────────────────────────
def build_confirmation(expense: dict, insights: list[dict], savings: float) -> str:
    month_name = datetime.now(AMSTERDAM_TZ).strftime("%B").capitalize()
    is_savings = expense["categoria"] == "Poupança"

    lines = []
    lines.append(f"✅ *Lançado com sucesso!*\n")
    lines.append(f"👤 *{expense['quem_pagou']}*")

    if is_savings:
        valor = float(expense['valor'])
        is_withdrawal = valor < 0
        action_label = "Retirada da" if is_withdrawal else "Depósito na"
        lines.append(f"🐷 {action_label} Poupança  |  📝 {expense.get('descricao', '–')}")
        lines.append(f"💶 *€ {abs(valor):.2f}*  ({expense['pago_com']})")
        if expense.get("observacao"):
            lines.append(f"💬 _{expense['observacao']}_")
        lines.append("")
        lines.append(f"🐷 *Fundo Poupança (acumulado):*")
        lines.append(f"💰 *€ {savings:.2f}*")
    else:
        lines.append(f"📂 {expense['categoria']}  |  📝 {expense.get('descricao', '–')}")
        lines.append(f"💶 *€ {float(expense['valor']):.2f}*  ({expense['pago_com']})")
        if expense.get("observacao"):
            lines.append(f"💬 _{expense['observacao']}_")

        this_cat = next((i for i in insights if i["categoria"] == expense["categoria"]), None)
        lines.append("")
        if this_cat and this_cat["meta"] > 0:
            lines.append(f"📊 *{expense['categoria']} — {month_name}:*")
            lines.append(f"`{progress_bar(this_cat['pct'])}`")
            lines.append(f"Gasto €{this_cat['gasto']:.2f}  /  Meta €{this_cat['meta']:.2f}  |  Saldo €{this_cat['saldo']:.2f}")
            if this_cat['pct'] >= 100:
                lines.append(f"\n🔴 *Meta de {expense['categoria']} estourada!*")
            elif this_cat['pct'] >= 75:
                lines.append(f"\n🟡 Quase no limite de {expense['categoria']}!")
        elif this_cat:
            lines.append(f"📊 *{expense['categoria']} — {month_name}:*")
            lines.append(f"Gasto total: €{this_cat['gasto']:.2f}  _(sem meta definida)_")

    return "\n".join(lines)

# ─── Build full summary ───────────────────────────────────────────────────────
def build_full_summary(insights: list[dict], savings: float, title: str = None) -> str:
    month_name = datetime.now(AMSTERDAM_TZ).strftime("%B").capitalize()
    if not title:
        title = f"📊 *Resumo Mensal — {month_name}*"

    lines = [title, ""]
    total_gasto = 0
    total_meta = 0

    sorted_cats = sorted([i for i in insights if i["meta"] > 0 or i["gasto"] > 0], key=lambda x: x["pct"], reverse=True)

    total_gasto_geral = 0.0
    for cat in sorted_cats:
        lines.append(f"*{cat['categoria']}*")
        if cat["meta"] > 0:
            lines.append(f"`{progress_bar(cat['pct'])}`")
            lines.append(f"€{cat['gasto']:.2f} / €{cat['meta']:.2f}  |  Saldo €{cat['saldo']:.2f}")
            total_gasto += cat["gasto"]
            total_meta += cat["meta"]
        else:
            lines.append(f"€{cat['gasto']:.2f}  _(sem meta definida)_")
        lines.append("")
        total_gasto_geral += cat["gasto"]

    total_pct = (total_gasto / total_meta * 100) if total_meta > 0 else 0
    lines.append(f"💰 *TOTAL GASTOS (com meta): €{total_gasto:.2f} / €{total_meta:.2f}*")
    lines.append(f"`{progress_bar(total_pct)}`")
    sem_meta = total_gasto_geral - total_gasto
    if sem_meta > 0:
        lines.append(f"_+ €{sem_meta:.2f} em categorias sem meta_")

    # Savings — separate, never mixed with expenses
    lines.append("")
    lines.append(f"🐷 *Fundo Poupança (acumulado): €{savings:.2f}*")

    over    = [i for i in sorted_cats if i["pct"] >= 100]
    warning = [i for i in sorted_cats if 75 <= i["pct"] < 100]
    if over or warning:
        lines.append("")
    if over:
        names = " | ".join([f"*{i['categoria']}*" for i in over])
        lines.append(f"🔴 Estourou: {names}")
    if warning:
        names = " | ".join([f"*{i['categoria']}*" for i in warning])
        lines.append(f"🟡 Atenção (>75%): {names}")

    return "\n".join(lines)

# ─── Generate monthly PDF report ─────────────────────────────────────────────
def generate_monthly_pdf(insights: list[dict], savings: float) -> str:
    now = datetime.now(AMSTERDAM_TZ)
    month_name = now.strftime("%B").capitalize()
    filepath = f"/tmp/relatorio_{now.strftime('%Y_%m')}.pdf"

    doc = SimpleDocTemplate(filepath, pagesize=A4,
                             topMargin=1.5*cm, bottomMargin=1.5*cm,
                             leftMargin=1.5*cm, rightMargin=1.5*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], fontSize=20, alignment=TA_CENTER, spaceAfter=4)
    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], fontSize=12, alignment=TA_CENTER, textColor=colors.grey, spaceAfter=20)

    elements = []
    elements.append(Paragraph(f"💰 Relatório de Gastos — {month_name} {now.year}", title_style))
    elements.append(Paragraph("Rafa &amp; Renata", subtitle_style))

    # Table data
    table_data = [["Categoria", "Gasto (€)", "Meta (€)", "Saldo (€)", "% Meta"]]
    total_gasto, total_meta = 0.0, 0.0

    sorted_cats = sorted(insights, key=lambda x: x["gasto"], reverse=True)
    for cat in sorted_cats:
        if cat["meta"] == 0 and cat["gasto"] == 0:
            continue
        table_data.append([
            cat["categoria"],
            f"{cat['gasto']:.2f}",
            f"{cat['meta']:.2f}" if cat["meta"] > 0 else "–",
            f"{cat['saldo']:.2f}" if cat["meta"] > 0 else "–",
            f"{cat['pct']:.0f}%" if cat["meta"] > 0 else "–",
        ])
        if cat["meta"] > 0:
            total_gasto += cat["gasto"]
            total_meta += cat["meta"]

    table_data.append(["TOTAL", f"{total_gasto:.2f}", f"{total_meta:.2f}", f"{total_meta-total_gasto:.2f}",
                        f"{(total_gasto/total_meta*100) if total_meta else 0:.0f}%"])

    table = Table(table_data, colWidths=[5*cm, 3*cm, 3*cm, 3*cm, 2.5*cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d2d2d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e8e8e8")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f5f5f5")]),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 20))

    savings_style = ParagraphStyle("Savings", parent=styles["Normal"], fontSize=13, textColor=colors.HexColor("#1a7a3c"))
    elements.append(Paragraph(f"🐷 Fundo Poupança (acumulado): € {savings:.2f}", savings_style))

    over = [c for c in insights if c["pct"] >= 100 and c["meta"] > 0]
    if over:
        elements.append(Spacer(1, 10))
        alert_style = ParagraphStyle("Alert", parent=styles["Normal"], fontSize=11, textColor=colors.HexColor("#c0392b"))
        names = ", ".join([c["categoria"] for c in over])
        elements.append(Paragraph(f"🔴 Categorias que estouraram a meta: {names}", alert_style))

    elements.append(Spacer(1, 30))
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey, alignment=TA_CENTER)
    elements.append(Paragraph(f"Gerado automaticamente em {now.strftime('%d/%m/%Y %H:%M')} (Amsterdam)", footer_style))

    doc.build(elements)
    return filepath

# ─── Weekly summary job ───────────────────────────────────────────────────────
async def send_weekly_summary(context) -> None:
    chat_id = context.job.data
    try:
        insights, savings = get_category_insights()
        now = datetime.now(AMSTERDAM_TZ)
        title = f"📊 *Resumo Semanal — {now.strftime('%d/%m/%Y')}*"
        msg = build_full_summary(insights, savings, title)
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Weekly summary error: {e}", exc_info=True)

# ─── Handle delete request (find candidates + ask confirmation) ─────────────
async def handle_delete_request(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str, sender: str):
    await update.message.reply_text("🔍 Procurando o lançamento...")
    try:
        matches = find_matching_lancamentos(query_text, sender)

        if not matches:
            await update.message.reply_text(
                "❌ Não encontrei nenhum lançamento que combine com isso.\n"
                "Tenta descrever melhor (ex: \"remove a compra da Hornbach\")."
            )
            return

        if len(matches) == 1:
            m = matches[0]
            keyboard = [[
                InlineKeyboardButton("✅ Sim, apagar", callback_data=f"delrow_{m['row_number']}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="delrow_cancel"),
            ]]
            text = (
                f"Encontrei este lançamento:\n\n"
                f"📅 {m['data']} | 👤 {m['quem_pagou']}\n"
                f"📂 {m['categoria']} | 📝 {m['descricao']}\n"
                f"💶 €{m['valor_raw']}\n\n"
                f"Quer apagar?"
            )
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            keyboard = []
            lines = ["Encontrei alguns lançamentos parecidos, qual deles?\n"]
            for m in matches:
                label = f"{m['data']} | {m['categoria']} | {m['descricao']} | €{m['valor_raw']}"
                lines.append(f"• {label}")
                keyboard.append([InlineKeyboardButton(label[:60], callback_data=f"delrow_{m['row_number']}")])
            keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="delrow_cancel")])
            await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(f"Delete search error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro ao buscar: `{e}`", parse_mode="Markdown")

# ─── Callback handler for delete confirmation buttons ────────────────────────
async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "delrow_cancel":
        await query.edit_message_text("❌ Cancelado. Nada foi apagado.")
        return

    if query.data.startswith("delrow_"):
        row_number = int(query.data.replace("delrow_", ""))
        try:
            delete_lancamento_row(row_number)
            await query.edit_message_text("🗑️ Lançamento apagado com sucesso!")
        except Exception as e:
            logger.error(f"Delete row error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Erro ao apagar: `{e}`")

# ─── Build preview message for confirmation ──────────────────────────────────
def build_preview(expense: dict) -> str:
    is_savings = expense["categoria"] == "Poupança"
    valor = float(expense["valor"])

    lines = ["🔎 *Confirma esse lançamento?*\n"]
    lines.append(f"👤 {expense['quem_pagou']}")
    if is_savings:
        action = "Retirada da" if valor < 0 else "Depósito na"
        lines.append(f"🐷 {action} Poupança")
        lines.append(f"💶 € {abs(valor):.2f}  ({expense['pago_com']})")
    else:
        lines.append(f"📂 {expense['categoria']}")
        lines.append(f"📝 {expense.get('descricao', '–')}")
        lines.append(f"💶 € {valor:.2f}  ({expense['pago_com']})")
    if expense.get("observacao"):
        lines.append(f"💬 {expense['observacao']}")

    if expense.get("_warnings"):
        lines.append("")
        for w in expense["_warnings"]:
            lines.append(w)

    return "\n".join(lines)

# ─── Callback handler for expense confirmation ───────────────────────────────
async def handle_expense_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "exp_cancel":
        await query.edit_message_text("❌ Cancelado. Nada foi lançado.")
        return

    if query.data.startswith("exp_confirm_"):
        key = query.data.replace("exp_confirm_", "")
        expense = PENDING_EXPENSES.pop(key, None)
        if not expense:
            await query.edit_message_text("⚠️ Esse lançamento expirou, manda de novo por favor.")
            return

        try:
            insert_lancamento(expense)
            insights, savings = get_category_insights()
            reply = build_confirmation(expense, insights, savings)
            await query.edit_message_text(reply, parse_mode="Markdown")

            # Proactive alert check (90%+) on the affected category
            await check_and_send_alert(context, query.message.chat_id, expense, insights)

        except Exception as e:
            logger.error(f"Confirm insert error: {e}", exc_info=True)
            error_str = str(e).lower()
            if any(x in error_str for x in ["503", "unavailable", "quota", "timeout"]):
                await query.edit_message_text(
                    "⚠️ O Google Sheets ficou temporariamente fora do ar.\n"
                    "Tenta confirmar de novo em alguns segundos."
                )
            else:
                await query.edit_message_text(f"❌ Erro ao lançar: `{e}`", parse_mode="Markdown")
        return

    if query.data.startswith("exp_edit_"):
        key = query.data.replace("exp_edit_", "")
        expense = PENDING_EXPENSES.get(key)
        if not expense:
            await query.edit_message_text("⚠️ Esse lançamento expirou, manda de novo por favor.")
            return
        await query.edit_message_text(
            "✏️ Manda a versão corrigida em texto ou áudio (ex: \"era 25 euros, não 35\")."
        )
        PENDING_EXPENSES.pop(key, None)

# ─── Proactive alert when a category crosses 90% ─────────────────────────────
async def check_and_send_alert(context, chat_id, expense: dict, insights: list[dict]):
    if expense["categoria"] == "Poupança":
        return
    this_cat = next((i for i in insights if i["categoria"] == expense["categoria"]), None)
    if not this_cat or this_cat["meta"] <= 0:
        return
    if this_cat["pct"] >= 90:
        try:
            if this_cat["pct"] >= 100:
                msg = (
                    f"🔴 *Alerta de orçamento!*\n\n"
                    f"*{expense['categoria']}* ultrapassou a meta este mês.\n"
                    f"Gasto: €{this_cat['gasto']:.2f} / Meta: €{this_cat['meta']:.2f}"
                )
            else:
                msg = (
                    f"🟡 *Atenção!*\n\n"
                    f"*{expense['categoria']}* já está em {this_cat['pct']:.0f}% da meta.\n"
                    f"Faltam apenas €{this_cat['saldo']:.2f} para estourar."
                )
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Alert send error: {e}", exc_info=True)

# ─── Voice handler ────────────────────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = identify_sender(update)
    await update.message.reply_text("🎙️ Recebi! Processando...")
    try:
        voice = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await tg_file.download_to_drive(tmp.name)
            tmp_path = tmp.name

        transcript = await transcribe_audio(tmp_path)
        logger.info(f"Transcript [{sender}]: {transcript}")
        os.unlink(tmp_path)

        parsed = parse_expense(transcript, sender)

        if parsed.get("is_summary_request"):
            insights, savings = get_category_insights()
            await update.message.reply_text(build_full_summary(insights, savings), parse_mode="Markdown")
            return

        if parsed.get("is_delete_request"):
            await handle_delete_request(update, context, parsed.get("delete_query", transcript), sender)
            return

        key = store_pending(parsed)
        keyboard = [[
            InlineKeyboardButton("✅ Confirmar", callback_data=f"exp_confirm_{key}"),
            InlineKeyboardButton("✏️ Editar", callback_data=f"exp_edit_{key}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="exp_cancel"),
        ]]
        await update.message.reply_text(
            build_preview(parsed), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"Voice error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── Text handler ─────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text.startswith("/"):
        return

    sender = identify_sender(update)
    summary_keywords = ["resumo", "summary", "relatório", "relatorio", "como estamos"]
    if any(kw in text.lower() for kw in summary_keywords):
        await update.message.reply_text("📊 Carregando resumo...")
        insights, savings = get_category_insights()
        await update.message.reply_text(build_full_summary(insights, savings), parse_mode="Markdown")
        return

    delete_keywords = ["remove", "remover", "apaga", "apagar", "deleta", "deletar", "exclui", "excluir", "errei", "lancei errado", "lancei por engano"]
    if any(kw in text.lower() for kw in delete_keywords):
        await handle_delete_request(update, context, text, sender)
        return

    if not re.search(r'\d', text):
        return

    await update.message.reply_text("📝 Processando...")
    try:
        expense = parse_expense(text, sender)
        if expense.get("is_summary_request"):
            insights, savings = get_category_insights()
            await update.message.reply_text(build_full_summary(insights, savings), parse_mode="Markdown")
            return
        if expense.get("is_delete_request"):
            await handle_delete_request(update, context, text, sender)
            return

        key = store_pending(expense)
        keyboard = [[
            InlineKeyboardButton("✅ Confirmar", callback_data=f"exp_confirm_{key}"),
            InlineKeyboardButton("✏️ Editar", callback_data=f"exp_edit_{key}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="exp_cancel"),
        ]]
        await update.message.reply_text(
            build_preview(expense), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Text error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    job_name = f"weekly_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_daily(
        send_weekly_summary,
        time=datetime.now(AMSTERDAM_TZ).replace(hour=8, minute=0, second=0, microsecond=0).timetz(),
        days=(0,),
        name=job_name,
        data=chat_id
    )

    # Daily check for last day of month → triggers PDF report
    monthly_job_name = f"monthly_check_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(monthly_job_name):
        job.schedule_removal()

    context.job_queue.run_daily(
        check_last_day_of_month,
        time=datetime.now(AMSTERDAM_TZ).replace(hour=20, minute=0, second=0, microsecond=0).timetz(),
        name=monthly_job_name,
        data=chat_id
    )

    await update.message.reply_text(
        "👋 *Bot de Gastos — Rafa & Renata* ativo!\n\n"
        "🎙️ Mande um *áudio* com o gasto:\n"
        "_\"Gastei 32 euros no supermercado, débito\"_\n\n"
        "🐷 Para registrar poupança:\n"
        "_\"Guardei 200 euros este mês\"_\n\n"
        "📊 Para ver o resumo:\n"
        "_\"resumo\"_ ou `/resumo`\n\n"
        "📂 Para ver gastos por categoria:\n"
        "`/categorias`\n\n"
        "📄 Para gerar um relatório PDF:\n"
        "`/relatorio`\n\n"
        "🗓️ Para ver resumo por mês:\n"
        "`/meses`\n\n"
        "🎯 Para ver as metas atuais:\n"
        "`/metas`\n\n"
        "🗑️ Para apagar um lançamento:\n"
        "_\"remove a compra do supermercado\"_\n\n"
        "✅ Todo lançamento pede confirmação antes de salvar!\n\n"
        "🗓️ Toda segunda-feira às 8h: resumo automático\n"
        "🗓️ Todo fim de mês às 20h: relatório PDF automático",
        parse_mode="Markdown"
    )

# ─── /categorias — menu to pick a category and see its expenses ─────────────
async def categorias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    row = []
    for i, cat in enumerate(CATEGORIES):
        row.append(InlineKeyboardButton(cat, callback_data=f"catview_{cat}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "📂 *Escolha uma categoria para ver os gastos do mês:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ─── Callback handler for category view buttons ─────────────────────────────
async def handle_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    categoria = query.data.replace("catview_", "")
    month_name = datetime.now(AMSTERDAM_TZ).strftime("%B").capitalize()

    try:
        expenses = list_expenses_by_category(categoria)

        is_savings_cat = (categoria == "Poupança")
        period_label = "Acumulado" if is_savings_cat else month_name

        if not expenses:
            await query.edit_message_text(f"📂 *{categoria} — {period_label}*\n\nNenhum lançamento encontrado.", parse_mode="Markdown")
            return

        lines = [f"📂 *{categoria} — {period_label}*\n"]
        total = 0.0
        for e in expenses:
            val_str = re.sub(r'[€ \t]', '', e["valor_raw"]).replace(".", "").replace(",", ".").strip()
            try:
                val = float(val_str) if val_str else 0.0
            except ValueError:
                val = 0.0
            total += val
            desc = e["descricao"] or "–"
            obs = f" _({e['observacao']})_" if e["observacao"] else ""
            lines.append(f"• {e['data']} | {e['quem_pagou']} | {desc} | €{val:.2f}{obs}")

        lines.append(f"\n💰 *Total: €{total:.2f}*")

        # Add a back button
        keyboard = [[InlineKeyboardButton("⬅️ Voltar às categorias", callback_data="catview_back")]]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        logger.error(f"Category view error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── Callback handler for "back to categories" button ────────────────────────
async def handle_category_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = []
    row = []
    for cat in CATEGORIES:
        row.append(InlineKeyboardButton(cat, callback_data=f"catview_{cat}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await query.edit_message_text(
        "📂 *Escolha uma categoria para ver os gastos do mês:*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ─── /resumo ──────────────────────────────────────────────────────────────────
async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Carregando resumo...")
    try:
        insights, savings = get_category_insights()
        await update.message.reply_text(build_full_summary(insights, savings), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── /metas — show current monthly goals ─────────────────────────────────────
async def metas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 Carregando metas...")
    try:
        now = datetime.now(AMSTERDAM_TZ)
        result = get_month_summary(now.year, now.month)
        insights = result["insights"]

        month_name = now.strftime("%B %Y").capitalize()
        lines = [f"🎯 *Metas \u2014 {month_name}*\n"]

        # Sort by meta value descending
        sorted_cats = sorted(
            [i for i in insights if i["meta"] > 0],
            key=lambda x: x["meta"], reverse=True
        )
        no_meta = [i for i in insights if i["meta"] == 0 and i["gasto"] > 0]

        total_meta = 0.0
        for cat in sorted_cats:
            lines.append(f"• *{cat['categoria']}:* €{cat['meta']:.2f}")
            total_meta += cat["meta"]

        if no_meta:
            lines.append("")
            lines.append("_Sem meta definida:_")
            for cat in no_meta:
                lines.append(f"• {cat['categoria']}")

        lines.append("")
        lines.append(f"💰 *Total orçamento: €{total_meta:.2f}*")
        lines.append("")
        lines.append("_Para alterar as metas, edita a aba **Metas** na planilha._")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Metas error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── /meses — month picker menu ──────────────────────────────────────────────
async def meses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗓️ Carregando meses disponíveis...")
    try:
        available = get_available_months()
        if not available:
            await update.message.reply_text("Nenhum lançamento encontrado ainda.")
            return

        keyboard = []
        for m in available:
            keyboard.append([InlineKeyboardButton(
                m["label"],
                callback_data=f"month_{m['year']}_{m['month']:02d}"
            )])

        await update.message.reply_text(
            "🗓️ *Seleciona um mês para ver o resumo:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Meses error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── Callback handler for month view ─────────────────────────────────────────
async def handle_month_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, year_str, month_str = query.data.split("_")
        year = int(year_str)
        month = int(month_str)
    except Exception:
        await query.edit_message_text("❌ Erro ao processar o mês selecionado.")
        return

    await query.edit_message_text("📊 Carregando resumo do mês...")

    try:
        result = get_month_summary(year, month)
        insights = result["insights"]
        savings = result["savings"]

        from datetime import date
        dt = date(year, month, 1)
        month_label = dt.strftime("%B %Y").capitalize()

        # Build summary text
        title = f"📊 *Resumo — {month_label}*"
        summary = build_full_summary(insights, savings, title)

        # Add back button
        keyboard = [[InlineKeyboardButton("⬅️ Voltar aos meses", callback_data="month_back")]]
        await query.edit_message_text(
            summary,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Month callback error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Erro ao carregar mês: `{e}`", parse_mode="Markdown")

# ─── Callback: back to month list ─────────────────────────────────────────────
async def handle_month_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        available = get_available_months()
        keyboard = []
        for m in available:
            keyboard.append([InlineKeyboardButton(
                m["label"],
                callback_data=f"month_{m['year']}_{m['month']:02d}"
            )])
        await query.edit_message_text(
            "🗓️ *Seleciona um mês para ver o resumo:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── /relatorio — PDF mensal ──────────────────────────────────────────────────
async def relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📄 Gerando relatório PDF...")
    try:
        insights, savings = get_category_insights()
        filepath = generate_monthly_pdf(insights, savings)
        with open(filepath, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(filepath))
    except Exception as e:
        logger.error(f"PDF error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro ao gerar PDF: `{e}`", parse_mode="Markdown")

# ─── Monthly PDF job (last day of month, 20h Amsterdam) ──────────────────────
async def send_monthly_pdf(context) -> None:
    chat_id = context.job.data
    try:
        insights, savings = get_category_insights()
        filepath = generate_monthly_pdf(insights, savings)
        with open(filepath, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f, filename=os.path.basename(filepath),
                                              caption="📄 Relatório mensal de gastos!")
    except Exception as e:
        logger.error(f"Monthly PDF job error: {e}", exc_info=True)

async def check_last_day_of_month(context) -> None:
    """Runs daily; sends the PDF only if today is the last day of the month."""
    now = datetime.now(AMSTERDAM_TZ)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    if (tomorrow + timedelta(days=1)).month != now.month:
        await send_monthly_pdf(context)

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("resumo",     resumo))
    app.add_handler(CommandHandler("relatorio",  relatorio))
    app.add_handler(CommandHandler("categorias", categorias))
    app.add_handler(CommandHandler("meses",      meses))
    app.add_handler(CommandHandler("metas",      metas))
    app.add_handler(CallbackQueryHandler(handle_delete_callback,  pattern="^delrow_"))
    app.add_handler(CallbackQueryHandler(handle_category_back,    pattern="^catview_back$"))
    app.add_handler(CallbackQueryHandler(handle_category_callback,pattern="^catview_"))
    app.add_handler(CallbackQueryHandler(handle_expense_callback, pattern="^exp_"))
    app.add_handler(CallbackQueryHandler(handle_month_back,       pattern="^month_back$"))
    app.add_handler(CallbackQueryHandler(handle_month_callback,   pattern="^month_"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🤖 Bot iniciado!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
