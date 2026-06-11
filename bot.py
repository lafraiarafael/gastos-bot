import os
import json
import logging
import tempfile
import re
from datetime import datetime
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
    today = datetime.now(AMSTERDAM_TZ).strftime("%d/%m/%y")
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

    insights = []
    for row in data[2:]:
        if not row or not row[0].strip() or row[0].strip().upper() == "TOTAL":
            continue
        try:
            def parse_currency(s):
                s = re.sub(r'[R$€\s]', '', s).replace(".", "").replace(",", ".").strip()
                return float(s) if s and s not in ["#DIV/0!", "-", ""] else 0.0

            categoria = row[0].strip()
            meta  = parse_currency(row[1]) if len(row) > 1 else 0.0
            gasto = parse_currency(row[2]) if len(row) > 2 else 0.0
            saldo = parse_currency(row[3]) if len(row) > 3 else 0.0
            pct_raw = row[4].strip() if len(row) > 4 else "0"
            pct_raw = pct_raw.replace("%", "").replace(",", ".").strip()
            pct = float(pct_raw) if pct_raw and pct_raw not in ["#DIV/0!", ""] else 0.0

            insights.append({"categoria": categoria, "meta": meta, "gasto": gasto, "saldo": saldo, "pct": pct})
        except Exception as e:
            logger.warning(f"Skipping row: {row} → {e}")

    return insights

# ─── Build confirmation message (category only) ──────────────────────────────
def build_confirmation(expense: dict, insights: list[dict]) -> str:
    month_name = datetime.now(AMSTERDAM_TZ).strftime("%B").capitalize()
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
        lines.append(f"📊 *{expense['categoria']} — {month_name}:*")
        lines.append(f"`{progress_bar(this_cat['pct'])}`")
        lines.append(f"Gasto €{this_cat['gasto']:.2f}  /  Meta €{this_cat['meta']:.2f}  |  Saldo €{this_cat['saldo']:.2f}")

        if this_cat['pct'] >= 100:
            lines.append(f"\n🔴 *Atenção! Meta de {expense['categoria']} estourada!*")
        elif this_cat['pct'] >= 75:
            lines.append(f"\n🟡 Quase no limite de {expense['categoria']}!")

    return "\n".join(lines)

# ─── Build full summary ──────────────────────────────────────────────────────
def build_full_summary(insights: list[dict], title: str = None) -> str:
    month_name = datetime.now(AMSTERDAM_TZ).strftime("%B").capitalize()
    if not title:
        title = f"📊 *Resumo Mensal — {month_name}*"

    lines = [title, ""]
    total_gasto = 0
    total_meta = 0

    sorted_cats = sorted([i for i in insights if i["meta"] > 0], key=lambda x: x["pct"], reverse=True)

    for cat in sorted_cats:
        lines.append(f"*{cat['categoria']}*")
        lines.append(f"`{progress_bar(cat['pct'])}`")
        lines.append(f"€{cat['gasto']:.2f} / €{cat['meta']:.2f}  |  Saldo €{cat['saldo']:.2f}")
        lines.append("")
        total_gasto += cat["gasto"]
        total_meta += cat["meta"]

    total_pct = (total_gasto / total_meta * 100) if total_meta > 0 else 0
    lines.append(f"💰 *TOTAL: €{total_gasto:.2f} / €{total_meta:.2f}*")
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

    # Summary request via text
    summary_keywords = ["resumo", "summary", "relatório", "relatorio", "como estamos"]
    if any(kw in text.lower() for kw in summary_keywords):
        await update.message.reply_text("📊 Carregando resumo...")
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
        "_\"Gastei 32 euros no supermercado, débito\"_\n\n"
        "📊 Para ver o resumo completo, mande:\n"
        "_\"resumo\"_ ou _\"como estamos?\"_\n\n"
        "🗓️ Toda segunda-feira às 8h recebem o resumo automático!\n\n"
        "Vamos economizar! 💪",
        parse_mode="Markdown"
    )

# ─── /resumo ─────────────────────────────────────────────────────────────────
async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Carregando resumo...")
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
