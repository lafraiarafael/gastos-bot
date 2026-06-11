import os
import json
import logging
import tempfile
import re
from datetime import datetime
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

# ─── Categories (must match sheet exactly) ──────────────────────────────────
CATEGORIES = [
    "Alimentação", "Transporte", "Moradia", "Utilidades",
    "Saúde", "Lazer", "Educação", "Roupas",
    "Farmácia", "Impostos", "Outros"
]

# ─── Identify sender from Telegram profile ──────────────────────────────────
def identify_sender(update: Update) -> str:
    user = update.effective_user
    full_name = (user.full_name or "").lower()
    username  = (user.username  or "").lower()
    combined  = full_name + " " + username

    if "renata" in combined:
        return "Renata"
    if any(w in combined for w in ["rafael", "rafa"]):
        return "Rafa"
    # fallback: first name as typed
    return (user.first_name or "Desconhecido").capitalize()

# ─── Step 1: Transcribe with Whisper ────────────────────────────────────────
async def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="pt"
        )
    return transcript.text

# ─── Step 2: Parse expense with GPT-4o ──────────────────────────────────────
def parse_expense(text: str, sender: str) -> dict:
    today    = datetime.now().strftime("%d/%m/%y")
    week_num = datetime.now().isocalendar()[1]
    cats     = ", ".join(CATEGORIES)

    prompt = f"""Você é um assistente de controle financeiro de casal que mora na Holanda.
A moeda usada é o Euro (€).

O texto abaixo foi dito por **{sender}** e descreve um gasto.
Hoje: {today} | Semana ISO: {week_num}

Extraia e responda APENAS com JSON válido (sem markdown, sem explicação):
{{
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
- observacao: detalhes extras (opcional)

Texto: "{text}"
"""

    resp = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"}
    )
    return json.loads(resp.choices[0].message.content)

# ─── Step 3: Insert row in Lançamentos ──────────────────────────────────────
def insert_lancamento(expense: dict) -> int:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Lançamentos")

    all_vals = ws.get_all_values()

    # Find the first pre-filled row for this person that has date but no value,
    # OR find first truly empty row after last real entry
    target_row = None

    for idx, row in enumerate(all_vals):
        if idx < 2:          # skip title + header
            continue
        date_cell  = row[0].strip() if len(row) > 0 else ""
        who_cell   = row[2].strip() if len(row) > 2 else ""
        value_cell = row[5].strip() if len(row) > 5 else ""
        cat_cell   = row[3].strip() if len(row) > 3 else ""

        # Pre-filled row: has date + who, but no category/value yet
        if date_cell and who_cell and not cat_cell and not value_cell:
            if who_cell.lower() == expense["quem_pagou"].lower():
                target_row = idx + 1  # gspread is 1-indexed
                break

    # Fallback: append after last non-empty row
    if target_row is None:
        for idx in range(len(all_vals) - 1, 1, -1):
            if any(cell.strip() for cell in all_vals[idx]):
                target_row = idx + 2   # row after last filled
                break
        if target_row is None:
            target_row = len(all_vals) + 1

    # Format value as € string matching sheet style: " €  1.525,00 "
    valor_float = float(expense["valor"])
    valor_str   = f" €  {valor_float:,.2f} ".replace(",", "X").replace(".", ",").replace("X", ".")

    row_data = [
        expense["data"],
        expense["semana"],
        expense["quem_pagou"],
        expense["categoria"],
        expense.get("descricao", ""),
        valor_str,
        expense["pago_com"],
        expense.get("observacao", "")
    ]

    # Check if target row is pre-filled (update) or empty (insert)
    row_vals = all_vals[target_row - 1] if target_row <= len(all_vals) else []
    date_existing = row_vals[0].strip() if row_vals else ""

    if date_existing:
        # Update in place (pre-filled row)
        cell_range = f"A{target_row}:H{target_row}"
        ws.update(cell_range, [row_data])
        logger.info(f"Updated pre-filled row {target_row}: {row_data}")
    else:
        # Insert new row
        ws.insert_row(row_data, target_row)
        logger.info(f"Inserted new row {target_row}: {row_data}")

    return target_row

# ─── Step 4: Read Por Categoria insights ────────────────────────────────────
def get_category_insights() -> list[dict]:
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Por Categoria")
    data = ws.get_all_values()

    insights = []
    for row in data[2:]:   # skip title row + header row
        if not row or not row[0].strip() or row[0].strip().upper() == "TOTAL":
            continue
        try:
            def parse_currency(s):
                s = re.sub(r'[R$€\s]', '', s).replace(".", "").replace(",", ".").strip()
                return float(s) if s and s not in ["#DIV/0!", "-", ""] else 0.0

            categoria = row[0].strip()
            meta      = parse_currency(row[1]) if len(row) > 1 else 0.0
            gasto     = parse_currency(row[2]) if len(row) > 2 else 0.0
            saldo     = parse_currency(row[3]) if len(row) > 3 else 0.0
            pct_raw   = row[4].strip()         if len(row) > 4 else "0"
            pct_raw   = pct_raw.replace("%", "").replace(",", ".").strip()
            pct       = float(pct_raw) if pct_raw and pct_raw not in ["#DIV/0!", ""] else 0.0

            insights.append({
                "categoria": categoria,
                "meta":  meta,
                "gasto": gasto,
                "saldo": saldo,
                "pct":   pct
            })
        except Exception as e:
            logger.warning(f"Skipping row parse error: {row} → {e}")

    return insights

# ─── Step 5: Build reply message ─────────────────────────────────────────────
def build_reply(expense: dict, insights: list[dict]) -> str:
    month_name = datetime.now().strftime("%B").capitalize()

    def progress_bar(pct: float) -> str:
        pct_capped = min(pct, 100)
        filled     = round(pct_capped / 10)
        empty      = 10 - filled
        if pct >= 100:
            emoji = "🔴"
        elif pct >= 75:
            emoji = "🟡"
        else:
            emoji = "🟢"
        return f"{'█' * filled}{'░' * empty} {pct:.0f}% {emoji}"

    # ── This expense's category ──
    this_cat = next((i for i in insights if i["categoria"] == expense["categoria"]), None)

    lines = []
    lines.append(f"✅ *Lançado com sucesso!*")
    lines.append(f"")
    lines.append(f"👤 *{expense['quem_pagou']}*")
    lines.append(f"📂 {expense['categoria']}  |  📝 {expense.get('descricao','–')}")
    lines.append(f"💶 *€ {float(expense['valor']):.2f}*  ({expense['pago_com']})")
    if expense.get("observacao"):
        lines.append(f"💬 _{expense['observacao']}_")

    # ── Category meter ──
    lines.append("")
    if this_cat and this_cat["meta"] > 0:
        lines.append(f"📊 *{expense['categoria']} — {month_name}:*")
        lines.append(f"`{progress_bar(this_cat['pct'])}`")
        lines.append(f"Gasto €{this_cat['gasto']:.2f}  /  Meta €{this_cat['meta']:.2f}  |  Saldo €{this_cat['saldo']:.2f}")

    # ── Overview ──
    lines.append("")
    lines.append(f"📈 *Visão geral — {month_name}:*")

    sorted_cats = sorted([i for i in insights if i["meta"] > 0], key=lambda x: x["pct"], reverse=True)
    for cat in sorted_cats:
        lines.append(f"• {cat['categoria']:<14} `{progress_bar(cat['pct'])}`")

    # ── Alerts ──
    over    = [i for i in insights if i["pct"] >= 100 and i["meta"] > 0]
    warning = [i for i in insights if 75 <= i["pct"] < 100 and i["meta"] > 0]
    lines.append("")

    if over:
        names = " | ".join([f"*{i['categoria']}*" for i in over])
        lines.append(f"🔴 Estourou a meta: {names}")
    if warning:
        names = " | ".join([f"*{i['categoria']}*" for i in warning])
        lines.append(f"🟡 Atenção (>75%): {names}")
    if not over and not warning:
        lines.append("🟢 Tudo dentro do orçamento!")

    return "\n".join(lines)

# ─── Audio handler ───────────────────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = identify_sender(update)
    logger.info(f"Voice from {sender}")
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

        expense = parse_expense(transcript, sender)
        logger.info(f"Parsed: {expense}")

        insert_lancamento(expense)
        insights = get_category_insights()
        reply = build_reply(expense, insights)
        await update.message.reply_text(reply, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Voice error: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Erro ao processar o áudio.\n`{e}`\n\nTente novamente ou mande em texto.",
            parse_mode="Markdown"
        )

# ─── Text handler (fallback for typed expenses) ──────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text.startswith("/"):
        return
    if not re.search(r'\d', text):   # needs at least a number to be an expense
        return

    sender = identify_sender(update)
    await update.message.reply_text("📝 Processando...")

    try:
        expense = parse_expense(text, sender)
        insert_lancamento(expense)
        insights = get_category_insights()
        reply = build_reply(expense, insights)
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Text error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── /start ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Bot de Gastos — Rafa & Renata* ativo!\n\n"
        "🎙️ Mande um *áudio* com o gasto:\n"
        "_\"Gastei 32 euros no supermercado, débito\"_\n\n"
        "📝 Ou escreva:\n"
        "_\"Farmácia 15 euros crédito\"_\n\n"
        "Vou registrar na planilha e mostrar as metas do mês! 📊",
        parse_mode="Markdown"
    )

# ─── /resumo — current month overview ───────────────────────────────────────
async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Carregando resumo...")
    try:
        insights = get_category_insights()
        month_name = datetime.now().strftime("%B").capitalize()

        def bar(pct):
            filled = round(min(pct, 100) / 10)
            empty  = 10 - filled
            emoji  = "🔴" if pct >= 100 else "🟡" if pct >= 75 else "🟢"
            return f"{'█'*filled}{'░'*empty} {pct:.0f}% {emoji}"

        lines = [f"📊 *Resumo — {month_name}*\n"]
        total_gasto = 0
        total_meta  = 0

        for cat in sorted(insights, key=lambda x: x["pct"], reverse=True):
            if cat["meta"] == 0:
                continue
            lines.append(f"*{cat['categoria']}*")
            lines.append(f"`{bar(cat['pct'])}`  €{cat['gasto']:.0f} / €{cat['meta']:.0f}")
            total_gasto += cat["gasto"]
            total_meta  += cat["meta"]

        total_pct = (total_gasto / total_meta * 100) if total_meta > 0 else 0
        lines.append(f"\n💰 *Total: €{total_gasto:.2f} / €{total_meta:.2f}*")
        lines.append(f"`{bar(total_pct)}`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: `{e}`", parse_mode="Markdown")

# ─── Main ────────────────────────────────────────────────────────────────────
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
