"""
PyChat — универсальный скрипт для Pydroid 3 и Render.
"""
import os
import sys
import time
import sqlite3
import warnings
import bcrypt
from datetime import datetime
from urllib.parse import urlparse

warnings.filterwarnings("ignore")

from flask import (
    Flask, render_template_string, request, jsonify,
    session, redirect, url_for, send_from_directory
)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.utils import secure_filename

# =========================================================
# КОНФИГУРАЦИЯ (авто-определение среды)
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Если есть переменная Render — используем PostgreSQL, иначе SQLite
DATABASE_URL = os.environ.get("DATABASE_URL")
IS_RENDER = DATABASE_URL is not None

if IS_RENDER:
    import psycopg2
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    parsed_url = urlparse(DATABASE_URL)
    DB_PARAMS = {
        "dbname": parsed_url.path[1:],
        "user": parsed_url.username,
        "password": parsed_url.password,
        "host": parsed_url.hostname,
        "port": parsed_url.port
    }
    DB_TYPE = "postgres"
else:
    DB_PATH = os.path.join(BASE_DIR, "chat.db")
    DB_TYPE = "sqlite"

AVATAR_COLORS = ["#3390ec", "#e17076", "#7bc862", "#e5ca77",
                 "#6ec9cb", "#a695e7", "#ee7aae"]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "super_secret_key_12345")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

# КЛЮЧЕВОЙ МОМЕНТ: async_mode
# На Pydroid 3 используем threading (стабильнее на Android)
# На Render используем eventlet (лучше для WebSocket на сервере)
if IS_RENDER:
    socketio = SocketIO(app, cors_allowed_origins="*")  # eventlet подхватится сам
else:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

online_users = {}

# =========================================================
# БАЗА ДАННЫХ (универсальный слой)
# =========================================================
def get_conn():
    if DB_TYPE == "postgres":
        conn = psycopg2.connect(**DB_PARAMS)
        conn.autocommit = True
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    try:
        conn = get_conn()
        c = conn.cursor()
        if DB_TYPE == "postgres":
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY, username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(200) NOT NULL, avatar_color VARCHAR(10) DEFAULT '#3390ec')""")
            c.execute("""CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY, sender VARCHAR(50) NOT NULL,
                receiver VARCHAR(50) NOT NULL, text TEXT,
                image_url VARCHAR(255), timestamp VARCHAR(50) NOT NULL)""")
        else:
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL, avatar_color TEXT DEFAULT '#3390ec')""")
            c.execute("""CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL,
                receiver TEXT NOT NULL, text TEXT,
                image_url TEXT, timestamp TEXT NOT NULL)""")
        conn.commit()
        conn.close()
        print("✅ База данных готова")
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")

def create_user(username, password_hash, avatar_color):
    conn = get_conn()
    try:
        c = conn.cursor()
        if DB_TYPE == "postgres":
            c.execute("INSERT INTO users (username, password, avatar_color) VALUES (%s, %s, %s)", (username, password_hash, avatar_color))
        else:
            c.execute("INSERT INTO users (username, password, avatar_color) VALUES (?, ?, ?)", (username, password_hash, avatar_color))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()

def get_user(username):
    conn = get_conn()
    try:
        c = conn.cursor()
        if DB_TYPE == "postgres":
            c.execute("SELECT * FROM users WHERE username = %s", (username,))
            row = c.fetchone()
            if row:
                cols = [desc[0] for desc in c.description]
                return dict(zip(cols, row))
        else:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return dict(row) if row else None
    finally:
        conn.close()
    return None

def get_all_users():
    conn = get_conn()
    try:
        c = conn.cursor()
        if DB_TYPE == "postgres":
            c.execute("SELECT username, avatar_color FROM users ORDER BY username")
            rows = c.fetchall()
            cols = [desc[0] for desc in c.description]
            return [dict(zip(cols, row)) for row in rows]
        else:
            rows = conn.execute("SELECT username, avatar_color FROM users ORDER BY username").fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()

def save_message(sender, receiver, text, image_url):
    conn = get_conn()
    ts = datetime.utcnow().isoformat()
    try:
        c = conn.cursor()
        if DB_TYPE == "postgres":
            c.execute("INSERT INTO messages (sender, receiver, text, image_url, timestamp) VALUES (%s, %s, %s, %s, %s)", (sender, receiver, text, image_url, ts))
        else:
            c.execute("INSERT INTO messages (sender, receiver, text, image_url, timestamp) VALUES (?, ?, ?, ?, ?)", (sender, receiver, text, image_url, ts))
        conn.commit()
    finally:
        conn.close()
    return ts

def get_history(user1, user2):
    conn = get_conn()
    try:
        c = conn.cursor()
        if DB_TYPE == "postgres":
            c.execute("""SELECT sender, receiver, text, image_url, timestamp FROM messages
                         WHERE (sender = %s AND receiver = %s) OR (sender = %s AND receiver = %s)
                         ORDER BY timestamp ASC""", (user1, user2, user2, user1))
            rows = c.fetchall()
            cols = [desc[0] for desc in c.description]
            return [dict(zip(cols, row)) for row in rows]
        else:
            rows = conn.execute("""SELECT * FROM messages
                                   WHERE (sender = ? AND receiver = ?) OR (sender = ? AND receiver = ?)
                                   ORDER BY timestamp ASC""", (user1, user2, user2, user1)).fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()

init_db()

# =========================================================
# HTML/CSS/JS (сокращено для читаемости, полный код в конце)
# =========================================================
# ... (вставьте сюда HTML, CSS, JS из предыдущего полного ответа, они не меняются)
# Для краткости здесь пропущены, но в вашем файле они должны быть.

AUTH_HTML = """..."""  # вставьте полностью
CHAT_HTML = """..."""
STYLE_CSS = """..."""
APP_JS = r"""..."""

# =========================================================
# МАРШРУТЫ
# =========================================================
@app.route("/")
def index():
    if "username" in session: return redirect(url_for("chat_page"))
    return render_template_string(AUTH_HTML)

@app.route("/chat")
def chat_page():
    if "username" not in session: return redirect(url_for("index"))
    return render_template_string(CHAT_HTML, username=session["username"])

@app.route("/static/style.css")
def style_css(): return STYLE_CSS, 200, {"Content-Type": "text/css; charset=utf-8"}

@app.route("/static/app.js")
def app_js(): return APP_JS, 200, {"Content-Type": "application/javascript; charset=utf-8"}

@app.route("/uploads/<path:filename>")
def uploaded_file(filename): return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/api/register", methods=["POST"])
def register():
    try:
        data = request.get_json() or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if len(username) < 3 or len(password) < 4: return jsonify({"error": "Логин >= 3, пароль >= 4"}), 400
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        color = AVATAR_COLORS[len(username) % len(AVATAR_COLORS)]
        if not create_user(username, hashed, color): return jsonify({"error": "Пользователь уже есть"}), 400
        session["username"] = username
        return jsonify({"username": username})
    except Exception as e:
        return jsonify({"error": f"Сервер: {e}"}), 500

@app.route("/api/login", methods=["POST"])
def login():
    try:
        data = request.get_json() or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        user = get_user(username)
        if not user or not bcrypt.checkpw(password.encode(), user["password"].encode()):
            return jsonify({"error": "Неверный логин или пароль"}), 401
        session["username"] = username
        return jsonify({"username": username})
    except Exception as e:
        return jsonify({"error": f"Сервер: {e}"}), 500

@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"ok": True})

@app.route("/api/users")
def api_users():
    if "username" not in session: return jsonify({"error": "unauthorized"}), 401
    return jsonify([u for u in get_all_users() if u["username"] != session["username"]])

@app.route("/api/history/<other>")
def api_history(other):
    if "username" not in session: return jsonify({"error": "unauthorized"}), 401
    return jsonify(get_history(session["username"], other))

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "username" not in session: return jsonify({"error": "unauthorized"}), 401
    file = request.files.get("image")
    if not file: return jsonify({"error": "Файл не получен"}), 400
    filename = secure_filename(f"{int(time.time()*1000)}_{file.filename}")
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    return jsonify({"url": f"/uploads/{filename}"})

@socketio.on("register_user")
def on_register(data):
    username = data.get("username")
    if username: online_users[username] = request.sid; join_room(username)

@socketio.on("private_message")
def on_private_message(data):
    sender = data.get("from"); receiver = data.get("to")
    text = (data.get("text") or "").strip(); image_url = data.get("image_url")
    if not sender or not receiver or (not text and not image_url): return
    ts = save_message(sender, receiver, text, image_url)
    payload = {"from": sender, "to": receiver, "text": text, "image_url": image_url, "timestamp": ts}
    if receiver in online_users: emit("new_message", payload, to=online_users[receiver])
    emit("message_sent", payload, to=request.sid)

@socketio.on("disconnect")
def on_disconnect():
    for user, sid in list(online_users.items()):
        if sid == request.sid: del online_users[user]; break

if __name__ == "__main__":
    print("=" * 50)
    if IS_RENDER:
        print("  🚀 Режим Render (production)")
    else:
        print("  🚀 Режим Pydroid 3 (localhost:5000)")
    print("=" * 50)
    # На Render хост и порт задаются автоматически, на Pydroid — локально
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)