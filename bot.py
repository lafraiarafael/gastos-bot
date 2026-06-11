import os
import json
import logging
import tempfile
import re
import unicodedata
from datetime import datetime, date
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes, JobQueue
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

# ─── Text helpers ───────────────────────────────────────────────────────────
def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

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

    # Google Sheets can display dates in multiple formats depending on locale.
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

def current_month_bounds() -> tuple[date, date]:
    now = datetime.now(AMSTERDAM_TZ).date()
    start = date(now.year, now.month, 1)

    if now.month == 12:
        end = date(now.year + 1, 1, 1)
    else:
        end = date(now.year, now.month + 1, 1)

    return start, end

def is_current_month(value) -> bool:
    parsed = parse_sheet_date(value)
    if parsed is None:
        return False

    start, end = current_month_bounds()
    return start <= parsed < end

def current_month_label() -> str:
    return datetime.now(AMSTERDAM_TZ).strftime("%m/%Y")

# ─── Category list intent ───────────────────────────────────────────────────
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

def extract_category_from_text(text: str) -> str | None:
    normalized = normalize_text(text)

    for category in CATEGORIES:
        if normalize_text(category) in normalized:
            return category

    return None

def get_monthly_spend_by_category() -> dict[str, float]:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    rows = ws.get_all_values()

    totals = {category: 0.0 for category in CATEGORIES}

    for row in rows[2:]:
        if len(row) < 6:
            continue

        row_date = row[0] if len(row) > 0 else ""
        if not is_current_month(row_date):
            continue

        row_category = row[3].strip() if len(row) > 3 else ""
        canonical_category = extract_category_from_text(row_category) or row_category

        if canonical_category not in totals:
            totals[canonical_category] = 0.0

        totals[canonical_category] += parse_currency_value(row[5] if len(row) > 5 else "")

    return totals

def get_expenses_by_category(category: str, current_month_only: bool = True) -> list[dict]:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")
    rows = ws.get_all_values()

    category_norm = normalize_text(category)
    expenses = []

    for row in rows[2:]:
        if len(row) < 4:
            continue

        row_date = row[0].strip() if len(row) > 0 else ""
        if current_month_only and not is_current_month(row_date):
            continue

        row_category = row[3].strip() if len(row) > 3 else ""
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

def build_category_expenses_reply(category: str, expenses: list[dict]) -> str:
    if not expenses:
        valid = ", ".join(CATEGORIES)
        return (
            f"📋 Nenhum gasto encontrado na categoria {category} no mês atual ({current_month_label()}).\n\n"
            f"Categorias válidas: {valid}"
        )

    total = sum(item["valor"] for item in expenses)
    lines = [
        f"📋 Gastos da categoria: {category}",
        f"Mês: {current_month_label()}",
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
    cats = ", ".join(CATEGORIES)

    # Check if user is requesting a summary
    summary_keywords = ["resumo", "summary", "relatório", "relatorio", "como estamos", "situação", "situacao"]
    if any(kw in text.lower() for kw in summary_keywords):
        return {"is_summary_request": True}

    prompt = f"""Você é um assistente de controle financeiro de casal que mora na Holanda.
A moeda usada é o Euro (€).

O texto abaixo foi dito por **{sender}** e descreve um gasto.
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
- descricao: nome do estabelecimento ou produto

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

    # Store the amount as a real number, not as a formatted text string.
    # Currency formatting must be handled by the Google Sheets column format.
    valor_float = round(float(expense["valor"]), 2)

    row_data = [
        expense["data"], expense["semana"], expense["quem_pagou"],
        expense["categoria"], expense.get("descricao", ""),
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
def get_category_insights() -> list[dict]:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Por Categoria")
    data = ws.get_all_values()
    monthly_spend = get_monthly_spend_by_category()

    insights = []
    for row in data[2:]:
        if not row or not row[0].strip() or row[0].strip().upper() == "TOTAL":
            continue
        try:
            categoria = row[0].strip()
            meta = parse_currency_value(row[1]) if len(row) > 1 else 0.0
            gasto = monthly_spend.get(categoria, 0.0)
            saldo = meta - gasto
            pct = (gasto / meta * 100) if meta > 0 else 0.0

            insights.append({"categoria": categoria, "meta": meta, "gasto": gasto, "saldo": saldo, "pct": pct})
        except Exception as e:
            logger.warning(f"Skipping row: {row} → {e}")

    return insights

# ─── Build confirmation message (category only) ──────────────────────────────
def build_confirmation(expense: dict, insights: list[dict]) -> str:
    this_cat = next((i for i in insights if i["categoria"] == expense["categoria"]), None)

    lines = []
    lines.append(f"✅ *Lançado com sucesso!*\n")
    lines.append(f"👤 *{expense['quem_pagou']}*")
    lines.append(f"📂 {expense['categoria']}  |  📝 {expense.get('descricao', '–')}")
    lines.append(f"💶 *€ {float(expense['valor']):.2f}*  ({expense['pago_com']})")
    if expense.get("observacao"):
        lines.append(f"💬 _{expense['observacao']}_")

    lines.append("")
    if this_cat and this_cat["meta"] > 0:
        lines.append(f"📊 *{expense['categoria']} — mês {current_month_label()}:*")
        lines.append(f"`{progress_bar(this_cat['pct'])}`")
        lines.append(f"Gasto {format_eur(this_cat['gasto'])}  /  Meta {format_eur(this_cat['meta'])}  |  Saldo {format_eur(this_cat['saldo'])}")

        if this_cat['pct'] >= 100:
            lines.append(f"\n🔴 *Atenção! Meta de {expense['categoria']} estourada!*")
        elif this_cat['pct'] >= 75:
            lines.append(f"\n🟡 Quase no limite de {expense['categoria']}!")

    return "\n".join(lines)

# ─── Build full summary ──────────────────────────────────────────────────────
def build_full_summary(insights: list[dict], title: str = None) -> str:
    if not title:
        title = f"📊 *Resumo Mensal — {current_month_label()}*"

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
    lines.append(f"💰 *TOTAL: {format_eur(total_gasto)} / {format_eur(total_meta)}*")
    lines.append(f"`{progress_bar(total_pct)}`")

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

# ─── Weekly summary job ──────────────────────────────────────────────────────
async def send_weekly_summary(context) -> None:
    chat_id = context.job.data
    try:
        insights = get_category_insights()
        now = datetime.now(AMSTERDAM_TZ)
        title = f"📊 *Resumo Semanal — {now.strftime('%d/%m/%Y')}*"
        msg = build_full_summary(insights, title)
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
            category = extract_category_from_text(transcript)
            if not category:
                await update.message.reply_text(
                    "Não encontrei a categoria no pedido. Categorias válidas: " + ", ".join(CATEGORIES)
                )
                return

            expenses = get_expenses_by_category(category)
            reply = build_category_expenses_reply(category, expenses)
            await update.message.reply_text(reply)
            return

        parsed = parse_expense(transcript, sender)

        # Summary request via audio
        if parsed.get("is_summary_request"):
            insights = get_category_insights()
            reply = build_full_summary(insights)
            await update.message.reply_text(reply, parse_mode="Markdown")
            return

        expense = parsed
        insert_lancamento(expense)
        insights = get_category_insights()
        reply = build_confirmation(expense, insights)
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

    # Category list request via text
    if is_category_list_request(text):
        await update.message.reply_text("📋 Buscando gastos da categoria no mês atual...")
        try:
            category = extract_category_from_text(text)
            if not category:
                await update.message.reply_text(
                    "Não encontrei a categoria no pedido. Categorias válidas: " + ", ".join(CATEGORIES)
                )
                return

            expenses = get_expenses_by_category(category)
            reply = build_category_expenses_reply(category, expenses)
            await update.message.reply_text(reply)
            return
        except Exception as e:
            logger.error(f"Category list error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")
            return

    # Summary request via text
    summary_keywords = ["resumo", "summary", "relatório", "relatorio", "como estamos"]
    if any(kw in text.lower() for kw in summary_keywords):
        await update.message.reply_text("📊 Carregando resumo do mês atual...")
        insights = get_category_insights()
        reply = build_full_summary(insights)
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    if not re.search(r'\d', text):
        return

    await update.message.reply_text("📝 Processando...")

    try:
        expense = parse_expense(text, sender)
        if expense.get("is_summary_request"):
            insights = get_category_insights()
            reply = build_full_summary(insights)
            await update.message.reply_text(reply, parse_mode="Markdown")
            return

        insert_lancamento(expense)
        insights = get_category_insights()
        reply = build_confirmation(expense, insights)
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
        "📊 Para ver o resumo do mês atual, mande:\n"
        "_resumo_ ou _como estamos?_\n\n"
        "📋 Para listar uma categoria no mês atual, mande:\n"
        "_mostre os gastos da categoria Alimentação_\n\n"
        "🗓️ Toda segunda-feira às 8h recebem o resumo automático!\n\n"
        "Vamos economizar! 💪",
        parse_mode="Markdown"
    )

# ─── /resumo ─────────────────────────────────────────────────────────────────
async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Carregando resumo do mês atual...")
    try:
        insights = get_category_insights()
        reply = build_full_summary(insights)
        await update.message.reply_text(reply, parse_mode="Markdown")
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
