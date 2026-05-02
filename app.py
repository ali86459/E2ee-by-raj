# bot_webui.py - Railway Compatible Version
import os
import sys
import time
import json
import random
import sqlite3
import threading
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
from collections import deque
from functools import wraps

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ==================== CONFIGURATION ====================
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'teri-ma-ki-chut-mdc-2024')
CODE = "03102003"
MAX_TASKS = 50
PORT = int(os.environ.get("PORT", 8080))  # Railway ka PORT

DB_PATH = Path('/data/bot_data.db') if not Path(__file__).parent.exists() else Path(__file__).parent / 'bot_data.db'
ENCRYPTION_KEY_FILE = Path('/data/.encryption_key') if not Path(__file__).parent.exists() else Path(__file__).parent / '.encryption_key'

# Ensure data directory exists
Path('/data').mkdir(exist_ok=True)

# Logs storage
task_logs = {}

def log_message(task_id: str, msg: str):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    
    if task_id not in task_logs:
        task_logs[task_id] = deque(maxlen=200)
    
    task_logs[task_id].append(formatted_msg)
    print(formatted_msg)

# ==================== ENCRYPTION ====================
def get_encryption_key():
    if ENCRYPTION_KEY_FILE.exists():
        with open(ENCRYPTION_KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(ENCRYPTION_KEY_FILE, 'wb') as f:
            f.write(key)
        return key

ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_data(data):
    if not data:
        return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except:
        return ""

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            cookies_encrypted TEXT,
            chat_id TEXT,
            name_prefix TEXT,
            messages TEXT,
            delay INTEGER DEFAULT 30,
            status TEXT DEFAULT 'stopped',
            messages_sent INTEGER DEFAULT 0,
            start_time TIMESTAMP,
            last_active TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create default admin user
    cursor.execute('SELECT * FROM users WHERE username = "admin"')
    if not cursor.fetchone():
        password_hash = hashlib.sha256("admin123".encode()).hexdigest()
        cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', 
                      ('admin', password_hash))
    
    conn.commit()
    conn.close()

init_db()

# ==================== TASK CLASS ====================
@dataclass
class Task:
    task_id: str
    username: str
    cookies: List[str]
    chat_id: str
    name_prefix: str
    messages: List[str]
    delay: int
    status: str
    messages_sent: int
    start_time: Optional[datetime]
    last_active: Optional[datetime]
    running: bool = False
    stop_flag: bool = False
    
    def get_uptime(self):
        if not self.start_time:
            return "00:00:00"
        delta = datetime.now() - self.start_time
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        seconds = delta.seconds % 60
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

# ==================== TASK MANAGER ====================
class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.task_threads: Dict[str, threading.Thread] = {}
        self.load_tasks_from_db()
    
    def load_tasks_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks')
        for row in cursor.fetchall():
            try:
                cookies = json.loads(decrypt_data(row[2])) if row[2] else []
                messages = json.loads(decrypt_data(row[5])) if row[5] else []
                
                task = Task(
                    task_id=row[0],
                    username=row[1],
                    cookies=cookies,
                    chat_id=row[3] or "",
                    name_prefix=row[4] or "",
                    messages=messages,
                    delay=row[6] or 30,
                    status=row[7] or "stopped",
                    messages_sent=row[8] or 0,
                    start_time=datetime.fromisoformat(row[9]) if row[9] else None,
                    last_active=datetime.fromisoformat(row[10]) if row[10] else None
                )
                self.tasks[task.task_id] = task
                if task.status == "running":
                    self.start_task(task.task_id)
            except Exception as e:
                print(f"Error loading task: {e}")
        conn.close()
    
    def save_task(self, task: Task):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks 
            (task_id, username, cookies_encrypted, chat_id, name_prefix, messages, 
             delay, status, messages_sent, start_time, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id,
            task.username,
            encrypt_data(json.dumps(task.cookies)),
            task.chat_id,
            task.name_prefix,
            encrypt_data(json.dumps(task.messages)),
            task.delay,
            task.status,
            task.messages_sent,
            task.start_time.isoformat() if task.start_time else None,
            task.last_active.isoformat() if task.last_active else None
        ))
        conn.commit()
        conn.close()
    
    def delete_task(self, task_id: str):
        if task_id in self.tasks:
            self.stop_task(task_id)
            del self.tasks[task_id]
            if task_id in task_logs:
                del task_logs[task_id]
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            conn.commit()
            conn.close()
            return True
        return False
    
    def start_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        if task.status == "running":
            return False
        if len([t for t in self.tasks.values() if t.status == "running"]) >= MAX_TASKS:
            return False
        task.status = "running"
        task.stop_flag = False
        if not task.start_time:
            task.start_time = datetime.now()
        task.last_active = datetime.now()
        self.save_task(task)
        
        thread = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
        thread.start()
        self.task_threads[task_id] = thread
        return True
    
    def stop_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        task.stop_flag = True
        task.status = "stopped"
        task.last_active = datetime.now()
        self.save_task(task)
        return True
    
    def _setup_browser(self, task_id: str):
        """Railway-compatible browser setup"""
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-setuid-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        
        # Railway specific Chromium path
        chromium_paths = [
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/usr/bin/google-chrome',
            '/usr/bin/chrome'
        ]
        
        for chromium_path in chromium_paths:
            if Path(chromium_path).exists():
                chrome_options.binary_location = chromium_path
                log_message(task_id, f'Found browser at: {chromium_path}')
                break
        
        try:
            driver = webdriver.Chrome(options=chrome_options)
            log_message(task_id, 'Browser started successfully!')
            return driver
        except Exception as error:
            log_message(task_id, f'Browser setup failed: {error}')
            raise error
    
    def _find_message_input(self, driver, task_id: str, process_id: str):
        """Find Facebook message input"""
        log_message(task_id, f"{process_id}: Finding message input...")
        
        message_input_selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-lexical-editor="true"]',
            'div[aria-label*="message" i][contenteditable="true"]',
            'div[aria-label*="Message" i][contenteditable="true"]',
            '[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"]'
        ]
        
        for selector in message_input_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        is_editable = driver.execute_script("""
                            return arguments[0].contentEditable === 'true' || 
                                   arguments[0].tagName === 'TEXTAREA';
                        """, element)
                        
                        if is_editable:
                            element.click()
                            log_message(task_id, f"{process_id}: Found message input")
                            return element
                    except:
                        continue
            except:
                continue
        
        log_message(task_id, f"{process_id}: Message input not found!")
        return None
    
    def _send_messages(self, task: Task, process_id: str):
        """Send messages to Facebook"""
        driver = None
        message_rotation_index = 0
        task_id = task.task_id
        
        try:
            log_message(task_id, f"{process_id}: Starting automation...")
            driver = self._setup_browser(task_id)
            
            log_message(task_id, f"{process_id}: Navigating to Facebook...")
            driver.get('https://www.facebook.com/')
            time.sleep(8)
            
            # Add cookies
            if task.cookies and task.cookies[0]:
                cookie_string = task.cookies[0]
                cookie_pairs = cookie_string.split(';')
                for pair in cookie_pairs:
                    if '=' in pair:
                        name, value = pair.strip().split('=', 1)
                        try:
                            driver.add_cookie({'name': name, 'value': value, 'domain': '.facebook.com'})
                        except:
                            pass
            
            # Navigate to chat
            if task.chat_id:
                driver.get(f'https://www.facebook.com/messages/t/{task.chat_id}')
            else:
                driver.get('https://www.facebook.com/messages')
            
            time.sleep(15)
            
            message_input = self._find_message_input(driver, task_id, process_id)
            if not message_input:
                task.status = "stopped"
                self.save_task(task)
                return 0
            
            messages_list = [msg.strip() for msg in task.messages if msg.strip()]
            if not messages_list:
                messages_list = ['Hello!']
            
            log_message(task_id, f"{process_id}: Starting message loop...")
            messages_sent = 0
            
            while task.status == "running" and not task.stop_flag:
                message = messages_list[message_rotation_index % len(messages_list)]
                message_rotation_index += 1
                
                if task.name_prefix:
                    message = f"{task.name_prefix} {message}"
                
                try:
                    # Type message
                    driver.execute_script("""
                        arguments[0].textContent = arguments[1];
                        arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
                    """, message_input, message)
                    
                    time.sleep(1)
                    
                    # Send via Enter key
                    driver.execute_script("""
                        var event = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true });
                        arguments[0].dispatchEvent(event);
                    """, message_input)
                    
                    messages_sent += 1
                    task.messages_sent = messages_sent
                    task.last_active = datetime.now()
                    self.save_task(task)
                    
                    log_message(task_id, f"{process_id}: Message #{messages_sent} sent. Waiting {task.delay}s...")
                    time.sleep(task.delay)
                    
                except Exception as e:
                    log_message(task_id, f"{process_id}: Send error: {str(e)[:100]}")
                    time.sleep(5)
            
            return messages_sent
            
        except Exception as e:
            log_message(task_id, f"{process_id}: Fatal error: {str(e)}")
            task.status = "stopped"
            self.save_task(task)
            return 0
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
    
    def _run_task(self, task_id: str):
        task = self.tasks[task_id]
        task.running = True
        process_id = f"TASK-{task_id[-6:]}"
        
        while task.status == "running" and not task.stop_flag:
            try:
                self._send_messages(task, process_id)
            except Exception as e:
                log_message(task_id, f"ERROR: {str(e)[:100]}")
                time.sleep(5)
        
        task.running = False
        if task_id in self.task_threads:
            del self.task_threads[task_id]

task_manager = TaskManager()

# ==================== FLASK WEB UI ====================
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'your-secret-key-here-2024')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# HTML Templates (shortened for brevity - same as your original)
HTML_TEMPLATE = '''<!DOCTYPE html>
<html>
<head><title>Facebook Bot</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Arial;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;padding:20px}
.container{max-width:1400px;margin:0 auto}
.header{background:white;border-radius:10px;padding:20px;margin-bottom:20px;display:flex;justify-content:space-between}
.card{background:white;border-radius:10px;padding:20px;margin-bottom:20px}
button{background:#667eea;color:white;padding:10px 20px;border:none;border-radius:5px;cursor:pointer}
.logs{background:#1e1e1e;color:#d4d4d4;border-radius:5px;padding:15px;font-family:monospace;height:400px;overflow-y:auto}
</style>
</head>
<body>
<div class="container">
<div class="header"><h1>🤖 Facebook Bot</h1><a href="/logout" style="background:#dc3545;color:white;padding:10px 20px;text-decoration:none;border-radius:5px">Logout</a></div>
<div class="card"><h2>Create Task</h2>
<form id="createForm">
<input type="text" name="chat_id" placeholder="Chat ID" required><br><br>
<textarea name="messages" placeholder="Messages (one per line)" required></textarea><br><br>
<input type="number" name="delay" value="30" placeholder="Delay (seconds)"><br><br>
<textarea name="cookies" placeholder="Facebook Cookies" required></textarea><br><br>
<button type="submit">Create & Start</button>
</form>
</div>
<div class="card"><h2>Tasks</h2><div id="tasks"></div></div>
<div class="card"><h2>Logs</h2><div class="logs" id="logs">Select a task</div></div>
</div>
<script>
let currentTask=null;
function loadTasks(){fetch('/api/tasks').then(r=>r.json()).then(tasks=>{document.getElementById('tasks').innerHTML=tasks.map(t=>`<div onclick="selectTask('${t.task_id}')" style="border-left:4px solid ${t.status=='running'?'green':'red'};padding:10px;margin:10px 0;background:#f8f9fa"><b>${t.task_id}</b> - ${t.status} - Sent:${t.messages_sent}<br><button onclick="event.stopPropagation();fetch('/api/tasks/${t.task_id}/start',{method:'POST'}).then(()=>loadTasks())">Start</button> <button onclick="event.stopPropagation();fetch('/api/tasks/${t.task_id}/stop',{method:'POST'}).then(()=>loadTasks())">Stop</button> <button onclick="event.stopPropagation();fetch('/api/tasks/${t.task_id}',{method:'DELETE'}).then(()=>{loadTasks();if(currentTask==='${t.task_id}')selectTask(null)})">Delete</button></div>`).join('')})}
function selectTask(id){currentTask=id;if(id){fetch(`/api/logs/${id}`).then(r=>r.json()).then(d=>{document.getElementById('logs').innerHTML=d.logs.map(l=>`<div>${l}</div>`).join('')})}else{document.getElementById('logs').innerHTML='Select a task'}}
document.getElementById('createForm').addEventListener('submit',(e)=>{e.preventDefault();fetch('/api/tasks/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:e.target.chat_id.value,messages:e.target.messages.value.split('\\n'),delay:parseInt(e.target.delay.value),cookies:e.target.cookies.value})}).then(()=>{loadTasks();e.target.reset()})});
setInterval(loadTasks,3000);loadTasks();
</script>
</body>
</html>'''

LOGIN_TEMPLATE = '''<!DOCTYPE html>
<html>
<head><title>Login</title>
<style>body{font-family:Arial;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);display:flex;justify-content:center;align-items:center;height:100vh}.login-box{background:white;padding:40px;border-radius:10px;width:350px}h1{color:#667eea}input{width:100%;padding:10px;margin:10px 0}button{width:100%;padding:10px;background:#667eea;color:white;border:none}</style>
</head>
<body>
<div class="login-box"><h1>Login</h1>
<form method="POST"><input type="text" name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password" required><button type="submit">Login</button></form>
<div style="margin-top:20px;font-size:12px;text-align:center">admin / admin123</div>
</div>
</body>
</html>'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ? AND password_hash = ?', (username, password_hash))
        user = cursor.fetchone()
        conn.close()
        
        if user:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template_string(LOGIN_TEMPLATE, error='Invalid')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/stats')
@login_required
def api_stats():
    username = session.get('username')
    user_tasks = [t for t in task_manager.tasks.values() if t.username == username]
    return jsonify({
        'total_tasks': len(user_tasks),
        'running_tasks': sum(1 for t in user_tasks if t.status == 'running'),
        'total_messages': sum(t.messages_sent for t in user_tasks)
    })

@app.route('/api/tasks')
@login_required
def api_tasks():
    username = session.get('username')
    tasks = [t for t in task_manager.tasks.values() if t.username == username]
    return jsonify([{
        'task_id': t.task_id,
        'status': t.status,
        'chat_id': t.chat_id,
        'messages_sent': t.messages_sent,
        'uptime': t.get_uptime()
    } for t in tasks])

@app.route('/api/tasks/create', methods=['POST'])
@login_required
def api_create_task():
    data = request.json
    username = session.get('username')
    
    try:
        task_id = f"task_{random.randint(10000, 99999)}"
        task = Task(
            task_id=task_id,
            username=username,
            cookies=[data.get('cookies', '')],
            chat_id=data.get('chat_id', ''),
            name_prefix=data.get('name_prefix', ''),
            messages=data.get('messages', ['Hello!']),
            delay=int(data.get('delay', 30)),
            status='stopped',
            messages_sent=0,
            start_time=None,
            last_active=None
        )
        
        task_manager.tasks[task_id] = task
        task_manager.save_task(task)
        task_manager.start_task(task_id)
        
        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/tasks/<task_id>/start', methods=['POST'])
@login_required
def api_start_task(task_id):
    if task_manager.start_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed'}), 400

@app.route('/api/tasks/<task_id>/stop', methods=['POST'])
@login_required
def api_stop_task(task_id):
    if task_manager.stop_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed'}), 400

@app.route('/api/tasks/<task_id>', methods=['DELETE'])
@login_required
def api_delete_task(task_id):
    if task_manager.delete_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed'}), 400

@app.route('/api/logs/<task_id>')
@login_required
def api_logs(task_id):
    logs = list(task_logs.get(task_id, []))
    return jsonify({'logs': logs[-100:]})

@app.route('/health')
def health():
    return jsonify({'status': 'alive', 'tasks': len(task_manager.tasks)})

if __name__ == '__main__':
    print("=" * 50)
    print("🤖 Facebook Bot - Starting on Railway")
    print(f"📍 Port: {PORT}")
    print(f"🔑 Login: admin / admin123")
    print("=" * 50)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
