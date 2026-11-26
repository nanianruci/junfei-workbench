"""Local team accounts. Passwords and session/capture tokens are stored as hashes."""
import hashlib
import os
import hmac
import json
import re
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from pathlib import Path
from contextlib import contextmanager
import app_paths

DB = Path(os.environ.get('JUNFEI_ACCOUNTS_DB', str(app_paths.DATA / 'accounts.sqlite3')))

@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, name TEXT, password TEXT, role TEXT, status TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, user_id INTEGER, kind TEXT, expires REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, count INTEGER, expires REAL)')
    try:
        with c:
            yield c
    finally:
        c.close()

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def password_hash(value, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 600000).hex()

def public(row):
    return {k: row[k] for k in ('id', 'username', 'name', 'role', 'status')}

def setup_needed():
    with connect() as c:
        return c.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0


def local_workspace():
    """Use the existing desktop workspace ID or provision it without a login."""
    query = "SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1"
    with connect() as c:
        row = c.execute(query).fetchone()
        if row is None:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute(query).fetchone()
            if row is None:
                uid = next_account_id(c)
                c.execute('INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)',
                          (uid, 'local-workspace', '本机工作台', '!', 'admin', 'active'))
                row = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if row['status'] != 'active':
            raise ValueError('本机工作区已停用')
        return public(row)


def next_account_id(c):
    """Keep deleted employee workspace identities from being assigned to new accounts."""
    c.execute('CREATE TABLE IF NOT EXISTS account_sequence(id INTEGER PRIMARY KEY CHECK(id=1),next_id INTEGER NOT NULL)')
    saved=c.execute('SELECT next_id FROM account_sequence WHERE id=1').fetchone()
    current=c.execute('SELECT COALESCE(MAX(id),0)+1 FROM users').fetchone()[0]
    value=max(current,saved[0] if saved else 1)
    c.execute('INSERT OR REPLACE INTO account_sequence VALUES(1,?)',(value,))
    return value

def register(data, setup=False):
    username = str(data.get('username', '')).strip().lower()
    password = str(data.get('password', ''))
    name = str(data.get('name', '')).strip()[:50] or username
    if not re.fullmatch(r'[a-z0-9_.-]{3,40}', username):
        raise ValueError('账号需为3–40位英文字母、数字、点、横线或下划线')
    if not 12 <= len(password) <= 128:
        raise ValueError('密码需为12–128位')
    hashed = password_hash(password)
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        empty = c.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
        if setup != empty:
            raise ValueError('请刷新页面，使用当前可用的注册方式')
        try:
            uid=next_account_id(c)
            c.execute('INSERT INTO users(id,username,name,password,role,status) VALUES (?,?,?,?,?,?)', (uid, username, name, hashed, 'admin' if setup else 'staff', 'active' if setup else 'pending'))
            c.execute('UPDATE account_sequence SET next_id=? WHERE id=1',(uid+1,))
        except sqlite3.IntegrityError:
            raise ValueError('账号已存在')
    return {'message': '管理员创建成功，请登录' if setup else '注册成功，等待管理员审核'}

def issue(c, user_id, kind, seconds):
    token = secrets.token_urlsafe(32)
    c.execute('DELETE FROM tokens WHERE expires < ?', (time.time(),))
    c.execute('INSERT INTO tokens VALUES (?,?,?,?)', (digest(token), user_id, kind, time.time() + seconds))
    return token

def login(data, ip):
    username = str(data.get('username', '')).strip().lower()[:40]
    key = digest(ip + ':' + username)
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        attempt = c.execute('SELECT * FROM attempts WHERE key=?', (key,)).fetchone()
        if attempt and attempt['expires'] > time.time() and attempt['count'] >= 8:
            raise ValueError('尝试过于频繁，请15分钟后再试')
        row = c.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        password = str(data.get('password', ''))[:129]
        valid = row and hmac.compare_digest(row['password'], password_hash(password, row['password'].split(':')[0]))
        if not row:
            password_hash(password, 'unknown-account-timing-salt')
        if not valid or row['status'] != 'active':
            count = attempt['count'] + 1 if attempt and attempt['expires'] > time.time() else 1
            c.execute('INSERT OR REPLACE INTO attempts VALUES (?,?,?)', (key, count, time.time()+900))
            c.commit()
            raise ValueError('账号或密码错误，或账号尚未审核/已停用')
        c.execute('DELETE FROM attempts WHERE key=?', (key,))
        return issue(c, row['id'], 'session', 43200), public(row)

def session_token(headers):
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get('Cookie', ''))
        return cookie['studio_session'].value if 'studio_session' in cookie else ''
    except Exception:
        return ''

def identify(token, kind='session'):
    if not token:
        return None
    with connect() as c:
        row = c.execute('SELECT u.* FROM users u JOIN tokens t ON u.id=t.user_id WHERE t.hash=? AND t.kind=? AND t.expires>? AND u.status=?', (digest(token), kind, time.time(), 'active')).fetchone()
        return public(row) if row else None

def issue_codex(user, product, market, lifetime):
    kind = 'codex:' + json.dumps([product, market], ensure_ascii=False, separators=(',', ':'))
    with connect() as c:
        return issue(c, user, kind, lifetime)


def revoke_codex(user, product, market):
    kind = 'codex:' + json.dumps([product, market], ensure_ascii=False, separators=(',', ':'))
    with connect() as c:
        c.execute('DELETE FROM tokens WHERE user_id=? AND kind=?', (user, kind))


def identify_codex(token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        return None
    with connect() as c:
        row = c.execute("""SELECT u.*, t.kind FROM users u JOIN tokens t ON u.id=t.user_id
            WHERE t.hash=? AND t.kind LIKE 'codex:%' AND t.expires>? AND u.status='active'""",
                        (digest(token), time.time())).fetchone()
    if row is None:
        return None
    return public(row), tuple(json.loads(row['kind'][6:]))


def confirm_operator(user, password, address):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('请输入当前工作台账号密码以确认操作')
    token, operator = login({'username': user['username'], 'password': password}, address)
    logout(token)
    if operator['id'] != user['id']:
        raise ValueError('登录账号已变化，请重新核对')

def local_confirmation_unset(user):
    with connect() as c:
        return c.execute("SELECT 1 FROM users WHERE id=? AND password='!'", (user,)).fetchone() is not None


def set_local_confirmation(user, password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('发布确认密码需为12–128位')
    with connect() as c:
        changed = c.execute("UPDATE users SET password=? WHERE id=? AND role='admin' AND status='active' AND password='!'",
                            (password_hash(password), user)).rowcount
        if changed != 1:
            raise ValueError('已设置发布确认密码，请使用当前密码')


def logout(token):
    with connect() as c:
        c.execute('DELETE FROM tokens WHERE hash=?', (digest(token),))

def users():
    with connect() as c:
        return {'users': [public(r) for r in c.execute('SELECT * FROM users ORDER BY id')]}

def update(data, actor):
    import account_spaces
    # The main service dispatcher reads the same account state under this guard.
    with account_spaces.LOCK:
        return _update(data,actor)


def _update(data, actor):
    uid = int(data.get('id', 0))
    action = data.get('action', '')
    status = 'deleted' if action == 'delete' else 'disabled' if action == 'restore' else data.get('status')
    if actor.get('role') != 'admin' or uid == actor['id']:
        raise ValueError('只能管理其他员工账号，不能删除自己或管理员')
    if status not in ('active', 'disabled', 'deleted') and action != 'purge':
        raise ValueError('无效的账号操作')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM users WHERE id=? AND role=?', (uid, 'staff')).fetchone()
        if not row:
            raise ValueError('员工账号不存在，不能删除管理员')
        if action == 'purge':
            if row['status'] != 'deleted':
                raise ValueError('请先删除账号，再执行彻底删除')
            if data.get('confirmUsername') != row['username']:
                raise ValueError('请输入正确的账号名称确认彻底删除')
            import account_spaces
            next_account_id(c)  # Migrate legacy databases before deleting their highest ID.
            account_spaces.purge_user(uid)
            c.execute('DELETE FROM tokens WHERE user_id=?', (uid,))
            c.execute('DELETE FROM users WHERE id=? AND role=?', (uid, 'staff'))
            return {'message': '账号及其产品、图片、模型配置已彻底删除，无法恢复'}
        if row['status'] == 'deleted' and action != 'restore':
            raise ValueError('请先恢复已删除账号')
        if action == 'restore' and row['status'] != 'deleted':
            raise ValueError('该账号未删除')
        c.execute('UPDATE users SET status=? WHERE id=?', (status, uid))
        c.execute('DELETE FROM tokens WHERE user_id=?', (uid,))
        import account_spaces
        account_spaces.supplier_account_status(uid,status)
    return {'message': '账号已删除，产品与配置保留，可在已删除账号中恢复' if action == 'delete' else '账号已恢复为停用状态，需重新启用' if action == 'restore' else '账号已更新'}


def capture_token(user):
    with connect() as c:
        c.execute('DELETE FROM tokens WHERE user_id=? AND kind=?', (user['id'], 'capture'))
        return {'token': issue(c, user['id'], 'capture', 2592000), 'message': '采集配对码有效期30天，仅可提交商品。重新生成后旧码失效。'}
