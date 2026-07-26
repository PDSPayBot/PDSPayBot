import os
import re
import uuid
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")

raw_group_id = os.getenv("GROUP_ID")
GROUP_ID = int(raw_group_id) if raw_group_id else None

AMOUNT = 520000  # 5,200 KES in cents

# State definition for collecting email
WAITING_FOR_EMAIL = 1

app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()


# ---------- Telegram Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message with payment button."""
    keyboard = [
        [InlineKeyboardButton("💳 Pay $40.00 USD / £30 GBP", callback_data="pay")],
        [
            InlineKeyboardButton("❓ FAQ & Info", callback_data="faq"),
            InlineKeyboardButton("💬 Support", callback_data="support"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Hey **{user_name}**!\n\n"
        "Join our exclusive private VIP group for insights and community access.\n\n"
        "💰 **Price:** $40.00 USD / £30 GBP (5,200 KES)\n\n"
        "Tap a button below to get started:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def start_pay_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when user clicks Pay. Asks for their email."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "✉️ **Please enter your email address:**\n\n"
        "Paystack needs your email to issue your official receipt.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_EMAIL


async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures email input, validates it, and generates Paystack checkout link."""
    user_email = update.message.text.strip()

    # Simple email validation regex
    email_pattern = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    if not re.match(email_pattern, user_email):
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid email address. Please type a valid email (e.g. `name@example.com`):",
            parse_mode="Markdown",
        )
        return WAITING_FOR_EMAIL

    user = update.effective_user
    reference = f"tg_{user.id}_{uuid.uuid4().hex[:8]}"

    # Inform the user we are processing
    processing_msg = await update.message.reply_text("🔄 Generating your payment link...")

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json",
    }
    payload = {
        "email": user_email,  # Using the real email entered by the customer
        "amount": AMOUNT,
        "currency": "KES",
        "reference": reference,
        "metadata": {
            "telegram_id": user.id,
            "telegram_username": user.username or "",
            "telegram_name": user.full_name,
        },
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers,
        )
        data = res.json()

    if data.get("status"):
        payment_url = data["data"]["authorization_url"]

        keyboard = [
            [InlineKeyboardButton("💳 Complete $40 USD / £30 GBP Checkout", url=payment_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await processing_msg.edit_text(
            f"✅ **Email saved:** `{user_email}`\n\n"
            "Your payment link is ready! Click the button below to complete your payment on Paystack:\n\n"
            "📌 *Note:* Checkout is processed in 5,200 KES (~$40 USD / £30 GBP). "
            "Your bank will automatically convert this to your local currency.",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        print("Paystack error:", data)
        await processing_msg.edit_text(
            "Something went wrong generating your payment link. Please try again later with `/start`."
        )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the conversation state."""
    await update.message.reply_text("Operation canceled. Type `/start` to begin again.")
    return ConversationHandler.END


# Set up the Conversation Handler
pay_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(start_pay_flow, pattern="^pay$")],
    states={
        WAITING_FOR_EMAIL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

# Register Handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(pay_conversation)


# ---------- Webhook Endpoint for Telegram Updates ----------
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}


# ---------- Paystack Webhook with Verification ----------
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    payload = await request.json()

    if payload.get("event") != "charge.success":
        return {"status": "ignored"}

    data = payload.get("data", {})
    reference = data.get("reference")
    metadata = data.get("metadata", {})
    telegram_id = metadata.get("telegram_id")

    if not telegram_id or not reference:
        raise HTTPException(status_code=400, detail="Missing metadata")

    # Double check transaction with Paystack API
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
    async with httpx.AsyncClient() as client:
        verify_res = await client.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=headers,
        )
        verify_data = verify_res.json()

    if not verify_data.get("status") or verify_data.get("data", {}).get("status") != "success":
        print(f"Unverified payment attempt for reference: {reference}")
        return {"status": "unverified"}

    if not GROUP_ID:
        print("Error: GROUP_ID is not configured in Environment Variables.")
        return {"status": "error", "message": "GROUP_ID not set"}

    try:
        invite = await telegram_app.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            member_limit=1,
            name=f"Paid-{telegram_id}",
        )
        invite_link = invite.invite_link
    except Exception as e:
        print("Error creating invite:", e)
        return {"status": "error"}

    try:
        await telegram_app.bot.send_message(
            chat_id=telegram_id,
            text=(
                "🎉 *Payment Successful & Verified!*\n\n"
                "Welcome to the community! Here is your exclusive, single-use invite link:\n\n"
                f"{invite_link}\n\n"
                "Click it above to join now."
            ),
            parse_mode="Markdown",
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