import os
import re
import json
import time
import signal
import shutil
import asyncio
import logging
import html
import subprocess
import sys
import venv
import hashlib

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = "8794670279:AAHtOmyRRw-NJfwEwE3C8EZ2vLIW15CF4k8"

ADMIN_ID = 7529369260

SUPPORT_USERNAME = "@sujoy_012"

BASE_DIR = "hosting_data"
USERS_DIR = os.path.join(BASE_DIR, "users")
BOTS_DIR = os.path.join(BASE_DIR, "bots")

USERS_FILE = "users.json"
CHANNELS_FILE = "channels.json"
BOTS_FILE = "bots.json"
FILES_FILE = "files.json"
HOST_LIMITS_FILE = "host_limits.json"
BANNED_USERS_FILE = "banned_users.json"

# Multi-language/container hosting.  When Docker is available, hosted bots
# run inside their own container; otherwise the existing local runtime remains
# available as a compatibility fallback.
USE_DOCKER_IF_AVAILABLE = True
DOCKER_BIN = os.environ.get("DOCKER_BIN", "docker")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "bridge")
DOCKER_MEMORY = os.environ.get("DOCKER_MEMORY", "512m")
DOCKER_CPUS = os.environ.get("DOCKER_CPUS", "1.0")
DOCKER_TIMEOUT = int(os.environ.get("DOCKER_BUILD_TIMEOUT", "1800"))

MAINTENANCE_FILE = os.path.join(BASE_DIR, "maintenance.json")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
META_AI_API_KEY = os.environ.get("META_AI_API_KEY", "nxt_3a454c41e6a84aeead28d1fb4aec87a4").strip()
META_AI_GEM_ID = "ba0fbe0d-976e-493a-afdb-6d8469e53df0"
META_AI_ENDPOINT = f"https://nxtai.zipohostbd.workers.dev/api/use?gem={META_AI_GEM_ID}"
TS_AI_API_KEY = os.environ.get("TS_AI_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
POLLINATIONS_API_KEY = os.environ.get("POLLINATIONS_API_KEY", "").strip()
TS_AI_BASE_URL = os.environ.get("TS_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
TS_AI_MODEL = os.environ.get("TS_AI_MODEL", "gpt-5.6-mini").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
POLLINATIONS_MODEL = os.environ.get("POLLINATIONS_MODEL", "google/gemini-3.8-flash").strip()
TS_AI_HISTORY_LIMIT = 12

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(USERS_DIR, exist_ok=True)
os.makedirs(BOTS_DIR, exist_ok=True)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def load_json(filename, default):
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error("JSON load error %s: %s", filename, e)

    return default


def save_json(filename, data):
    temp = filename + ".tmp"

    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(temp, filename)

    except Exception as e:
        logger.error("JSON save error %s: %s", filename, e)


users = load_json(USERS_FILE, {})
channels = load_json(CHANNELS_FILE, [])
bots = load_json(BOTS_FILE, {})
uploaded_files = load_json(FILES_FILE, {})
host_limits = load_json(HOST_LIMITS_FILE, {})
banned_users = load_json(BANNED_USERS_FILE, {})
maintenance_state = load_json(MAINTENANCE_FILE, {"enabled": False})
TS_AI_SESSIONS = {}
# Active TS AI jobs per user. Menu taps can cancel an in-progress AI job
# so the bot remains responsive instead of waiting for a long AI generation.
TS_AI_TASKS = {}
LANGUAGE_DEFAULT = "en"


# Running processes
RUNNING_PROCESSES = {}
# Bot lifecycle controls: prevents a STOP action from being undone by a
# still-running startup/auto-repair task.
STARTING_BOTS = set()
STOP_REQUESTED = set()


# =========================================================
# KEYBOARDS
# =========================================================



def get_user_language(user_id):
    info = users.get(str(user_id), {})
    lang = info.get("language", LANGUAGE_DEFAULT)
    return lang if lang in ("en", "bn") else LANGUAGE_DEFAULT


def set_user_language(user_id, lang):
    lang = lang if lang in ("en", "bn") else LANGUAGE_DEFAULT
    uid = str(user_id)
    old = users.get(uid, {"id": user_id})
    users[uid] = {**old, "language": lang}
    save_json(USERS_FILE, users)
    return lang


def _styled_reply_keyboard(keyboard):
    """Apply Telegram native button colors requested for the user panel."""
    danger_words = (
        "Ban", "Unban", "Remove", "Delete", "DELETE", "STOP",
        "🗑", "🚫", "⏹", "♻️"
    )
    success_words = (
        "📤 ফাইল আপলোড করুন", "📁 ফাইল দেখুন", "👤 আমার প্রোফাইল",
        "📤 Upload File", "📁 My Files", "👤 My Profile",
        "👑 প্রিমিয়াম প্লান 🌟", "👑 Premium Plan 🌟",
        "🛠️ সাপোর্ট", "🛠️ Support",
    )
    rows = []
    for row in keyboard:
        styled_row = []
        for label in row:
            if any(word in label for word in danger_words):
                style = "danger"
            elif label in success_words:
                style = "success"
            else:
                style = "primary"
            styled_row.append(KeyboardButton(text=label, style=style))
        rows.append(styled_row)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def user_keyboard(lang="en"):
    if lang == "bn":
        keyboard = [
            ["📢 আপডেট চ্যানেল", "📤 ফাইল আপলোড করুন"],
            ["📁 ফাইল দেখুন", "🤖 আমার বট"],
            ["💰 ডিপোজিট", "👤 আমার প্রোফাইল"],
            ["👑 প্রিমিয়াম প্লান 🌟", "🤖 TS Ai"],
            ["🌐 Language", "🛠️ সাপোর্ট"],
        ]
    else:
        keyboard = [
            ["📢 Update Channel", "📤 Upload File"],
            ["📁 My Files", "🤖 My Bots"],
            ["💰 Deposit", "👤 My Profile"],
            ["👑 Premium Plan 🌟", "🤖 TS Ai"],
            ["🌐 Language", "🛠️ Support"],
        ]
    return _styled_reply_keyboard(keyboard)

def admin_keyboard():

    keyboard = [
        ["📊 Statistics", "👥 User List"],
        ["🤖 All Bots", "📁 All Files"],
        ["📢 Broadcast", "📢 Manage Channels"],
        ["👤 User Host Permission", "🚫 User Ban/Unban"],
        ["🛠 Maintain"],
        ["🔙 User Panel"],
    ]

    return _styled_reply_keyboard(keyboard)


def channel_keyboard():

    keyboard = [
        ["➕ Add Channel"],
        ["🗑 Remove Channel"],
        ["📋 Channel List"],
        ["🔙 Admin Panel"],
    ]

    return _styled_reply_keyboard(keyboard)


# =========================================================
# HELPERS
# =========================================================

def bot_folder(bot_id):

    return os.path.join(
        BOTS_DIR,
        str(bot_id)
    )


def bot_log_file(bot_id):

    return os.path.join(
        bot_folder(bot_id),
        "logs.txt"
    )


def bot_main_file(bot_id):

    # Backward-compatible path for old hosted bots.
    return os.path.join(
        bot_folder(bot_id),
        "main.py"
    )


def bot_entry_file(bot_id):

    data = bots.get(str(bot_id), {})
    filename = data.get("main_file") or data.get("entry_file") or "main.py"

    return os.path.join(
        bot_folder(bot_id),
        filename
    )


def bot_requirements_file(bot_id):

    return os.path.join(
        bot_folder(bot_id),
        "requirements.txt"
    )




def is_admin(user_id):

    return user_id == ADMIN_ID


def touch_user(user):

    """Register/update user activity without changing the existing UI."""
    uid = str(user.id)
    old = users.get(uid, {})

    users[uid] = {
        **old,
        "id": user.id,
        "name": user.full_name,
        "username": user.username or "",
        "last_seen": int(time.time()),
    }

    save_json(USERS_FILE, users)
    return uid


def get_user_bots(user_id):

    result = []

    for bot_id, data in bots.items():

        if data.get("owner_id") == user_id:
            result.append((bot_id, data))

    return result


def get_user_host_limit(user_id):
    """Default host permission is always 1 unless admin grants a custom limit."""
    try:
        value = int(host_limits.get(str(user_id), 1))
        return max(1, value)
    except (TypeError, ValueError):
        return 1


def set_user_host_limit(user_id, limit):
    host_limits[str(user_id)] = max(1, int(limit))
    save_json(HOST_LIMITS_FILE, host_limits)


def is_banned(user_id):
    return str(user_id) in banned_users


def set_user_banned(user_id, banned=True):
    uid = str(user_id)
    if banned:
        banned_users[uid] = {"banned_at": int(time.time())}
    else:
        banned_users.pop(uid, None)
    save_json(BANNED_USERS_FILE, banned_users)


def permission_list_text():
    if not host_limits:
        return "📋 <b>HOST PERMISSION LIST</b>\n\nকোনো Custom Host Permission দেওয়া হয়নি।\n\nDefault: <b>1টি Bot</b>"
    lines = ["📋 <b>HOST PERMISSION LIST</b>", ""]
    for uid, limit in host_limits.items():
        info = users.get(str(uid), {})
        name = html.escape(info.get("name") or "Unknown")
        username = info.get("username") or ""
        user_line = f"@{html.escape(username)}" if username else "Username নেই"
        lines.append(f"👤 <b>{name}</b>\n🆔 <code>{uid}</code>\n🔗 {user_line}\n🤖 Permission: <b>{limit}টি Bot</b>\n")
    return "\n".join(lines)


def is_process_running(bot_id):

    process = RUNNING_PROCESSES.get(bot_id)

    if not process:
        return False

    if process.poll() is None:
        return True

    return False


async def stop_all_user_bots(user_id):
    """Immediately stop every hosted bot owned by a banned user."""
    owned = [bid for bid, data in bots.items() if str(data.get("owner_id")) == str(user_id)]
    results = []
    for bot_id in owned:
        results.append(await stop_hosted_bot(bot_id, force=True))
    return results


def set_bot_status(bot_id, status):

    if bot_id in bots:
        bots[bot_id]["status"] = status
        bots[bot_id]["updated"] = int(time.time())
        save_json(BOTS_FILE, bots)


# =========================================================
# MAINTENANCE MODE + TS AI
# =========================================================

def is_maintenance():
    return bool(maintenance_state.get("enabled", False))

def set_maintenance(enabled):
    maintenance_state["enabled"] = bool(enabled)
    maintenance_state["updated"] = int(time.time())
    save_json(MAINTENANCE_FILE, maintenance_state)

def _ts_ai_provider():
    if META_AI_API_KEY:
        return "meta"
    if GEMINI_API_KEY:
        return "gemini"
    if TS_AI_API_KEY or OPENAI_API_KEY:
        return "openai"
    if POLLINATIONS_API_KEY:
        return "pollinations"
    return None

async def _http_json_post(url, payload, headers=None, timeout=120):
    import urllib.request
    import urllib.error
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    def _do():
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
    return await asyncio.to_thread(_do)

async def _ts_ai_meta(history):
    prompt = ""
    for item in reversed(history):
        if item.get("role") == "user":
            prompt = str(item.get("content", ""))
            break
    if not prompt:
        raise RuntimeError("Meta AI-তে পাঠানোর জন্য কোনো প্রশ্ন পাওয়া যায়নি।")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
        "Origin": "https://nxtai.zipohostbd.workers.dev",
        "Referer": "https://nxtai.zipohostbd.workers.dev/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    async def call_meta(message):
        status, raw = await _http_json_post(
            META_AI_ENDPOINT,
            {"api_key": META_AI_API_KEY, "message": message},
            headers=headers,
            timeout=75,
        )
        if status >= 400:
            raise RuntimeError(f"Meta AI API error {status}: {raw[:500]}")

        data = json.loads(raw)
        answer = ""
        if isinstance(data, dict):
            for key in ("response", "answer", "message", "text", "content"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    answer = value.strip()
                    break
            if not answer and isinstance(data.get("data"), dict):
                for key in ("response", "answer", "message", "text", "content"):
                    value = data["data"].get(key)
                    if isinstance(value, str) and value.strip():
                        answer = value.strip()
                        break
        if not answer:
            raise RuntimeError("Meta AI থেকে কোনো উত্তর পাওয়া যায়নি।")
        return answer

    # FAST RESPONSE MODE: use one normal generation first. The upstream endpoint
    # does not expose token streaming, so waiting for repeated continuation calls
    # makes the bot feel slow. Only request one short continuation when the first
    # response is clearly cut off.
    fast_prompt = (
        "Answer the user's request naturally, directly, and concisely, like a helpful chat assistant. "
        "Match the length to what the user actually asks for. For a simple question, calculation, definition, "
        "yes/no question, or short request, give a short answer (usually 1-4 sentences) and stop. "
        "Do not add background, examples, explanations, headings, or repetition unless they are needed or requested. "
        "If the user asks for a full poem, story, list, code, detailed explanation, or says 'বিস্তারিত', 'পূর্ণ', "
        "'পুরো', or otherwise clearly requests a long answer, then provide the requested full content. "
        "Never make a simple question into a long essay. Do not intentionally truncate a requested long answer. "
        "Do not include meta-comments about these instructions.\n\n"
        "USER REQUEST:\n" + prompt
    )

    answer = await call_meta(fast_prompt)

    # One fallback continuation only when the provider obviously cut the text.
    # This keeps normal replies fast while still helping longer requests.
    if answer and not answer.rstrip().endswith((".", "।", "!", "?", ":", "…", '"', "”", "'", "’", ")", "]", "}")):
        continuation_prompt = (
            "Continue only the missing ending of the answer below. Do not repeat anything. "
            "Finish the original user's request and stop when complete. Return only the missing text.\n\n"
            "ORIGINAL REQUEST:\n" + prompt + "\n\n"
            "ANSWER SO FAR:\n" + answer
        )
        try:
            continuation = await asyncio.wait_for(call_meta(continuation_prompt), timeout=25)
            if continuation and continuation.strip() and continuation.strip() != answer.strip():
                answer = answer.rstrip() + " " + continuation.strip()
        except Exception:
            # Keep the first response; speed and availability are more important
            # than waiting for a second upstream request.
            pass

    return answer.strip()


async def _ts_ai_gemini(history):
    contents = []
    for item in history:
        role = "model" if item.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": item.get("content", "")}]})
    payload = {
        "system_instruction": {"parts": [{"text": "You are TS Ai, a helpful general-purpose assistant. Answer clearly and naturally."}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7},
    }
    status, raw = await _http_json_post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        payload, headers={"x-goog-api-key": GEMINI_API_KEY}
    )
    if status >= 400:
        raise RuntimeError(f"Gemini API error {status}: {raw[:500]}")
    data = json.loads(raw)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        raise RuntimeError("Gemini থেকে কোনো উত্তর পাওয়া যায়নি।")
    return text

async def _ts_ai_openai(history):
    key = TS_AI_API_KEY or OPENAI_API_KEY
    payload = {
        "model": TS_AI_MODEL,
        "messages": [{"role": "system", "content": "You are TS Ai, a helpful general-purpose assistant. Answer clearly and naturally."}, *history],
        "temperature": 0.7,
    }
    status, raw = await _http_json_post(
        f"{TS_AI_BASE_URL}/chat/completions", payload, headers={"Authorization": f"Bearer {key}"}
    )
    if status >= 400:
        raise RuntimeError(f"AI API error {status}: {raw[:500]}")
    data = json.loads(raw)
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(text, list):
        text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("AI থেকে কোনো উত্তর পাওয়া যায়নি।")
    return text

async def _ts_ai_pollinations(history):
    payload = {
        "model": POLLINATIONS_MODEL,
        "messages": [{"role": "system", "content": "You are TS Ai, a helpful general-purpose assistant. Answer clearly and naturally."}, *history],
    }
    status, raw = await _http_json_post(
        "https://gen.pollinations.ai/v1/chat/completions", payload,
        headers={"Authorization": f"Bearer {POLLINATIONS_API_KEY}"}
    )
    if status >= 400:
        raise RuntimeError(f"TS Ai API error {status}: {raw[:500]}")
    data = json.loads(raw)
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("AI থেকে কোনো উত্তর পাওয়া যায়নি।")
    return text

async def _ts_ai_answer(user_id, prompt):
    provider = _ts_ai_provider()
    if not provider:
        return ("⚠️ TS Ai Configure করা হয়নি।\n\n"
                "GEMINI_API_KEY, TS_AI_API_KEY/OPENAI_API_KEY অথবা "
                "POLLINATIONS_API_KEY-এর যেকোনো একটি সেট করুন।")
    history = TS_AI_SESSIONS.setdefault(str(user_id), [])
    history.append({"role": "user", "content": prompt})
    history[:] = history[-TS_AI_HISTORY_LIMIT:]
    try:
        if provider == "meta":
            answer = await _ts_ai_meta(history)
        elif provider == "gemini":
            answer = await _ts_ai_gemini(history)
        elif provider == "openai":
            answer = await _ts_ai_openai(history)
        else:
            answer = await _ts_ai_pollinations(history)
    except Exception as exc:
        logger.error("TS AI error: %s", exc)
        if history and history[-1].get("role") == "user":
            history.pop()
        return "❌ TS Ai উত্তর দিতে পারেনি।\n\n" + str(exc)[:700]
    history.append({"role": "assistant", "content": answer})
    history[:] = history[-TS_AI_HISTORY_LIMIT:]
    return answer

async def _send_long_text(message, text):
    text = str(text or "")
    if not text:
        await message.reply_text("❌ কোনো উত্তর পাওয়া যায়নি।")
        return
    for i in range(0, len(text), 3900):
        await message.reply_text(text[i:i + 3900])

async def ts_ai_start(update, context):
    if is_maintenance() and not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "🛠 <b>MAINTENANCE MODE</b>\n\nTS Ai সহ Bot Service এখন সাময়িকভাবে বন্ধ আছে।",
            parse_mode="HTML"
        )
        return
    context.user_data["ts_ai_chat"] = True
    lang = get_user_language(update.effective_user.id)
    if lang == "bn":
        prompt = ("🤖 <b>TS Ai</b> চালু হয়েছে।\n\n"
                  "আপনার প্রশ্ন লিখুন—সাধারণ প্রশ্ন, পড়াশোনা, coding, idea, explanation ইত্যাদি জিজ্ঞেস করতে পারবেন।\n\n"
                  "💬 এখন আপনার প্রশ্ন পাঠান।")
        new_label = "🧹 নতুন চ্যাট"
    else:
        prompt = ("🤖 <b>TS Ai</b> is ready.\n\n"
                  "Ask anything—general questions, study, coding, ideas, explanations and more.\n\n"
                  "💬 Send your question now.")
        new_label = "🧹 New Chat"
    await update.message.reply_text(
        prompt, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(new_label, callback_data="tsai_new")]])
    )

async def _ts_ai_reply_worker(message, user_id, prompt):
    """Run AI generation in the background so menu buttons stay responsive."""
    try:
        answer = await _ts_ai_answer(user_id, prompt)
        await _send_long_text(message, answer)
    except asyncio.CancelledError:
        # A menu action or a newer AI request cancelled this job.
        raise
    except Exception as exc:
        logger.error("TS AI background error: %s", exc)
        try:
            await message.reply_text("❌ TS Ai উত্তর দিতে পারেনি।\n\n" + str(exc)[:700])
        except Exception:
            pass
    finally:
        current = TS_AI_TASKS.get(str(user_id))
        if current is asyncio.current_task():
            TS_AI_TASKS.pop(str(user_id), None)


def _cancel_ts_ai_task(user_id):
    task = TS_AI_TASKS.pop(str(user_id), None)
    if task and not task.done():
        task.cancel()


async def ts_ai_message(update, context):
    if not context.user_data.get("ts_ai_chat"):
        return False
    prompt = (update.message.text or "").strip()
    if not prompt:
        return True
    if prompt.lower() in {"/new", "/newchat", "new chat", "নতুন চ্যাট"}:
        _cancel_ts_ai_task(update.effective_user.id)
        TS_AI_SESSIONS.pop(str(update.effective_user.id), None)
        await update.message.reply_text("🧹 নতুন TS Ai chat শুরু হয়েছে। আপনার প্রশ্ন পাঠান।")
        return True

    # Do not block the Telegram update loop while the external AI endpoint is
    # generating/continuing a long answer.
    _cancel_ts_ai_task(update.effective_user.id)
    await update.message.reply_text("⏳ TS Ai ভাবছে...")
    task = asyncio.create_task(
        _ts_ai_reply_worker(update.message, update.effective_user.id, prompt)
    )
    TS_AI_TASKS[str(update.effective_user.id)] = task
    return True

async def maintain_request(update, context):
    if not is_admin(update.effective_user.id):
        return

    current = is_maintenance()
    current_status = "🔴 OFF — সব Hosted Bot বন্ধ" if current else "🟢 ON — Hosting চালু"

    await update.message.reply_text(
        "🛠 <b>MAINTAIN</b>\n\n"
        f"বর্তমান Status: <b>{current_status}</b>\n\n"
        "🟢 ON = Userরা আবার তাদের Bot Activate করতে পারবে\n"
        "🔴 OFF = সব Hosted Bot বন্ধ হয়ে যাবে\n\n"
        "নিচের Button থেকে ON/OFF নির্বাচন করুন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 ON", callback_data="maintain:on"),
                InlineKeyboardButton("🔴 OFF", callback_data="maintain:off"),
            ],
            [
                InlineKeyboardButton("🔙 BACK", callback_data="maintain:back"),
            ],
        ])
    )


async def maintain_set_callback(query, enabled):
    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Admin only.", show_alert=True)
        return

    set_maintenance(enabled)

    if enabled:
        stopped = 0
        for bid, data in list(bots.items()):
            ok, _ = await stop_hosted_bot(bid, force=True)
            if ok:
                stopped += 1
        status = "🔴 OFF — সব Hosted Bot বন্ধ"
        extra = f"\n⏹ মোট বন্ধ করা হয়েছে: <b>{stopped}</b>টি\n\nএখন Userরা Bot Run/Restart করতে পারবে না।"
    else:
        status = "🟢 ON — Hosting চালু"
        extra = "\n\nএখন Userরা তাদের Bot আবার Run/Restart করে Activate করতে পারবে।"

    await query.edit_message_text(
        "🛠 <b>MAINTAIN</b>\n\n" + status + extra,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🟢 ON", callback_data="maintain:on"),
                InlineKeyboardButton("🔴 OFF", callback_data="maintain:off"),
            ],
            [
                InlineKeyboardButton("🔙 BACK", callback_data="maintain:back"),
            ],
        ])
    )


# =========================================================
# LANGUAGE + WELCOME
# =========================================================

async def language_menu(update, context):
    lang = get_user_language(update.effective_user.id)
    current = "English" if lang == "en" else "বাংলা"
    await update.message.reply_text(
        f"🌐 <b>Language</b>\n\nCurrent language: <b>{current}</b>\n\nChoose your language:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
             InlineKeyboardButton("🇧🇩 বাংলা", callback_data="lang:bn")]
        ])
    )


def welcome_text(user, lang="en"):
    name = html.escape(user.full_name or "User")
    uid = str(user.id)
    if lang == "bn":
        return (
            "┏━━━━━━━━━━━━━━━━━┓\n"
            "┃ ⚡ ʀ ʙᴏᴛ ᴘʀᴇᴍɪᴜᴍ ʜᴏsᴛɪɴɢ ʙᴏᴛ  ⚡\n"
            "┃    ᴘʀᴇᴍɪᴜᴍ 24/7 ᴄʟᴏᴜᴅ ꜱᴇʀᴠɪᴄᴇ\n"
            "┗━━━━━━━━━━━━━━━━━┛\n"
            f"┃ 👋 ᴡᴇʟᴄᴏᴍᴇ: — {name}\n"
            "┃\n"
            "┃ 🚀 ᴀᴜᴛᴏ ᴅᴇᴘᴇɴᴅᴇɴᴄɪᴇs\n"
            "┃ 🔍 ʀᴇᴀʟ-ᴛɪᴍᴇ ʟᴏɢs\n"
            "┃ ⚡ 24/7 ᴜᴘᴛɪᴍᴇ ᴀᴄᴛɪᴠᴇ\n"
            "┣━━━━━━━━━━━━━━━━━┫\n"
            f"┃ 🆔 ɪᴅ   : <code>{uid}</code>\n"
            "┃ ⚡ sᴛᴀᴛ : ONLINE ✅\n"
            "┃ 🏆 ᴘʟᴀɴ : ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ\n"
            "┗━━━━━━━━━━━━━━━━━┛\n\n"
            "👇 ʙᴇʟᴏᴡ ᴛʜᴇ ʙᴜᴛᴛᴏɴs ᴛᴏ ᴍᴀɴᴀɢᴇ!"
        )
    return (
        "┏━━━━━━━━━━━━━━━━━┓\n"
        "┃ ⚡ ʀ ʙᴏᴛ ᴘʀᴇᴍɪᴜᴍ ʜᴏsᴛɪɴɢ ʙᴏᴛ  ⚡\n"
        "┃    ᴘʀᴇᴍɪᴜᴍ 24/7 ᴄʟᴏᴜᴅ ꜱᴇʀᴠɪᴄᴇ\n"
        "┗━━━━━━━━━━━━━━━━━┛\n"
        f"┃ 👋 ᴡᴇʟᴄᴏᴍᴇ: — {name}\n"
        "┃\n"
        "┃ 🚀 ᴀᴜᴛᴏ ᴅᴇᴘᴇɴᴅᴇɴᴄɪᴇs\n"
        "┃ 🔍 ʀᴇᴀʟ-ᴛɪᴍᴇ ʟᴏɢs\n"
        "┃ ⚡ 24/7 ᴜᴘᴛɪᴍᴇ ᴀᴄᴛɪᴠᴇ\n"
        "┣━━━━━━━━━━━━━━━━━┫\n"
        f"┃ 🆔 ɪᴅ   : <code>{uid}</code>\n"
        "┃ ⚡ sᴛᴀᴛ : ONLINE ✅\n"
        "┃ 🏆 ᴘʟᴀɴ : ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ\n"
        "┗━━━━━━━━━━━━━━━━━┛\n\n"
        "👇 ᴜsᴇ ʙᴜᴛᴛᴏɴs ʙᴇʟᴏᴡ ᴛᴏ ᴍᴀɴᴀɢᴇ!"
    )


async def send_user_home(update, user, lang=None):
    lang = lang or get_user_language(user.id)
    await update.message.reply_text(
        welcome_text(user, lang),
        parse_mode="HTML",
        reply_markup=user_keyboard(lang)
    )


# =========================================================
# FORCE JOIN
# =========================================================

async def is_member(bot, channel, user_id):

    try:

        member = await bot.get_chat_member(
            chat_id=channel,
            user_id=user_id
        )

        return member.status in [
            "member",
            "administrator",
            "creator"
        ]

    except Exception as e:

        logger.warning(
            "Join check failed %s: %s",
            channel,
            e
        )

        return False


async def get_missing_channels(update, context):

    user_id = update.effective_user.id

    missing = []

    for channel in channels:

        if not await is_member(
            context.bot,
            channel,
            user_id
        ):
            missing.append(channel)

    return missing


async def send_force_join(update, missing):

    buttons = []

    for channel in missing:

        username = channel.replace("@", "")

        buttons.append([
            InlineKeyboardButton(
                f"📢 Join {channel}",
                url=f"https://t.me/{username}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "✅ CHECK JOIN",
            callback_data="check_join"
        )
    ])

    lang = get_user_language(update.effective_user.id)
    if lang == "bn":
        prompt = ("🔐 <b>ACCESS LOCKED</b>\n\n"
                  "বট ব্যবহার করার আগে নিচের সব Channel/Group-এ Join করুন।\n\n"
                  "Join করার পরে <b>CHECK JOIN</b> চাপুন।")
    else:
        prompt = ("🔐 <b>ACCESS LOCKED</b>\n\n"
                  "Please join all required channels/groups before using the bot.\n\n"
                  "After joining, press <b>CHECK JOIN</b>.")
    await update.message.reply_text(prompt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def check_join(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    missing = []

    for channel in channels:

        if not await is_member(
            context.bot,
            channel,
            user_id
        ):
            missing.append(channel)

    if missing:

        buttons = []

        for channel in missing:

            username = channel.replace("@", "")

            buttons.append([
                InlineKeyboardButton(
                    f"📢 Join {channel}",
                    url=f"https://t.me/{username}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "🔄 CHECK JOIN",
                callback_data="check_join"
            )
        ])

        await query.edit_message_text(
            "❌ এখনো সব Channel/Group-এ Join করেননি।",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

        return

    await query.edit_message_text(
        "✅ <b>JOINED SUCCESSFULLY</b>",
        parse_mode="HTML"
    )

    lang = get_user_language(user_id)
    await query.message.chat.send_message(
        welcome_text(query.from_user, lang),
        parse_mode="HTML",
        reply_markup=user_keyboard(lang)
    )


# =========================================================
# START
# =========================================================

async def start(update, context):

    user = update.effective_user

    uid = touch_user(user)

    if not is_admin(user.id) and is_banned(user.id):
        await update.message.reply_text(
            "🚫 <b>আপনার Account Banned</b>\n\nAdmin-এর সাথে যোগাযোগ করুন।",
            parse_mode="HTML"
        )
        return

    if is_admin(user.id):

        await update.message.reply_text(
            "👑 <b>ADMIN PANEL</b>\n\n"
            "স্বাগতম Admin!",
            parse_mode="HTML",
            reply_markup=admin_keyboard()
        )

        return

    if is_maintenance():
        await update.message.reply_text(
            "🛠 <b>MAINTENANCE MODE</b>\n\n"
            "বটের Hosting Service সাময়িকভাবে বন্ধ আছে।\n"
            "Admin চালু করলে আবার ব্যবহার করতে পারবেন।",
            parse_mode="HTML"
        )
        return

    if channels:

        missing = await get_missing_channels(
            update,
            context
        )

        if missing:

            await send_force_join(
                update,
                missing
            )

            return

    await send_user_home(update, user, get_user_language(user.id))


# =========================================================
# USER PANEL
# =========================================================

async def user_panel(update):
    await send_user_home(update, update.effective_user, get_user_language(update.effective_user.id))


# =========================================================
# FILE UPLOAD REQUEST
# =========================================================

async def upload_request(update, context):

    if is_admin(update.effective_user.id):

        await update.message.reply_text(
            "👑 Admin Panel থেকে User Panel-এ গিয়ে Upload করুন।"
        )

        return

    context.user_data["waiting_bot_username"] = True
    context.user_data["waiting_upload"] = False

    await update.message.reply_text(
        "🤖 <b>আপনার Bot-এর Username দিন</b>\n\n"
        "উদাহরণ:\n"
        "<code>@my_bot</code>\n\n"
        "Username দেওয়ার পর File Upload করার অপশন আসবে।",
        parse_mode="HTML"
    )

    return

    await update.message.reply_text(
        "📁 <b>FILE UPLOAD</b>\n\n"
        "এখন আপনার File পাঠান।\n\n"
        "উদাহরণ:\n"
        "• main.py\n"
        "• requirements.txt\n"
        "• ZIP\n\n"
        "Upload করার পর File-এর Action পাওয়া যাবে।",
        parse_mode="HTML"
    )


# =========================================================
# SAVE UPLOADED FILE
# =========================================================

async def receive_document(update, context):

    if not context.user_data.get("waiting_upload"):

        return False

    message = update.message

    if not message.document:

        return False

    user = update.effective_user
    touch_user(user)

    uid = str(user.id)

    document = message.document

    filename = document.file_name or "file"

    safe_name = os.path.basename(filename)

    user_folder = os.path.join(
        USERS_DIR,
        uid
    )

    os.makedirs(
        user_folder,
        exist_ok=True
    )

    target = os.path.join(
        user_folder,
        safe_name
    )

    try:

        telegram_file = await document.get_file()

        await telegram_file.download_to_drive(
            custom_path=target
        )

    except Exception as e:

        logger.error(
            "File download error: %s",
            e
        )

        await message.reply_text(
            "❌ File Save করা যায়নি। আবার চেষ্টা করুন।"
        )

        return True

    if uid not in uploaded_files:

        uploaded_files[uid] = []

    # Remove old duplicate
    uploaded_files[uid] = [
        x for x in uploaded_files[uid]
        if x.get("name") != safe_name
    ]

    uploaded_files[uid].append({
        "name": safe_name,
        "path": target,
        "size": document.file_size or 0,
        "uploaded": int(time.time()),
        "bot_username": context.user_data.get("upload_bot_username", "")
    })

    save_json(
        FILES_FILE,
        uploaded_files
    )

    context.user_data["waiting_upload"] = False
    context.user_data.pop("upload_bot_username", None)

    keyboard = [
        [
            InlineKeyboardButton(
                "🤖 HOST THIS BOT",
                callback_data=f"hostfile:{uid}:{safe_name}"
            )
        ],
        [
            InlineKeyboardButton(
                "📂 MY FILES",
                callback_data="my_files"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 DELETE FILE",
                callback_data=f"deletefile:{uid}:{safe_name}"
            ),
            InlineKeyboardButton(
                "🔃 REFRESH",
                callback_data="refresh_files"
            )
        ]
    ]

    await message.reply_text(
        "✅ <b>FILE UPLOADED SUCCESSFULLY</b>\n\n"
        f"📁 File: <code>{safe_name}</code>\n"
        f"📦 Size: {document.file_size or 0} bytes\n\n"
        "এখন নিচের Action থেকে নির্বাচন করুন।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # Admin notification
    try:

        await context.bot.send_message(
            ADMIN_ID,
            "📥 <b>NEW FILE UPLOADED</b>\n\n"
            f"👤 User: {user.full_name}\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"📁 File: <code>{safe_name}</code>",
            parse_mode="HTML"
        )

        await context.bot.send_document(
            ADMIN_ID,
            document=document.file_id,
            caption=f"📁 Uploaded by {user.full_name}"
        )

    except Exception as e:

        logger.error(
            "Admin notification error: %s",
            e
        )

    return True


# =========================================================
# MY FILES
# =========================================================

async def show_my_files_message(update):

    uid = str(update.effective_user.id)

    files = uploaded_files.get(uid, [])

    if not files:

        await update.message.reply_text(
            "📂 <b>MY FILES</b>\n\n"
            "আপনার কোনো File নেই।",
            parse_mode="HTML"
        )

        return

    buttons = []

    for index, item in enumerate(files):

        name = item.get("name", "Unknown")

        buttons.append([
            InlineKeyboardButton(
                f"📁 {name}",
                callback_data=f"fileinfo:{uid}:{index}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔃 REFRESH",
            callback_data="refresh_files"
        )
    ])

    await update.message.reply_text(
        "📂 <b>MY FILES</b>\n\n"
        "একটি File নির্বাচন করুন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_my_files_callback(query):

    uid = str(query.from_user.id)

    files = uploaded_files.get(uid, [])

    if not files:

        await query.edit_message_text(
            "📂 আপনার কোনো File নেই।"
        )

        return

    buttons = []

    for index, item in enumerate(files):

        name = item.get("name", "Unknown")

        buttons.append([
            InlineKeyboardButton(
                f"📁 {name}",
                callback_data=f"fileinfo:{uid}:{index}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔃 REFRESH",
            callback_data="refresh_files"
        )
    ])

    await query.edit_message_text(
        "📂 <b>MY FILES</b>\n\n"
        "একটি File নির্বাচন করুন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# FILE INFO
# =========================================================

async def file_info(query, uid, index):

    if str(query.from_user.id) != str(uid):

        await query.answer(
            "❌ এটি আপনার File নয়।",
            show_alert=True
        )

        return

    # Respect the same dynamic host permission used by the real host flow.
    current_hosted = len(get_user_bots(int(uid)))
    host_limit = get_user_host_limit(int(uid))
    if current_hosted >= host_limit:
        await query.answer(
            f"❌ আপনি সর্বোচ্চ {host_limit}টি Bot Host করতে পারবেন।",
            show_alert=True
        )
        return

    files = uploaded_files.get(uid, [])

    try:

        item = files[int(index)]

    except Exception:

        await query.edit_message_text(
            "❌ File পাওয়া যায়নি।"
        )

        return

    name = item.get("name", "Unknown")

    keyboard = [
        [
            InlineKeyboardButton(
                "🤖 HOST",
                callback_data=f"hostfile:{uid}:{name}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 DELETE",
                callback_data=f"deletefile:{uid}:{name}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 BACK",
                callback_data="my_files"
            )
        ]
    ]

    await query.edit_message_text(
        "📁 <b>FILE INFO</b>\n\n"
        f"📄 Name: <code>{name}</code>\n"
        f"📦 Size: {item.get('size', 0)} bytes",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# DELETE FILE
# =========================================================

async def delete_file(query, uid, filename):

    if str(query.from_user.id) != str(uid):

        await query.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    files = uploaded_files.get(uid, [])

    found = None

    for item in files:

        if item.get("name") == filename:

            found = item
            break

    if not found:

        await query.edit_message_text(
            "❌ File পাওয়া যায়নি।"
        )

        return

    path = found.get("path")

    try:

        if path and os.path.exists(path):

            os.remove(path)

    except Exception as e:

        logger.error(
            "Delete file error: %s",
            e
        )

    uploaded_files[uid] = [
        x for x in files
        if x.get("name") != filename
    ]

    save_json(
        FILES_FILE,
        uploaded_files
    )

    await query.edit_message_text(
        f"🗑 <b>FILE DELETED</b>\n\n"
        f"📁 <code>{filename}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔃 REFRESH",
                    callback_data="refresh_files"
                )
            ]
        ])
    )


# =========================================================
# HOST FILE
# =========================================================

async def host_file(query, context, uid, filename):

    if str(query.from_user.id) != str(uid):

        await query.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    files = uploaded_files.get(uid, [])

    item = None

    for x in files:

        if x.get("name") == filename:

            item = x
            break

    if not item:

        await query.edit_message_text(
            "❌ File পাওয়া যায়নি।"
        )

        return

    path = item.get("path")

    if not path or not os.path.exists(path):

        await query.edit_message_text(
            "❌ Server-এ File পাওয়া যাচ্ছে না।"
        )

        return

    # Any runnable entry file can be hosted. The server will choose
    # the available runtime automatically when the bot starts.
    supported = (
        ".py", ".js", ".mjs", ".cjs", ".sh", ".bash",
        ".php", ".rb", ".pl", ".jar"
    )

    if not filename.lower().endswith(supported):

        await query.edit_message_text(
            "⚠️ এই File-এর runtime auto-detect করা যাচ্ছে না।\n\n"
            "Supported: .py, .js, .mjs, .cjs, .sh, .bash, .php, .rb, .pl, .jar",
            parse_mode="HTML"
        )

        return

    context.user_data["hosting_file"] = {
        "uid": uid,
        "filename": filename,
        "path": path,
        "bot_username": item.get("bot_username", "")
    }

    await query.edit_message_text(
        "🤖 <b>HOST BOT</b>\n\n"
        "এখন আপনার Bot Token পাঠান।\n\n"
        "উদাহরণ:\n"
        "<code>123456:ABC...</code>\n\n"
        "⚠️ Token অন্য কারো সাথে share করবেন না।",
        parse_mode="HTML"
    )


# =========================================================
# CREATE BOT
# =========================================================

async def create_hosted_bot(update, context, token):

    user = update.effective_user

    info = context.user_data.get(
        "hosting_file"
    )

    if not info:

        await update.message.reply_text(
            "❌ Hosting session পাওয়া যায়নি। আবার File select করুন।"
        )

        return

    if is_maintenance() and not is_admin(user.id):
        await update.message.reply_text("🛠 Maintenance Mode চালু আছে — এখন নতুন Bot Host করা যাবে না।")
        context.user_data.pop("hosting_file", None)
        return

    # Basic token check
    if ":" not in token or len(token) < 20:

        await update.message.reply_text(
            "❌ Bot Token সঠিক মনে হচ্ছে না।\n\n"
            "আবার Token পাঠান।"
        )

        return

    uid = str(user.id)

    # Host permission: default 1, admin can grant any custom limit.
    host_limit = get_user_host_limit(user.id)
    current_hosted = len(get_user_bots(user.id))

    if current_hosted >= host_limit:
        await update.message.reply_text(
            "❌ <b>HOST LIMIT REACHED</b>\n\n"
            f"আপনি সর্বোচ্চ <b>{host_limit}টি Bot</b> Host করতে পারবেন।\n"
            "একটি Bot Delete করলে আপনার Host করার জায়গা আবার খালি হবে।\n"
            "আর যদি আপনার বিশেষ প্রয়োজন হয় তাহলে Admin-@sujoy_012 কে বলে Bot-এর Host লিমিট বাড়িয়ে নিন।",
            parse_mode="HTML"
        )
        context.user_data.pop("hosting_file", None)
        return

    # New bot ID
    bot_id = str(
        int(time.time() * 1000)
    )

    while bot_id in bots:

        bot_id = str(
            int(time.time() * 1000) + 1
        )

    folder = bot_folder(bot_id)

    os.makedirs(
        folder,
        exist_ok=True
    )

    source = info["path"]

    entry_filename = os.path.basename(info["filename"])

    destination = os.path.join(
        folder,
        entry_filename
    )

    try:

        shutil.copy2(
            source,
            destination
        )

    except Exception as e:

        logger.error(
            "Copy bot file error: %s",
            e
        )

        await update.message.reply_text(
            "❌ Bot File copy করা যায়নি।"
        )

        return

    bots[bot_id] = {
        "id": bot_id,
        "owner_id": user.id,
        "owner_name": user.full_name,
        "name": info.get("bot_username") or os.path.splitext(entry_filename)[0],
        "bot_username": info.get("bot_username", ""),
        "main_file": entry_filename,
        "entry_file": entry_filename,
        "status": "STOPPED",
        "created": int(time.time()),
        "updated": int(time.time()),
        "token": token
    }

    save_json(
        BOTS_FILE,
        bots
    )

    context.user_data.pop(
        "hosting_file",
        None
    )

    context.user_data["package_bot_id"] = bot_id

    await update.message.reply_text(
        "✅ <b>ENTRY FILE UPLOADED</b>\n\n"
        f"🤖 Bot ID: <code>{bot_id}</code>\n"
        f"📁 Run File: <code>{entry_filename}</code>\n\n"
        "এখন <b>requirements.txt</b> Upload করুন অথবা নিচে package-এর নাম লিখুন।\n\n"
        "📦 Package না জানলেও সমস্যা নেই — <b>⏭️ Skip</b> চাপুন।\n"
        "তাহলে Bot নিজে Missing Package শনাক্ত করে Install করার চেষ্টা করবে।\n\n"
        "উদাহরণ:\n"
        "<code>requests\nfirebase-admin\npyotp\nbeautifulsoup4</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Skip", callback_data=f"skip_packages:{bot_id}")]
        ])
    )


# =========================================================
# PACKAGE INPUT / AUTO INSTALL
# =========================================================


# ---------------------------------------------------------
# AUTO PACKAGE DETECTION
# ---------------------------------------------------------
# Common import-name -> pip-name mappings.  When an import is not in
# this table we first try the same name; if pip cannot find it, the
# error is left in LOGS for the user/admin.
IMPORT_TO_PIP = {
    # Common import-name -> pip-name mappings
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "PIL": "Pillow",
    "telegram": "python-telegram-bot",
    "firebase_admin": "firebase-admin",
    "googleapiclient": "google-api-python-client",
    "jwt": "PyJWT",
    "Crypto": "pycryptodome",
    "yaml": "PyYAML",
    "dateutil": "python-dateutil",
    "discord": "discord.py",
    "flask": "Flask",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "numpy": "numpy",
    "pandas": "pandas",
    "requests": "requests",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
    "pyotp": "pyotp",
    "lxml": "lxml",
    "qrcode": "qrcode",
    "barcode": "python-barcode",
    "pytz": "pytz",
    "tzlocal": "tzlocal",
    "schedule": "schedule",
    "apscheduler": "APScheduler",
    "sqlalchemy": "SQLAlchemy",
    "pymysql": "PyMySQL",
    "mysql": "mysql-connector-python",
    "psycopg2": "psycopg2-binary",
    "redis": "redis",
    "motor": "motor",
    "pymongo": "pymongo",
    "aiosqlite": "aiosqlite",
    "cryptography": "cryptography",
    "nacl": "PyNaCl",
    "paramiko": "paramiko",
    "websocket": "websocket-client",
    "websockets": "websockets",
    "openpyxl": "openpyxl",
    "xlsxwriter": "XlsxWriter",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "reportlab": "reportlab",
    "matplotlib": "matplotlib",
    "scipy": "scipy",
    "sklearn": "scikit-learn",
    "torch": "torch",
    "transformers": "transformers",
    "selenium": "selenium",
    "playwright": "playwright",
    "aiofiles": "aiofiles",
    "aiogram": "aiogram",
    "telebot": "pyTelegramBotAPI",
    "pyrogram": "Pyrogram",
    "telethon": "Telethon",
    "instaloader": "instaloader",
    "yt_dlp": "yt-dlp",
    "youtube_dl": "youtube_dl",
    "cloudscraper": "cloudscraper",
    "fake_useragent": "fake-useragent",
    "user_agents": "user-agents",
    "colorama": "colorama",
    "rich": "rich",
    "click": "click",
    "typer": "typer",
    "pydantic": "pydantic",
    "orjson": "orjson",
    "ujson": "ujson",
    "toml": "toml",
    "decouple": "python-decouple",
    "boto3": "boto3",
    "botocore": "botocore",
    "stripe": "stripe",
    "paypalrestsdk": "paypalrestsdk",
    "google": "google",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "oauthlib": "oauthlib",
    "requests_oauthlib": "requests-oauthlib",
    "filelock": "filelock",
    "tenacity": "tenacity",
    "tqdm": "tqdm",
    "packaging": "packaging",
    "pythonjsonlogger": "python-json-logger",
}

# A broad catalog of common PyPI packages.  These are NOT all installed
# up-front (that would be slow, huge, and can cause dependency conflicts).
# They are available to the auto-repair resolver: if a bot imports one of
# these names, the corresponding package can be installed automatically.
COMMON_AUTO_PACKAGES = """
aiohttp aioboto3 aiohttp-retry aioconsole aiocron aiocsv aiohttp-socks
aiodns aiofiles aiogram aiomysql aioredis aiormq aiosmtplib aiosqlite
aiosqlitepool alembic anyio argon2-cffi arrow asgiref astroid async-timeout
asyncpg attrs authlib automata-lib av babel bcrypt beautifulsoup4 bitarray
black bleach blinker blis bokeh boto3 botocore bottle brotli cachetools
cairocffi cairosvg captcha pillow cattrs celery certifi cffi chardet charset-normalizer
click clickclick cloudpickle colorama coloredlogs commontools connexion cookiecutter
cryptography cssselect cssselect2 cycler dataclasses-json datasets dateparser
decorator defusedxml dill diskcache dnspython docopt docutils duckdb email-validator
emoji environs et-xmlfile eventlet fastapi fastapi-cli feedparser ffmpeg-python
filelock firebase-admin flask flask-cors flask-login flask-sqlalchemy flask-socketio
fonttools freezegun fsspec fuzzywuzzy geopy gitpython google google-api-core
google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib
google-cloud-core google-cloud-firestore google-cloud-storage google-crc32c
google-resumable-media googleapis-common-protos greenlet gunicorn h11 h2 h5py
httpcore httplib2 httpx humanize idna imageio importlib-metadata iniconfig
instaloader itsdangerous jinja2 joblib json5 jsonschema jsonschema-specifications
jupyter jupyterlab jupyter-client jupyter-core keras keyring kiwisolver kombu
langchain langchain-core langsmith lark lazy-loader levenshtein libsass lxml
markdown markdown2 markupsafe marshmallow matplotlib maxminddb multidict
multidict mysql-connector-python mysqlclient networkx nltk numba numpy oauthlib
onnx onnxruntime openai openpyxl optuna orjson packaging pandas paramiko
passlib pendulum pillow pipx plotly pluggy polars prometheus-client prompt-toolkit
propcache protobuf psutil psycopg2-binary pydantic pydantic-settings pyjwt
pymongo pymysql pyopenssl pyparsing pyperclip pyphen pyppeteer pyrogram
pyquery pyrfc3339 pyserial pysocks pytest pytest-asyncio python-barcode
python-dateutil python-decouple python-dotenv python-json-logger python-multipart
python-slugify python-telegram-bot python-timeparser pytesseract pytz pyttsx3
pyyaml qrcode redis regex reportlab requests requests-cache requests-file
requests-oauthlib requests-toolbelt rich rlp rsa ruamel.yaml schedule scipy
scikit-image scikit-learn scrapy selenium sentry-sdk setuptools shapely six
sniffio snowballstemmer socketio soupsieve spacy sqlalchemy starlette stripe
sympy tabulate tenacity termcolor text-unidecode tiktoken tinycss2 toml
tomli tomlkit toolz tornado tqdm traitlets trio trio-websocket twilio typer
typing-extensions tzdata tzlocal ujson urllib3 uvicorn uvloop validators
vine virtualenv watchfiles webdriver-manager webob websocket-client websockets
werkzeug wheel whitenoise wrapt wsproto xgboost xlrd xlsxwriter xlwt
xmltodict yarl yt-dlp youtube-dl zipp zope.interface zstandard
apscheduler asyncpg aiojobs aioredis aioredlock arq dramatiq huey rq
rq-dashboard redis-om celery-redbeat flower kombu eventlet gevent
gevent-websocket gunicorn uvicorn gunicorn gevent waitress hypercorn
quart sanic tornado falcon bottle pyramid web2py django djangorestframework
django-cors-headers django-filter django-environ django-redis django-rest-framework
django-allauth django-crispy-forms django-debug-toolbar django-q2
channels channels-redis graphene graphene-django strawberry-graphql ariadne
flask-restful flask-restx flask-jwt-extended flask-migrate flask-mail flask-wtf
wtforms wtforms-sqlalchemy marshmallow-sqlalchemy connexion aiohttp-cors
fastapi-pagination slowapi python-jose jose authlib passlib argon2-cffi
bcrypt pycryptodome pycryptodomex pynacl cryptography pyotp onetimepass
totp pyotp-async pyotp-cli segno pyzbar qrcode-artistic barcode
reportlab svglib weasyprint xhtml2pdf pdfplumber pypdf PyPDF2 pymupdf fitz
pdf2image img2pdf pikepdf camelot-py tabula-py pdfminer-six
python-docx docx2txt mammoth python-pptx pptxgenjs odfpy xlwings
xlsxwriter openpyxl pandas-stubs numpy-financial statsmodels sympy
seaborn plotly bokeh altair vega-datasets pygal folium geopandas geopy
rasterio cartopy descartes contextily shapely fiona pyproj
opencv-python opencv-contrib-python imageio-ffmpeg moviepy ffmpeg-python
pydub librosa soundfile sounddevice simpleaudio speechrecognition pocketsphinx
gtts edge-tts pyttsx3 pywin32 pyautogui keyboard mouse pynput
pytesseract easyocr paddleocr transformers tokenizers accelerate datasets
sentencepiece safetensors huggingface-hub openai anthropic groq mistralai
google-generativeai cohere ollama llama-index chromadb faiss-cpu
langchain-community langchain-text-splitters tiktoken beautifulsoup4
scrapy scrapy-playwright selenium-wire undetected-chromedriver playwright
pyppeteer requests-html cloudscraper cfscrape fake-useragent user-agents
httpx-socks aiohttp-socks socksio pysocks urllib3 requests-toolbelt
paramiko fabric invoke ansible ansible-core netmiko napalm scp
docker docker-compose kubernetes minio boto3 botocore azure-storage-blob
azure-identity azure-core google-cloud-storage google-cloud-bigquery
google-cloud-pubsub google-cloud-aiplatform google-cloud-translate
google-cloud-vision google-cloud-speech google-cloud-dialogflow
google-cloud-firestore google-cloud-secret-manager firebase-admin
stripe paypalrestsdk razorpay square-python-sdk twilio sendgrid mailgun3
boto3 s3fs gcsfs adlfs fsspec minio supabase psycopg2-binary asyncpg
aiomysql pymysql mysqlclient mysql-connector-python mariadb sqlalchemy
alembic databases encode-databases tortoise-orm peewee dataset pony
mongoengine motor redis hiredis cassandra-driver neo4j elasticsearch
elasticsearch-dsl opensearch-py influxdb-client clickhouse-driver duckdb
mysql-connector-python pyodbc cx-oracle oracledb sqlalchemy-utils
passlib argon2-cffi bcrypt scrypt cryptography pyopenssl certifi
cffi pynacl pyjwt python-jose authlib oauthlib requests-oauthlib
itsdangerous secure-cookie flask-login flask-principal flask-security-too
python-multipart email-validator dnspython email-validator aiosmtplib
smtplib-ssl smtp-mailer premailer yagmail imapclient exchangelib
telethon pyrogram pyTelegramBotAPI aiogram python-telegram-bot
telegramify-markdown telegram-text formatter pyromod tgcrypto telethon-session
discord.py nextcord disnake py-cord hikari lightbulb interactions.py
slack-sdk slack-bolt discord-webhook discord-py-interactions twitchio
tweepy mastodon.py atproto instagrapi instaloader pinterest-api
youtube-search-python youtube-search yt-dlp yt-dlp-ejs pytube pytubefix
youtube-dl moviepy ffmpeg-python spotipy deezer-python
wikipedia wikipedia-api googlesearch-python duckduckgo-search serpapi
google-search-results newspaper3k feedparser trafilatura readability-lxml
newspaper newspaper4k parsel cssselect lxml selectolax html5lib html5-parser
chardet charset-normalizer ftfy langdetect lingua-language-detector polyglot
nltk spacy textblob vaderSentiment gensim wordcloud rapidfuzz fuzzywuzzy
python-Levenshtein regex unidecode slugify python-slugify dateparser arrow
pendulum humanize babel pytz tzdata tzlocal holidays workalendar
icalendar croniter schedule APScheduler celery rq dramatiq huey
click typer rich textual prompt-toolkit colorama termcolor tabulate tqdm alive-progress
loguru structlog python-json-logger coloredlogs verboselogs
pytest pytest-asyncio pytest-cov unittest2 nose hypothesis tox nox
mypy pyright ruff black isort autopep8 yapf flake8 pylint bandit
pre-commit coverage codecov
jupyter jupyterlab notebook ipykernel ipython nbformat nbconvert voila
streamlit gradio dash dash-bootstrap-components panel nicegui reflex
plotly bokeh altair streamlit-extras
marshmallow pydantic attrs cattrs dataclasses-json typing-inspect typing-extensions
jsonschema jsonschema-specifications fastjsonschema orjson ujson msgpack
toml tomli tomlkit ruamel.yaml pyyaml configparser python-dotenv environs
python-decouple dynaconf decouple hydra-core omegaconf
psutil watchdog watchfiles filelock portalocker lockfile diskcache cachetools
cachetools joblib cloudpickle dill multiprocess pathos
networkx igraph graphviz pygraphviz pydot
scipy scikit-learn scikit-image xgboost lightgbm catboost
tensorflow tensorflow-cpu keras torch torchvision torchaudio
jax jaxlib cupy numexpr bottleneck
opencv-python-headless imageio tifffile pillow-heif rawpy
numpy pandas polars pyarrow fastparquet parquet-tools
sqlparse sqlglot moz-sql-parser
"""

# Build fallback mappings for the broad catalog. Explicit mappings above win.
# This gives the auto-installer hundreds of package candidates without
# installing hundreds of packages for every bot.
for _pkg in COMMON_AUTO_PACKAGES.split():
    IMPORT_TO_PIP.setdefault(_pkg.replace("-", "_"), _pkg)



def _extract_missing_module(log_text):
    """Extract the LAST missing module from a fresh traceback/log segment."""
    patterns = [
        r"No module named ['\"]([^'\"]+)['\"]",
        r"cannot import name ['\"]([^'\"]+)['\"]",
    ]
    found = []
    text = log_text or ""
    for pattern in patterns:
        found.extend(m.group(1) for m in re.finditer(pattern, text))
    if not found:
        return None
    # Prefer the last error because the log can contain previous attempts.
    return found[-1].split(".")[0]


def _pip_name_for_module(module):
    """Translate Python import names to their real PyPI distribution names."""
    direct = {
        "firebase_admin": "firebase-admin",
        "googleapiclient": "google-api-python-client",
        "google_auth_oauthlib": "google-auth-oauthlib",
        "google.auth": "google-auth",
        "google.cloud": "google-cloud-core",
        "google.cloud.firestore": "google-cloud-firestore",
        "google.cloud.storage": "google-cloud-storage",
        "google.cloud.bigquery": "google-cloud-bigquery",
        "google.cloud.pubsub": "google-cloud-pubsub",
        "bs4": "beautifulsoup4",
        "cv2": "opencv-python",
        "dotenv": "python-dotenv",
        "PIL": "Pillow",
        "telegram": "python-telegram-bot",
        "jwt": "PyJWT",
        "Crypto": "pycryptodome",
        "yaml": "PyYAML",
        "dateutil": "python-dateutil",
        "sklearn": "scikit-learn",
        "psycopg2": "psycopg2-binary",
        "nacl": "PyNaCl",
        "websocket": "websocket-client",
        "docx": "python-docx",
        "pptx": "python-pptx",
        "telebot": "pyTelegramBotAPI",
        "yt_dlp": "yt-dlp",
        "fake_useragent": "fake-useragent",
        "user_agents": "user-agents",
    }
    if module in direct:
        return direct[module]
    return IMPORT_TO_PIP.get(module, module)


def _read_log(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _python_imports(path):
    """Return top-level imports used by a Python file."""
    try:
        import ast
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=path)
    except Exception:
        return set()

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                imports.add(item.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
    return imports


def _is_stdlib_module(name):
    try:
        stdlib = getattr(sys, "stdlib_module_names", set())
        return name in stdlib
    except Exception:
        return False


def _module_available(module, package_dir):
    """Fast check: is this import already available without downloading anything?"""
    try:
        import importlib.util
        old_path = list(sys.path)
        if package_dir not in sys.path:
            sys.path.insert(0, package_dir)
        try:
            return importlib.util.find_spec(module) is not None
        finally:
            sys.path[:] = old_path
    except Exception:
        return False


async def _pip_install_one(package_dir, package_name, log_path):
    """Install one package only when it is actually needed."""
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            "--prefer-binary",
            "--target", package_dir,
            package_name,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
    )
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n[AUTO PACKAGE] pip install {package_name}\n")
        log_file.write(result.stdout or "")
        log_file.write("\n")
    return result.returncode == 0


async def _pip_install_many(package_dir, packages, log_path):
    """Install several missing packages in ONE pip process for much faster startup."""
    packages = list(dict.fromkeys(p for p in packages if p))
    if not packages:
        return True

    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            "--prefer-binary",
            "--target", package_dir,
            *packages,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
    )
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write("\n[AUTO PACKAGE BATCH] pip install " + " ".join(packages) + "\n")
        log_file.write(result.stdout or "")
        log_file.write("\n")
    return result.returncode == 0


async def _preinstall_python_imports(main_file, package_dir, log_path):
    """
    FAST zero-config preflight:
    1) scan imports;
    2) skip standard-library and already-installed modules;
    3) install all missing packages in one pip call;
    4) fall back to one-by-one only if the batch install fails.
    """
    imports = _python_imports(main_file)
    if not imports:
        return

    candidates = []
    skipped = 0
    for module in sorted(imports):
        if _is_stdlib_module(module):
            continue
        if _module_available(module, package_dir):
            skipped += 1
            continue
        package = _pip_name_for_module(module)
        if package and package not in candidates:
            candidates.append(package)

    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(
            "\n[AUTO PREFLIGHT] FAST import scan started.\n"
            f"[AUTO PREFLIGHT] Missing package candidates: {len(candidates)}; "
            f"already available: {skipped}.\n"
        )

    if not candidates:
        return

    ok = await _pip_install_many(package_dir, candidates, log_path)
    if ok:
        return

    # If one optional/invalid package broke the batch, keep the useful ones.
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write("[AUTO PREFLIGHT] Batch install failed; trying packages individually.\n")
    for package in candidates:
        await _pip_install_one(package_dir, package, log_path)


async def _install_requirements_resilient(requirements, package_dir, log_path):
    """Fast requirements install with a hash marker; bad lines still fall back individually."""
    if not os.path.isfile(requirements) or os.path.getsize(requirements) == 0:
        return True

    try:
        raw = open(requirements, "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
        marker = os.path.join(package_dir, ".requirements.sha256")
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                if f.read().strip() == digest:
                    with open(log_path, "a", encoding="utf-8") as log_file:
                        log_file.write("[PACKAGE CACHE] requirements.txt unchanged — install skipped.\n")
                    return True
    except Exception:
        digest = None
        marker = None

    with open(requirements, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = f.read().splitlines()

    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            "--prefer-binary",
            "--target", package_dir,
            "-r", requirements,
        ],
        cwd=os.path.dirname(requirements),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
    )
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write("Installing requirements...\n")
        log_file.write(result.stdout or "")
        log_file.write("\n")

    if result.returncode == 0:
        if digest and marker:
            try:
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(digest)
            except Exception:
                pass
        return True

    # A typo such as aiohttpt should not block valid packages.
    candidates = []
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("-r ", "--requirement", "-e ")):
            continue
        candidates.append(line)

    any_success = False
    for package in candidates:
        ok = await _pip_install_one(package_dir, package, log_path)
        any_success = any_success or ok

    if any_success and digest and marker:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(digest)
        except Exception:
            pass

    return any_success or not candidates


async def install_and_run_bot(update, context, bot_id):

    if bot_id not in bots:
        await update.message.reply_text("❌ Bot পাওয়া যায়নি।")
        return

    await update.message.reply_text(
        "⚙️ <b>PACKAGE INSTALL হচ্ছে...</b>\n\n"
        "Install শেষ হলে Bot নিজে থেকেই RUN হবে।",
        parse_mode="HTML"
    )

    success, message = await start_hosted_bot(bot_id)

    await update.message.reply_text(
        ("🟢 " if success else "❌ ") + message,
        parse_mode="HTML"
    )

    context.user_data.pop("package_bot_id", None)


def normalize_requirements(text):

    lines = []

    for part in text.replace(",", "\n").splitlines():
        item = part.strip()
        if not item or item.startswith("#"):
            continue
        lines.append(item)

    return lines


async def receive_package_document(update, context):

    bot_id = context.user_data.get("package_bot_id")

    if not bot_id:
        return False

    if bot_id not in bots:
        context.user_data.pop("package_bot_id", None)
        return True

    document = update.message.document
    if not document:
        return False

    filename = (document.file_name or "").lower()

    allowed_package_files = {
        "requirements.txt", "package.json", "package-lock.json",
        "composer.json", "composer.lock", "Gemfile", "Gemfile.lock",
        "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
        "pom.xml", "build.gradle", "build.gradle.kts", "gradle.properties",
        "packages.txt", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "requirements-dev.txt", "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock",
        "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb", "composer.json",
        "mix.exs", "mix.lock", "pubspec.yaml", "pubspec.lock", "Package.swift",
        "Makefile", "cpanfile", "cpanfile.snapshot"
    }
    if document.file_name not in allowed_package_files:
        await update.message.reply_text(
            "❌ Package file supported নয়।\n\n"
            "Supported: Python/Node/PHP/Ruby/Rust/Go/Java/Perl manifests, "
            "packages.txt, Dockerfile, pyproject.toml, Poetry/Pipenv, lock files.",
            parse_mode="HTML"
        )
        return True

    bot_folder_path = bot_folder(bot_id)
    os.makedirs(bot_folder_path, exist_ok=True)
    tg_file = await document.get_file()
    path = os.path.join(bot_folder_path, document.file_name)
    await tg_file.download_to_drive(custom_path=path)
    # Keep requirements.txt compatibility with the existing installer.
    if document.file_name == "requirements.txt":
        req_path = bot_requirements_file(bot_id)
        if path != req_path:
            shutil.copy2(path, req_path)

    await update.message.reply_text(
        "✅ <b>Package File Uploaded!</b>",
        parse_mode="HTML"
    )

    await install_and_run_bot(update, context, bot_id)
    return True


async def save_package_text_and_run(update, context):

    bot_id = context.user_data.get("package_bot_id")
    if not bot_id:
        return False

    packages = normalize_requirements(update.message.text or "")

    if not packages:
        await update.message.reply_text(
            "❌ Package-এর নাম দিন। যেমন: <code>requests</code>",
            parse_mode="HTML"
        )
        return True

    # Keep the existing text-package UI, but store the input in a
    # language-neutral file so Docker can route it to the correct manager.
    package_input = os.path.join(bot_folder(bot_id), ".hosting_packages.txt")
    with open(package_input, "w", encoding="utf-8") as f:
        f.write("\n".join(packages) + "\n")

    # Python/local fallback keeps the traditional requirements.txt behavior.
    runtime_hint = _detect_project_runtime(
        bot_folder(bot_id),
        bots.get(bot_id, {}).get("entry_file") or bots.get(bot_id, {}).get("main_file") or "main.py"
    ) if "_detect_project_runtime" in globals() else "python"
    if runtime_hint == "python":
        path = bot_requirements_file(bot_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(packages) + "\n")

    await update.message.reply_text(
        "✅ <b>Package List Saved!</b>\n\n"
        f"📦 Total: {len(packages)} package",
        parse_mode="HTML"
    )

    await install_and_run_bot(update, context, bot_id)
    return True



# =========================================================
# UNIVERSAL DOCKER / MULTI-PACKAGE HOSTING
# =========================================================

def _docker_available():
    if not USE_DOCKER_IF_AVAILABLE:
        return False
    try:
        r = subprocess.run(
            [DOCKER_BIN, "version", "--format", "{{.Server.Version}}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=8
        )
        return r.returncode == 0
    except Exception:
        return False


def _docker_container_name(bot_id):
    return "tg_host_" + re.sub(r"[^a-zA-Z0-9_.-]", "_", str(bot_id))


def _detect_project_runtime(folder, entry):
    ext = os.path.splitext(entry)[1].lower()
    if os.path.exists(os.path.join(folder, "Dockerfile")):
        return "dockerfile"
    if ext == ".py" or any(os.path.exists(os.path.join(folder, x))
                            for x in ("requirements.txt", "pyproject.toml", "poetry.lock", "Pipfile")):
        return "python"
    if ext in (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts") or os.path.exists(os.path.join(folder, "package.json")):
        return "node"
    if ext == ".php" or os.path.exists(os.path.join(folder, "composer.json")):
        return "php"
    if ext == ".rb" or os.path.exists(os.path.join(folder, "Gemfile")):
        return "ruby"
    if ext == ".rs" or os.path.exists(os.path.join(folder, "Cargo.toml")):
        return "rust"
    if ext == ".go" or os.path.exists(os.path.join(folder, "go.mod")):
        return "go"
    if ext in (".jar", ".java") or os.path.exists(os.path.join(folder, "pom.xml")) \
            or os.path.exists(os.path.join(folder, "build.gradle")) \
            or os.path.exists(os.path.join(folder, "build.gradle.kts")):
        return "java"
    if ext == ".pl" or os.path.exists(os.path.join(folder, "cpanfile")):
        return "perl"
    if ext in (".ex", ".exs") or os.path.exists(os.path.join(folder, "mix.exs")):
        return "elixir"
    if ext == ".dart" or os.path.exists(os.path.join(folder, "pubspec.yaml")):
        return "dart"
    return "python"


def _dockerfile_for_project(folder, entry):
    """Generate a broad language-specific Docker environment.

    Dockerfile supplied by the user wins.  Otherwise the hosting layer
    chooses the matching official runtime and installs every recognized
    dependency manifest found in the bot folder.  System packages in
    packages.txt are installed *inside the container*, never on the host.
    """
    runtime = _detect_project_runtime(folder, entry)
    if runtime == "dockerfile":
        return None

    ext = os.path.splitext(entry)[1].lower()
    run = ""
    install = []

    if runtime == "python":
        base = "python:3.12-slim"
        install += [
            "python -m pip install --upgrade pip setuptools wheel",
            "if [ -f requirements.txt ]; then pip install --prefer-binary -r requirements.txt || true; fi",
            "if [ -f .hosting_packages.txt ]; then pip install --prefer-binary -r .hosting_packages.txt || true; fi",
            "if [ -f requirements-dev.txt ]; then pip install --prefer-binary -r requirements-dev.txt || true; fi",
            "if [ -f pyproject.toml ]; then pip install --prefer-binary . || true; fi",
            "if [ -f poetry.lock ] && [ -f pyproject.toml ]; then pip install poetry && poetry install --no-interaction || true; fi",
            "if [ -f Pipfile ]; then pip install pipenv && pipenv install --system --deploy || true; fi",
        ]
        run = f'CMD ["python", "{entry}"]'
    elif runtime == "node":
        base = "node:22-bookworm-slim"
        install += [
            "if [ -f package.json ]; then npm install --omit=dev || npm install; fi",
            "if [ -f .hosting_packages.txt ]; then npm install $(cat .hosting_packages.txt) || true; fi",
            "if [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile || pnpm install; fi",
            "if [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile || yarn install; fi",
            "if [ -f bun.lock ] || [ -f bun.lockb ]; then npm install -g bun && bun install || true; fi",
        ]
        run = f'CMD ["node", "{entry}"]'
    elif runtime == "php":
        base = "php:8.3-cli"
        install += [
            "apt-get update && apt-get install -y --no-install-recommends git unzip curl ca-certificates && rm -rf /var/lib/apt/lists/*",
            "php -r \"copy('https://getcomposer.org/installer', 'composer-setup.php');\"",
            "php composer-setup.php --install-dir=/usr/local/bin --filename=composer && rm composer-setup.php",
            "if [ -f composer.json ]; then composer install --no-interaction --prefer-dist || true; fi",
            "if [ -f .hosting_packages.txt ]; then composer require $(cat .hosting_packages.txt) --no-interaction || true; fi",
        ]
        run = f'CMD ["php", "{entry}"]'
    elif runtime == "ruby":
        base = "ruby:3.3-slim"
        install += [
            "apt-get update && apt-get install -y --no-install-recommends build-essential git && rm -rf /var/lib/apt/lists/*",
            "if [ -f Gemfile ]; then bundle install; fi",
            "if [ -f .hosting_packages.txt ]; then gem install $(cat .hosting_packages.txt) || true; fi",
        ]
        run = f'CMD ["ruby", "{entry}"]'
    elif runtime == "rust":
        base = "rust:1-bookworm"
        install += [
            "if [ -f Cargo.toml ]; then cargo fetch; fi",
            "if [ -f .hosting_packages.txt ]; then for p in $(cat .hosting_packages.txt); do cargo add \"$p\" || true; done; fi",
        ]
        run = f'CMD ["cargo", "run", "--release"]' if os.path.exists(os.path.join(folder, "Cargo.toml")) else f'CMD ["./{entry}"]'
    elif runtime == "go":
        base = "golang:1.24-bookworm"
        install += ["if [ -f go.mod ]; then go mod download; fi",
                     "if [ -f .hosting_packages.txt ]; then go get $(cat .hosting_packages.txt) || true; fi"]
        run = 'CMD ["go", "run", "."]' if os.path.exists(os.path.join(folder, "go.mod")) else f'CMD ["go", "run", "{entry}"]'
    elif runtime == "java":
        base = "eclipse-temurin:21-jdk"
        install += [
            "apt-get update && apt-get install -y --no-install-recommends maven gradle && rm -rf /var/lib/apt/lists/*",
            "if [ -f pom.xml ]; then mvn -B -DskipTests package || true; fi",
            "if [ -f build.gradle ] || [ -f build.gradle.kts ]; then gradle build -x test || true; fi",
        ]
        run = f'CMD ["java", "-jar", "{entry}"]' if ext == ".jar" else 'CMD ["sh", "-c", "if [ -f target/*.jar ]; then java -jar target/*.jar; else gradle run || true; fi"]'
    elif runtime == "perl":
        base = "perl:5.40"
        install += [
            "apt-get update && apt-get install -y --no-install-recommends build-essential cpanminus && rm -rf /var/lib/apt/lists/*",
            "if [ -f cpanfile ]; then cpanm --installdeps .; fi",
            "if [ -f .hosting_packages.txt ]; then cpanm $(cat .hosting_packages.txt) || true; fi",
        ]
        run = f'CMD ["perl", "{entry}"]'
    elif runtime == "elixir":
        base = "elixir:1.18"
        install += ["if [ -f mix.exs ]; then mix deps.get; fi",
                     "if [ -f .hosting_packages.txt ]; then mix hex.info >/dev/null 2>&1 || true; fi"]
        run = 'CMD ["mix", "run", "--no-halt"]'
    elif runtime == "dart":
        base = "dart:stable"
        install += ["if [ -f pubspec.yaml ]; then dart pub get; fi"]
        run = f'CMD ["dart", "run", "{entry}"]'
    else:
        base = "python:3.12-slim"
        run = f'CMD ["python", "{entry}"]'

    apt_file = os.path.join(folder, "packages.txt")
    if os.path.isfile(apt_file):
        install.insert(0, "apt-get update && xargs -r apt-get install -y --no-install-recommends < packages.txt && rm -rf /var/lib/apt/lists/*")

    lines = [f"FROM {base}", "ENV PYTHONUNBUFFERED=1", "WORKDIR /app",
             "RUN apt-get update >/dev/null 2>&1 || true",
             "RUN " + " && ".join(install) if install else "RUN true",
             "COPY . /app", run]
    # Dependency commands must run after COPY so manifests exist.
    # Rebuild the Dockerfile in a deterministic order.
    header = [f"FROM {base}", "ENV PYTHONUNBUFFERED=1", "WORKDIR /app", "COPY . /app"]
    body = []
    if apt_file and os.path.isfile(apt_file):
        body.append("apt-get update && xargs -r apt-get install -y --no-install-recommends < packages.txt && rm -rf /var/lib/apt/lists/*")
    body += [x for x in install if not x.startswith("apt-get update && apt-get install -y --no-install-recommends") and x not in body]
    # For PHP/Ruby/Java the apt bootstrap is already included in install; keep it.
    return "\n".join(header + ["RUN " + " && ".join(body) if body else "RUN true", run])


async def _start_docker_bot(bot_id, data, folder, entry, log_path):
    """Build and run an isolated per-bot container."""
    if not _docker_available():
        return None

    name = _docker_container_name(bot_id)
    dockerfile = os.path.join(folder, ".hosting.Dockerfile")
    custom = os.path.join(folder, "Dockerfile")

    if not os.path.isfile(custom):
        content = _dockerfile_for_project(folder, entry)
        with open(dockerfile, "w", encoding="utf-8") as f:
            f.write(content + "\n")
        build_file = ".hosting.Dockerfile"
    else:
        build_file = "Dockerfile"

    image = f"tg-hosting:{re.sub(r'[^a-zA-Z0-9_.-]', '_', str(bot_id))}"
    build = await asyncio.to_thread(
        subprocess.run,
        [DOCKER_BIN, "build", "--pull", "-f", build_file, "-t", image, "."],
        cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=DOCKER_TIMEOUT
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n[DOCKER BUILD]\n" + (build.stdout or "") + "\n")
    if build.returncode != 0:
        return False, "Docker image build failed। LOGS দেখুন।"

    subprocess.run([DOCKER_BIN, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = os.environ.copy()
    env["BOT_TOKEN"] = data.get("token", "")
    cmd = [
        DOCKER_BIN, "run", "--rm", "--name", name,
        "--network", DOCKER_NETWORK,
        "--memory", DOCKER_MEMORY, "--cpus", DOCKER_CPUS,
        "--pids-limit", "256",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-e", "BOT_TOKEN",
        image
    ]
    log_file = open(log_path, "a", encoding="utf-8")
    process = subprocess.Popen(cmd, cwd=folder, stdout=log_file, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    RUNNING_PROCESSES[bot_id] = process
    bots[bot_id]["container_name"] = name
    bots[bot_id]["container_image"] = image
    bots[bot_id]["runtime_mode"] = "docker"
    save_json(BOTS_FILE, bots)
    set_bot_status(bot_id, "RUNNING")
    await asyncio.sleep(3)
    if process.poll() is None:
        return True, "Bot Started Successfully (Docker Container)।"
    try:
        log_file.close()
    except Exception:
        pass
    RUNNING_PROCESSES.pop(bot_id, None)
    return False, "Docker Container start হয়নি। LOGS দেখুন।"


# =========================================================
# START HOSTED BOT
# =========================================================

async def _run_dependency_command(command, cwd, log_path, title, timeout=900):
    """Run a project-local dependency manager without modifying the host globally."""
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n[DEPENDENCY] {title}: {' '.join(command)}\n")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(result.stdout or "")
            log_file.write(f"\n[DEPENDENCY] exit={result.returncode}\n")
        return result.returncode == 0
    except FileNotFoundError:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"[DEPENDENCY] command not available: {command[0]}\n")
        return False
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"[DEPENDENCY] ERROR: {e}\n")
        return False


async def _install_project_dependencies(folder, package_dir, log_path, entry_file):
    """Best-effort A-to-Z project dependency bootstrap.

    Supports Python/pip, Node/npm, PHP/Composer, Ruby/Bundler, Rust/Cargo,
    Go modules, Maven and Gradle. It never runs apt as the hosting user because
    apt changes the shared host and is not safe for multi-user hosting.
    """
    ext = os.path.splitext(entry_file)[1].lower()
    found = []

    # Node.js
    package_json = os.path.join(folder, "package.json")
    if os.path.isfile(package_json) and shutil.which("npm"):
        cmd = ["npm", "install", "--no-audit", "--no-fund"]
        if os.path.isfile(os.path.join(folder, "package-lock.json")):
            cmd = ["npm", "ci", "--no-audit", "--no-fund"]
        if await _run_dependency_command(cmd, folder, log_path, "Node.js / npm"):
            found.append("npm")

    # PHP
    composer_json = os.path.join(folder, "composer.json")
    if os.path.isfile(composer_json) and shutil.which("composer"):
        cmd = ["composer", "install", "--no-interaction", "--prefer-dist"]
        if await _run_dependency_command(cmd, folder, log_path, "PHP / Composer"):
            found.append("composer")

    # Ruby
    if os.path.isfile(os.path.join(folder, "Gemfile")) and shutil.which("bundle"):
        cmd = ["bundle", "install", "--path", os.path.join(folder, ".bundle")]
        if await _run_dependency_command(cmd, folder, log_path, "Ruby / Bundler"):
            found.append("bundler")

    # Rust
    if os.path.isfile(os.path.join(folder, "Cargo.toml")) and shutil.which("cargo"):
        if await _run_dependency_command(["cargo", "fetch"], folder, log_path, "Rust / Cargo"):
            found.append("cargo")

    # Go
    if os.path.isfile(os.path.join(folder, "go.mod")) and shutil.which("go"):
        if await _run_dependency_command(["go", "mod", "download"], folder, log_path, "Go modules"):
            found.append("go")

    # Java Maven / Gradle projects
    if os.path.isfile(os.path.join(folder, "pom.xml")):
        mvnw = os.path.join(folder, "mvnw")
        if os.path.isfile(mvnw):
            os.chmod(mvnw, os.stat(mvnw).st_mode | 0o111)
            cmd = [mvnw, "-q", "-DskipTests", "dependency:go-offline"]
        elif shutil.which("mvn"):
            cmd = ["mvn", "-q", "-DskipTests", "dependency:go-offline"]
        else:
            cmd = None
        if cmd and await _run_dependency_command(cmd, folder, log_path, "Java / Maven"):
            found.append("maven")

    gradlew = os.path.join(folder, "gradlew")
    if os.path.isfile(os.path.join(folder, "build.gradle")) or os.path.isfile(os.path.join(folder, "build.gradle.kts")):
        if os.path.isfile(gradlew):
            os.chmod(gradlew, os.stat(gradlew).st_mode | 0o111)
            cmd = [gradlew, "dependencies"]
        elif shutil.which("gradle"):
            cmd = ["gradle", "dependencies"]
        else:
            cmd = None
        if cmd and await _run_dependency_command(cmd, folder, log_path, "Java / Gradle"):
            found.append("gradle")

    with open(log_path, "a", encoding="utf-8") as log_file:
        if found:
            log_file.write("[DEPENDENCY] Installed/loaded: " + ", ".join(found) + "\n")
        else:
            log_file.write("[DEPENDENCY] No additional project manifest/runtime manager was available.\n")
            log_file.write("[DEPENDENCY] System-wide apt packages are intentionally not installed per user.\n")

    return found


async def start_hosted_bot(bot_id):

    if bot_id not in bots:
        return False, "Bot পাওয়া যায়নি।"

    owner_id = bots[bot_id].get("owner_id")
    if is_maintenance() and owner_id != ADMIN_ID:
        return False, "🛠 Maintenance Mode চালু আছে — এখন User Bot চালানো যাবে না।"
    if is_banned(owner_id):
        set_bot_status(bot_id, "BANNED")
        return False, "এই User Banned — Bot চালানো যাবে না।"

    if is_process_running(bot_id):
        return True, "Bot ইতিমধ্যে Running।"

    if bot_id in STARTING_BOTS:
        return False, "Bot Start হচ্ছে। একটু পরে আবার চেষ্টা করুন।"

    data = bots[bot_id]
    folder = os.path.abspath(bot_folder(bot_id))
    main_file = os.path.abspath(bot_entry_file(bot_id))

    if not os.path.exists(main_file):
        legacy_file = bot_main_file(bot_id)
        if os.path.exists(legacy_file):
            main_file = legacy_file
        else:
            return False, "Entry file পাওয়া যাচ্ছে না।"

    STARTING_BOTS.add(bot_id)
    STOP_REQUESTED.discard(bot_id)

    log_path = os.path.abspath(bot_log_file(bot_id))

    # Prefer isolated Docker hosting when Docker is installed. The old local
    # runner remains below as a compatibility fallback.
    if _docker_available():
        try:
            folder = os.path.abspath(bot_folder(bot_id))
            main_file = os.path.abspath(bot_entry_file(bot_id))
            os.makedirs(folder, exist_ok=True)
            docker_result = await _start_docker_bot(
                bot_id, data, folder, os.path.basename(main_file), log_path
            )
            if docker_result is not None:
                return docker_result
        except Exception as e:
            logger.exception("Docker start failed: %s", e)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n[DOCKER ERROR] " + str(e) + "\n")
            # Fall through to the existing local runner.
    package_dir = os.path.abspath(os.path.join(folder, ".packages"))

    try:
        os.makedirs(folder, exist_ok=True)
        os.makedirs(package_dir, exist_ok=True)

        # Each startup gets a clear marker. Runtime auto-repair reads only the
        # log generated after the current process was launched, so an old
        # "requests" error can never hide a newer "firebase_admin" error.
        startup_log = _read_log(log_path)
        startup_offset = len(startup_log)

        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write("\n\n==============================\n")
            log_file.write(f"START {time.ctime()}\n")
            log_file.write("==============================\n")
            log_file.flush()

        requirements = os.path.abspath(bot_requirements_file(bot_id))

        # Multi-language dependency bootstrap. Existing Python requirements
        # handling remains unchanged; this adds npm/composer/bundler/cargo/go/
        # Maven/Gradle support for projects that ship their own manifest.
        await _install_project_dependencies(folder, package_dir, log_path, main_file)

        if bot_id in STOP_REQUESTED or is_banned(owner_id):
            set_bot_status(bot_id, "BANNED" if is_banned(owner_id) else "STOPPED")
            return False, "Bot Start বাতিল করা হয়েছে।"

        # 1) User supplied requirements first.
        if os.path.isfile(requirements) and os.path.getsize(requirements) > 0:
            await _install_requirements_resilient(
                requirements, package_dir, log_path
            )

        env = os.environ.copy()
        env["BOT_TOKEN"] = data.get("token", "")

        old_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            package_dir + os.pathsep + old_pythonpath
            if old_pythonpath else package_dir
        )

        ext = os.path.splitext(main_file)[1].lower()

        if ext == ".py":
            command = [sys.executable, main_file]
        elif ext in (".js", ".mjs", ".cjs"):
            command = ["node", main_file]
        elif ext in (".sh", ".bash"):
            command = ["bash", main_file]
        elif ext == ".php":
            command = ["php", main_file]
        elif ext == ".rb":
            command = ["ruby", main_file]
        elif ext == ".pl":
            command = ["perl", main_file]
        elif ext == ".jar":
            command = ["java", "-jar", main_file]
        else:
            return False, "এই File-এর runtime server-এ পাওয়া যায়নি।"

        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"Runtime command: {' '.join(command)}\n")
            log_file.flush()

        # 2) Python preflight: inspect imports BEFORE starting the bot.
        # This prevents the old "requests -> restart -> firebase_admin -> stop"
        # behavior for normal/static imports.
        if ext == ".py":
            await _preinstall_python_imports(main_file, package_dir, log_path)

        # 3) Runtime repair remains as a safety net for dynamic imports and
        # packages that cannot be discovered statically.
        max_auto_repairs = 50 if ext == ".py" else 0
        attempted_packages = set()

        for attempt in range(max_auto_repairs + 1):
            if bot_id in STOP_REQUESTED or is_banned(owner_id):
                set_bot_status(bot_id, "BANNED" if is_banned(owner_id) else "STOPPED")
                return False, "Bot Start বাতিল করা হয়েছে।"

            attempt_log_offset = len(_read_log(log_path))
            log_file = open(log_path, "a", encoding="utf-8")
            process = subprocess.Popen(
                command,
                cwd=folder,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True
            )

            RUNNING_PROCESSES[bot_id] = process
            set_bot_status(bot_id, "RUNNING")

            # A high repair limit is only a safety net; a healthy bot exits
            # this loop immediately because it stays alive.
            await asyncio.sleep(2.0)

            if process.poll() is None:
                return True, "Bot Started Successfully."

            try:
                log_file.close()
            except Exception:
                pass

            RUNNING_PROCESSES.pop(bot_id, None)
            set_bot_status(bot_id, "STOPPED")

            # Read ONLY the current failed attempt, not old tracebacks.
            full_log = _read_log(log_path)
            fresh_log = full_log[attempt_log_offset:]
            missing_module = _extract_missing_module(fresh_log)

            if not missing_module:
                set_bot_status(bot_id, "ERROR")
                return False, "Bot Start হয়নি। LOGS দেখুন।"

            package_name = _pip_name_for_module(missing_module)

            if package_name in attempted_packages:
                set_bot_status(bot_id, "ERROR")
                return False, (
                    f"Auto package repair বারবার একই package-এ আটকে গেছে: "
                    f"{package_name}. LOGS দেখুন।"
                )

            attempted_packages.add(package_name)

            with open(log_path, "a", encoding="utf-8") as log_file2:
                log_file2.write(
                    f"\n[AUTO DETECT] Missing module: {missing_module} -> {package_name}\n"
                )

            if _module_available(missing_module, package_dir):
                ok = True
            else:
                ok = await _pip_install_one(package_dir, package_name, log_path)
            if not ok:
                set_bot_status(bot_id, "ERROR")
                return False, (
                    f"Auto package install failed: {package_name}. LOGS দেখুন।"
                )

            # Retry automatically with the newly installed package.

        set_bot_status(bot_id, "ERROR")
        return False, "Bot Start হয়নি। Auto package limit শেষ হয়েছে; LOGS দেখুন।"

    except subprocess.TimeoutExpired:
        set_bot_status(bot_id, "ERROR")
        return False, "Package install timeout হয়েছে। LOGS দেখুন।"

    except Exception as e:
        logger.error("Start bot error: %s", e)
        set_bot_status(bot_id, "ERROR")
        return False, str(e)
    finally:
        STARTING_BOTS.discard(bot_id)


# =========================================================
# STOP HOSTED BOT
# =========================================================

async def stop_hosted_bot(bot_id, force=False):

    # Mark first, so an in-progress startup/auto-repair cannot launch it again.
    STOP_REQUESTED.add(bot_id)

    process = RUNNING_PROCESSES.get(
        bot_id
    )

    if not process:
        container_name = bots.get(bot_id, {}).get("container_name")
        if container_name and _docker_available():
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [DOCKER_BIN, "rm", "-f", str(container_name)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=20
                )
            except Exception:
                pass

        set_bot_status(
            bot_id,
            "BANNED" if is_banned(bots.get(bot_id, {}).get("owner_id")) else "STOPPED"
        )

        return True, "Bot already stopped."

    try:

        if process.poll() is None:

            try:

                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGTERM
                )

            except Exception:

                process.terminate()

            try:

                process.wait(
                    timeout=5
                )

            except subprocess.TimeoutExpired:

                try:

                    os.killpg(
                        os.getpgid(process.pid),
                        signal.SIGKILL
                    )

                except Exception:

                    process.kill()

        RUNNING_PROCESSES.pop(
            bot_id,
            None
        )

        container_name = bots.get(bot_id, {}).get("container_name")
        if container_name and _docker_available():
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [DOCKER_BIN, "rm", "-f", str(container_name)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=20
                )
            except Exception:
                pass

        set_bot_status(
            bot_id,
            "BANNED" if is_banned(bots.get(bot_id, {}).get("owner_id")) else "STOPPED"
        )

        return True, "Bot Stopped."

    except Exception as e:

        logger.error(
            "Stop bot error: %s",
            e
        )

        return False, str(e)


# =========================================================
# RESTART
# =========================================================

async def restart_hosted_bot(bot_id):

    await stop_hosted_bot(bot_id)
    await asyncio.sleep(1)
    if bot_id in bots and is_banned(bots[bot_id].get("owner_id")):
        return False, "এই User Banned — Bot চালানো যাবে না।"
    STOP_REQUESTED.discard(bot_id)
    return await start_hosted_bot(bot_id)


# =========================================================
# BOT LIST
# =========================================================

async def my_bots(update):

    user_id = update.effective_user.id

    mybots = get_user_bots(
        user_id
    )

    if not mybots:

        await update.message.reply_text(
            "🤖 <b>আমার বট</b>\n\n"
            "আপনার কোনো Hosted Bot নেই।\n\n"
            "প্রথমে main.py Upload করুন।",
            parse_mode="HTML"
        )

        return

    buttons = []

    for bot_id, data in mybots:

        running = is_process_running(
            bot_id
        )

        if is_banned(data.get("owner_id")):
            status = "🚫 BANNED"
        else:
            status = "🟢 RUNNING" if running else "🔴 STOPPED"

        buttons.append([
            InlineKeyboardButton(
                f"🤖 {data.get('name', bot_id)} • {status}",
                callback_data=f"botinfo:{bot_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔃 REFRESH",
            callback_data="refresh_bots"
        )
    ])

    await update.message.reply_text(
        "🤖 <b>MY BOTS</b>\n\n"
        "আপনার Hosted Bot নির্বাচন করুন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def my_bots_callback(query):

    user_id = query.from_user.id

    mybots = get_user_bots(
        user_id
    )

    if not mybots:

        await query.edit_message_text(
            "🤖 আপনার কোনো Hosted Bot নেই।"
        )

        return

    buttons = []

    for bot_id, data in mybots:

        running = is_process_running(
            bot_id
        )

        if is_banned(data.get("owner_id")):
            status = "🚫 BANNED"
        else:
            status = "🟢 RUNNING" if running else "🔴 STOPPED"

        buttons.append([
            InlineKeyboardButton(
                f"🤖 {data.get('name', bot_id)} • {status}",
                callback_data=f"botinfo:{bot_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔃 REFRESH",
            callback_data="refresh_bots"
        )
    ])

    await query.edit_message_text(
        "🤖 <b>MY BOTS</b>\n\n"
        "আপনার Hosted Bot নির্বাচন করুন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# BOT INFO
# =========================================================

async def bot_info(query, bot_id):

    user_id = query.from_user.id

    if bot_id not in bots:

        await query.edit_message_text(
            "❌ Bot পাওয়া যায়নি।"
        )

        return

    data = bots[bot_id]

    if data.get("owner_id") != user_id and user_id != ADMIN_ID:

        await query.answer(
            "❌ এটি আপনার Bot নয়।",
            show_alert=True
        )

        return

    running = is_process_running(
        bot_id
    )

    if is_banned(data.get("owner_id")):
        status = "🚫 BANNED"
    elif running:
        status = "🟢 RUNNING"
    else:
        status = "🔴 STOPPED"

    keyboard = [
        [
            InlineKeyboardButton(
                "▶️ RUN",
                callback_data=f"runbot:{bot_id}"
            ),
            InlineKeyboardButton(
                "⏹ STOP",
                callback_data=f"stopbot:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 RESTART",
                callback_data=f"restartbot:{bot_id}"
            ),
            InlineKeyboardButton(
                "📜 LOGS",
                callback_data=f"logs:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 DELETE",
                callback_data=f"deletebot:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔃 REFRESH",
                callback_data=f"botinfo:{bot_id}"
            ),
            InlineKeyboardButton(
                "🔙 BACK",
                callback_data="refresh_bots"
            )
        ]
    ]

    await query.edit_message_text(
        "🤖 <b>BOT CONTROL PANEL</b>\n\n"
        f"📛 Name: <code>{data.get('name')}</code>\n"
        f"🆔 ID: <code>{bot_id}</code>\n"
        f"📄 File: <code>{data.get('main_file')}</code>\n"
        f"📊 Status: {status}\n\n"
        "নিচের Button ব্যবহার করুন।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# BOT LOGS
# =========================================================

async def show_logs(query, bot_id):

    if bot_id not in bots:

        await query.edit_message_text(
            "❌ Bot পাওয়া যায়নি।"
        )

        return

    data = bots[bot_id]

    if (
        data.get("owner_id") != query.from_user.id
        and query.from_user.id != ADMIN_ID
    ):

        await query.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    path = bot_log_file(
        bot_id
    )

    if not os.path.exists(path):

        logs = "📜 কোনো Log এখনো নেই।"

    else:

        try:

            with open(
                path,
                "r",
                encoding="utf-8",
                errors="ignore"
            ) as f:

                content = f.read()

            logs = content[-3500:]

            if not logs:

                logs = "📜 Log empty."

        except Exception as e:

            logs = str(e)

    text = (
        "📜 <b>BOT LOGS</b>\n\n"
        "<pre>"
        + logs.replace("<", "&lt;").replace(">", "&gt;")
        + "</pre>"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "🔃 REFRESH LOGS",
                callback_data=f"logs:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 BACK",
                callback_data=f"botinfo:{bot_id}"
            )
        ]
    ]

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# DELETE BOT
# =========================================================

async def delete_bot(query, bot_id):

    if bot_id not in bots:

        await query.edit_message_text(
            "❌ Bot পাওয়া যায়নি।"
        )

        return

    data = bots[bot_id]

    if (
        data.get("owner_id") != query.from_user.id
        and query.from_user.id != ADMIN_ID
    ):

        await query.answer(
            "❌ Access denied.",
            show_alert=True
        )

        return

    await stop_hosted_bot(
        bot_id
    )

    folder = bot_folder(
        bot_id
    )

    try:

        if os.path.exists(folder):

            shutil.rmtree(
                folder
            )

    except Exception as e:

        logger.error(
            "Delete bot folder error: %s",
            e
        )

    bots.pop(
        bot_id,
        None
    )

    save_json(
        BOTS_FILE,
        bots
    )

    await query.edit_message_text(
        "🗑 <b>BOT DELETED</b>\n\n"
        "Bot এবং তার Files মুছে দেওয়া হয়েছে।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🤖 MY BOTS",
                    callback_data="refresh_bots"
                )
            ]
        ])
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(update, context):

    query = update.callback_query
    touch_user(query.from_user)

    if not is_admin(query.from_user.id) and is_banned(query.from_user.id):
        await query.answer("🚫 আপনার Account Banned", show_alert=True)
        return

    await query.answer()

    data = query.data

    # Maintenance ON/OFF controls
    if data == "maintain:on":
        # ON = hosting চালু; বন্ধ থাকা Botগুলো নিজে থেকে restart হবে না
        await maintain_set_callback(query, False)
        return

    if data == "maintain:off":
        # OFF = সব Hosted Bot বন্ধ + user Run/Restart বন্ধ
        await maintain_set_callback(query, True)
        return

    if data == "maintain:back":
        await query.edit_message_text(
            "👑 <b>ADMIN PANEL</b>\n\nস্বাগতম Admin!",
            parse_mode="HTML"
        )
        await query.message.chat.send_message(
            "👑 <b>ADMIN PANEL</b>\n\nস্বাগতম Admin!",
            parse_mode="HTML",
            reply_markup=admin_keyboard()
        )
        return

    if data.startswith("lang:"):
        lang = data.split(":", 1)[1]
        set_user_language(query.from_user.id, lang)
        if lang == "bn":
            await query.edit_message_text("🇧🇩 <b>বাংলা ভাষা নির্বাচন করা হয়েছে।</b>\n\nএখন থেকে User Panel বাংলা থাকবে।", parse_mode="HTML")
        else:
            await query.edit_message_text("🇬🇧 <b>English selected.</b>\n\nThe User Panel is now in English.", parse_mode="HTML")
        await query.message.chat.send_message(
            welcome_text(query.from_user, lang),
            parse_mode="HTML",
            reply_markup=user_keyboard(lang)
        )
        return

    if is_maintenance() and not is_admin(query.from_user.id):
        await query.answer("🛠 Maintenance Mode — Service এখন বন্ধ আছে।", show_alert=True)
        return

    # Force join
    if data == "check_join":

        await check_join(
            update,
            context
        )

        return

    # Skip package selection: start with zero user-supplied requirements.
    if data.startswith("skip_packages:"):
        bot_id = data.split(":", 1)[1]
        if bot_id not in bots or str(bots[bot_id].get("owner_id")) != str(query.from_user.id):
            await query.edit_message_text("❌ এই Bot-এর Access আপনার নেই।")
            return
        context.user_data.pop("package_bot_id", None)
        await query.edit_message_text(
            "⏭️ <b>Package Skip করা হয়েছে</b>\n\n"
            "⚙️ Bot চালু করা হচ্ছে...\n"
            "Missing Package পাওয়া গেলে system নিজে Install করার চেষ্টা করবে।",
            parse_mode="HTML"
        )
        success, message = await start_hosted_bot(bot_id)
        try:
            await query.message.reply_text(
                ("🟢 " if success else "❌ ") + message,
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # TS Ai controls
    if data == "tsai_new":
        TS_AI_SESSIONS.pop(str(query.from_user.id), None)
        await query.message.reply_text("🧹 TS Ai-এর নতুন chat শুরু হয়েছে। আপনার প্রশ্ন পাঠান।" if get_user_language(query.from_user.id) == "bn" else "🧹 A new TS Ai chat has started. Send your question.")
        return

    # My files
    if data == "my_files":

        await show_my_files_callback(
            query
        )

        return

    if data == "refresh_files":

        await show_my_files_callback(
            query
        )

        return

    # File info
    if data.startswith("fileinfo:"):

        _, uid, index = data.split(
            ":",
            2
        )

        await file_info(
            query,
            uid,
            index
        )

        return

    # Delete file
    if data.startswith("deletefile:"):

        _, uid, filename = data.split(
            ":",
            2
        )

        await delete_file(
            query,
            uid,
            filename
        )

        return

    # Host file
    if data.startswith("hostfile:"):

        _, uid, filename = data.split(
            ":",
            2
        )

        await host_file(
            query,
            context,
            uid,
            filename
        )

        return

    # My bots
    if data == "refresh_bots":

        await my_bots_callback(
            query
        )

        return

    # Bot info
    if data.startswith("botinfo:"):

        bot_id = data.split(
            ":",
            1
        )[1]

        await bot_info(
            query,
            bot_id
        )

        return

    # Run
    if data.startswith("runbot:"):

        bot_id = data.split(
            ":",
            1
        )[1]

        if bot_id not in bots:

            await query.edit_message_text(
                "❌ Bot পাওয়া যায়নি।"
            )

            return

        if (
            bots[bot_id].get("owner_id")
            != query.from_user.id
            and query.from_user.id != ADMIN_ID
        ):

            await query.answer(
                "❌ Access denied.",
                show_alert=True
            )

            return

        if is_banned(bots[bot_id].get("owner_id")):
            await query.answer("🚫 User Banned — Bot চালানো যাবে না।", show_alert=True)
            return

        if is_maintenance() and query.from_user.id != ADMIN_ID:
            await query.answer("🛠 Maintenance Mode — এখন Bot চালানো যাবে না।", show_alert=True)
            return

        STOP_REQUESTED.discard(bot_id)
        success, message = await start_hosted_bot(
            bot_id
        )

        await query.edit_message_text(
            ("🟢 " if success else "❌ ")
            + message,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔃 REFRESH",
                        callback_data=f"botinfo:{bot_id}"
                    )
                ]
            ])
        )

        return

    # Stop
    if data.startswith("stopbot:"):

        bot_id = data.split(
            ":",
            1
        )[1]

        if bot_id not in bots:

            await query.edit_message_text(
                "❌ Bot পাওয়া যায়নি।"
            )

            return

        if (
            bots[bot_id].get("owner_id")
            != query.from_user.id
            and query.from_user.id != ADMIN_ID
        ):

            await query.answer(
                "❌ Access denied.",
                show_alert=True
            )

            return

        success, message = await stop_hosted_bot(
            bot_id
        )

        await query.edit_message_text(
            ("⏹ " if success else "❌ ")
            + message,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔃 REFRESH",
                        callback_data=f"botinfo:{bot_id}"
                    )
                ]
            ])
        )

        return

    # Restart
    if data.startswith("restartbot:"):

        bot_id = data.split(
            ":",
            1
        )[1]

        if bot_id not in bots:

            await query.edit_message_text(
                "❌ Bot পাওয়া যায়নি।"
            )

            return

        if (
            bots[bot_id].get("owner_id")
            != query.from_user.id
            and query.from_user.id != ADMIN_ID
        ):

            await query.answer(
                "❌ Access denied.",
                show_alert=True
            )

            return

        if is_maintenance() and query.from_user.id != ADMIN_ID:
            await query.answer("🛠 Maintenance Mode — এখন Bot Restart করা যাবে না।", show_alert=True)
            return

        success, message = await restart_hosted_bot(
            bot_id
        )

        await query.edit_message_text(
            ("🔄 " if success else "❌ ")
            + message,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔃 REFRESH",
                        callback_data=f"botinfo:{bot_id}"
                    )
                ]
            ])
        )

        return

    # Logs
    if data.startswith("logs:"):

        bot_id = data.split(
            ":",
            1
        )[1]

        await show_logs(
            query,
            bot_id
        )

        return

    # Delete bot
    if data.startswith("deletebot:"):

        bot_id = data.split(
            ":",
            1
        )[1]

        await delete_bot(
            query,
            bot_id
        )

        return


# =========================================================
# PROFILE
# =========================================================

async def profile(update):

    user = update.effective_user

    uid = str(user.id)

    mybots = get_user_bots(
        user.id
    )

    files = uploaded_files.get(
        uid,
        []
    )

    username = (
        f"@{user.username}"
        if user.username
        else "No Username"
    )

    await update.message.reply_text(
        "👤 <b>MY PROFILE</b>\n\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Name: {user.full_name}\n"
        f"🔗 Username: {username}\n"
        f"📁 Files: {len(files)}\n"
        f"🤖 Hosted Bots: {len(mybots)}",
        parse_mode="HTML"
    )


# =========================================================
# UPDATE CHANNEL
# =========================================================

async def update_channel(update):

    if not channels:

        await update.message.reply_text(
            "📢 বর্তমানে কোনো Update Channel নেই।"
        )

        return

    buttons = []

    for channel in channels:

        username = channel.replace(
            "@",
            ""
        )

        buttons.append([
            InlineKeyboardButton(
                f"📢 {channel}",
                url=f"https://t.me/{username}"
            )
        ])

    await update.message.reply_text(
        "📢 <b>UPDATE CHANNELS</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# DEPOSIT / PREMIUM / SUPPORT
# =========================================================

async def deposit(update):

    await update.message.reply_text(
        "💰 <b>DEPOSIT</b>\n\n"
        "Deposit করার জন্য Admin Support-এর সাথে যোগাযোগ করুন।\n\n"
        f"💬 {SUPPORT_USERNAME}",
        parse_mode="HTML"
    )


async def premium(update):

    await update.message.reply_text(
        "🌟 <b>PREMIUM PLAN</b>\n\n"
        "💎 Premium Hosting সুবিধার জন্য Support-এ যোগাযোগ করুন।\n\n"
        f"💬 {SUPPORT_USERNAME}",
        parse_mode="HTML"
    )


async def support(update):

    await update.message.reply_text(
        "💬 <b>SUPPORT</b>\n\n"
        f"Admin Support: {SUPPORT_USERNAME}",
        parse_mode="HTML"
    )


# =========================================================
# USER HOST PERMISSION
# =========================================================

async def user_host_permission_request(update, context):

    if not is_admin(update.effective_user.id):
        return

    context.user_data.pop("host_permission_user_id", None)
    context.user_data.pop("host_permission_limit", None)
    context.user_data.pop("host_permission_revoke_id", None)

    keyboard = [
        ["➕ Give Host Permission"],
        ["📋 Permission List"],
        ["♻️ Remove Host Permission"],
        ["🔙 Admin Panel"],
    ]

    await update.message.reply_text(
        "👤 <b>USER HOST PERMISSION</b>\n\n"
        "এখান থেকে User-এর Host Permission দেওয়া, দেখা এবং তুলে নেওয়া যাবে।",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def handle_user_host_permission(update, context):

    if not is_admin(update.effective_user.id):
        return False

    text = (update.message.text or "").strip()

    if text == "➕ Give Host Permission":
        context.user_data["host_permission_user_id"] = True
        context.user_data.pop("host_permission_limit", None)
        await update.message.reply_text(
            "🆔 <b>User ID দিন</b>\n\nউদাহরণ: <code>123456789</code>",
            parse_mode="HTML"
        )
        return True

    if text == "📋 Permission List":
        await update.message.reply_text(
            permission_list_text(),
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                [["➕ Give Host Permission"], ["♻️ Remove Host Permission"], ["🔙 Admin Panel"]],
                resize_keyboard=True
            )
        )
        return True

    if text == "♻️ Remove Host Permission":
        context.user_data["host_permission_revoke_id"] = True
        await update.message.reply_text(
            "♻️ <b>Permission Change</b>\n\n"
            "যে User-এর Host Permission পরিবর্তন করতে চান, তার Telegram ID দিন।",
            parse_mode="HTML"
        )
        return True

    if text == "🔙 Admin Panel":
        await admin_panel(update)
        return True

    if context.user_data.get("host_permission_revoke_id"):
        if not text.isdigit():
            await update.message.reply_text("❌ সঠিক Telegram User ID দিন।")
            return True
        target_id = int(text)
        context.user_data["host_permission_revoke_target_id"] = target_id
        context.user_data.pop("host_permission_revoke_id", None)
        current = get_user_host_limit(target_id)
        context.user_data["host_permission_revoke_limit"] = True
        await update.message.reply_text(
            "🔢 <b>NEW HOST PERMISSION</b>\n\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"📌 Current Limit: <b>{current}টি Bot</b>\n\n"
            "এখন এই User কতটি Bot Host করতে পারবে সেটি লিখুন।\n"
            "যেমন: <code>1</code>, <code>2</code>, <code>3</code>, <code>10</code>, <code>50</code>",
            parse_mode="HTML"
        )
        return True

    if context.user_data.get("host_permission_revoke_limit"):
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("❌ ১ বা তার বেশি একটি সংখ্যা দিন।")
            return True
        target_id = context.user_data.get("host_permission_revoke_target_id")
        limit = int(text)
        set_user_host_limit(target_id, limit)
        context.user_data.pop("host_permission_revoke_limit", None)
        context.user_data.pop("host_permission_revoke_target_id", None)
        await update.message.reply_text(
            "✅ <b>HOST PERMISSION UPDATED</b>\n\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"🤖 New Host Permission: <b>{limit}টি Bot</b>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                [["➕ Give Host Permission"], ["📋 Permission List"], ["♻️ Remove Host Permission"], ["🔙 Admin Panel"]],
                resize_keyboard=True
            )
        )
        return True

    if context.user_data.get("host_permission_user_id"):
        if not text.isdigit():
            await update.message.reply_text(
                "❌ সঠিক Telegram User ID দিন।\nউদাহরণ: <code>123456789</code>",
                parse_mode="HTML"
            )
            return True

        target_id = int(text)
        context.user_data["host_permission_target_id"] = target_id
        context.user_data.pop("host_permission_user_id", None)
        context.user_data["host_permission_limit"] = True
        current = get_user_host_limit(target_id)
        await update.message.reply_text(
            "🔢 <b>HOST PERMISSION</b>\n\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"📌 Current Limit: <b>{current}</b>\n\n"
            "এই User কতটি Bot Host করতে পারবে?\n"
            "যেমন: <code>2</code>, <code>10</code>, <code>50</code>",
            parse_mode="HTML"
        )
        return True

    if context.user_data.get("host_permission_limit"):
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text(
                "❌ ১ বা তার বেশি একটি সংখ্যা দিন।",
                parse_mode="HTML"
            )
            return True
        target_id = context.user_data.get("host_permission_target_id")
        limit = int(text)
        set_user_host_limit(target_id, limit)
        context.user_data.pop("host_permission_limit", None)
        context.user_data.pop("host_permission_target_id", None)
        await update.message.reply_text(
            "✅ <b>HOST PERMISSION UPDATED</b>\n\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"🤖 Host Permission: <b>{limit}টি Bot</b>",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(
                [["➕ Give Host Permission"], ["📋 Permission List"], ["♻️ Remove Host Permission"], ["🔙 Admin Panel"]],
                resize_keyboard=True
            )
        )
        return True

    return False


# =========================================================
# USER BAN / UNBAN
# =========================================================

async def user_ban_request(update, context):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.pop("ban_action", None)
    context.user_data["ban_action"] = True
    await update.message.reply_text(
        "🚫 <b>USER BAN / UNBAN</b>\n\n"
        "User-এর Telegram ID দিন।\n\n"
        "User banned থাকলে → Unban হবে।\n"
        "Banned না থাকলে → Ban হবে।",
        parse_mode="HTML"
    )


async def handle_user_ban(update, context):
    if not is_admin(update.effective_user.id) or not context.user_data.get("ban_action"):
        return False
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("❌ সঠিক Telegram User ID দিন।")
        return True
    target_id = int(text)
    if target_id == ADMIN_ID:
        await update.message.reply_text("❌ Admin-কে Ban করা যাবে না।")
        context.user_data.pop("ban_action", None)
        return True
    if is_banned(target_id):
        set_user_banned(target_id, False)
        msg = "♻️ <b>USER UNBANNED</b>"
        status = "🟢 এখন User আবার Bot ব্যবহার করতে পারবে।"
    else:
        set_user_banned(target_id, True)
        stopped = await stop_all_user_bots(target_id)
        msg = "🚫 <b>USER BANNED</b>"
        status = (
            "🔴 এখন User Bot ব্যবহার করতে পারবে না।\n"
            f"⏹ Hosted Bot বন্ধ করা হয়েছে: <b>{len(stopped)}</b>টি"
        )
    context.user_data.pop("ban_action", None)
    await update.message.reply_text(
        f"{msg}\n\n🆔 User ID: <code>{target_id}</code>\n{status}",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )
    return True


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_panel(update):

    if not is_admin(
        update.effective_user.id
    ):
        return

    await update.message.reply_text(
        "👑 <b>ADMIN PANEL</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


# =========================================================
# STATISTICS
# =========================================================

async def statistics(update):

    if not is_admin(
        update.effective_user.id
    ):
        return

    total_users = len(users)

    total_files = sum(
        len(x)
        for x in uploaded_files.values()
    )

    total_bots = len(bots)

    running = 0

    for bot_id in bots:

        if is_process_running(
            bot_id
        ):

            running += 1

    await update.message.reply_text(
        "📊 <b>STATISTICS</b>\n\n"
        f"👥 Users: {total_users}\n"
        f"🤖 Hosted Bots: {total_bots}\n"
        f"🟢 Running Bots: {running}\n"
        f"📁 Files: {total_files}\n"
        f"📢 Required Channels: {len(channels)}",
        parse_mode="HTML"
    )


# =========================================================
# USER LIST
# =========================================================

async def user_list(update):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not users:

        await update.message.reply_text(
            "👥 কোনো User নেই।"
        )

        return

    now = int(time.time())
    text = "👥 <b>USER LIST</b>\n\n"

    for i, (uid, data) in enumerate(users.items(), 1):

        name = html.escape(str(data.get("name", "Unknown")))
        username = data.get("username") or "No Username"
        username = html.escape(str(username))

        last_seen = int(data.get("last_seen", 0) or 0)
        active = bool(last_seen and (now - last_seen) <= 600)
        active_text = "🟢 ACTIVE" if active else "⚪ INACTIVE"

        user_bots = [
            (bot_id, bot_data)
            for bot_id, bot_data in bots.items()
            if str(bot_data.get("owner_id")) == str(uid)
        ]
        running_bots = [
            (bot_id, bot_data)
            for bot_id, bot_data in user_bots
            if is_process_running(bot_id)
        ]

        text += (
            f"{i}. 👤 <b>{name}</b>\n"
            f"   🆔 <code>{data.get('id', uid)}</code>\n"
            f"   🔗 @{username}\n"
            f"   {active_text}\n"
            f"   🤖 Bots: {len(user_bots)} | 🟢 Running: {len(running_bots)}\n"
            f"   📁 Files: {len(uploaded_files.get(str(uid), []))}\n\n"
        )

        if len(text) > 3600:
            text += "\n… আরও User আছে।"
            break

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# =========================================================
# ALL FILES ADMIN
# =========================================================

async def all_files(update):

    if not is_admin(
        update.effective_user.id
    ):
        return

    total = sum(
        len(x)
        for x in uploaded_files.values()
    )

    if total == 0:

        await update.message.reply_text(
            "📁 <b>ALL FILES</b>\n\n"
            "Total Files: 0",
            parse_mode="HTML"
        )
        return

    text = "📁 <b>ALL FILES</b>\n\n"
    count = 0

    for uid, files in uploaded_files.items():

        user = users.get(str(uid), {})
        name = html.escape(str(user.get("name", "Unknown User")))
        username = user.get("username") or "No Username"
        username = html.escape(str(username))

        for item in files:

            count += 1
            filename = html.escape(str(item.get("name", "Unknown")))
            size = item.get("size", 0) or 0
            uploaded = item.get("uploaded", 0) or 0

            if uploaded:
                upload_time = time.strftime(
                    "%Y-%m-%d %H:%M",
                    time.localtime(uploaded)
                )
            else:
                upload_time = "Unknown"

            text += (
                f"<b>{count}. 📄 {filename}</b>\n"
                f"   👤 {name}\n"
                f"   🔗 @{username}\n"
                f"   🆔 <code>{uid}</code>\n"
                f"   📦 {size} bytes\n"
                f"   🕐 {upload_time}\n\n"
            )

            if len(text) > 3600:
                text += "\n… আরও File আছে।"
                await update.message.reply_text(
                    text,
                    parse_mode="HTML"
                )
                return

    text = f"📁 <b>ALL FILES</b>\n\nTotal Files: {total}\n\n" + text.split("\n\n", 1)[1]

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# =========================================================
# ALL BOTS ADMIN
# =========================================================

async def all_bots_admin(update):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not bots:

        await update.message.reply_text(
            "🤖 কোনো Hosted Bot নেই।"
        )

        return

    text = "🤖 <b>ALL HOSTED BOTS</b>\n\n"

    for bot_id, data in bots.items():

        status = (
            "🟢 RUNNING"
            if is_process_running(bot_id)
            else "🔴 STOPPED"
        )

        bot_username = data.get("bot_username") or data.get("name") or "No Username"
        bot_username = html.escape(str(bot_username))

        text += (
            f"🤖 {bot_username}\n"
            f"🆔 <code>{bot_id}</code>\n"
            f"👤 Owner: <code>{data.get('owner_id')}</code>\n"
            f"📊 {bot_username}\n\n"
        )

    await update.message.reply_text(
        text[:4000],
        parse_mode="HTML"
    )


# =========================================================
# CHANNEL MANAGEMENT
# =========================================================

async def manage_channels(update):

    if not is_admin(
        update.effective_user.id
    ):
        return

    await update.message.reply_text(
        "📢 <b>MANAGE CHANNELS</b>",
        parse_mode="HTML",
        reply_markup=channel_keyboard()
    )


async def add_channel_request(update, context):

    if not is_admin(
        update.effective_user.id
    ):
        return

    context.user_data["adding_channel"] = True

    await update.message.reply_text(
        "➕ Channel Username পাঠান।\n\n"
        "উদাহরণ:\n"
        "<code>@mychannel</code>",
        parse_mode="HTML"
    )


async def remove_channel_request(update, context):

    if not is_admin(
        update.effective_user.id
    ):
        return

    context.user_data["removing_channel"] = True

    await update.message.reply_text(
        "🗑 Remove করার Channel Username পাঠান।",
        parse_mode="HTML"
    )


async def channel_list(update):

    if not is_admin(
        update.effective_user.id
    ):
        return

    if not channels:

        await update.message.reply_text(
            "📋 কোনো Channel নেই।"
        )

        return

    text = "📋 <b>REQUIRED CHANNELS</b>\n\n"

    for i, channel in enumerate(
        channels,
        1
    ):

        text += f"{i}. {channel}\n"

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# =========================================================
# BROADCAST
# =========================================================

async def broadcast_request(update, context):

    if not is_admin(
        update.effective_user.id
    ):
        return

    context.user_data["broadcasting"] = True

    await update.message.reply_text(
        "📢 <b>BROADCAST</b>\n\n"
        "এখন Broadcast Message পাঠান।",
        parse_mode="HTML"
    )


async def do_broadcast(update, context):

    if not is_admin(
        update.effective_user.id
    ):
        return False

    if not context.user_data.get(
        "broadcasting"
    ):
        return False

    message = update.message

    success = 0
    failed = 0

    for uid in list(users.keys()):

        try:

            await message.copy(
                chat_id=int(uid)
            )

            success += 1

        except Exception:

            failed += 1

    context.user_data[
        "broadcasting"
    ] = False

    await update.message.reply_text(
        "✅ <b>BROADCAST COMPLETE</b>\n\n"
        f"✅ Sent: {success}\n"
        f"❌ Failed: {failed}",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )

    return True


# =========================================================
# TEXT HANDLER
# =========================================================

def clear_pending_actions(context):
    """Cancel any pending input flow when the user taps a normal menu button.

    This prevents a previous action (for example Bot Token input) from
    hijacking the next menu button the user presses.
    """
    keys = [
        "waiting_bot_username",
        "waiting_upload",
        "upload_bot_username",
        "package_bot_id",
        "hosting_file",
        "adding_channel",
        "removing_channel",
        "broadcasting",
        "ban_action",
        "host_permission_user_id",
        "host_permission_limit",
        "host_permission_target_id",
        "host_permission_revoke_id",
        "host_permission_revoke_target_id",
        "host_permission_revoke_limit",
        "ts_ai_chat",
    ]
    for key in keys:
        context.user_data.pop(key, None)


def is_navigation_button(text, user_id):
    """Return True for normal keyboard navigation buttons."""
    user_buttons = {
        "📢 আপডেট চ্যানেল", "📤 ফাইল আপলোড করুন", "📁 ফাইল দেখুন",
        "🤖 আমার বট", "💰 ডিপোজিট", "👤 আমার প্রোফাইল",
        "👑 প্রিমিয়াম প্লান 🌟", "🤖 TS Ai", "🌐 Language", "🛠️ সাপোর্ট",
        "📢 Update Channel", "📤 Upload File", "📁 My Files", "🤖 My Bots",
        "💰 Deposit", "👤 My Profile", "👑 Premium Plan 🌟", "🛠️ Support",
    }
    admin_buttons = {
        "📊 Statistics",
        "👥 User List",
        "🤖 All Bots",
        "📁 All Files",
        "📢 Broadcast",
        "📢 Manage Channels",
        "👤 User Host Permission",
        "🚫 User Ban/Unban",
        "🛠 Maintain",
        "🔙 User Panel",
        "➕ Add Channel",
        "🗑 Remove Channel",
        "📋 Channel List",
        "🔙 Admin Panel",
        "➕ Give Host Permission",
        "📋 Permission List",
        "♻️ Remove Host Permission",
    }
    return text in user_buttons or (user_id == ADMIN_ID and text in admin_buttons)


async def text_handler(update, context):

    user = update.effective_user
    touch_user(user)

    text = (update.message.text or "").strip()

    if not is_admin(user.id) and is_banned(user.id):
        await update.message.reply_text(
            "🚫 <b>আপনার Account Banned</b>",
            parse_mode="HTML"
        )
        return

    # Language selection remains available even while hosting is in maintenance.
    if text == "🌐 Language":
        await language_menu(update, context)
        return

    if is_maintenance() and not is_admin(user.id):
        await update.message.reply_text(
            "🛠 <b>MAINTENANCE MODE</b>\n\n"
            "Hosting Service সাময়িকভাবে বন্ধ আছে। Admin চালু করলে আবার ব্যবহার করতে পারবেন." if get_user_language(user.id) == "bn" else
            "🛠 <b>MAINTENANCE MODE</b>\n\nHosting Service is temporarily unavailable. Please try again after the Admin turns it ON.",
            parse_mode="HTML"
        )
        return

    if context.user_data.get("ts_ai_chat") and not is_navigation_button(text, user.id):
        if await ts_ai_message(update, context):
            return

    # ---------------------------------------------------------
    # MENU BUTTONS ALWAYS HAVE PRIORITY
    # ---------------------------------------------------------
    # If a previous flow is waiting for Token/User ID/Package/etc.,
    # tapping a normal menu button cancels that old flow immediately.
    # The tapped button is then handled normally below.
    if is_navigation_button(text, user.id):
        _cancel_ts_ai_task(user.id)
        clear_pending_actions(context)

    # -----------------------------
    # Admin Broadcast
    # -----------------------------

    if is_admin(user.id):

        if await do_broadcast(
            update,
            context
        ):

            return

    # -----------------------------
    # Admin User Ban / Unban
    # -----------------------------

    if is_admin(user.id):
        if await handle_user_ban(update, context):
            return

    # -----------------------------
    # Admin User Host Permission
    # -----------------------------

    if is_admin(user.id):
        if await handle_user_host_permission(update, context):
            return

    # -----------------------------
    # Bot Username Before File Upload
    # -----------------------------

    if context.user_data.get("waiting_bot_username"):

        bot_username = text.strip()

        if not bot_username:
            await update.message.reply_text(
                "❌ Bot Username দিন। উদাহরণ: <code>@my_bot</code>",
                parse_mode="HTML"
            )
            return

        if not bot_username.startswith("@"): 
            bot_username = "@" + bot_username

        context.user_data["upload_bot_username"] = bot_username
        context.user_data["waiting_bot_username"] = False
        context.user_data["waiting_upload"] = True

        await update.message.reply_text(
            "📁 <b>FILE UPLOAD</b>\n\n"
            f"🤖 Bot Username: <code>{bot_username}</code>\n\n"
            "এখন আপনার File পাঠান।\n\n"
            "উদাহরণ:\n"
            "• main.py\n"
            "• requirements.txt\n"
            "• ZIP\n\n"
            "Upload করার পর File-এর Action পাওয়া যাবে।",
            parse_mode="HTML"
        )
        return

    # -----------------------------
    # Package Text Input
    # -----------------------------

    if context.user_data.get("package_bot_id"):
        await save_package_text_and_run(
            update,
            context
        )
        return

    # -----------------------------
    # Hosting Token
    # -----------------------------

    if context.user_data.get(
        "hosting_file"
    ):

        await create_hosted_bot(
            update,
            context,
            text.strip()
        )

        return

    # -----------------------------
    # Admin Add Channel
    # -----------------------------

    if is_admin(user.id):

        if context.user_data.get(
            "adding_channel"
        ):

            channel = text.strip()

            if not channel.startswith("@"):

                channel = "@" + channel

            try:

                me = await context.bot.get_me()

                member = await context.bot.get_chat_member(
                    channel,
                    me.id
                )

                if member.status not in [
                    "administrator",
                    "creator"
                ]:

                    await update.message.reply_text(
                        "❌ Bot-কে ওই Channel-এ Administrator করুন।"
                    )

                    return

            except Exception:

                await update.message.reply_text(
                    "❌ Channel পাওয়া যাচ্ছে না অথবা Bot-এর Access নেই।"
                )

                return

            if channel not in channels:

                channels.append(channel)

                save_json(
                    CHANNELS_FILE,
                    channels
                )

                await update.message.reply_text(
                    f"✅ {channel} Added.",
                    reply_markup=admin_keyboard()
                )

            else:

                await update.message.reply_text(
                    "⚠️ এই Channel আগে থেকেই আছে।"
                )

            context.user_data[
                "adding_channel"
            ] = False

            return

        # Remove channel

        if context.user_data.get(
            "removing_channel"
        ):

            channel = text.strip()

            if not channel.startswith("@"):

                channel = "@" + channel

            if channel in channels:

                channels.remove(
                    channel
                )

                save_json(
                    CHANNELS_FILE,
                    channels
                )

                await update.message.reply_text(
                    f"🗑 {channel} Removed.",
                    reply_markup=admin_keyboard()
                )

            else:

                await update.message.reply_text(
                    "❌ Channel List-এ নেই।"
                )

            context.user_data[
                "removing_channel"
            ] = False

            return

    # -----------------------------
    # Admin Menu
    # -----------------------------

    if is_admin(user.id):

        if text == "📊 Statistics":

            await statistics(update)
            return

        if text == "👥 User List":

            await user_list(update)
            return

        if text == "🤖 All Bots":

            await all_bots_admin(update)
            return

        if text == "📁 All Files":

            await all_files(update)
            return

        if text == "📢 Broadcast":

            await broadcast_request(
                update,
                context
            )

            return

        if text == "📢 Manage Channels":

            await manage_channels(update)
            return

        if text == "👤 User Host Permission":

            await user_host_permission_request(update, context)
            return

        if text == "🚫 User Ban/Unban":

            await user_ban_request(update, context)
            return

        if text == "🛠 Maintain":

            await maintain_request(update, context)
            return

        if text == "➕ Add Channel":

            await add_channel_request(
                update,
                context
            )

            return

        if text == "🗑 Remove Channel":

            await remove_channel_request(
                update,
                context
            )

            return

        if text == "📋 Channel List":

            await channel_list(update)
            return

        if text == "🔙 User Panel":

            await user_panel(update)
            return

        if text == "🔙 Admin Panel":

            await admin_panel(update)
            return

    # -----------------------------
    # Force Join
    # -----------------------------

    if not is_admin(user.id) and channels:

        missing = await get_missing_channels(
            update,
            context
        )

        if missing:

            await send_force_join(
                update,
                missing
            )

            return

    # -----------------------------
    # User Menu
    # -----------------------------

    if text == "📢 আপডেট চ্যানেল":

        await update_channel(update)
        return

    if text == "📤 ফাইল আপলোড করুন":

        await upload_request(
            update,
            context
        )

        return

    if text == "📁 ফাইল দেখুন":

        await show_my_files_message(
            update
        )

        return

    if text == "🤖 আমার বট":

        await my_bots(update)
        return

    if text == "💰 ডিপোজিট":

        await deposit(update)
        return

    if text == "👤 আমার প্রোফাইল":

        await profile(update)
        return

    if text == "👑 প্রিমিয়াম প্লান 🌟":

        await premium(update)
        return

    if text in ("🤖 TS Ai", "🤖 TS AI"):

        await ts_ai_start(update, context)
        return

    if text in ("🛠️ সাপোর্ট", "🛠️ Support"):

        await support(update)
        return

    # -----------------------------
    # English User Menu
    # -----------------------------
    # Keep the same underlying features; only the button language changes.
    if text == "📢 Update Channel":
        await update_channel(update)
        return

    if text == "📤 Upload File":
        await upload_request(update, context)
        return

    if text == "📁 My Files":
        await show_my_files_message(update)
        return

    if text == "🤖 My Bots":
        await my_bots(update)
        return

    if text == "💰 Deposit":
        await deposit(update)
        return

    if text == "👤 My Profile":
        await profile(update)
        return

    if text == "👑 Premium Plan 🌟":
        await premium(update)
        return

    lang = get_user_language(user.id)
    unknown = "❓ I didn't understand that option." if lang == "en" else "❓ অপশনটি বুঝতে পারিনি।"
    await update.message.reply_text(
        unknown,
        reply_markup=(admin_keyboard() if is_admin(user.id) else user_keyboard(lang))
    )


# =========================================================
# DOCUMENT HANDLER
# =========================================================

async def document_handler(update, context):

    if not is_admin(update.effective_user.id) and is_banned(update.effective_user.id):
        await update.message.reply_text(
            "🚫 <b>আপনার Account Banned</b>",
            parse_mode="HTML"
        )
        return

    if is_maintenance() and not is_admin(update.effective_user.id):
        await update.message.reply_text("🛠 <b>MAINTENANCE MODE</b>\n\nHosting Service এখন বন্ধ আছে।", parse_mode="HTML")
        return

    if context.user_data.get("package_bot_id"):
        handled = await receive_package_document(
            update,
            context
        )
        if handled:
            return

    handled = await receive_document(
        update,
        context
    )

    if handled:

        return

    # যদি upload mode না থাকে
    await update.message.reply_text(
        "📁 আগে <b>📤 ফাইল আপলোড করুন</b> চাপুন।",
        parse_mode="HTML"
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    logger.error(
        "BOT ERROR: %s",
        context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if BOT_TOKEN == "YOUR_NEW_BOT_TOKEN":

        print(
            "❌ BOT_TOKEN সেট করুন।"
        )

        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_panel
        )
    )

    application.add_handler(
        CommandHandler(
            "tsai",
            ts_ai_start
        )
    )

    # Callback
    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # Documents
    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler
        )
    )

    # Text
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "================================="
    )

    print(
        "🤖 TS-HOST-RUN STARTING..."
    )

    print(
        "✅ HOSTING SYSTEM READY"
    )

    print(
        "================================="
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()