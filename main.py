import os
import time
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

# Whop events that mean the customer paid and should receive an invite
FULFILLMENT_EVENTS = {
    "payment.succeeded",
    "payment_succeeded",
    "membership.went_valid",
    "membership.activated",
}

# Prevent duplicate invite links when Whop retries the same webhook
fulfilled_payment_ids: set[str] = set()

# Fallback mapping when checkout-configuration API is unavailable (ref -> telegram_id)
pending_checkouts: dict[str, tuple[str, float]] = {}
PENDING_CHECKOUT_TTL_SECONDS = PAYMENT_TTL_MINUTES * 60

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


# ---------- Whop / Telegram Helpers ----------


def _metadata_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _cleanup_pending_checkouts() -> None:
    cutoff = time.time() - PENDING_CHECKOUT_TTL_SECONDS
    expired = [ref for ref, (_, created_at) in pending_checkouts.items() if created_at < cutoff]
    for ref in expired:
        pending_checkouts.pop(ref, None)


def register_pending_checkout(telegram_id: str) -> str:
    """Store a short-lived ref so fallback checkout links can be matched on webhook."""
    _cleanup_pending_checkouts()
    checkout_ref = uuid.uuid4().hex[:12]
    pending_checkouts[checkout_ref] = (str(telegram_id), time.time())
    return checkout_ref


def resolve_passthrough(passthrough: str | None) -> str | None:
    if not passthrough:
        return None

    passthrough = str(passthrough)
    _cleanup_pending_checkouts()
    pending = pending_checkouts.get(passthrough)
    if pending:
        return pending[0]

    # Direct passthrough may already be the Telegram user ID.
    if passthrough.isdigit():
        return passthrough
    return None


def extract_telegram_id(payload: dict, data: dict) -> str | None:
    """Pull telegram_id from every location Whop may place it."""
    metadata = {}
    for source in (
        data.get("metadata"),
        payload.get("metadata"),
        (data.get("plan") or {}).get("metadata"),
        (data.get("product") or {}).get("metadata"),
        (data.get("membership") or {}).get("metadata"),
    ):
        metadata.update(_metadata_dict(source))

    custom_fields = _metadata_dict(data.get("custom_fields"))

    for candidate in (
        metadata.get("telegram_id"),
        custom_fields.get("telegram_id"),
        resolve_passthrough(data.get("passthrough")),
        resolve_passthrough(payload.get("passthrough")),
        data.get("telegram_id"),
    ):
        if candidate:
            return str(candidate)
    return None


async def create_whop_checkout_session(telegram_id: str) -> tuple[str | None, str | None]:
    """Create a Whop checkout session with metadata. Returns (payment_url, error_message)."""
    headers = {
        "Authorization": f"Bearer {WHOP_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "plan_id": WHOP_PLAN_ID,
        "mode": "payment",
        "metadata": {"telegram_id": str(telegram_id)},
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.whop.com/api/v1/checkout_configurations",
                json=body,
                headers=headers,
                timeout=10.0,
            )
    except Exception as e:
        print(f"Exception creating Whop checkout session: {e}")
        return None, str(e)

    if resp.status_code in (200, 201):
        session_data = resp.json()
        config_id = session_data.get("id")
        payment_url = session_data.get("purchase_url") or session_data.get("url")
        if not payment_url and config_id:
            payment_url = (
                f"https://whop.com/checkout/{WHOP_PLAN_ID}?session={config_id}"
            )
        print(f"Whop session link created: {payment_url}")
        return payment_url, None

    error_text = resp.text
    print(f"Whop API Error ({resp.status_code}): {error_text}")

    if resp.status_code == 400 and "permission" in error_text.lower():
        print(
            "FIX: Use a Company API key from the same Whop company that owns "
            "WHOP_PLAN_ID, with checkout_configuration:create enabled. "
            "Dashboard -> Developer -> Company API keys -> Create key."
        )
    return None, error_text


async def fetch_telegram_id_from_checkout_config(config_id: str) -> str | None:
    """Fallback: read metadata from the checkout session Whop created at pay time."""
    headers = {"Authorization": f"Bearer {WHOP_API_KEY}"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.whop.com/api/v1/checkout_configurations/{config_id}",
                headers=headers,
                timeout=10.0,
            )
        if resp.status_code == 200:
            return extract_telegram_id({}, resp.json())
        print(
            f"Could not fetch checkout config {config_id}: "
            f"{resp.status_code} {resp.text}"
        )
    except Exception as e:
        print(f"Exception fetching checkout config {config_id}: {e}")
    return None


async def deliver_invite_link(telegram_id: str, fulfillment_id: str) -> dict:
    """Create a single-use invite link and DM it to the paying customer."""
    if fulfillment_id in fulfilled_payment_ids:
        print(f"Already fulfilled {fulfillment_id}, skipping duplicate webhook")
        return {"status": "already_fulfilled"}

    if not GROUP_ID:
        print("ERROR: GROUP_ID is not configured in Environment Variables.")
        return {"status": "error", "message": "GROUP_ID not set"}

    try:
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
        support_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Link not working?", callback_data="support")]]
        )
        # Plain text avoids Markdown parse errors on invite link characters
        await telegram_app.bot.send_message(
            chat_id=int(telegram_id),
            text=(
                "🎉 Payment Successful & Verified!\n\n"
                "Welcome to the community! Here is your exclusive, single-use invite link:\n\n"
                f"{invite_link}\n\n"
                "Tap the link above to join now."
            ),
            reply_markup=support_keyboard,
        )
        fulfilled_payment_ids.add(fulfillment_id)
        print(f"SUCCESS: DM delivered to user {telegram_id}")
    except Exception as e:
        print(f"ERROR sending DM to user {telegram_id}: {e}")
        return {"status": "dm_failed", "detail": str(e)}

    return {"status": "success"}


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
    telegram_id = str(user.id)

    payment_url, whop_error = await create_whop_checkout_session(telegram_id)
    using_fallback = False

    if not payment_url:
        using_fallback = True
        checkout_ref = register_pending_checkout(telegram_id)
        print(f"Using direct link fallback with ref {checkout_ref}...")
        payment_url = (
            f"https://whop.com/checkout/{WHOP_PLAN_ID}?"
            f"passthrough={checkout_ref}"
        )

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
    if using_fallback and whop_error:
        message_text += (
            "\n\n_Note: checkout session could not be created via API. "
            "If you pay and do not receive an invite, contact support._"
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


# Register button router handler
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_command))
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
        payload.get("type")
        or payload.get("action")
        or payload.get("event")
        or payload.get("event_type")
    )
    data = payload.get("data", payload)

    print(f"Detected Event Type: {event_type}")

    if event_type and event_type not in FULFILLMENT_EVENTS:
        print(f"Ignored unsupported event type: {event_type}")
        return {"status": "ignored", "reason": f"unsupported event: {event_type}"}

    if event_type in {"payment.succeeded", "payment_succeeded"}:
        payment_status = data.get("status")
        if payment_status and payment_status != "succeeded":
            print(f"Ignored payment event with status: {payment_status}")
            return {
                "status": "ignored",
                "reason": f"payment status: {payment_status}",
            }

    telegram_id = extract_telegram_id(payload, data)

    if not telegram_id:
        checkout_config_id = data.get("checkout_configuration_id")
        if checkout_config_id:
            print(
                f"No telegram_id in webhook; fetching checkout config {checkout_config_id}"
            )
            telegram_id = await fetch_telegram_id_from_checkout_config(
                checkout_config_id
            )

    if not telegram_id:
        print("ERROR: Whop payload received without telegram_id!")
        print(f"Event: {event_type}, data keys: {list(data.keys())}")
        return {"status": "missing_telegram_id"}

    fulfillment_id = data.get("id") or payload.get("id") or f"{event_type}:{telegram_id}"
    return await deliver_invite_link(telegram_id, fulfillment_id)


# ---------- App Lifecycle ----------
@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()