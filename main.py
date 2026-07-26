import os
import uuid
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")

# Read GROUP_ID safely
raw_group_id = os.getenv("GROUP_ID")
GROUP_ID = int(raw_group_id) if raw_group_id else None

AMOUNT = 4000  # $40.00 USD in cents (1 USD = 100 cents)

app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Pay $40.00 USD", callback_data="pay")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome!\n\nClick the button below to pay **$40.00 USD** (approx. £30) and get instant access to the group.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    reference = f"tg_{user.id}_{uuid.uuid4().hex[:8]}"

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json"
    }
    payload = {
        "email": f"user_{user.id}@telegram.com",  # Standard valid domain format
        "amount": AMOUNT,
        "currency": "USD",  # Set currency to USD
        "reference": reference,
        "metadata": {
            "telegram_id": user.id,
            "telegram_username": user.username or "",
            "telegram_name": user.full_name
        }
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers
        )
        data = res.json()

    if data.get("status"):
        payment_url = data["data"]["authorization_url"]
        
        keyboard = [[InlineKeyboardButton("💳 Click Here to Pay $40.00 USD", url=payment_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "Your payment link is ready! Click the button below to complete your checkout on Paystack.\n\n"
            "Once completed, you will automatically receive your invite link right here.",
            reply_markup=reply_markup
        )
    else:
        print("Paystack error:", data)
        await query.edit_message_text("Something went wrong generating your payment link. Please try again later.")

# Register Handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CallbackQueryHandler(pay_callback, pattern="^pay$"))


# ---------- Webhook Endpoint for Telegram Updates ----------
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Receives commands and button clicks from Telegram safely without polling conflicts."""
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}


# ---------- Paystack Webhook ----------
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    payload = await request.json()

    if payload.get("event") != "charge.success":
        return {"status": "ignored"}

    data = payload.get("data", {})
    metadata = data.get("metadata", {})
    telegram_id = metadata.get("telegram_id")

    if not telegram_id:
        raise HTTPException(status_code=400, detail="No telegram_id")

    if not GROUP_ID:
        print("Error: GROUP_ID is not configured in Environment Variables.")
        return {"status": "error", "message": "GROUP_ID not set"}

    # Create one-time invite link
    try:
        invite = await telegram_app.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            member_limit=1,
            name=f"Paid-{telegram_id}"
        )
        invite_link = invite.invite_link
    except Exception as e:
        print("Error creating invite:", e)
        return {"status": "error"}

    # Send invite link to the user
    try:
        await telegram_app.bot.send_message(
            chat_id=telegram_id,
            text=(
                "✅ *Payment successful!*\n\n"
                "Here is your private group invite link (one-time use only):\n\n"
                f"{invite_link}\n\n"
                "Click it to join now."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print("Error sending message to user:", e)

    return {"status": "success"}


# ---------- App Lifecycle ----------
@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()

@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()