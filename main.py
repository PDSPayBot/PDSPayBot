import os
import uuid
from dotenv import load_dotenv
from fastapi import FastAPI, Request
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WHOP_API_KEY = os.getenv("WHOP_API_KEY")
WHOP_PLAN_ID = os.getenv("WHOP_PLAN_ID")  # e.g., plan_lKZLzJB0ZE0tk

raw_group_id = os.getenv("GROUP_ID")
GROUP_ID = int(raw_group_id) if raw_group_id else None

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
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔄 Generate New Payment Link", callback_data="pay"
                )
            ]
        ]
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


async def send_support_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Reusable function for Support via command or button click."""
    text = (
        "💬 **Contact Support**\n\n"
        "If you need any help, have questions, or need assistance with your group access, "
        "please reach out to our team. We will get back to you as soon as possible!\n\n"
        "👉 Message directly: @pewee7"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "⬅️ Back to Main Menu", callback_data="back_start"
            )
        ]
    ]
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
    user = update.effective_user
    username_str = f"@{user.username}" if user.username else "No Username"
    print(
        f"👤 USER STARTED BOT: {user.first_name} | Username: {username_str} | ID: {user.id}"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "💳 Pay $40.00 USD / £30 GBP", callback_data="pay"
            )
        ],
        [InlineKeyboardButton("💬 Support / Help", callback_data="support")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_name = user.first_name
    text = (
        f"👋 Hey **{user_name}**!\n\n"
        "Join our exclusive private VIP group for insights and community access.\n\n"
        "💰 **Price:** $40.00 USD / £30 GBP\n\n"
        "Tap below to proceed:"
    )

    if update.message:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers the support flow when /help is typed."""
    await send_support_message(update, context)


async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates a dynamic Whop Checkout Session with bound metadata."""
    query = update.callback_query
    await query.answer()

    user = query.from_user

    # Generate an API Checkout Session directly with Whop
    headers = {
        "Authorization": f"Bearer {WHOP_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "plan_id": WHOP_PLAN_ID,
        "metadata": {"telegram_id": str(user.id)},
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.whop.com/v5/checkout_sessions",
                json=body,
                headers=headers,
                timeout=10.0,
            )

        if resp.status_code in [200, 201]:
            session_data = resp.json()
            payment_url = session_data.get("url") or session_data.get(
                "checkout_url"
            )
        else:
            print(
                f"Whop API Error ({resp.status_code}): {resp.text}. Falling back to standard link."
            )
            payment_url = f"https://whop.com/checkout/{WHOP_PLAN_ID}"
    except Exception as e:
        print(f"Exception creating Whop checkout session: {e}")
        payment_url = f"https://whop.com/checkout/{WHOP_PLAN_ID}"

    keyboard = [
        [
            InlineKeyboardButton(
                "💳 Complete Checkout on Whop", url=payment_url
            )
        ],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_start")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = (
        "⚡️ **Payment Link Ready!**\n\n"
        f"⏰ **Time Limit:** This link will expire in **{PAYMENT_TTL_MINUTES} minutes**.\n\n"
        "Tap the checkout button below to complete your payment on Whop securely."
    )

    sent_msg = await query.edit_message_text(
        message_text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

    # Schedule the 10-minute message expiration timer in Telegram's JobQueue
    ref_id = uuid.uuid4().hex[:8]
    context.job_queue.run_once(
        expire_payment_job,
        when=PAYMENT_TTL_MINUTES * 60,
        data={
            "chat_id": sent_msg.chat_id,
            "message_id": sent_msg.message_id,
        },
        name=f"expire_{ref_id}",
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
telegram_app.add_handler(
    CallbackQueryHandler(button_router, pattern="^(support|back_start)$")
)


# ---------- Root Route (For Scanners/Health Checks) ----------
@app.get("/")
@app.post("/")
async def root_health_check():
    return {"status": "bot is running online"}


# ---------- Webhook Endpoint for Telegram Updates ----------
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}


# ---------- Whop Webhook Handler ----------
@app.post("/whop-webhook")
async def whop_webhook(request: Request):
    payload = await request.json()
    print("--- INCOMING WHOP WEBHOOK ---")
    print(f"Payload: {payload}")

    event_type = (
        payload.get("action")
        or payload.get("event")
        or payload.get("type")
        or payload.get("event_type")
    )
    data = payload.get("data", payload)

    print(f"Detected Event Type: {event_type}")

    valid_events = [
        "payment.succeeded",
        "payment_succeeded",
        "membership.went_valid",
        "membership.activated",
        "payment.created",
    ]

    if event_type and event_type not in valid_events:
        print(f"Ignored unsupported event type: {event_type}")
        return {"status": "ignored", "reason": f"unsupported event: {event_type}"}

    # Extract metadata objects across payload levels
    metadata = (
        data.get("metadata", {})
        or payload.get("metadata", {})
        or (data.get("plan", {}) or {}).get("metadata", {})
    )
    custom_fields = data.get("custom_fields", {}) or {}

    telegram_id = (
        metadata.get("telegram_id")
        or custom_fields.get("telegram_id")
        or data.get("passthrough")
        or payload.get("passthrough")
        or data.get("telegram_id")
    )

    if not telegram_id:
        print("ERROR: Whop payload received without telegram_id!")
        return {"status": "missing_telegram_id"}

    if not GROUP_ID:
        print("ERROR: GROUP_ID is not configured in Environment Variables.")
        return {"status": "error", "message": "GROUP_ID not set"}

    try:
        # Create 1-time single use invite link
        invite = await telegram_app.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            member_limit=1,
            name=f"Paid-{telegram_id}",
        )
        invite_link = invite.invite_link
        print(f"Generated Invite Link: {invite_link}")
    except Exception as e:
        print(f"ERROR creating invite link in Telegram: {e}")
        return {"status": "telegram_api_error", "detail": str(e)}

    try:
        await telegram_app.bot.send_message(
            chat_id=int(telegram_id),
            text=(
                "🎉 *Payment Successful & Verified!*\n\n"
                "Welcome to the community! Here is your exclusive, single-use invite link:\n\n"
                f"{invite_link}\n\n"
                "Click it above to join now."
            ),
            parse_mode="Markdown",
        )
        print(f"SUCCESS: DM delivered to user {telegram_id}")
    except Exception as e:
        print(f"ERROR sending DM to user {telegram_id}: {e}")
        return {"status": "dm_failed", "detail": str(e)}

    return {"status": "success"}


# ---------- Legacy Dummy Route for Paystack Webhooks ----------
@app.post("/paystack-webhook")
async def paystack_webhook_dummy():
    """Returns 200 OK to stop leftover Paystack retry attempts from cluttering Render logs."""
    return {"status": "ignored"}


# ---------- App Lifecycle ----------
@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()