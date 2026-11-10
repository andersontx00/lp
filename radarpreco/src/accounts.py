"""Prototype email/password accounts + server-side sessions and synced favorites.

No paid billing, no social login, no unverified email-based password recovery.
Runs over HTTPS in production; never store plaintext passwords or session tokens.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime,timedelta,timezone

SESSION_SECONDS=60*60*24*21

def iso():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def init_schema(cn,postgres=False):
    id_col='BIGSERIAL PRIMARY KEY' if postgres else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    cn.execute(f'''CREATE TABLE IF NOT EXISTS users (
        id {id_col},email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,plan TEXT NOT NULL DEFAULT 'free')''')
    cn.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    cn.execute('CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)')
    cn.execute('''CREATE TABLE IF NOT EXISTS favorites (
        user_id INTEGER NOT NULL,product TEXT NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(user_id,product))''')

def clean_email(value):
    s=str(value or '').strip().casefold()
    if len(s)>160 or not re.fullmatch(r'[a-z0-9._%+\-]{1,70}@[a-z0-9.\-]{1,80}\.[a-z]{2,24}',s):
        raise ValueError('Digite um e-mail válido.')
    return s

def make_hash(password):
    pwd=str(password or '')
    if not 10<=len(pwd)<=128:
        raise ValueError('A senha precisa ter entre 10 e 128 caracteres.')
    salt=os.urandom(16)
    rounds=280_000
    digest=hashlib.pbkdf2_hmac('sha256',pwd.encode(),salt,rounds)
    return f'pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}'

def verify(password,encoded):
    try:
        algorithm, count,salt,digest=encoded.split('$')
        if algorithm!='pbkdf2_sha256':return False
        candidate=hashlib.pbkdf2_hmac('sha256',str(password).encode(),bytes.fromhex(salt),int(count))
        return hmac.compare_digest(candidate,bytes.fromhex(digest))
    except (ValueError,TypeError):return False

def register(cn,email,password):
    normalized=clean_email(email)
    ph=make_hash(password)
    if cn.execute('SELECT id FROM users WHERE email=?',(normalized,)).fetchone():
        raise ValueError('Esse e-mail já está cadastrado.')
    cn.execute('INSERT INTO users(email,password_hash,created_at) VALUES (?,?,?)',(normalized,ph,iso()))
    row=cn.execute('SELECT id,email,plan FROM users WHERE email=?',(normalized,)).fetchone()
    return dict(row)

def login(cn,email,password):
    # Dummy hash evaluation makes non-existing accounts cost similar work.
    normalized=clean_email(email)
    row=cn.execute('SELECT id,email,plan,password_hash FROM users WHERE email=?',(normalized,)).fetchone()
    if not row:
        verify(password,'pbkdf2_sha256$280000$'+'00'*16+'$'+'00'*32)
        raise ValueError('E-mail ou senha incorretos.')
    if not verify(password,row['password_hash']):raise ValueError('E-mail ou senha incorretos.')
    return {'id':row['id'],'email':row['email'],'plan':row['plan']}

def new_session(cn,user):
    secret=secrets.token_urlsafe(36)
    key=hashlib.sha256(secret.encode()).hexdigest()
    expiry=(datetime.now(timezone.utc)+timedelta(seconds=SESSION_SECONDS)).isoformat(timespec='seconds')
    cn.execute('INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)',
               (key,user['id'],expiry,iso()))
    return secret

def who(cn,secret):
    if not isinstance(secret,str) or not 30<=len(secret)<=100:return None
    key=hashlib.sha256(secret.encode()).hexdigest()
    row=cn.execute('''SELECT users.id,users.email,users.plan FROM sessions
        JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires_at>?''',(key,iso())).fetchone()
    return dict(row) if row else None

def logout(cn,secret):
    if isinstance(secret,str):
        cn.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(secret.encode()).hexdigest(),))

def csrf(secret):
    # The session cookie remains HttpOnly; CSRF token is intentionally public to same origin.
    return hashlib.sha256(('radar-csrf-v1:'+str(secret)).encode()).hexdigest()

def check_csrf(secret,candidate):
    return isinstance(candidate,str) and hmac.compare_digest(csrf(secret),candidate)

def favorites(cn,user_id):
    return [r['product'] for r in cn.execute('SELECT product FROM favorites WHERE user_id=? ORDER BY created_at DESC LIMIT 100',(user_id,)).fetchall()]

def save_favorites(cn,user_id,products):
    if not isinstance(products,list) or len(products)>100:raise ValueError('Limite de 100 favoritos.')
    found=[]
    for n in products:
        v=str(n).strip()
        if not 2<=len(v)<=160 or any(ord(c)<32 for c in v):raise ValueError('Produto inválido.')
        if v not in found:found.append(v)
    # Full-state synchronization is atomic. Server treats this account as source of truth.
    cn.execute('DELETE FROM favorites WHERE user_id=?',(user_id,))
    for v in found:
        cn.execute('INSERT INTO favorites(user_id,product,created_at) VALUES(?,?,?)',(user_id,v,iso()))
    return favorites(cn,user_id)
