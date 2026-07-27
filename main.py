import os
import uuid
import datetime
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")

raw_group_id = os.getenv("GROUP_ID")
GROUP_ID = int(raw_group_id) if raw_group_id else None

AMOUNT = 520000  # 5,200 KES in subunits (approx $40 USD / £30 GBP)
PAYMENT_TTL_MINUTES = 10  # 10 minute expiration period

app = FastAPI()

# Build Telegram app with JobQueue enabled
telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .read_timeout(10)
    .write_timeout(10)
    .connect_timeout(10)
    .pool_timeout(10)
    .build()
)


# ---------- Expiration Job Handler ----------

async def expire_payment_job(context: ContextTypes.DEFAULT_TYPE):
    """Job triggered after 10 minutes to revoke the payment button in chat."""
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    message_id = job_data["message_id"]

    try:
        keyboard = [[InlineKeyboardButton("🔄 Generate New Payment Link", callback_data="pay")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "⏱️ **Payment Link Expired**\n\n"
                "Your 10-minute checkout session has ended to keep your account secure.\n\n"
                "Tap the button below to generate a new payment link when you're ready:"
            ),
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"Could not update expired message: {e}")


# ---------- Support Helper Function ----------

async def send_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reusable function for Support via command or button click."""
    text = (
        "💬 **Contact Support**\n\n"
        "If you need any help, have questions, or need assistance with your group access, "
        "please reach out to our team. We will get back to you as soon as possible!\n\n"
        "👉 Message directly: @pewee7"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_start")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )


# ---------- Telegram Command Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main welcome message with direct Checkout and Support options."""
    keyboard = [
        [InlineKeyboardButton("💳 Pay $40.00 USD / £30 GBP", callback_data="pay")],
        [InlineKeyboardButton("💬 Support / Help", callback_data="support")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_name = update.effective_user.first_name
    text = (
        f"👋 Hey **{user_name}**!\n\n"
        "Join our exclusive private VIP group for insights and community access.\n\n"
        "💰 **Price:** $40.00 USD / £30 GBP (5,200 KES)\n\n"
        "Tap below to proceed:"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers the support flow when /help is typed."""
    await send_support_message(update, context)


async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates a 10-minute time-limited Paystack checkout link."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    reference = f"tg_{user.id}_{uuid.uuid4().hex[:8]}"
    init_email = f"user_{user.id}@telegram.com"

    # Set Paystack link expiration timestamp (10 minutes from now)
    expire_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=PAYMENT_TTL_MINUTES)

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json",
    }
    payload = {
        "email": init_email,
        "amount": AMOUNT,
        "currency": "KES",
        "reference": reference,
        "expire_after": expire_at.isoformat(),  # Paystack automated expiration
        "metadata": {
            "telegram_id": user.id,
            "telegram_username": user.username or "",
            "telegram_name": user.full_name,
        },
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers,
        )
        data = res.json()

    if data.get("status"):
        payment_url = data["data"]["authorization_url"]

        keyboard = [
            [InlineKeyboardButton("💳 Complete Checkout on Paystack", url=payment_url)],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message_text = (
            "⚡️ **Payment Link Ready!**\n\n"
            f"⏰ **Time Limit:** This link will expire in **{PAYMENT_TTL_MINUTES} minutes**.\n\n"
            "You will be asked to enter your email on the checkout page for your official receipt.\n\n"
            "📌 *Note:* Checkout is processed in 5,200 KES (~$40 USD / £30 GBP). "
            "Your bank converts this automatically."
        )

        sent_msg = await query.edit_message_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )

        # Schedule the expiration job in Telegram's JobQueue (600 seconds)
        context.job_queue.run_once(
            expire_payment_job,
            when=PAYMENT_TTL_MINUTES * 60,
            data={
                "chat_id": sent_msg.chat_id,
                "message_id": sent_msg.message_id,
            },
            name=f"expire_{reference}"
        )
    else:
        print("Paystack error:", data)
        await query.edit_message_text(
            "Something went wrong generating your payment link. Please try again in a moment.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_start")]]),
        )


async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router for navigation buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "support":
        await send_support_message(update, context)
    elif data == "back_start":
        await start(update, context)


# Register Handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(CommandHandler("support", help_command))

telegram_app.add_handler(CallbackQueryHandler(pay_callback, pattern="^pay$"))
telegram_app.add_handler(CallbackQueryHandler(button_router, pattern="^(support|back_start)$"))


# ---------- Webhook Endpoint for Telegram Updates ----------
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}


# ---------- Paystack Webhook with Direct API Verification ----------
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

    # Double check transaction with Paystack API before issuing link
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
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