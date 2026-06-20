import sqlite3
import hashlib
import uuid
import random
from datetime import datetime
import urllib.request
import json
from flask import Flask, render_template, request, redirect, url_for, make_response, jsonify

app = Flask(__name__)
DB_NAME = 'database.db'

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            session_token TEXT,
            earnings REAL DEFAULT 0.00,
            active_model INTEGER DEFAULT 4
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            pos_x REAL DEFAULT NULL,
            pos_y REAL DEFAULT NULL,
            is_failed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            timestamp TEXT NOT NULL,
            message TEXT NOT NULL,
            type TEXT DEFAULT 'info',
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        password_hash = hashlib.sha256("admin".encode('utf-8')).hexdigest()
        cursor.execute("INSERT INTO users (username, password_hash, earnings) VALUES (?, ?, 0.00)", ('admin', password_hash))
        conn.commit()
        
        cursor.execute("SELECT id FROM users WHERE username = 'admin'")
        admin_id = cursor.fetchone()['id']
        cursor.execute("INSERT INTO system_logs (user_id, timestamp, message, type) VALUES (?, ?, ?, ?)",
                       (admin_id, datetime.now().strftime('%H:%M:%S'), 'SCADA Dashboard inicializovaný. Zariadenia sú prázdne.', 'success'))
        conn.commit()
    conn.close()

init_db()

def get_user_by_cookie(request):
    token = request.cookies.get('session_token')
    if not token:
        return None
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE session_token = ?', (token,)).fetchone()
    conn.close()
    return user

def fetch_real_okte_prices():
    today_str = datetime.now().strftime('%Y-%m-%d')
    url = f"https://tisot.okte.sk/api/v1/dam/results?deliveryDayFrom={today_str}&deliveryDayTo={today_str}"
    
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            prices = [0.0] * 24
            for entry in data:
                hour = entry.get('deliveryHour', 1) - 1
                price = entry.get('price', 0.0)
                if 0 <= hour < 24:
                    prices[hour] = float(price)
            return prices
    except Exception:
        # Plynulá krivka OKTE (Slovenský spot)
        return [88.4, 75.1, 71.5, 68.2, 70.1, 85.4, 110.2, 128.6, 135.1, 115.5,
                82.2, 48.1, 31.3, 28.2, 44.6, 70.3, 105.5, 129.1, 145.6, 152.4,
                138.2, 112.3, 98.1, 88.4]

def main_controller(model_id, price, avg_price):
    if model_id == 1:
        status, action, profit = ("AKTÍVNY ODBER", "CONSUMING", -0.01) if price > 0 else ("VYPNUTÉ", "IDLE", 0.0)
    elif model_id == 2:
        status, action, profit = ("NABÍJANIE AKUMULÁTORA", "CHARGING", -0.005) if price < avg_price else ("SPOTREBA Z BATÉRIE", "CONSUMING", 0.015)
    elif model_id == 3:
        status, action, profit = ("AKUMULÁCIA", "CHARGING", -0.005) if price < avg_price else ("PREDAJ DO SIETE", "SELLING", (price - avg_price) / 1000.0)
    elif model_id == 4:
        if price > avg_price * 1.25:
            status, action, profit = "PREDAJ DO SIETE (Špička OKTE)", "SELLING", 0.06
        elif price < avg_price * 0.75:
            status, action, profit = "NABÍJANIE (Sieťový nadbytok)", "CHARGING", -0.003
        else:
            status, action, profit = "DYNAMICKÝ AUTO-PILOT", "HYBRID", 0.012
    else:
        status, action, profit = ("PASÍVNY REŽIM", "IDLE", 0.0)
        
    return status, action, round(profit, 5)

# --- TRASY ---

@app.route('/')
def index():
    user = get_user_by_cookie(request)
    if not user:
        return redirect(url_for('login'))
    return render_template('dashboard.html', username=user['username'], active_model=user['active_model'])

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        password_hash = hashlib.sha256(password.encode('utf-8')).hexdigest()
        
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ? AND password_hash = ?', (username, password_hash)).fetchone()
        
        if user:
            token = str(uuid.uuid4())
            conn.execute('UPDATE users SET session_token = ? WHERE id = ?', (token, user['id']))
            conn.commit()
            conn.close()
            
            response = make_response(redirect(url_for('index')))
            response.set_cookie('session_token', token, max_age=3600*24, httponly=True, samesite='Lax')
            return response
        conn.close()
        return render_template('login.html', error="Nesprávne prihlasovacie údaje.")
    return render_template('login.html')

@app.route('/logout')
def logout():
    user = get_user_by_cookie(request)
    response = make_response(redirect(url_for('login')))
    if user:
        conn = get_db_connection()
        conn.execute('UPDATE users SET session_token = NULL WHERE id = ?', (user['id'],))
        conn.commit()
        conn.close()
    response.delete_cookie('session_token')
    return response

# --- REAL-TIME API ENDPOINTS ---

@app.route('/api/system_state')
def system_state():
    user = get_user_by_cookie(request)
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    hourly_prices = fetch_real_okte_prices()
    current_hour = datetime.now().hour
    current_price = hourly_prices[current_hour]
    avg_price = round(sum(hourly_prices) / 24, 2)
    
    status, action, profit = main_controller(user['active_model'], current_price, avg_price)
    
    new_earnings = round(user['earnings'] + profit, 5)
    conn = get_db_connection()
    conn.execute('UPDATE users SET earnings = ? WHERE id = ?', (new_earnings, user['id']))
    conn.commit()
    
    devices = conn.execute('SELECT * FROM devices WHERE user_id = ?', (user['id'],)).fetchall()
    logs = conn.execute('SELECT * FROM system_logs WHERE user_id = ? ORDER BY id DESC LIMIT 20', (user['id'],)).fetchall()
    conn.close()
    
    # Generovanie fiktívnej telemetrie pre tooltipy na mape v reálnom čase
    enriched_devices = []
    for d in devices:
        telemetry = "0.0 kW"
        if d['is_active'] == 1 and d['is_failed'] == 0:
            if d['type'] == 'solar':
                telemetry = f"+{round(random.uniform(2.5, 6.8), 2)} kW (Solár)"
            elif d['type'] == 'battery':
                telemetry = f"SOC: {random.randint(60,88)}% | -1.8 kW"
            elif d['type'] == 'ev':
                telemetry = "11.0 kW (Nabíjanie)"
            else:
                telemetry = f"{round(random.uniform(0.3, 2.1), 2)} kW"
        elif d['is_failed'] == 1:
            telemetry = "CHYBA SPOJENIA"
        
        enriched_devices.append({
            'id': d['id'],
            'name': d['name'],
            'type': d['type'],
            'is_active': d['is_active'],
            'pos_x': d['pos_x'],
            'pos_y': d['pos_y'],
            'is_failed': d['is_failed'],
            'telemetry': telemetry
        })
    
    return jsonify({
        'price': current_price,
        'avg_price': avg_price,
        'status': status,
        'action': action,
        'earnings': new_earnings,
        'active_model': user['active_model'],
        'devices': enriched_devices,
        'logs': [{'timestamp': l['timestamp'], 'message': l['message'], 'type': l['type']} for l in logs],
        'hourly_prices': hourly_prices,
        'current_hour': current_hour
    })

@app.route('/api/change_model/<int:model_id>', methods=['POST'])
def change_model_api(model_id):
    user = get_user_by_cookie(request)
    if user and model_id in [1, 2, 3, 4]:
        conn = get_db_connection()
        conn.execute('UPDATE users SET active_model = ? WHERE id = ?', (model_id, user['id']))
        conn.execute('INSERT INTO system_logs (user_id, timestamp, message, type) VALUES (?, ?, ?, ?)',
                     (user['id'], datetime.now().strftime('%H:%M:%S'), f"Zmena algoritmu na Model {model_id}.", 'info'))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    return jsonify({'error': 'Failed'}), 400

@app.route('/api/add_device', methods=['POST'])
def add_device_api():
    user = get_user_by_cookie(request)
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json
    name = data.get('name')
    dtype = data.get('type', 'appl')
    
    if name:
        conn = get_db_connection()
        conn.execute('INSERT INTO devices (user_id, name, type, is_active, is_failed) VALUES (?, ?, ?, 1, 0)', (user['id'], name, dtype))
        conn.execute('INSERT INTO system_logs (user_id, timestamp, message, type) VALUES (?, ?, ?, ?)',
                     (user['id'], datetime.now().strftime('%H:%M:%S'), f"Zariadenie '{name}' uložené.", 'success'))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    return jsonify({'error': 'Missing name'}), 400

@app.route('/api/update_device_pin', methods=['POST'])
def update_device_pin():
    user = get_user_by_cookie(request)
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    device_id = data.get('id')
    x = data.get('x')
    y = data.get('y')
    
    conn = get_db_connection()
    conn.execute('UPDATE devices SET pos_x = ?, pos_y = ? WHERE id = ? AND user_id = ?', (x, y, device_id, user['id']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/toggle_device/<int:device_id>', methods=['POST'])
def toggle_device_api(device_id):
    user = get_user_by_cookie(request)
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
        
    conn = get_db_connection()
    device = conn.execute('SELECT * FROM devices WHERE id = ? AND user_id = ?', (device_id, user['id'])).fetchone()
    if device:
        new_state = 0 if device['is_active'] == 1 else 1
        conn.execute('UPDATE devices SET is_active = ? WHERE id = ?', (new_state, device_id))
        state_str = "ZAPNUTÉ" if new_state == 1 else "VYPNUTÉ"
        conn.execute('INSERT INTO system_logs (user_id, timestamp, message, type) VALUES (?, ?, ?, ?)',
                     (user['id'], datetime.now().strftime('%H:%M:%S'), f"Relé {device['name']}: {state_str}.", 'info'))
        conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/toggle_device_failure/<int:device_id>', methods=['POST'])
def toggle_device_failure(device_id):
    user = get_user_by_cookie(request)
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
        
    conn = get_db_connection()
    device = conn.execute('SELECT * FROM devices WHERE id = ? AND user_id = ?', (device_id, user['id'])).fetchone()
    if device:
        new_fail = 0 if device['is_failed'] == 1 else 1
        conn.execute('UPDATE devices SET is_failed = ? WHERE id = ?', (new_fail, device_id))
        
        msg = f"CHYBA: {device['name']} odpojené!" if new_fail == 1 else f"Zariadenie {device['name']} v poriadku."
        ltype = 'danger' if new_fail == 1 else 'success'
        
        conn.execute('INSERT INTO system_logs (user_id, timestamp, message, type) VALUES (?, ?, ?, ?)',
                     (user['id'], datetime.now().strftime('%H:%M:%S'), msg, ltype))
        conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/delete_device/<int:device_id>', methods=['POST'])
def delete_device_api(device_id):
    user = get_user_by_cookie(request)
    if user:
        conn = get_db_connection()
        conn.execute('DELETE FROM devices WHERE id = ? AND user_id = ?', (device_id, user['id']))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    return jsonify({'error': 'Failed'}), 400

if __name__ == '__main__':
    app.run(debug=True)
