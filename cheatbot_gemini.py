import os
import re
import uuid
import asyncio
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from docx import Document

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters
)

from google import genai


# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY", "").strip()
    or os.environ.get("GOOGLE_API_KEY", "").strip()
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set in environment variables.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

USER_TEXTS: dict[int, str] = {}

MAX_CONTEXT_CHARS = 15000
MAX_FILE_MB = 20

main_keyboard = ReplyKeyboardMarkup(
    [["شروع 📄", "فراموشی 🗑"]],
    resize_keyboard=True
)
# =========================================


# ---------- Utilities ----------
def normalize_persian(text: str) -> str:
    text = text.replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def safe_unique_path(original_name: Optional[str], suffix: str = "") -> Path:
    name = original_name or f"file{suffix}"
    name = re.sub(r"[^a-zA-Z0-9_.\-\u0600-\u06FF]+", "_", name)
    uid = uuid.uuid4().hex[:10]
    return DOWNLOAD_DIR / f"{uid}_{name}"


def file_too_large(size_bytes: int) -> bool:
    return size_bytes > MAX_FILE_MB * 1024 * 1024


# ---------- Extract Text ----------
def extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    text = ""

    if ext == ".pdf":
        doc = fitz.open(path)
        out = []
        for page in doc:
            out.append(page.get_text("text"))
        text = "\n".join(out)

    elif ext in [".png", ".jpg", ".jpeg", ".webp"]:
        img = Image.open(path)
        text = pytesseract.image_to_string(img, lang="fas+eng", config="--psm 6")

    elif ext == ".docx":
        doc = Document(path)
        text = "\n".join(p.text for p in doc.paragraphs)

    elif ext == ".txt":
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()

    return normalize_persian(text)


# ---------- Gemini Call ----------
def gemini_answer(prompt: str) -> str:
    # طبق docs: client.models.generate_content(...).text :contentReference[oaicite:3]{index=3}
    resp = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return (resp.text or "").strip()


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 سلام!\n\n"
        "📄 یک فایل (PDF، Word، عکس یا TXT) ارسال کن\n"
        "❓ بعدش سؤال بپرس تا جواب بدم\n\n"
        "🗑 با «فراموشی» فایل قبلی پاک می‌شه",
        reply_markup=main_keyboard
    )


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = (update.message.text or "").strip()

    if text == "فراموشی 🗑":
        USER_TEXTS.pop(user_id, None)
        await update.message.reply_text("✅ فایل قبلی فراموش شد.\n📄 فایل جدید ارسال کن.")

    elif text == "شروع 📄":
        await update.message.reply_text("📄 لطفاً فایل را ارسال کن.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    doc = update.message.document
    if not doc:
        return

    if doc.file_size and file_too_large(doc.file_size):
        await update.message.reply_text(f"❌ فایل خیلی بزرگه. (حداکثر {MAX_FILE_MB}MB)")
        return

    file_path = safe_unique_path(doc.file_name, suffix=os.path.splitext(doc.file_name or "")[1])
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(str(file_path))

    await update.message.reply_text("⏳ در حال استخراج متن...")

    text = await asyncio.to_thread(extract_text, str(file_path))

    if len(text) < 50:
        await update.message.reply_text("❌ متن قابل‌استفاده‌ای استخراج نشد.")
        return

    USER_TEXTS[user_id] = text
    await update.message.reply_text("✅ متن استخراج شد.\n❓ حالا سؤال خودت رو بپرس.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photos = update.message.photo
    if not photos:
        return

    largest = photos[-1]
    tg_file = await largest.get_file()

    file_path = safe_unique_path("photo.jpg", suffix=".jpg")
    await tg_file.download_to_drive(str(file_path))

    await update.message.reply_text("⏳ در حال OCR عکس...")

    text = await asyncio.to_thread(extract_text, str(file_path))

    if len(text) < 50:
        await update.message.reply_text("❌ متن قابل‌استفاده‌ای از عکس استخراج نشد.")
        return

    USER_TEXTS[user_id] = text
    await update.message.reply_text("✅ متن استخراج شد.\n❓ حالا سؤال خودت رو بپرس.")


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    question = (update.message.text or "").strip()
    if not question:
        return

    # اگر فایل قبلاً داده شده باشد، با متن فایل جواب بده. اگر نه، آزاد جواب بده.
    if user_id in USER_TEXTS:
        context_text = USER_TEXTS[user_id][:MAX_CONTEXT_CHARS]
        prompt = f"""
تو یک دستیار پاسخ‌گویی هستی.
اول متن را در نظر بگیر و بعد به سؤال جواب بده.
اگر پاسخ در متن نبود، صریح بگو:
«این اطلاعات در متن موجود نیست.»

متن:
{context_text}

سؤال:
{question}
""".strip()
    else:
        prompt = f"به این سؤال دقیق و واضح جواب بده:\n{question}"

    await update.message.reply_text("🤖 دارم جواب رو آماده می‌کنم...")

    try:
        answer = await asyncio.to_thread(gemini_answer, prompt)
        if not answer:
            answer = "❌ پاسخی تولید نشد."
        await update.message.reply_text(answer)

    except Exception as e:
        # پیام ساده برای کاربر + چاپ خطا برای دیباگ
        print("Gemini error:", repr(e))
        await update.message.reply_text("❌ خطا در اتصال/پاسخ‌دهی Gemini. لطفاً دوباره امتحان کن.")


# ---------- Main ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(شروع 📄|فراموشی 🗑)$"), handle_buttons))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question))

    print("🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
