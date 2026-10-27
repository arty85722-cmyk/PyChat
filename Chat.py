"""
PyChat — Telegram-подобный чат.
Универсален: работает в Pydroid 3 (SQLite + threading) и на Render (PostgreSQL + threading).
"""

import os
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
# КОНФИГУРАЦИЯ
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
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

# ВАЖНО: используем threading вместо eventlet
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
online_users = {}

# =========================================================
# БАЗА ДАННЫХ
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
# HTML
# =========================================================
AUTH_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PyChat — Вход</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body class="auth-body">
  <div class="auth-card">
    <div class="logo">✈️</div>
    <h2>PyChat</h2>
    <p class="hint">Войдите или создайте аккаунт</p>
    <input id="username" type="text" placeholder="Логин (мин. 3)">
    <input id="password" type="password" placeholder="Пароль (мин. 4)">
    <div class="auth-buttons">
      <button class="primary" onclick="doAuth('login')">Войти</button>
      <button class="secondary" onclick="doAuth('register')">Регистрация</button>
    </div>
    <p id="error" class="error"></p>
  </div>
  <script>
    async function doAuth(action) {
      const username = document.getElementById('username').value.trim();
      const password = document.getElementById('password').value;
      const errBox = document.getElementById('error');
      errBox.innerText = 'Подождите...';
      
      try {
        const res = await fetch('/api/' + action, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({username, password})
        });
        const text = await res.text();
        let data;
        try { data = JSON.parse(text); } 
        catch (e) { errBox.innerText = 'Ошибка сервера: ' + text.substring(0, 50); return; }

        if (res.ok) { location.href = '/chat'; } 
        else { errBox.innerText = data.error || 'Ошибка ' + res.status; }
      } catch (e) {
        errBox.innerText = 'Ошибка сети: ' + e.message;
      }
    }
    document.getElementById('password').addEventListener('keydown', e => {
      if (e.key === 'Enter') doAuth('login');
    });
  </script>
</body>
</html>
"""

CHAT_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PyChat</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body class="chat-body">
  <div class="chat-container" id="chat-container">
    <aside class="sidebar">
      <div class="sidebar-header">
        <div class="avatar" id="my-avatar"></div>
        <span id="my-name">{{ username }}</span>
        <button onclick="logout()" class="logout">Выйти</button>
      </div>
      <input class="search" placeholder="Поиск..." oninput="filterUsers(this.value)">
      <div id="user-list" class="user-list"></div>
    </aside>

    <main class="main-chat">
      <div class="chat-header">
        <button class="back-btn" onclick="backToUsers()">←</button>
        <span id="chat-header">Выберите собеседника</span>
      </div>
      <div class="messages" id="messages"></div>
      <div class="input-area">
        <button class="icon-btn" onclick="toggleEmoji()">😊</button>
        <input type="file" id="image-input" accept="image/*" hidden>
        <button class="icon-btn" onclick="document.getElementById('image-input').click()">📎</button>
        <input type="text" id="message-input" placeholder="Написать сообщение..." disabled>
        <button class="send-btn" onclick="sendMessage()">➤</button>
      </div>
      <div id="emoji-picker" class="emoji-picker hidden"></div>
    </main>
  </div>

  <script>window.ME = "{{ username }}";</script>
  <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
  <script src="/static/app.js"></script>
</body>
</html>
"""

# =========================================================
# CSS
# =========================================================
STYLE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
body { height: 100vh; }
.auth-body { display: flex; justify-content: center; align-items: center; background: linear-gradient(135deg, #3390ec 0%, #6ec9cb 100%); padding: 16px; }
.auth-card { background: white; padding: 40px 30px; border-radius: 14px; width: 100%; max-width: 340px; text-align: center; box-shadow: 0 10px 40px rgba(0,0,0,.2); }
.logo { font-size: 42px; }
.auth-card h2 { margin: 8px 0; color: #1f2f3d; }
.hint { color: #888; font-size: 13px; margin-bottom: 20px; }
.auth-card input { width: 100%; padding: 12px; border: 1px solid #e0e0e0; border-radius: 10px; margin: 6px 0; outline: none; font-size: 15px; }
.auth-card input:focus { border-color: #3390ec; }
.auth-buttons { display: flex; gap: 8px; margin-top: 14px; }
.auth-buttons button { flex: 1; padding: 12px; border: none; border-radius: 10px; cursor: pointer; font-weight: 600; font-size: 15px; }
button.primary { background: #3390ec; color: white; }
button.primary:hover { background: #2b7fd4; }
button.secondary { background: #eff4f8; color: #3390ec; }
button.secondary:hover { background: #e1eaf2; }
.error { color: #e17076; font-size: 13px; margin-top: 10px; min-height: 18px; }
.chat-body { background: #e6ebee; display: flex; justify-content: center; align-items: center; }
.chat-container { display: flex; width: 100vw; height: 100vh; max-width: 1100px; max-height: 95vh; background: white; overflow: hidden; }
@media (min-width: 768px) { .chat-container { border-radius: 14px; box-shadow: 0 10px 40px rgba(0,0,0,.15); } }
.sidebar { width: 300px; border-right: 1px solid #eaeaea; display: flex; flex-direction: column; flex-shrink: 0; background: white; }
@media (max-width: 640px) { .sidebar { width: 100%; } .main-chat { display: none; } .chat-container.mobile-open .sidebar { display: none; } .chat-container.mobile-open .main-chat { display: flex; } }
.sidebar-header { display: flex; align-items: center; gap: 10px; padding: 12px; background: #fff; border-bottom: 1px solid #eaeaea; }
.sidebar-header span { flex: 1; font-weight: 600; color: #1f2f3d; }
.logout { border: none; background: #f0f0f0; padding: 6px 10px; border-radius: 8px; cursor: pointer; font-size: 12px; }
.search { margin: 8px; padding: 10px 12px; background: #f0f3f5; border: none; border-radius: 20px; outline: none; font-size: 14px; }
.user-list { flex: 1; overflow-y: auto; }
.user-item { display: flex; align-items: center; gap: 10px; padding: 12px; cursor: pointer; border-bottom: 1px solid #f5f5f5; }
.user-item:hover, .user-item.active { background: #f0f7ff; }
.avatar { width: 36px; height: 36px; border-radius: 50%; color: white; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 14px; flex-shrink: 0; }
.main-chat { flex: 1; display: flex; flex-direction: column; background: #e6ebee; position: relative; min-width: 0; }
.chat-header { padding: 14px 18px; background: white; font-weight: 600; border-bottom: 1px solid #eaeaea; display: flex; align-items: center; gap: 10px; }
.back-btn { display: none; background: none; border: none; font-size: 20px; cursor: pointer; padding: 0 8px 0 0; color: #3390ec; }
@media (max-width: 640px) { .back-btn { display: block; } }
.messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 6px; }
.message { max-width: 75%; padding: 9px 13px; border-radius: 12px; background: white; box-shadow: 0 1px 2px rgba(0,0,0,.08); word-break: break-word; }
.message.my { align-self: flex-end; background: #effdde; }
.message .time { font-size: 10px; color: #999; display: block; text-align: right; margin-top: 4px; }
.message img { max-width: 100%; max-height: 300px; border-radius: 8px; margin-top: 6px; display: block; }
.input-area { display: flex; gap: 6px; padding: 10px; background: white; border-top: 1px solid #eaeaea; align-items: center; }
#message-input { flex: 1; padding: 12px 16px; border: none; background: #f0f3f5; border-radius: 22px; outline: none; font-size: 14px; min-width: 0; }
.icon-btn { background: none; border: none; font-size: 22px; cursor: pointer; padding: 4px; color: #707579; }
.send-btn { background: #3390ec; color: white; border: none; width: 42px; height: 42px; border-radius: 50%; cursor: pointer; font-size: 16px; flex-shrink: 0; }
.emoji-picker { position: absolute; bottom: 70px; right: 14px; background: white; padding: 10px; border-radius: 12px; box-shadow: 0 6px 24px rgba(0,0,0,.15); display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; z-index: 20; max-height: 260px; overflow-y: auto; }
.emoji-picker span { font-size: 22px; cursor: pointer; padding: 4px; border-radius: 6px; text-align: center; }
.emoji-picker span:hover { background: #f0f3f5; }
.hidden { display: none !important; }
"""

# =========================================================
# JS
# =========================================================
APP_JS = r"""
const socket = io();
const EMOJIS = ["😀","😁","😂","🤣","😊","😍","😘","😎","🤔","😴","😭","😡","👍","👎","👏","🙏","🔥","❤️","💯","🎉","💬","📷","🌹","🍕","⚽","🚀","✨","🥰","😇","🤗"];
let currentChat = null;
let allUsers = [];

socket.emit("register_user", { username: ME });
document.getElementById('my-avatar').innerText = ME[0].toUpperCase();
document.getElementById('my-avatar').style.background = colorFor(ME);

document.getElementById('message-input').addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(); });
document.getElementById('image-input').addEventListener('change', uploadImage);

loadUsers();

async function loadUsers() {
  try {
    const res = await fetch('/api/users');
    allUsers = await res.json();
    renderUsers(allUsers);
  } catch (e) { document.getElementById('user-list').innerHTML = '<div style="padding:16px;color:red;">Ошибка загрузки</div>'; }
}

function renderUsers(list) {
  const box = document.getElementById('user-list');
  box.innerHTML = '';
  if (!Array.isArray(list) || list.length === 0) {
    box.innerHTML = '<div style="padding:16px;color:#999;font-size:13px;text-align:center;">Нет пользователей</div>';
    return;
  }
  list.forEach(u => {
    const div = document.createElement('div');
    div.className = 'user-item';
    div.dataset.user = u.username;
    div.innerHTML = '<div class="avatar" style="background:' + u.avatar_color + '">' + u.username[0].toUpperCase() + '</div><span>' + u.username + '</span>';
    div.onclick = () => openChat(u.username, div);
    box.appendChild(div);
  });
}

function filterUsers(q) { renderUsers(allUsers.filter(u => u.username.toLowerCase().includes(q.toLowerCase()))); }

async function openChat(username, el) {
  currentChat = username;
  document.querySelectorAll('.user-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('chat-header').innerText = username;
  document.getElementById('message-input').disabled = false;
  document.getElementById('chat-container').classList.add('mobile-open');
  try {
    const res = await fetch('/api/history/' + encodeURIComponent(username));
    const history = await res.json();
    const box = document.getElementById('messages');
    box.innerHTML = '';
    history.forEach(m => renderMessage(m, m.sender === ME));
    scrollBottom();
  } catch (e) { console.error(e); }
}

function backToUsers() { document.getElementById('chat-container').classList.remove('mobile-open'); }

function sendMessage() {
  const input = document.getElementById('message-input');
  const text = input.value.trim();
  if (!text || !currentChat) return;
  socket.emit('private_message', { from: ME, to: currentChat, text });
  input.value = '';
  document.getElementById('emoji-picker').classList.add('hidden');
}

async function uploadImage(e) {
  if (!currentChat) { alert('Выберите собеседника'); return; }
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('image', file);
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.url) socket.emit('private_message', { from: ME, to: currentChat, image_url: data.url, text: '' });
    else alert('Ошибка: ' + data.error);
  } catch (err) { alert('Ошибка сети'); }
  e.target.value = '';
}

socket.on('new_message', (m) => {
  if (currentChat === m.from) { renderMessage(m, false); scrollBottom(); }
  else { const el = document.querySelector('.user-item[data-user="' + m.from + '"]'); if (el) el.style.background = '#fff5d6'; }
});

socket.on('message_sent', (m) => { if (currentChat === m.to) { renderMessage(m, true); scrollBottom(); } });

function renderMessage(m, mine) {
  const div = document.createElement('div');
  div.className = 'message' + (mine ? ' my' : '');
  let html = '';
  if (m.text) html += escapeHtml(m.text).replace(/\n/g, '<br>');
  if (m.image_url) html += '<img src="' + m.image_url + '">';
  try {
    const d = new Date(m.timestamp);
    html += '<span class="time">' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) + '</span>';
  } catch (e) {}
  div.innerHTML = html;
  document.getElementById('messages').appendChild(div);
}

function scrollBottom() { const box = document.getElementById('messages'); box.scrollTop = box.scrollHeight; }
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function colorFor(name) { const colors = ["#3390ec","#e17076","#7bc862","#e5ca77","#6ec9cb","#a695e7","#ee7aae"]; return colors[name.charCodeAt(0) % colors.length]; }

const picker = document.getElementById('emoji-picker');
EMOJIS.forEach(e => {
  const s = document.createElement('span'); s.innerText = e;
  s.onclick = () => { const inp = document.getElementById('message-input'); inp.value += e; inp.focus(); };
  picker.appendChild(s);
});
function toggleEmoji() { picker.classList.toggle('hidden'); }
async function logout() { try { await fetch('/api/logout', { method: 'POST' }); } catch (e) {} location.href = '/'; }
"""

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
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
