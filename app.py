import os
from flask import Flask, render_template, redirect, url_for, request, flash, session, g
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import datetime
import pytz

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # pokud DB neexistuje, vytvoří se nová
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE,
        name TEXT,
        password TEXT,
        is_admin INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS shift (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        description TEXT,
        is_template INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS shift_step (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shift_id INTEGER,
        position INTEGER,
        description TEXT,
        FOREIGN KEY(shift_id) REFERENCES shift(id)
    );
    CREATE TABLE IF NOT EXISTS progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        shift_id INTEGER,
        step_id INTEGER,
        completed INTEGER DEFAULT 0,
        timestamp TEXT,
        FOREIGN KEY(user_id) REFERENCES user(id),
        FOREIGN KEY(shift_id) REFERENCES shift(id),
        FOREIGN KEY(step_id) REFERENCES shift_step(id)
    );
    CREATE TABLE IF NOT EXISTS note (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shift_id INTEGER,
        user_id INTEGER,
        content TEXT,
        timestamp TEXT,
        FOREIGN KEY(shift_id) REFERENCES shift(id),
        FOREIGN KEY(user_id) REFERENCES user(id)
    );
    """)

    # vytvoření admin účtu, pokud neexistuje
    cur.execute("SELECT id FROM user WHERE email=?", ('admin@example.com',))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO user (email, name, password, is_admin) VALUES (?,?,?,1)",
            ('admin@example.com', 'Admin', generate_password_hash('admin123'))
        )
    conn.commit()
    conn.close()


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')
    login = LoginManager()
    login.init_app(app)
    login.login_view = 'login'

    init_db()

    class User(UserMixin):
        def __init__(self, row):
            self.id = row['id']
            self.email = row['email']
            self.name = row['name']
            self.is_admin = bool(row['is_admin'])

    @login.user_loader
    def load_user(user_id):
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM user WHERE id=?', (int(user_id),))
        row = cur.fetchone()
        conn.close()
        return User(row) if row else None

    @app.before_request
    def before_request():
        g.db = get_db()

    @app.teardown_request
    def teardown_request(exc):
        db = getattr(g, 'db', None)
        if db is not None:
            db.close()

    @app.route('/')
    def index():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))
        return render_template('index.html')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            email = request.form['email'].strip().lower()
            name = request.form['name'].strip()
            password = request.form['password']
            hashed = generate_password_hash(password, method='pbkdf2:sha256')
            try:
                cur = g.db.cursor()
                cur.execute('INSERT INTO user (email,name,password,is_admin) VALUES (?,?,?,0)', (email, name, hashed))
                g.db.commit()
                flash('Registrace úspěšná, přihlas se.', 'success')
                return redirect(url_for('login'))
            except Exception as e:
                flash('Chyba při registraci: ' + str(e), 'danger')
        return render_template('register.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form['email'].strip().lower()
            password = request.form['password']
            cur = g.db.cursor()
            cur.execute('SELECT * FROM user WHERE email=?', (email,))
            row = cur.fetchone()
            if row and check_password_hash(row['password'], password):
                login_user(User(row))
                flash('Přihlášení úspěšné', 'success')
                return redirect(url_for('dashboard'))
            flash('Špatné přihlašovací údaje', 'danger')
        return render_template('login.html')

    @app.route('/dashboard')
    @login_required
    def dashboard():
        cur = g.db.cursor()
        cur.execute('''
            SELECT s.*
            FROM shift s
            JOIN progress p ON s.id = p.shift_id
            WHERE p.user_id=?
            GROUP BY s.id
            HAVING SUM(p.completed) < COUNT(p.id)
            ORDER BY MAX(p.timestamp) DESC
            LIMIT 1
        ''', (current_user.id,))
        current_shift = cur.fetchone()
        prague_tz = pytz.timezone('Europe/Prague')
    now = datetime.now(prague_tz)
    current_time = now.strftime("%H:%M:%S")
    current_date = now.strftime("%A, %d. %B %Y")
        return render_template('dashboard.html', current_shift=current_shift, current_time=current_time, current_date=current_date)

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        flash('Odhlášeno', 'info')
        return redirect(url_for('index'))

    @app.route('/shifts')
    @login_required
    def shifts():
        cur = g.db.cursor()
        cur.execute('SELECT * FROM shift')
        shifts = cur.fetchall()
        return render_template('shifts.html', shifts=shifts)

    @app.route('/shift/<int:shift_id>', methods=['GET', 'POST'])
    @login_required
    def shift_detail(shift_id):
        cur = g.db.cursor()
        cur.execute('SELECT * FROM shift WHERE id=?', (shift_id,))
        shift = cur.fetchone()
        if not shift:
            flash('Směna nenalezena', 'danger')
            return redirect(url_for('shifts'))

        cur.execute('SELECT * FROM shift_step WHERE shift_id=? ORDER BY position', (shift_id,))
        steps = cur.fetchall()

        if request.method == 'POST':
            action = request.form.get('action')

            if action == 'select_shift':
                cur.execute('DELETE FROM progress WHERE user_id=? AND shift_id=?', (current_user.id, shift_id))
                for s in steps:
                    cur.execute(
                        'INSERT INTO progress (user_id, shift_id, step_id, completed, timestamp) VALUES (?,?,?,?,?)',
                        (current_user.id, shift_id, s['id'], 0, datetime.utcnow().isoformat())
                    )
                g.db.commit()
                flash('Směna vybrána – hodně štěstí!', 'success')
                return redirect(url_for('shift_detail', shift_id=shift_id))

            elif action and action.startswith('toggle_'):
                step_id = int(action.split('_', 1)[1])
                cur.execute('SELECT completed FROM progress WHERE user_id=? AND shift_id=? AND step_id=?',
                            (current_user.id, shift_id, step_id))
                row = cur.fetchone()
                if row:
                    newc = 0 if row['completed'] else 1
                    cur.execute('UPDATE progress SET completed=?, timestamp=? WHERE user_id=? AND shift_id=? AND step_id=?',
                                (newc, datetime.utcnow().isoformat(), current_user.id, shift_id, step_id))
                    g.db.commit()
                return redirect(url_for('shift_detail', shift_id=shift_id))

            elif action == 'complete_shift':
                cur.execute('DELETE FROM progress WHERE user_id=? AND shift_id=?', (current_user.id, shift_id))
                g.db.commit()
                flash('Směna byla označena jako dokončená.', 'info')
                return redirect(url_for('dashboard'))

            elif action == 'add_note':
                content = request.form.get('note_content', '').strip()
                if content:
                    cur.execute(
                        'INSERT INTO note (shift_id, user_id, content, timestamp) VALUES (?,?,?,?)',
                        (shift_id, current_user.id, content, datetime.utcnow().isoformat())
                    )
                    g.db.commit()
                    flash('Poznámka přidána.', 'success')
                else:
                    flash('Poznámka nesmí být prázdná.', 'warning')
                return redirect(url_for('shift_detail', shift_id=shift_id))

        cur.execute('SELECT * FROM progress WHERE user_id=? AND shift_id=?', (current_user.id, shift_id))
        progress = {p['step_id']: p for p in cur.fetchall()}

        # načtení poznámek
        cur.execute('''
            SELECT n.*, u.name AS user_name
            FROM note n
            JOIN user u ON n.user_id = u.id
            WHERE n.shift_id=?
            ORDER BY n.timestamp DESC
        ''', (shift_id,))
        notes = cur.fetchall()

        return render_template('shift_detail.html', shift=shift, steps=steps, progress=progress, notes=notes)

    @app.route('/admin', methods=['GET', 'POST'])
    def admin():
        if not session.get('is_admin_access'):
            flash('Nemáš oprávnění', 'danger')
            return redirect(url_for('admin_access'))

        cur = g.db.cursor()
        if request.method == 'POST':
            title = request.form['title']
            description = request.form.get('description', '')
            steps = request.form.get('steps', '').split('\n')
            cur.execute('INSERT INTO shift (title, description, is_template) VALUES (?,?,1)', (title, description))
            shift_id = cur.lastrowid
            pos = 1
            for s in steps:
                s = s.strip()
                if s:
                    cur.execute('INSERT INTO shift_step (shift_id, position, description) VALUES (?,?,?)', (shift_id, pos, s))
                    pos += 1
            g.db.commit()
            flash('Směna vytvořena', 'success')
            return redirect(url_for('admin'))

        cur.execute('SELECT * FROM shift ORDER BY id DESC')
        shifts = cur.fetchall()
        return render_template('admin.html', shifts=shifts)

    @app.route('/admin/edit/<int:shift_id>', methods=['GET', 'POST'])
    def edit_shift(shift_id):
        if not session.get('is_admin_access'):
            flash('Nemáš oprávnění', 'danger')
            return redirect(url_for('admin_access'))

        cur = g.db.cursor()
        cur.execute('SELECT * FROM shift WHERE id=?', (shift_id,))
        shift = cur.fetchone()
        if not shift:
            flash('Směna nenalezena.', 'danger')
            return redirect(url_for('admin'))

        cur.execute('SELECT * FROM shift_step WHERE shift_id=? ORDER BY position', (shift_id,))
        steps = cur.fetchall()
        steps_text = "\n".join([s['description'] for s in steps])

        if request.method == 'POST':
            title = request.form['title']
            description = request.form.get('description', '')
            steps_input = request.form.get('steps', '').split('\n')

            cur.execute('UPDATE shift SET title=?, description=? WHERE id=?', (title, description, shift_id))
            cur.execute('DELETE FROM shift_step WHERE shift_id=?', (shift_id,))
            pos = 1
            for s in steps_input:
                s = s.strip()
                if s:
                    cur.execute('INSERT INTO shift_step (shift_id, position, description) VALUES (?,?,?)', (shift_id, pos, s))
                    pos += 1
            g.db.commit()
            flash('Směna upravena.', 'success')
            return redirect(url_for('admin'))

        return render_template('admin_edit_shift.html', shift=shift, steps_text=steps_text)

    @app.route('/admin/delete/<int:shift_id>', methods=['POST'])
    def delete_shift(shift_id):
        if not session.get('is_admin_access'):
            flash('Nemáš oprávnění', 'danger')
            return redirect(url_for('admin_access'))
        cur = g.db.cursor()
        cur.execute('DELETE FROM progress WHERE shift_id=?', (shift_id,))
        cur.execute('DELETE FROM shift_step WHERE shift_id=?', (shift_id,))
        cur.execute('DELETE FROM note WHERE shift_id=?', (shift_id,))
        cur.execute('DELETE FROM shift WHERE id=?', (shift_id,))
        g.db.commit()
        flash('Směna a všechny související údaje byly smazány.', 'info')
        return redirect(url_for('admin'))

    @app.route('/admin-access', methods=['GET', 'POST'])
    def admin_access():
        if request.method == 'POST':
            password = request.form['password']
            if password == 'admin123':
                session['is_admin_access'] = True
                flash('Přístup povolen', 'success')
                return redirect(url_for('admin'))
            else:
                flash('Špatné heslo', 'danger')
        return render_template('admin_access.html')

    return app

app = create_app()
if __name__ == '__main__':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app = create_app()
    app.wsgi_app = ProxyFix(app.wsgi_app)
    app.run(debug=True)
