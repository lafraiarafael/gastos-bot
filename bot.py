import os
import json
import logging
import tempfile
import re
import unicodedata
from datetime import datetime, date
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Clients ────────────────────────────────────────────────────────────────
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

# ─── Categories ─────────────────────────────────────────────────────────────
CATEGORIES = [
    "Alimentação", "Transporte", "Moradia", "Utilidades",
    "Saúde", "Lazer", "Educação", "Roupas",
    "Farmácia", "Impostos", "Outros"
]
SAVINGS_CATEGORY = "Poupança"
VALID_CATEGORIES = CATEGORIES + [SAVINGS_CATEGORY]

MONTH_ALIASES = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}

MONTH_NAMES = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}

# ─── Text helpers ───────────────────────────────────────────────────────────
def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9/]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def is_savings_category(value: str) -> bool:
    return normalize_text(value) == normalize_text(SAVINGS_CATEGORY)

def parse_currency_value(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value or "").strip()
    s = re.sub(r"[R$€'\s]", "", s)

    if not s or s in ["#DIV/0!", "-"]:
        return 0.0

    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")

    try:
        return float(s)
    except ValueError:
        return 0.0

def format_eur(value: float) -> str:
    return f"€ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def parse_sheet_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    raw = str(value or "").strip()
    if not raw:
        return None

    formats = [
        "%d/%m/%Y",
        "%d/%m/%y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d-%m-%y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    return None

def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)

    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    return start, end

def current_month_bounds() -> tuple[date, date]:
    now = datetime.now(AMSTERDAM_TZ).date()
    return month_bounds(now.year, now.month)

def period_label(start: date) -> str:
    return f"{MONTH_NAMES[start.month].capitalize()}/{start.year}"

def bounds_from_date(value) -> tuple[date, date]:
    parsed = parse_sheet_date(value) or datetime.now(AMSTERDAM_TZ).date()
    return month_bounds(parsed.year, parsed.month)

def extract_period_from_text(text: str) -> tuple[date, date]:
    normalized = normalize_text(text)
    today = datetime.now(AMSTERDAM_TZ).date()

    if any(phrase in normalized for phrase in ["mes passado", "mes anterior", "ultimo mes"]):
        if today.month == 1:
            return month_bounds(today.year - 1, 12)
        return month_bounds(today.year, today.month - 1)

    numeric_match = re.search(r"\b(0?[1-9]|1[0-2])[/\-](20\d{2}|\d{2})\b", normalized)
    if numeric_match:
        month = int(numeric_match.group(1))
        year = int(numeric_match.group(2))
        if year < 100:
            year += 2000
        return month_bounds(year, month)

    year_match = re.search(r"\b(20\d{2})\b", normalized)
    explicit_year = int(year_match.group(1)) if year_match else None

    for alias, month in MONTH_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            if explicit_year:
                year = explicit_year
            else:
                # If the month requested is ahead of the current month, assume the previous year.
                # Example: in January, "resumo de dezembro" usually means last December.
                year = today.year if month <= today.month else today.year - 1
            return month_bounds(year, month)

    return current_month_bounds()

def is_in_period(value, start: date, end: date) -> bool:
    parsed = parse_sheet_date(value)
    if parsed is None:
        return False

    return start <= parsed < end

# ─── Intent helpers ─────────────────────────────────────────────────────────
def is_category_list_request(text: str) -> bool:
    normalized = normalize_text(text)

    has_expense_word = any(word in normalized for word in [
        "gastos", "despesas", "lancamentos", "lancamento"
    ])
    has_category_word = "categoria" in normalized
    has_request_word = any(phrase in normalized for phrase in [
        "quais foram", "me mostre", "mostre", "mostrar",
        "listar", "liste", "lista", "ver", "consultar", "consulta"
    ])

    return has_expense_word and has_category_word and has_request_word

def is_summary_request(text: str) -> bool:
    normalized = normalize_text(text)
    return any(kw in normalized for kw in [
        "resumo", "summary", "relatorio", "como estamos", "situacao"
    ])

def extract_category_from_text(text: str, include_savings: bool = False) -> str | None:
    normalized = normalize_text(text)
    categories = VALID_CATEGORIES if include_savings else CATEGORIES

    for category in categories:
        if normalize_text(category) in normalized:
            return category

    return None

# ─── Google Sheets readers ──────────────────────────────────────────────────
def get_launch_rows() -> list[list[str]]:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    return ws.get_all_values()

def get_monthly_spend_by_category(start: date, end: date) -> dict[str, float]:
    rows = get_launch_rows()
    totals = {category: 0.0 for category in CATEGORIES}

    for row in rows[2:]:
        if len(row) < 6:
            continue

        row_date = row[0] if len(row) > 0 else ""
        if not is_in_period(row_date, start, end):
            continue

        row_category = row[3].strip() if len(row) > 3 else ""
        if is_savings_category(row_category):
            continue

        canonical_category = extract_category_from_text(row_category) or row_category

        if canonical_category not in totals:
            totals[canonical_category] = 0.0

        totals[canonical_category] += parse_currency_value(row[5] if len(row) > 5 else "")

    return totals

def get_savings_total() -> float:
    rows = get_launch_rows()
    total = 0.0

    for row in rows[2:]:
        row_category = row[3].strip() if len(row) > 3 else ""
        if not is_savings_category(row_category):
            continue

        total += parse_currency_value(row[5] if len(row) > 5 else "")

    return total

def get_expenses_by_category(category: str, start: date, end: date) -> list[dict]:
    rows = get_launch_rows()
    category_norm = normalize_text(category)
    expenses = []

    for row in rows[2:]:
        if len(row) < 4:
            continue

        row_date = row[0].strip() if len(row) > 0 else ""
        if not is_in_period(row_date, start, end):
            continue

        row_category = row[3].strip() if len(row) > 3 else ""
        if is_savings_category(row_category):
            continue

        if normalize_text(row_category) != category_norm:
            continue

        amount_raw = row[5] if len(row) > 5 else ""
        amount = parse_currency_value(amount_raw)

        expenses.append({
            "data": row_date,
            "quem_pagou": row[2].strip() if len(row) > 2 else "",
            "categoria": row_category,
            "descricao": row[4].strip() if len(row) > 4 else "",
            "valor": amount,
            "pago_com": row[6].strip() if len(row) > 6 else "",
            "observacao": row[7].strip() if len(row) > 7 else "",
        })

    return expenses

def build_category_expenses_reply(category: str, expenses: list[dict], start: date) -> str:
    label = period_label(start)

    if not expenses:
        valid = ", ".join(CATEGORIES)
        return (
            f"📋 Nenhum gasto encontrado na categoria {category} em {label}.\n\n"
            f"Categorias válidas: {valid}"
        )

    total = sum(item["valor"] for item in expenses)
    lines = [
        f"📋 Gastos da categoria: {category}",
        f"Mês: {label}",
        f"Registros: {len(expenses)}",
        f"Total: {format_eur(total)}",
        "",
    ]

    max_items = 30
    for item in expenses[:max_items]:
        # Columns A, C, D, E, F, G, H:
        # data, quem_pagou, categoria, descricao, valor, pago_com, observacao
        line = (
            f"• {item['data']} | {item['quem_pagou']} | {item['categoria']} | "
            f"{item['descricao']} | {format_eur(item['valor'])} | {item['pago_com']}"
        )
        if item["observacao"]:
            line += f" | {item['observacao']}"
        lines.append(line)

    remaining = len(expenses) - max_items
    if remaining > 0:
        lines.append("")
        lines.append(f"...mais {remaining} lançamento(s). Refine a busca se quiser ver menos itens.")

    return "\n".join(lines)

# ─── Identify sender ─────────────────────────────────────────────────────────
def identify_sender(update: Update) -> str:
    user = update.effective_user
    combined = ((user.full_name or "") + " " + (user.username or "")).lower()
    if "renata" in combined:
        return "Renata"
    if any(w in combined for w in ["rafael", "rafa"]):
        return "Rafa"
    return (user.first_name or "Desconhecido").capitalize()

# ─── Progress bar ────────────────────────────────────────────────────────────
def progress_bar(pct: float) -> str:
    pct_capped = min(pct, 100)
    filled = round(pct_capped / 10)
    empty = 10 - filled
    emoji = "🔴" if pct >= 100 else "🟡" if pct >= 75 else "🟢"
    return f"{'█' * filled}{'░' * empty} {pct:.0f}% {emoji}"

# ─── Transcribe ──────────────────────────────────────────────────────────────
async def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1", file=f, language="pt"
        )
    return transcript.text

# ─── Parse expense ───────────────────────────────────────────────────────────
def parse_expense(text: str, sender: str) -> dict:
    today = datetime.now(AMSTERDAM_TZ).strftime("%d/%m/%Y")
    week_num = datetime.now(AMSTERDAM_TZ).isocalendar()[1]
    cats = ", ".join(VALID_CATEGORIES)

    if is_summary_request(text):
        return {"is_summary_request": True}

    prompt = f"""Você é um assistente de controle financeiro de casal que mora na Holanda.
A moeda usada é o Euro (€).

O texto abaixo foi dito por **{sender}** e descreve um lançamento financeiro.
Hoje: {today} | Semana ISO: {week_num}

Extraia e responda APENAS com JSON válido (sem markdown, sem explicação):
{{
  "is_summary_request": false,
  "data": "DD/MM/YYYY",
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
- quem_pagou: use "{sender}" se não mencionado
- pago_com: "Débito" se não mencionado
- valor: número puro sem símbolo (ex: 32.50)
- data: hoje se não mencionada ({today})
- descricao: nome do estabelecimento, produto ou objetivo
- Se o texto falar em poupança, guardar dinheiro, reserva, fundo ou economia acumulada, use categoria "{SAVINGS_CATEGORY}"
- "{SAVINGS_CATEGORY}" não é gasto mensal; é um fundo acumulado

Texto: "{text}"
"""

    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"}
    )
    return json.loads(resp.choices[0].message.content)

# ─── Insert lancamento ───────────────────────────────────────────────────────
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

    valor_float = round(float(expense["valor"]), 2)
    categoria = expense.get("categoria", "Outros")
    if is_savings_category(categoria):
        categoria = SAVINGS_CATEGORY

    row_data = [
        expense["data"], expense["semana"], expense["quem_pagou"],
        categoria, expense.get("descricao", ""),
        valor_float, expense["pago_com"], expense.get("observacao", "")
    ]

    row_vals = all_vals[target_row - 1] if target_row <= len(all_vals) else []
    date_existing = row_vals[0].strip() if row_vals else ""

    if date_existing:
        ws.update(f"A{target_row}:H{target_row}", [row_data], value_input_option="USER_ENTERED")
    else:
        ws.insert_row(row_data, target_row, value_input_option="USER_ENTERED")

    logger.info(f"Row {target_row}: {row_data}")
    return target_row

# ─── Get category insights ───────────────────────────────────────────────────
def get_category_insights(start: date, end: date) -> list[dict]:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Por Categoria")
    data = ws.get_all_values()
    monthly_spend = get_monthly_spend_by_category(start, end)

    insights = []
    for row in data[2:]:
        if not row or not row[0].strip() or row[0].strip().upper() == "TOTAL":
            continue
        try:
            categoria = row[0].strip()
            if is_savings_category(categoria):
                continue

            meta = parse_currency_value(row[1]) if len(row) > 1 else 0.0
            gasto = monthly_spend.get(categoria, 0.0)
            saldo = meta - gasto
            pct = (gasto / meta * 100) if meta > 0 else 0.0

            insights.append({"categoria": categoria, "meta": meta, "gasto": gasto, "saldo": saldo, "pct": pct})
        except Exception as e:
            logger.warning(f"Skipping row: {row} → {e}")

    return insights

# ─── Build confirmation message ─────────────────────────────────────────────
def build_confirmation(expense: dict, insights: list[dict], start: date) -> str:
    categoria = expense.get("categoria", "")

    if is_savings_category(categoria):
        return (
            "✅ *Poupança registrada!*\n\n"
            f"👤 *{expense['quem_pagou']}*\n"
            f"💶 *{format_eur(float(expense['valor']))}*\n"
            f"📝 {expense.get('descricao', 'Poupança')}"
        )

    this_cat = next((i for i in insights if i["categoria"] == categoria), None)

    lines = []
    lines.append(f"✅ *Lançado com sucesso!*\n")
    lines.append(f"👤 *{expense['quem_pagou']}*")
    lines.append(f"📂 {categoria}  |  📝 {expense.get('descricao', '–')}")
    lines.append(f"💶 *€ {float(expense['valor']):.2f}*  ({expense['pago_com']})")
    if expense.get("observacao"):
        lines.append(f"💬 _{expense['observacao']}_")

    lines.append("")
    if this_cat and this_cat["meta"] > 0:
        lines.append(f"📊 *{categoria} — {period_label(start)}:*")
        lines.append(f"`{progress_bar(this_cat['pct'])}`")
        lines.append(f"Gasto {format_eur(this_cat['gasto'])}  /  Meta {format_eur(this_cat['meta'])}  |  Saldo {format_eur(this_cat['saldo'])}")

        if this_cat['pct'] >= 100:
            lines.append(f"\n🔴 *Atenção! Meta de {categoria} estourada!*")
        elif this_cat['pct'] >= 75:
            lines.append(f"\n🟡 Quase no limite de {categoria}!")

    return "\n".join(lines)

# ─── Build full summary ──────────────────────────────────────────────────────
def build_full_summary(insights: list[dict], start: date, savings_total: float, title: str = None) -> str:
    if not title:
        title = f"📊 *Resumo Mensal — {period_label(start)}*"

    lines = [title, ""]
    total_gasto = 0
    total_meta = 0

    sorted_cats = sorted([i for i in insights if i["meta"] > 0], key=lambda x: x["pct"], reverse=True)

    for cat in sorted_cats:
        lines.append(f"*{cat['categoria']}*")
        lines.append(f"`{progress_bar(cat['pct'])}`")
        lines.append(f"{format_eur(cat['gasto'])} / {format_eur(cat['meta'])}  |  Saldo {format_eur(cat['saldo'])}")
        lines.append("")
        total_gasto += cat["gasto"]
        total_meta += cat["meta"]

    total_pct = (total_gasto / total_meta * 100) if total_meta > 0 else 0
    lines.append(f"💰 *TOTAL GASTOS: {format_eur(total_gasto)} / {format_eur(total_meta)}*")
    lines.append(f"`{progress_bar(total_pct)}`")
    lines.append(f"🏦 *Poupança: {format_eur(savings_total)}*")

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

async def reply_summary(update: Update, text: str):
    start, end = extract_period_from_text(text)
    await update.message.reply_text(f"📊 Carregando resumo de {period_label(start)}...")
    insights = get_category_insights(start, end)
    savings_total = get_savings_total()
    reply = build_full_summary(insights, start, savings_total)
    await update.message.reply_text(reply, parse_mode="Markdown")

async def reply_category_list(update: Update, text: str):
    start, end = extract_period_from_text(text)
    category = extract_category_from_text(text)
    if not category:
        await update.message.reply_text(
            "Não encontrei a categoria no pedido. Categorias válidas: " + ", ".join(CATEGORIES)
        )
        return

    await update.message.reply_text(f"📋 Buscando gastos de {category} em {period_label(start)}...")
    expenses = get_expenses_by_category(category, start, end)
    reply = build_category_expenses_reply(category, expenses, start)
    await update.message.reply_text(reply)

# ─── Weekly summary job ──────────────────────────────────────────────────────
async def send_weekly_summary(context) -> None:
    chat_id = context.job.data
    try:
        start, end = current_month_bounds()
        insights = get_category_insights(start, end)
        savings_total = get_savings_total()
        now = datetime.now(AMSTERDAM_TZ)
        title = f"📊 *Resumo Semanal — {now.strftime('%d/%m/%Y')}*"
        msg = build_full_summary(insights, start, savings_total, title)
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        logger.info(f"Weekly summary sent to {chat_id}")
    except Exception as e:
        logger.error(f"Weekly summary error: {e}", exc_info=True)

# ─── Voice handler ───────────────────────────────────────────────────────────
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

        if is_category_list_request(transcript):
            await reply_category_list(update, transcript)
            return

        if is_summary_request(transcript):
            await reply_summary(update, transcript)
            return

        parsed = parse_expense(transcript, sender)

        if parsed.get("is_summary_request"):
            await reply_summary(update, transcript)
            return

        expense = parsed
        if is_savings_category(expense.get("categoria", "")):
            expense["categoria"] = SAVINGS_CATEGORY

        insert_lancamento(expense)
        start, end = bounds_from_date(expense.get("data"))
        insights = get_category_insights(start, end)
        reply = build_confirmation(expense, insights, start)
        await update.message.reply_text(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Voice error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── Text handler ────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text.startswith("/"):
        return

    sender = identify_sender(update)

    if is_category_list_request(text):
        try:
            await reply_category_list(update, text)
            return
        except Exception as e:
            logger.error(f"Category list error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")
            return

    if is_summary_request(text):
        try:
            await reply_summary(update, text)
            return
        except Exception as e:
            logger.error(f"Summary error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")
            return

    if not re.search(r'\d', text):
        return

    await update.message.reply_text("📝 Processando...")

    try:
        expense = parse_expense(text, sender)
        if expense.get("is_summary_request"):
            await reply_summary(update, text)
            return

        if is_savings_category(expense.get("categoria", "")):
            expense["categoria"] = SAVINGS_CATEGORY

        insert_lancamento(expense)
        start, end = bounds_from_date(expense.get("data"))
        insights = get_category_insights(start, end)
        reply = build_confirmation(expense, insights, start)
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Text error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── /start ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Schedule weekly summary every Monday at 08:00 Amsterdam time
    job_name = f"weekly_{chat_id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()

    context.job_queue.run_daily(
        send_weekly_summary,
        time=datetime.now(AMSTERDAM_TZ).replace(hour=8, minute=0, second=0, microsecond=0).timetz(),
        days=(0,),  # 0 = Monday
        name=job_name,
        data=chat_id
    )

    await update.message.reply_text(
        "👋 *Bot de Gastos — Rafa & Renata* ativo!\n\n"
        "🎙️ Mande um *áudio* com o gasto:\n"
        "_Gastei 32 euros no supermercado, débito_\n\n"
        "🏦 Para registrar poupança:\n"
        "_poupança 100 euros_\n"
        "_guardei 250 euros na poupança_\n\n"
        "📊 Para ver resumo por mês:\n"
        "_resumo_\n"
        "_resumo de junho_\n"
        "_resumo do mês passado_\n\n"
        "📋 Para listar uma categoria por mês:\n"
        "_mostre os gastos da categoria Alimentação_\n"
        "_mostre os gastos da categoria Alimentação em junho_\n\n"
        "🗓️ Toda segunda-feira às 8h recebem o resumo automático do mês atual!\n\n"
        "Vamos economizar! 💪",
        parse_mode="Markdown"
    )

# ─── /resumo ─────────────────────────────────────────────────────────────────
async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = " ".join(context.args) if context.args else ""
        await reply_summary(update, query)
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("resumo", resumo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🤖 Bot iniciado!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
