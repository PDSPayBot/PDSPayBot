import os
import uuid
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import asyncio

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY")
GROUP_ID = int(os.getenv("GROUP_ID"))
AMOUNT = 520000  # 5,200 KES in cents

app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Pay 5,200 KES", callback_data="pay")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome!\n\nClick the button below to pay **5,200 KES** and get instant access to the group.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    reference = f"tg_{user.id}_{uuid.uuid4().hex[:8]}"

    # Create Paystack transaction
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json"
    }
    payload = {
        "email": f"{user.id}@telegram.user",  # temporary email
        "amount": amount,
        "currency": "KES",
        "reference": reference,
        "callback_url": "https://t.me/",  # optional
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
        await query.edit_message_text(
            f"Click the link below to complete payment of **5,200 KES**:\n\n{payment_url}\n\n"
            "After payment you will automatically receive the group invite link.",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("Something went wrong. Please try again later.")

# ---------- Paystack Webhook ----------
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):
    payload = await request.json()

    # Basic verification (you can add signature check later)
    if payload.get("event") != "charge.success":
        return {"status": "ignored"}

    data = payload.get("data", {})
    reference = data.get("reference", "")
    metadata = data.get("metadata", {})
    telegram_id = metadata.get("telegram_id")

    if not telegram_id:
        raise HTTPException(status_code=400, detail="No telegram_id")

    # Create one-time invite link
    try:
        invite = await telegram_app.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            member_limit=1,          # only 1 use
            name=f"Paid-{telegram_id}"
        )
        invite_link = invite.invite_link
    except Exception as e:
        print("Error creating invite:", e)
        return {"status": "error"}

    # Send the invite link to the user
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
        print("Error sending message:", e)

    return {"status": "success"}

# ---------- Startup ----------
async def main():
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CallbackQueryHandler(pay_callback, pattern="^pay$"))

    # Start Telegram bot in background (polling)
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

    # Start FastAPI
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    import uvicorn
    asyncio.run(main())