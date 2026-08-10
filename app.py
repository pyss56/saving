# -*- coding: utf-8 -*-
"""儿童储蓄记账系统 - Flask 后端
轻量局域网应用：Flask + SQLite，简单 token 认证。
单进程同时提供 REST API 与静态前端(PWA)。
"""
import os
import sqlite3
import secrets
import threading
import time
import datetime
import zoneinfo
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------- 路径/配置 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA_DIR, 'savings.db'))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
TOKEN_TTL = 60 * 60 * 24 * 30  # token 有效期 30 天

# 注册开关：未配置默认不允许注册；设为 1/true/yes/on 时允许
ALLOW_REGISTER = os.environ.get('ALLOW_REGISTER', '').strip().lower() in ('1', 'true', 'yes', 'on')

os.makedirs(DATA_DIR, exist_ok=True)

# 显式指定 instance_path，避免 Python 3.14 移除 pkgutil.get_loader 导致的自动探测崩溃
app = Flask(__name__, static_folder=None,
            instance_path=os.path.join(DATA_DIR, 'instance'))

# 内存 token 存储: token -> {user_id, expires}
TOKENS = {}

# 时区：统一由环境变量 TZ 控制（如 TZ=Asia/Shanghai）；未配置时默认东八区
# 系统无 IANA 时区库（如部分 Windows）时回退到固定 UTC+8，保证始终可用
DEFAULT_TZ = 'Asia/Shanghai'
TZ_NAME = os.environ.get('TZ', '').strip() or DEFAULT_TZ
try:
    TZ = zoneinfo.ZoneInfo(TZ_NAME)
except Exception:
    try:
        TZ = zoneinfo.ZoneInfo(DEFAULT_TZ)
    except Exception:
        TZ = datetime.timezone(datetime.timedelta(hours=8), name='UTC+8')


def now_dt():
    """当前时间（带配置时区 TZ 的 aware datetime）"""
    return datetime.datetime.now(TZ)


def now_str():
    return now_dt().strftime('%Y-%m-%d %H:%M:%S')


def parse_dt(value, default=None):
    """解析日期时间字符串为规范 'YYYY-MM-DD HH:MM:SS'。
    兼容 T 分隔（datetime-local）、仅日期、无秒等格式；解析失败返回 default。"""
    if not value:
        return default
    v = str(value).strip().replace('T', ' ')
    if len(v) == 10 and v[4] == '-' and v[7] == '-':
        v += ' 00:00:00'
    elif len(v) == 16 and v[4] == '-' and v[13] == ':':
        v += ':00'
    try:
        return datetime.datetime.strptime(v, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return default


# ---------------- 数据库 ----------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def load_schema():
    with open(os.path.join(BASE_DIR, 'schema.sql'), 'r', encoding='utf-8') as f:
        return f.read()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(load_schema())
    run_migrations(db)
    db.commit()
    db.close()
    seed_if_empty()


def _add_col(db, table, col, ddl):
    cols = [r[1] for r in db.execute('PRAGMA table_info(%s)' % table).fetchall()]
    if col not in cols:
        db.execute('ALTER TABLE %s ADD COLUMN %s' % (table, ddl))


# ---------------- 数据库版本化迁移 ----------------
# 每个版本一个迁移函数：把库从 version-1 升级到 version，函数内需幂等（可重复执行）。
# 追加新的数据库改动时：
#   1. 新增一个迁移函数并登记到 MIGRATIONS（版本号递增）；
#   2. 同步更新 schema.sql 的基础结构（新库直接生成最新结构）；
#   3. 递增 SCHEMA_VERSION。
SCHEMA_VERSION = 4


def mig_v1_cancelled_at(db):
    _add_col(db, 'transactions', 'cancelled_at', 'cancelled_at TEXT')


def mig_v2_settled_at(db):
    _add_col(db, 'term_deposits', 'settled_at', 'settled_at TEXT')


def mig_v3_username_ci_index(db):
    # 用户名大小写不敏感唯一索引：仅当现库无大小写重复时才建，避免脏数据导致启动失败
    dups = db.execute(
        'SELECT LOWER(username) AS u, COUNT(*) AS c FROM users GROUP BY LOWER(username) HAVING c>1'
    ).fetchall()
    if not dups:
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_ci '
                   'ON users (LOWER(username))')


def mig_v4_term_review(db):
    """v4：定期转入/转出需家长审核。
    - term_deposits 重建表，扩展 status 允许 pending_in / pending_out / rejected
      （SQLite 无法改 CHECK 约束，只能重建表；数据原样搬移）；
    - transactions 新增 deposit_id 列，审核定期单据时用于定位对应存单。
    """
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='term_deposits'").fetchone()
    if sql and "'pending_in'" not in (sql[0] or ''):
        db.execute('ALTER TABLE term_deposits RENAME TO term_deposits_old')
        db.execute(
            'CREATE TABLE term_deposits ('
            '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
            '  child_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,'
            '  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,'
            '  amount REAL NOT NULL, rate REAL NOT NULL, term_days INTEGER NOT NULL,'
            '  start_at TEXT NOT NULL, mature_at TEXT NOT NULL,'
            "  status TEXT NOT NULL DEFAULT 'active'"
            " CHECK (status IN ('active','matured','pending_in','pending_out','rejected')),"
            '  settled_at TEXT)')
        db.execute(
            'INSERT INTO term_deposits (id, child_id, account_id, amount, rate, term_days, '
            'start_at, mature_at, status, settled_at) '
            'SELECT id, child_id, account_id, amount, rate, term_days, start_at, mature_at, '
            'status, settled_at FROM term_deposits_old')
        db.execute('DROP TABLE term_deposits_old')
    _add_col(db, 'transactions', 'deposit_id',
             'deposit_id INTEGER REFERENCES term_deposits(id) ON DELETE SET NULL')


MIGRATIONS = {
    1: mig_v1_cancelled_at,
    2: mig_v2_settled_at,
    3: mig_v3_username_ci_index,
    4: mig_v4_term_review,
}


def run_migrations(db):
    """版本化迁移：程序每次启动执行；按版本递增应用未执行的迁移，记录到 schema_meta。

    已执行过的迁移不会重复执行；迁移函数本身幂等，重复执行也安全。
    """
    db.execute('CREATE TABLE IF NOT EXISTS schema_meta '
               '(id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL DEFAULT 0)')
    row = db.execute('SELECT version FROM schema_meta WHERE id=1').fetchone()
    current = row['version'] if row else 0
    if not row:
        db.execute('INSERT INTO schema_meta (id, version) VALUES (1, 0)')
        db.commit()
    for version in sorted(MIGRATIONS):
        if version > current:
            MIGRATIONS[version](db)
            db.execute('UPDATE schema_meta SET version=? WHERE id=1', (version,))
            db.commit()


def reset_db():
    """测试/重置用：清空 token 并重建数据库"""
    TOKENS.clear()
    try:
        os.remove(DB_PATH)
    except OSError:
        pass
    init_db()


def seed_if_empty():
    """首次运行时初始化账号与示例奖惩模板。

    生产默认只创建一个家长账号（用户名/密码可用 INIT_USERNAME / INIT_PASSWORD 环境变量覆盖）；
    SEED_DEMO=1 时（测试用）额外写入 parent1 + child1/child2 演示数据。
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    n = db.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
    if n > 0:
        db.close()
        return
    now = now_str()
    if os.environ.get('SEED_DEMO') == '1':
        # 测试/演示模式：写入演示家长与两个儿童
        p1 = db.execute(
            "INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
            ('parent1', generate_password_hash('123456'), '家长', 'parent', now),
        ).lastrowid
        c1 = db.execute(
            "INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
            ('child1', generate_password_hash('123456'), '小明', 'child', now),
        ).lastrowid
        c2 = db.execute(
            "INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
            ('child2', generate_password_hash('123456'), '小红', 'child', now),
        ).lastrowid
        db.executemany('INSERT INTO parent_child (parent_id, child_id) VALUES (?,?)',
                       [(p1, c1), (p1, c2)])
        db.executemany('INSERT INTO accounts (child_id, balance, interest_rate) VALUES (?,?,?)',
                       [(c1, 20.0, 0.02), (c2, 5.0, 0.03)])
    else:
        # 生产：只初始化一个家长账号（不写入任何账号密码到前端）
        p1 = db.execute(
            "INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
            (os.environ.get('INIT_USERNAME', 'parent'),
             generate_password_hash(os.environ.get('INIT_PASSWORD', '123456')),
             '家长', 'parent', now),
        ).lastrowid
    db.executemany(
        'INSERT INTO templates (parent_id, name, type, amount, description, icon) VALUES (?,?,?,?,?,?)',
        [
            (p1, '扫地', 'reward', 2, '把房间打扫干净', '🧹'),
            (p1, '洗碗', 'reward', 3, '饭后帮忙洗碗', '🍽️'),
            (p1, '完成作业', 'reward', 5, '按时完成作业', '📚'),
            (p1, '超时玩手机', 'punish', 5, '超过约定时间玩手机', '📱'),
            (p1, '打碎东西', 'punish', 10, '不小心打碎物品', '🏺'),
        ],
    )
    db.commit()
    db.close()


# ---------------- 工具函数 ----------------
def error(msg, code=400):
    return jsonify(ok=False, msg=msg), code


def ok(**kw):
    return jsonify(ok=True, **kw)


def get_account(db, child_id):
    row = db.execute('SELECT * FROM accounts WHERE child_id=?', (child_id,)).fetchone()
    if not row:
        db.execute('INSERT INTO accounts (child_id, balance, interest_rate) VALUES (?,0,0.02)', (child_id,))
        db.commit()
        row = db.execute('SELECT * FROM accounts WHERE child_id=?', (child_id,)).fetchone()
    return row


def is_child_of(parent_id, child_id):
    row = get_db().execute(
        'SELECT 1 FROM parent_child WHERE parent_id=? AND child_id=?', (parent_id, child_id)
    ).fetchone()
    return row is not None


def credit(db, child_id, amount, type_, description='', goal_id=None,
           related_task_id=None, reviewed_by=None):
    """入账：amount 正数加钱、负数扣钱，直接生效(已 approved)。"""
    acc = get_account(db, child_id)
    new_balance = round(float(acc['balance']) + amount, 2)
    db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
    cur = db.execute(
        "INSERT INTO transactions (child_id, account_id, goal_id, type, amount, balance_after, "
        "description, status, related_task_id, reviewed_by, reviewed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (child_id, acc['id'], goal_id, type_, amount, new_balance, description,
         'approved', related_task_id, reviewed_by, now_str(), now_str()),
    )
    tx_id = cur.lastrowid
    if goal_id and amount > 0:
        goal = db.execute('SELECT * FROM goals WHERE id=? AND child_id=?', (goal_id, child_id)).fetchone()
        if goal and goal['status'] == 'active':
            new_saved = round(float(goal['saved_amount']) + amount, 2)
            if new_saved >= float(goal['target_amount']):
                db.execute('UPDATE goals SET saved_amount=?, status=?, achieved_at=? WHERE id=?',
                           (new_saved, 'achieved', now_str(), goal_id))
            else:
                db.execute('UPDATE goals SET saved_amount=? WHERE id=?', (new_saved, goal_id))
    db.commit()
    return tx_id


def create_pending_debit(db, child_id, amount, type_, description=''):
    """取钱/消费：创建待家长审核的扣款单，不立即扣账，但立即冻结对应金额。

    可用余额 = 账户余额 - 已冻结的待审核支出，保证不会因多笔待审核取款而超取成负数。
    """
    acc = get_account(db, child_id)
    _, pending_withdraw = pending_amounts(db, child_id)
    available = round(float(acc['balance']) - pending_withdraw, 2)
    if amount > available:
        raise ValueError('可用余额不足（待审核取款已冻结 %s 元，可用 %s 元）'
                         % (fmt_money(pending_withdraw), fmt_money(available)))
    cur = db.execute(
        "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, description, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (child_id, acc['id'], type_, -amount, None, description, 'pending', now_str()),
    )
    db.commit()
    return cur.lastrowid


def fmt_money(v):
    """金额显示：xx.xx 元"""
    return '%.2f' % round(float(v or 0), 2)


def create_pending_credit(db, child_id, amount, type_, description='', goal_id=None):
    """存钱/入账：创建待家长确认的入账单，确认后才入账。"""
    acc = get_account(db, child_id)
    cur = db.execute(
        "INSERT INTO transactions (child_id, account_id, goal_id, type, amount, balance_after, "
        "description, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (child_id, acc['id'], goal_id, type_, amount, None, description, 'pending', now_str()),
    )
    db.commit()
    return cur.lastrowid


def review_pending_tx(db, tx_id, parent_id, action):
    """家长审核待处理的存款/取款/消费单（并发安全）。

    通过「UPDATE ... WHERE status='pending'」原子占位：审核与取消并发时，
    只有先执行成功的一方生效，另一方 rowcount=0 会抛错，避免重复入账/重复扣款。
    """
    tx = db.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
    if not tx:
        raise ValueError('该单据不存在')
    # 定期转入/转出走专门审核逻辑（本金/利息/存单状态联动）
    if tx['deposit_id'] and tx['type'] in ('term_in', 'term_out', 'term_early_out'):
        _review_term_tx(db, tx, parent_id, action)
        return
    # 非多租户：任何家长都可以审批任何孩子的存取/消费申请
    if action == 'approve':
        acc = get_account(db, tx['child_id'])
        # 取款/消费通过时复核余额，防止审核期间可用余额变化导致扣成负数
        if float(tx['amount']) < 0 and float(acc['balance']) < -float(tx['amount']):
            raise ValueError('余额不足，无法通过该取款')
        new_balance = round(float(acc['balance']) + float(tx['amount']), 2)  # amount 正负皆可
        cur = db.execute(
            "UPDATE transactions SET status='approved', balance_after=?, reviewed_by=?, "
            "reviewed_at=? WHERE id=? AND status='pending'",
            (new_balance, parent_id, now_str(), tx_id))
        if cur.rowcount != 1:
            db.rollback()
            raise ValueError('该单据已被处理，无法重复审核')
        db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
        # 存款入账后同步目标进度
        if float(tx['amount']) > 0 and tx['goal_id']:
            goal = db.execute('SELECT * FROM goals WHERE id=? AND child_id=?',
                              (tx['goal_id'], tx['child_id'])).fetchone()
            if goal and goal['status'] == 'active':
                new_saved = round(float(goal['saved_amount']) + float(tx['amount']), 2)
                if new_saved >= float(goal['target_amount']):
                    db.execute('UPDATE goals SET saved_amount=?, status=?, achieved_at=? WHERE id=?',
                               (new_saved, 'achieved', now_str(), tx['goal_id']))
                else:
                    db.execute('UPDATE goals SET saved_amount=? WHERE id=?', (new_saved, tx['goal_id']))
    else:
        cur = db.execute(
            "UPDATE transactions SET status='rejected', reviewed_by=?, reviewed_at=? "
            "WHERE id=? AND status='pending'", (parent_id, now_str(), tx_id))
        if cur.rowcount != 1:
            db.rollback()
            raise ValueError('该单据已被处理，无法重复审核')
    db.commit()


def _revert_term_deposit(db, tx):
    """驳回/取消定期申请时还原存单状态：转入驳回→rejected；结清驳回→恢复 active。"""
    if tx['type'] == 'term_in':
        db.execute("UPDATE term_deposits SET status='rejected' WHERE id=? AND status='pending_in'",
                   (tx['deposit_id'],))
    else:
        db.execute("UPDATE term_deposits SET status='active' WHERE id=? AND status='pending_out'",
                   (tx['deposit_id'],))


def _review_term_tx(db, tx, parent_id, action):
    """家长审核定期转入/转出申请（并发安全，原子占位）。

    - term_in 通过：从活期扣款，存单置 active；驳回：存单置 rejected（款未动）。
    - term_out/term_early_out 通过：返还本金+利息到活期，存单置 matured；
      驳回：存单恢复 active（款仍锁定在定期）。
    """
    now = now_str()
    d = db.execute('SELECT * FROM term_deposits WHERE id=?', (tx['deposit_id'],)).fetchone()
    acc = get_account(db, tx['child_id'])
    if tx['type'] == 'term_in':
        if action == 'approve':
            if not d or d['status'] != 'pending_in':
                raise ValueError('该转存申请状态异常，无法审核')
            amount = float(tx['amount'])  # 负数：活期扣款
            if float(acc['balance']) < -amount:
                raise ValueError('活期余额不足，无法通过该转存')
            new_balance = round(float(acc['balance']) + amount, 2)
            cur = db.execute(
                "UPDATE transactions SET status='approved', balance_after=?, reviewed_by=?, "
                "reviewed_at=? WHERE id=? AND status='pending'",
                (new_balance, parent_id, now, tx['id']))
            if cur.rowcount != 1:
                db.rollback()
                raise ValueError('该单据已被处理，无法重复审核')
            db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
            db.execute("UPDATE term_deposits SET status='active' WHERE id=? AND status='pending_in'",
                       (d['id'],))
            db.commit()
            return
        _reject_pending_term(db, tx, parent_id, now)
        _revert_term_deposit(db, tx)
        db.commit()
        return
    # term_out / term_early_out
    if action == 'approve':
        if not d or d['status'] != 'pending_out':
            raise ValueError('该结清申请状态异常，无法审核')
        amount = float(d['amount'])
        term_days = int(d['term_days'])
        rate = float(d['rate'])
        if now >= d['mature_at']:
            interest = round(amount * rate * term_days / 365, 2)
            early = False
        else:
            start = datetime.datetime.strptime(d['start_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
            elapsed_days = max(0.0, (now_dt() - start).total_seconds() / 86400.0)
            term_portion = min(1.0, elapsed_days / term_days)
            demand_rate = float(acc['interest_rate'])
            blended = rate * term_portion + demand_rate * (1 - term_portion)
            interest = round(amount * blended * elapsed_days / 365, 2)
            early = True
        total = round(amount + interest, 2)
        new_balance = round(float(acc['balance']) + total, 2)
        cur = db.execute(
            "UPDATE transactions SET status='approved', balance_after=?, reviewed_by=?, "
            "reviewed_at=? WHERE id=? AND status='pending'",
            (new_balance, parent_id, now, tx['id']))
        if cur.rowcount != 1:
            db.rollback()
            raise ValueError('该单据已被处理，无法重复审核')
        db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
        db.execute("UPDATE term_deposits SET status='matured', settled_at=? "
                   "WHERE id=? AND status='pending_out'", (now if early else None, d['id']))
        # 补记利息流水（本金流水即原申请单）
        db.execute(
            "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, "
            "description, status, reviewed_by, reviewed_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'],
             'term_early_interest' if early else 'term_interest', interest, new_balance,
             '提前结清利息（未到期部分按活期折算）' if early else '定期利息',
             'approved', parent_id, now, now))
        db.commit()
        return
    _reject_pending_term(db, tx, parent_id, now)
    _revert_term_deposit(db, tx)
    db.commit()


def _reject_pending_term(db, tx, parent_id, now):
    """定期申请驳回：原子置 rejected，失败抛错（与并发审核/取消互斥）。"""
    cur = db.execute(
        "UPDATE transactions SET status='rejected', reviewed_by=?, reviewed_at=? "
        "WHERE id=? AND status='pending'", (parent_id, now, tx['id']))
    if cur.rowcount != 1:
        db.rollback()
        raise ValueError('该单据已被处理，无法重复审核')


# ---------------- 认证 ----------------
def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else None
        t = TOKENS.get(token) if token else None
        if not t or t['expires'] < time.time():
            return error('未登录或登录已过期', 401)
        row = get_db().execute(
            'SELECT id, username, name, role FROM users WHERE id=?', (t['user_id'],)
        ).fetchone()
        if not row:
            return error('用户不存在', 401)
        g.user = dict(row)
        return fn(*args, **kwargs)
    return wrapper


def require_role(role):
    def deco(fn):
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.user['role'] != role:
                return error('无权限执行此操作', 403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


require_parent = require_role('parent')
require_child = require_role('child')


# ---------------- 认证接口 ----------------
@app.route('/api/health')
def health():
    return ok(msg='ok', allow_register=ALLOW_REGISTER)


@app.route('/api/auth/register', methods=['POST'])
def register():
    if not ALLOW_REGISTER:
        return error('注册功能已关闭', 403)
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    name = (data.get('name') or '').strip()
    role = data.get('role')
    if not username or len(username) < 3:
        return error('用户名至少 3 个字符')
    if len(password) < 6:
        return error('密码至少 6 位')
    if not name:
        return error('请填写昵称')
    if role not in ('parent', 'child'):
        return error('角色不合法')
    db = get_db()
    if db.execute('SELECT 1 FROM users WHERE LOWER(username)=LOWER(?)', (username,)).fetchone():
        return error('用户名已存在')
    try:
        cur = db.execute(
            'INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)',
            (username, generate_password_hash(password), name, role, now_str()),
        )
        uid = cur.lastrowid
    except sqlite3.IntegrityError:
        return error('用户名已存在')
    if role == 'child':
        db.execute('INSERT INTO accounts (child_id, balance, interest_rate) VALUES (?,0,0.02)', (uid,))
    db.commit()
    return ok(user={'id': uid, 'username': username, 'name': name, 'role': role})


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    db = get_db()
    row = db.execute('SELECT * FROM users WHERE LOWER(username)=LOWER(?)', (username,)).fetchone()
    if not row or not check_password_hash(row['password_hash'], password):
        return error('用户名或密码错误', 401)
    token = secrets.token_hex(32)
    TOKENS[token] = {'user_id': row['id'], 'expires': time.time() + TOKEN_TTL}
    return ok(token=token, user={'id': row['id'], 'username': row['username'],
                                 'name': row['name'], 'role': row['role']})


@app.route('/api/auth/change-password', methods=['POST'])
@require_auth
def change_password():
    data = request.get_json(silent=True) or {}
    old_pwd = data.get('old_password') or ''
    new_pwd = data.get('new_password') or ''
    if len(new_pwd) < 6:
        return error('新密码至少 6 位')
    db = get_db()
    row = db.execute('SELECT * FROM users WHERE id=?', (g.user['id'],)).fetchone()
    if not row or not check_password_hash(row['password_hash'], old_pwd):
        return error('当前密码不正确')
    db.execute('UPDATE users SET password_hash=? WHERE id=?',
               (generate_password_hash(new_pwd), g.user['id']))
    db.commit()
    # 使该用户其它设备上的 token 失效（保留当前 token，便于前端提示后登出）
    auth = request.headers.get('Authorization', '')
    cur_token = auth[7:] if auth.startswith('Bearer ') else None
    for t, v in list(TOKENS.items()):
        if v['user_id'] == g.user['id'] and t != cur_token:
            TOKENS.pop(t, None)
    return ok(msg='密码修改成功')


@app.route('/api/logout', methods=['POST'])
@require_auth
def logout():
    auth = request.headers.get('Authorization', '')
    token = auth[7:]
    TOKENS.pop(token, None)
    return ok()


@app.route('/api/me')
@require_auth
def me():
    return ok(user=g.user)


def pending_amounts(db, child_id):
    """待处理金额：返回 (待确认入账合计, 待冻结支出合计)。

    待确认入账 = 待家长确认的存钱/入账申请（尚未入账，正数）；
    待冻结支出 = 待家长审核的取钱/消费申请（尚未扣款，已从可用余额中冻结）。
    """
    rows = db.execute(
        "SELECT amount FROM transactions WHERE child_id=? AND status='pending'",
        (child_id,)).fetchall()
    deposit = 0.0
    withdraw = 0.0
    for r in rows:
        amt = float(r['amount'])
        if amt > 0:
            deposit += amt
        else:
            withdraw += -amt
    return round(deposit, 2), round(withdraw, 2)


def account_payload(db, child_id):
    """组装某孩子的完整储蓄账户信息（活期余额/利率阶梯/定期存款/待处理金额）。"""
    acc = get_account(db, child_id)
    tiers = get_tiers(db, child_id)
    eff = effective_annual_rate(db, child_id, float(acc['balance']))
    term_tiers = get_term_tiers(db, child_id)
    deposits = [dict(r) for r in db.execute(
        'SELECT * FROM term_deposits WHERE child_id=? ORDER BY id DESC',
        (child_id,)).fetchall()]
    # 定期合计：存期中 + 待审核结清（款项仍锁定在定期）；待确认转入（pending_in）未入账不计入
    term_balance = round(sum(float(d['amount'])
                             for d in deposits if d['status'] in ('active', 'pending_out')), 2)
    pending_deposit, pending_withdraw = pending_amounts(db, child_id)
    available = round(float(acc['balance']) - pending_withdraw, 2)
    return {'id': acc['id'], 'child_id': acc['child_id'],
            'balance': acc['balance'], 'interest_rate': acc['interest_rate'],
            'last_interest_at': acc['last_interest_at'],
            'tiers': tiers, 'effective_rate': eff,
            'term_tiers': term_tiers, 'term_deposits': deposits,
            'term_balance': term_balance,
            'pending_deposit': pending_deposit,
            'pending_withdraw': pending_withdraw,
            'available_balance': available}


@app.route('/api/me/account')
@require_child
def my_account():
    return ok(account=account_payload(get_db(), g.user['id']))


# ---------------- 家长查看/管理孩子储蓄账户 ----------------
@app.route('/api/children/<int:child_id>/account')
@require_parent
def child_account_api(child_id):
    """家长查看某孩子的完整储蓄账户（活期/定期/利率阶梯）。"""
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    return ok(account=account_payload(get_db(), child_id))


@app.route('/api/children/<int:child_id>/goals')
@require_parent
def list_child_goals(child_id):
    """家长查看某孩子的储蓄目标。"""
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    rows = get_db().execute(
        'SELECT * FROM goals WHERE child_id=? ORDER BY status, id DESC', (child_id,)).fetchall()
    return ok(goals=[dict(r) for r in rows])


@app.route('/api/children/<int:child_id>/deposit', methods=['POST'])
@require_parent
def parent_deposit(child_id):
    """家长直接存入（家长扮演银行，如现场给零花钱/压岁钱），立即入账并同步目标进度。"""
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    data = request.get_json(silent=True) or {}
    try:
        amount = round(float(data.get('amount') or 0), 2)
    except (TypeError, ValueError):
        return error('金额格式不正确')
    if amount <= 0:
        return error('金额需大于 0')
    goal_id = data.get('goal_id')
    desc = (data.get('description') or '').strip() or '家长存入'
    credit(get_db(), child_id, amount, 'parent_deposit',
           description=desc, goal_id=goal_id, reviewed_by=g.user['id'])
    return ok(msg='已存入孩子账户')


# ---------------- 阶梯利率配置 ----------------
@app.route('/api/children/<int:child_id>/tiers')
@require_parent
def get_tiers_api(child_id):
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    return ok(tiers=get_tiers(get_db(), child_id))


@app.route('/api/children/<int:child_id>/tiers', methods=['PUT'])
@require_parent
def set_tiers_api(child_id):
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    data = request.get_json(silent=True) or {}
    tiers = data.get('tiers')
    if tiers is None:
        return error('缺少 tiers 参数')
    parsed = []
    for t in tiers:
        try:
            mn = round(float(t.get('min_amount') or 0), 2)
            rate = round(float(t.get('rate') or 0), 4)
        except (TypeError, ValueError):
            return error('阶梯数据格式不正确')
        if mn < 0 or not (0 <= rate <= 1):
            return error('金额需 ≥ 0，利率需在 0~1 之间')
        parsed.append((mn, rate))
    parsed.sort(key=lambda x: x[0])
    dedup = []
    for mn, rate in parsed:
        if dedup and abs(dedup[-1][0] - mn) < 0.005:
            dedup[-1] = (mn, rate)
        else:
            dedup.append((mn, rate))
    db = get_db()
    db.execute('DELETE FROM interest_tiers WHERE child_id=?', (child_id,))
    db.executemany(
        'INSERT INTO interest_tiers (child_id, min_amount, rate) VALUES (?,?,?)',
        [(child_id, mn, rate) for mn, rate in dedup])
    db.commit()
    return ok(msg='阶梯利率已保存', tiers=[{'min_amount': m, 'rate': r} for m, r in dedup])


# ---------------- 定期利率阶梯（时间阶梯） ----------------
@app.route('/api/children/<int:child_id>/term-tiers')
@require_parent
def get_term_tiers_api(child_id):
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    return ok(tiers=get_term_tiers(get_db(), child_id))


@app.route('/api/children/<int:child_id>/term-tiers', methods=['PUT'])
@require_parent
def set_term_tiers_api(child_id):
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    data = request.get_json(silent=True) or {}
    tiers = data.get('tiers')
    if tiers is None:
        return error('缺少 tiers 参数')
    parsed = []
    for t in tiers:
        try:
            mn = int(float(t.get('min_days') or 0))
            rate = round(float(t.get('rate') or 0), 4)
        except (TypeError, ValueError):
            return error('阶梯数据格式不正确')
        if mn < 0 or not (0 <= rate <= 1):
            return error('天数需 ≥ 0，利率需在 0~1 之间')
        parsed.append((mn, rate))
    parsed.sort(key=lambda x: x[0])
    dedup = []
    for mn, rate in parsed:
        if dedup and dedup[-1][0] == mn:
            dedup[-1] = (mn, rate)
        else:
            dedup.append((mn, rate))
    db = get_db()
    db.execute('DELETE FROM term_tiers WHERE child_id=?', (child_id,))
    db.executemany('INSERT INTO term_tiers (child_id, min_days, rate) VALUES (?,?,?)',
                   [(child_id, mn, rate) for mn, rate in dedup])
    db.commit()
    return ok(msg='定期利率已保存', tiers=[{'min_days': m, 'rate': r} for m, r in dedup])


# ---------------- 定期存款 ----------------
@app.route('/api/term-deposits', methods=['POST'])
@require_child
def create_term_deposit():
    """儿童申请转存定期：不立即扣款，生成待家长审核单（金额从可用余额冻结），
    家长确认后才会从活期扣款并把存单置为 active。"""
    data = request.get_json(silent=True) or {}
    try:
        amount = round(float(data.get('amount') or 0), 2)
        term_days = int(data.get('term_days') or 0)
    except (TypeError, ValueError):
        return error('金额或期限格式不正确')
    if amount <= 0:
        return error('金额需大于 0')
    if term_days < 1:
        return error('存期至少 1 天')
    db = get_db()
    acc = get_account(db, g.user['id'])
    _, pending_withdraw = pending_amounts(db, g.user['id'])
    available = round(float(acc['balance']) - pending_withdraw, 2)
    if amount > available:
        return error('可用余额不足（待审核已冻结 %s 元，可用 %s 元）'
                     % (fmt_money(pending_withdraw), fmt_money(available)))
    rate = term_rate_for_days(db, g.user['id'], term_days)
    start = now_str()
    mature = (now_dt() + datetime.timedelta(days=term_days)).strftime('%Y-%m-%d %H:%M:%S')
    dep_id = db.execute(
        'INSERT INTO term_deposits (child_id, account_id, amount, rate, term_days, start_at, '
        "mature_at, status) VALUES (?,?,?,?,?,?,?, 'pending_in')",
        (g.user['id'], acc['id'], amount, rate, term_days, start, mature)).lastrowid
    db.execute(
        "INSERT INTO transactions (child_id, account_id, deposit_id, type, amount, balance_after, "
        "description, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (g.user['id'], acc['id'], dep_id, 'term_in', -amount, None,
         f'转存定期 {term_days} 天（年利率 {rate*100:.1f}%）', 'pending', start))
    db.commit()
    return ok(msg=f'已提交转存定期申请（{term_days} 天，年利率 {rate*100:.1f}%），等待家长确认后入账',
              rate=rate, mature_at=mature)


@app.route('/api/term-deposits')
@require_auth
def list_term_deposits():
    db = get_db()
    if g.user['role'] == 'child':
        rows = db.execute('SELECT * FROM term_deposits WHERE child_id=? ORDER BY id DESC',
                          (g.user['id'],)).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM term_deposits WHERE child_id IN '
            '(SELECT child_id FROM parent_child WHERE parent_id=?) ORDER BY id DESC',
            (g.user['id'],)).fetchall()
    return ok(deposits=[dict(r) for r in rows])


@app.route('/api/term-deposits/settle', methods=['POST'])
@require_auth
def settle_term_deposits():
    """结算所有已到期的定期：家长直接结算；儿童则逐笔提交家长审核。"""
    db = get_db()
    if g.user['role'] == 'parent':
        # 家长直接结算（家长即审核方，无需二次审核）
        count, interest = mature_due_deposits(db)
        db.commit()
        if count:
            return ok(msg=f'已结算 {count} 笔到期定期，利息 {interest:.2f} 元',
                      count=count, interest=round(interest, 2))
        return ok(msg='没有到期的定期存款', count=0)
    # 儿童：把已到期存单逐个提交家长审核
    now = now_str()
    deps = db.execute(
        "SELECT * FROM term_deposits WHERE child_id=? AND status='active' AND mature_at <= ?",
        (g.user['id'], now)).fetchall()
    n = 0
    for d in deps:
        cur = db.execute(
            "UPDATE term_deposits SET status='pending_out' WHERE id=? AND status='active'", (d['id'],))
        if cur.rowcount != 1:
            continue
        db.execute(
            "INSERT INTO transactions (child_id, account_id, deposit_id, type, amount, balance_after, "
            "description, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'], d['id'], 'term_out', d['amount'], None,
             '定期到期·本金返还（待家长审核）', 'pending', now_str()))
        n += 1
    db.commit()
    if n:
        return ok(msg=f'已提交 {n} 笔到期定期结清申请，等待家长审核', count=n)
    return ok(msg='没有到期的定期存款', count=0)


@app.route('/api/term-deposits/<int:deposit_id>/settle', methods=['POST'])
@require_auth
def settle_one_term_deposit(deposit_id):
    """结算单笔定期存单：家长直接结算；儿童提交申请走家长审核。"""
    db = get_db()
    d = db.execute('SELECT * FROM term_deposits WHERE id=?', (deposit_id,)).fetchone()
    if not d:
        return error('存单不存在')
    if g.user['role'] == 'child':
        if d['child_id'] != g.user['id']:
            return error('无权操作')
    elif not is_child_of(g.user['id'], d['child_id']):
        return error('无权操作')
    if d['status'] != 'active':
        return error('该存单不可结清（可能已在审核中或已结算）')
    if g.user['role'] == 'parent':
        # 家长直接结清（家长即审核方，无需二次审核）
        interest, early = settle_single_deposit(db, d)
        db.commit()
        if early:
            return ok(msg=f'已提前结清存单 #{deposit_id}，利息 {interest:.2f} 元（未到期部分按活期折算）',
                      interest=round(interest, 2), early=True)
        return ok(msg=f'已结算存单 #{deposit_id}，利息 {interest:.2f} 元',
                  interest=round(interest, 2), early=False)
    # 儿童申请结清 → 提交家长审核（存单置 pending_out，金额仍锁定在定期）
    matured = now_str() >= d['mature_at']
    ttype = 'term_out' if matured else 'term_early_out'
    desc = '定期到期·本金返还（待家长审核）' if matured else '定期提前结清·本金返还（待家长审核）'
    cur = db.execute(
        "UPDATE term_deposits SET status='pending_out' WHERE id=? AND status='active'", (d['id'],))
    if cur.rowcount != 1:
        return error('该存单状态已变化，无法申请结清')
    db.execute(
        "INSERT INTO transactions (child_id, account_id, deposit_id, type, amount, balance_after, "
        "description, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (d['child_id'], d['account_id'], d['id'], ttype, d['amount'], None, desc, 'pending', now_str()))
    db.commit()
    return ok(msg='已提交结清申请，等待家长审核后返还本金和利息', early=not matured)


# ---------------- 家长-孩子绑定 ----------------
@app.route('/api/children', methods=['POST'])
@require_parent
def create_child():
    """家长创建账号（默认孩子，可传 role=parent 创建家长账号；注册开关关闭时仍可用）。"""
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    name = (data.get('name') or '').strip()
    role = data.get('role') or 'child'
    if role not in ('parent', 'child'):
        return error('角色不合法')
    if not username or len(username) < 3:
        return error('用户名至少 3 个字符')
    if len(password) < 6:
        return error('密码至少 6 位')
    if not name:
        return error('请填写昵称')
    db = get_db()
    if db.execute('SELECT 1 FROM users WHERE LOWER(username)=LOWER(?)', (username,)).fetchone():
        return error('用户名已存在')
    try:
        cur = db.execute(
            'INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)',
            (username, generate_password_hash(password), name, role, now_str()),
        )
        uid = cur.lastrowid
    except sqlite3.IntegrityError:
        return error('用户名已存在')
    if role == 'child':
        db.execute('INSERT INTO accounts (child_id, balance, interest_rate) VALUES (?,0,0.02)', (uid,))
        db.execute('INSERT INTO parent_child (parent_id, child_id) VALUES (?,?)', (g.user['id'], uid))
    db.commit()
    return ok(msg='孩子账号已创建并绑定' if role == 'child' else '家长账号已创建',
              user={'id': uid, 'username': username, 'name': name, 'role': role})


@app.route('/api/children')
@require_parent
def list_children():
    db = get_db()
    rows = db.execute(
        'SELECT u.id, u.username, u.name, u.role, a.id AS account_id, a.balance, a.interest_rate, '
        'a.last_interest_at '
        'FROM parent_child pc JOIN users u ON u.id=pc.child_id '
        'LEFT JOIN accounts a ON a.child_id=u.id WHERE pc.parent_id=?',
        (g.user['id'],),
    ).fetchall()
    term_rows = db.execute(
        "SELECT child_id, SUM(amount) AS s FROM term_deposits WHERE status='active' "
        'GROUP BY child_id').fetchall()
    term_map = {r['child_id']: float(r['s']) for r in term_rows}
    children = []
    for r in rows:
        item = dict(r)
        item['tiers'] = get_tiers(db, r['id'])
        item['effective_rate'] = effective_annual_rate(db, r['id'], float(r['balance']))
        item['term_balance'] = round(term_map.get(r['id'], 0.0), 2)
        item['term_tiers'] = get_term_tiers(db, r['id'])
        children.append(item)
    return ok(children=children)


@app.route('/api/children/bind', methods=['POST'])
@require_parent
def bind_child():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    db = get_db()
    child = db.execute('SELECT * FROM users WHERE LOWER(username)=LOWER(?) AND role=?',
                       (username, 'child')).fetchone()
    if not child:
        return error('未找到该儿童账号')
    if db.execute('SELECT 1 FROM parent_child WHERE parent_id=? AND child_id=?',
                  (g.user['id'], child['id'])).fetchone():
        return error('已绑定该儿童')
    db.execute('INSERT INTO parent_child (parent_id, child_id) VALUES (?,?)',
               (g.user['id'], child['id']))
    db.commit()
    return ok(msg='绑定成功')


@app.route('/api/children/<int:child_id>', methods=['DELETE'])
@require_parent
def unbind_child(child_id):
    db = get_db()
    db.execute('DELETE FROM parent_child WHERE parent_id=? AND child_id=?', (g.user['id'], child_id))
    db.commit()
    return ok(msg='已解除绑定')


# ---------------- 账户 / 利息 ----------------
@app.route('/api/children/<int:child_id>/rate', methods=['PATCH'])
@require_parent
def set_rate(child_id):
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    data = request.get_json(silent=True) or {}
    try:
        rate = round(float(data.get('interest_rate')), 4)
    except (TypeError, ValueError):
        return error('利率格式不正确')
    if not 0 <= rate <= 1:
        return error('利率需在 0~1 之间（如 0.02 表示 2%）')
    db = get_db()
    db.execute('UPDATE accounts SET interest_rate=? WHERE child_id=?', (rate, child_id))
    db.commit()
    return ok(msg='利率已更新')


def get_tiers(db, child_id):
    """查询某孩子的阶梯利率（按 min_amount 升序）"""
    rows = db.execute(
        'SELECT id, min_amount, rate FROM interest_tiers WHERE child_id=? '
        'ORDER BY min_amount ASC', (child_id,)).fetchall()
    return [dict(r) for r in rows]


def tier_daily_interest(db, child_id, balance):
    """按阶梯计算当日利息（年利率/365，区间边际计息）。
    无阶梯配置时退回账户单利率。
    """
    balance = float(balance)
    if balance <= 0:
        return 0.0
    tiers = get_tiers(db, child_id)
    acc = db.execute('SELECT interest_rate FROM accounts WHERE child_id=?',
                     (child_id,)).fetchone()
    default_rate = float(acc['interest_rate']) if acc else 0.02
    if not tiers:
        return balance * default_rate / 365
    total = 0.0
    prev = 0.0
    n = len(tiers)
    for i, t in enumerate(tiers):
        top = float(tiers[i + 1]['min_amount']) if i + 1 < n else None
        upper = top if top is not None else balance
        low = max(prev, 0.0)
        if balance <= low:
            break
        portion = min(balance, upper) - low
        if portion > 0:
            total += portion * float(t['rate']) / 365
        prev = upper
        if top is None:
            break
    return total


def effective_annual_rate(db, child_id, balance):
    """基于当前余额折算的综合年利率（阶梯计息 / 余额 * 365）"""
    balance = float(balance)
    if balance <= 0:
        acc = db.execute('SELECT interest_rate FROM accounts WHERE child_id=?',
                         (child_id,)).fetchone()
        return float(acc['interest_rate']) if acc else 0.02
    daily = tier_daily_interest(db, child_id, balance)
    return round(daily * 365 / balance, 4)


def settle_interest_for_account(db, acc):
    """按日利率结算单账户利息（支持阶梯）。"""
    if float(acc['balance']) <= 0:
        return 0.0
    interest = round(tier_daily_interest(db, acc['child_id'], float(acc['balance'])), 2)
    if interest <= 0:
        return 0.0
    new_balance = round(float(acc['balance']) + interest, 2)
    db.execute('UPDATE accounts SET balance=?, last_interest_at=? WHERE id=?',
               (new_balance, now_str(), acc['id']))
    db.execute(
        "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, description, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (acc['child_id'], acc['id'], 'interest', interest, new_balance, '定期利息', 'approved', now_str()),
    )
    return interest


def get_term_tiers(db, child_id):
    """定期利率阶梯（时间阶梯）：按存期天数分段，存期越长利率越高"""
    rows = db.execute(
        'SELECT id, min_days, rate FROM term_tiers WHERE child_id=? '
        'ORDER BY min_days ASC', (child_id,)).fetchall()
    return [dict(r) for r in rows]


def term_rate_for_days(db, child_id, days):
    """根据存期天数选择定期利率（min_days <= days 的最高档）"""
    tiers = [dict(r) for r in db.execute(
        'SELECT min_days, rate FROM term_tiers WHERE child_id=? '
        'ORDER BY min_days ASC', (child_id,)).fetchall()]
    acc = db.execute('SELECT interest_rate FROM accounts WHERE child_id=?',
                     (child_id,)).fetchone()
    default = float(acc['interest_rate']) if acc else 0.02
    chosen = default
    for t in tiers:
        if days >= int(t['min_days']):
            chosen = float(t['rate'])
    return chosen


def settle_single_deposit(db, d):
    """结算单笔定期存单：返还本金+利息到活期，存单置为 matured。

    - 到期（now >= mature_at）：全程按定期利率结算。
    - 提前结清（未到期）：已满足存期部分按定期利率、未满足部分按活期利率折算——
      持有比例 term_portion = 已存天数/存期，blended_rate = 定期利率*term_portion
      + 活期利率*(1-term_portion)，利息 = 本金 * blended_rate * 已存天数/365。
    返回 (利息, 是否提前结清)。
    """
    now = now_str()
    amount = float(d['amount'])
    term_days = int(d['term_days'])
    rate = float(d['rate'])
    acc = db.execute('SELECT * FROM accounts WHERE id=?', (d['account_id'],)).fetchone()
    if now >= d['mature_at']:
        interest = round(amount * rate * term_days / 365, 2)
        early = False
    else:
        start = datetime.datetime.strptime(d['start_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
        elapsed_days = max(0.0, (now_dt() - start).total_seconds() / 86400.0)
        term_portion = min(1.0, elapsed_days / term_days)
        demand_rate = float(acc['interest_rate']) if acc else 0.02
        blended = rate * term_portion + demand_rate * (1 - term_portion)
        interest = round(amount * blended * elapsed_days / 365, 2)
        early = True
    total = round(amount + interest, 2)
    new_balance = round(float(acc['balance']) + total, 2)
    db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
    db.execute("UPDATE term_deposits SET status='matured', settled_at=? WHERE id=?",
               (now if early else None, d['id']))
    if early:
        db.execute(
            "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, "
            "description, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'], 'term_early_out', amount, new_balance,
             '定期提前结清·本金返还', 'approved', now))
        db.execute(
            "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, "
            "description, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'], 'term_early_interest', interest, new_balance,
             '提前结清利息（未到期部分按活期折算）', 'approved', now))
    else:
        db.execute(
            "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, "
            "description, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'], 'term_out', amount, new_balance,
             '定期到期·本金返还', 'approved', now))
        db.execute(
            "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, "
            "description, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'], 'term_interest', interest, new_balance,
             '定期利息', 'approved', now))
    return interest, early


def mature_due_deposits(db, child_id=None):
    """结算到期的定期存款：返还本金 + 发放定期利息。
    返回 (到期笔数, 发放利息合计)。child_id 为 None 时结算所有孩子。
    """
    now = now_str()
    if child_id is None:
        deps = db.execute(
            "SELECT * FROM term_deposits WHERE status='active' AND mature_at <= ?",
            (now,)).fetchall()
    else:
        deps = db.execute(
            "SELECT * FROM term_deposits WHERE child_id=? AND status='active' AND mature_at <= ?",
            (child_id, now)).fetchall()
    count = 0
    interest_sum = 0.0
    for d in deps:
        interest, _early = settle_single_deposit(db, d)
        count += 1
        interest_sum += interest
    return count, interest_sum


def settle_all_interest():
    """结算活期利息 + 到期定期，返回 (活期利息合计, 到期笔数, 定期利息合计)"""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    demand_total = 0.0
    for acc in db.execute('SELECT * FROM accounts').fetchall():
        demand_total += settle_interest_for_account(db, acc)
    matured, term_interest = mature_due_deposits(db)
    db.commit()
    db.close()
    return demand_total, matured, term_interest


@app.route('/api/children/<int:child_id>/interest', methods=['POST'])
@require_parent
def settle_child_interest(child_id):
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    db = get_db()
    acc = get_account(db, child_id)
    interest = settle_interest_for_account(db, acc)
    db.commit()
    return ok(msg=f'本次利息 {interest:.2f} 元', interest=interest,
              balance=round(float(acc['balance']) + interest, 2))


@app.route('/api/interest/settle', methods=['POST'])
@require_parent
def settle_interest():
    demand, matured, term_int = settle_all_interest()
    msg = f'活期利息 {demand:.2f} 元'
    if matured:
        msg += f'，到期定期 {matured} 笔（利息 {term_int:.2f} 元）'
    return ok(msg=msg, demand=round(demand, 2), matured=matured,
              term_interest=round(term_int, 2))


# ---------------- 奖惩模板 ----------------
@app.route('/api/templates')
@require_parent
def list_templates():
    db = get_db()
    rows = db.execute('SELECT * FROM templates WHERE parent_id=? ORDER BY id DESC',
                      (g.user['id'],)).fetchall()
    return ok(templates=[dict(r) for r in rows])


@app.route('/api/templates', methods=['POST'])
@require_parent
def create_template():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    ttype = data.get('type')
    if not name:
        return error('请填写项目名称')
    if ttype not in ('reward', 'punish'):
        return error('类型须为 reward 或 punish')
    try:
        amount = round(float(data.get('amount') or 0), 2)
    except (TypeError, ValueError):
        return error('价格格式不正确')
    if amount <= 0:
        return error('每次价格需大于 0')
    db = get_db()
    cur = db.execute(
        'INSERT INTO templates (parent_id, name, type, amount, description, icon, active) '
        'VALUES (?,?,?,?,?,?,1)',
        (g.user['id'], name, ttype, amount, data.get('description'), data.get('icon')),
    )
    db.commit()
    return ok(id=cur.lastrowid, msg='模板已创建')


@app.route('/api/templates/<int:tid>', methods=['PATCH'])
@require_parent
def update_template(tid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    tpl = db.execute('SELECT * FROM templates WHERE id=? AND parent_id=?',
                     (tid, g.user['id'])).fetchone()
    if not tpl:
        return error('模板不存在')
    fields = {}
    if 'name' in data and data['name']:
        fields['name'] = data['name'].strip()
    if 'description' in data:
        fields['description'] = data.get('description')
    if 'icon' in data:
        fields['icon'] = data.get('icon')
    if 'active' in data:
        fields['active'] = 1 if data['active'] else 0
    if 'amount' in data:
        try:
            amount = round(float(data['amount']), 2)
        except (TypeError, ValueError):
            return error('价格格式不正确')
        if amount <= 0:
            return error('每次价格需大于 0')
        fields['amount'] = amount
    if fields:
        db.execute(
            'UPDATE templates SET ' + ', '.join(f'{k}=?' for k in fields) + ' WHERE id=?',
            (*fields.values(), tid),
        )
        db.commit()
    return ok(msg='模板已更新')


@app.route('/api/templates/<int:tid>', methods=['DELETE'])
@require_parent
def delete_template(tid):
    db = get_db()
    db.execute('DELETE FROM templates WHERE id=? AND parent_id=?', (tid, g.user['id']))
    db.commit()
    return ok(msg='模板已删除')


# ---------------- 任务 ----------------
@app.route('/api/tasks')
@require_auth
def list_tasks():
    db = get_db()
    if g.user['role'] == 'child':
        rows = db.execute(
            'SELECT t.*, u.name AS child_name, p.name AS parent_name FROM tasks t '
            'LEFT JOIN users u ON u.id=t.child_id LEFT JOIN users p ON p.id=t.parent_id '
            'WHERE t.child_id=? ORDER BY t.id DESC', (g.user['id'],)).fetchall()
    else:
        rows = db.execute(
            'SELECT t.*, u.name AS child_name, p.name AS parent_name FROM tasks t '
            'LEFT JOIN users u ON u.id=t.child_id LEFT JOIN users p ON p.id=t.parent_id '
            'WHERE t.child_id IN (SELECT child_id FROM parent_child WHERE parent_id=?) '
            'AND (t.parent_id=? OR t.parent_id IS NULL OR t.parent_id IN '
            '(SELECT parent_id FROM parent_child WHERE child_id=t.child_id)) '
            'ORDER BY t.id DESC', (g.user['id'], g.user['id'])).fetchall()
    return ok(tasks=[dict(r) for r in rows])


@app.route('/api/tasks', methods=['POST'])
@require_auth
def create_task():
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    db = get_db()
    reward = 0.0
    template_id = data.get('template_id')
    if template_id:
        tpl = db.execute('SELECT * FROM templates WHERE id=? AND type=?',
                         (template_id, 'reward')).fetchone()
        if not tpl:
            return error('奖励模板不存在')
        reward = round(float(tpl['amount']), 2)
        # 名称/金额留空时使用模板内容
        if not title:
            title = (tpl['name'] or '').strip()
    if not title:
        return error('请填写任务名称')
    try:
        reward = round(float(data.get('reward_amount') or reward or 0), 2)
    except (TypeError, ValueError):
        return error('奖励金额格式不正确')

    if g.user['role'] == 'parent':
        child_id = data.get('child_id')
        if not child_id or not is_child_of(g.user['id'], int(child_id)):
            return error('请选择已绑定的孩子')
        # 家长可直接登记“已完成”的任务：completed=true 时跳过孩子标记完成，状态直接为 completed
        # 可自定义创建时间/完成时间（用于补录历史任务），缺省用当前时间
        completed = bool(data.get('completed'))
        status = 'completed' if completed else 'active'
        created_at = parse_dt(data.get('created_at'), now_str())
        completed_at = parse_dt(data.get('completed_at'), now_str()) if completed else None
        db.execute(
            "INSERT INTO tasks (parent_id, child_id, initiator, template_id, title, description, "
            "reward_amount, status, created_at, approved_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (g.user['id'], int(child_id), 'parent', template_id, title, data.get('description'),
             reward, status, created_at, now_str(), completed_at),
        )
    else:
        # 儿童申请任务：completed=true 表示“已完成”，用 completed_at 做标记，家长审批后直接发奖励
        completed = bool(data.get('completed'))
        completed_at = now_str() if completed else None
        db.execute(
            "INSERT INTO tasks (parent_id, child_id, initiator, template_id, title, description, "
            "reward_amount, status, created_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (None, g.user['id'], 'child', template_id, title, data.get('description'),
             reward, 'pending', now_str(), completed_at),
        )
    db.commit()
    return ok(msg='任务已创建')


@app.route('/api/tasks/<int:task_id>/complete', methods=['PATCH'])
@require_child
def complete_task(task_id):
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id=? AND child_id=?',
                      (task_id, g.user['id'])).fetchone()
    if not task:
        return error('任务不存在')
    if task['status'] != 'active':
        return error('当前状态不可标记完成')
    db.execute("UPDATE tasks SET status='completed', completed_at=? WHERE id=?", (now_str(), task_id))
    db.commit()
    return ok(msg='已提交完成，等待家长确认')


@app.route('/api/tasks/<int:task_id>/review', methods=['PATCH'])
@require_parent
def review_task(task_id):
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    if action not in ('approve', 'reject'):
        return error('action 须为 approve 或 reject')
    db = get_db()
    task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not task:
        return error('任务不存在')
    if task['parent_id'] != g.user['id'] and not is_child_of(g.user['id'], task['child_id']):
        return error('无权操作该任务')
    try:
        if task['status'] == 'pending':  # 孩子发起，家长审批
            if action == 'approve':
                if task['completed_at']:  # 申请时已勾选“已完成”：审批通过即完成并发奖励
                    credit(db, task['child_id'], float(task['reward_amount']), 'task_reward',
                           description='任务奖励：' + task['title'],
                           related_task_id=task_id, reviewed_by=g.user['id'])
                    db.execute("UPDATE tasks SET status='paid', parent_id=?, approved_at=?, reviewed_at=? "
                               "WHERE id=?", (g.user['id'], now_str(), now_str(), task_id))
                else:
                    db.execute("UPDATE tasks SET status='active', parent_id=?, approved_at=? WHERE id=?",
                               (g.user['id'], now_str(), task_id))
            else:
                db.execute("UPDATE tasks SET status='rejected', parent_id=?, reviewed_at=? WHERE id=?",
                           (g.user['id'], now_str(), task_id))
            db.commit()
        elif task['status'] == 'completed':  # 已完成，家长确认发放
            if action == 'approve':
                credit(db, task['child_id'], float(task['reward_amount']), 'task_reward',
                       description='任务奖励：' + task['title'],
                       related_task_id=task_id, reviewed_by=g.user['id'])
                db.execute("UPDATE tasks SET status='paid', reviewed_at=? WHERE id=?",
                           (now_str(), task_id))
            else:
                db.execute("UPDATE tasks SET status='rejected', reviewed_at=? WHERE id=?",
                           (now_str(), task_id))
            db.commit()
        else:
            return error('当前状态不可审核')
    except ValueError as e:
        return error(str(e))
    return ok(msg='处理完成')


@app.route('/api/children/<int:child_id>/punish', methods=['POST'])
@require_parent
def punish_child(child_id):
    if not is_child_of(g.user['id'], child_id):
        return error('无权操作')
    data = request.get_json(silent=True) or {}
    db = get_db()
    if data.get('template_id'):
        tpl = db.execute('SELECT * FROM templates WHERE id=? AND parent_id=? AND type=?',
                         (data['template_id'], g.user['id'], 'punish')).fetchone()
        if not tpl:
            return error('惩罚模板不存在')
        amount = round(float(tpl['amount']), 2)
        desc = '惩罚：' + tpl['name']
    else:
        try:
            amount = round(float(data.get('amount') or 0), 2)
        except (TypeError, ValueError):
            return error('金额格式不正确')
        desc = data.get('description') or '惩罚扣款'
    if amount <= 0:
        return error('金额需大于 0')
    acc = get_account(db, child_id)
    if float(acc['balance']) < amount:
        return error('孩子余额不足，无法执行惩罚扣款')
    credit(db, child_id, -amount, 'punish', description=desc, reviewed_by=g.user['id'])
    return ok(msg='已执行惩罚扣款')


# ---------------- 储蓄目标 ----------------
@app.route('/api/goals')
@require_child
def list_goals():
    db = get_db()
    rows = db.execute('SELECT * FROM goals WHERE child_id=? ORDER BY status, id DESC',
                      (g.user['id'],)).fetchall()
    return ok(goals=[dict(r) for r in rows])


@app.route('/api/goals', methods=['POST'])
@require_child
def create_goal():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return error('请填写目标名称')
    try:
        target = round(float(data.get('target_amount') or 0), 2)
    except (TypeError, ValueError):
        return error('目标金额格式不正确')
    if target <= 0:
        return error('目标金额需大于 0')
    db = get_db()
    cur = db.execute(
        'INSERT INTO goals (child_id, name, target_amount, saved_amount, status, deadline, created_at) '
        'VALUES (?,?,?,?,?,?,?)',
        (g.user['id'], name, target, 0, 'active', data.get('deadline') or None, now_str()),
    )
    db.commit()
    return ok(id=cur.lastrowid, msg='目标已创建')


@app.route('/api/goals/<int:goal_id>/cancel', methods=['PATCH'])
@require_child
def cancel_goal(goal_id):
    db = get_db()
    goal = db.execute('SELECT * FROM goals WHERE id=? AND child_id=?', (goal_id, g.user['id'])).fetchone()
    if not goal or goal['status'] != 'active':
        return error('目标不存在或不可取消')
    db.execute("UPDATE goals SET status='cancelled' WHERE id=?", (goal_id,))
    db.commit()
    return ok(msg='目标已取消')


# ---------------- 流水 / 存取 ----------------
@app.route('/api/transactions', methods=['GET'])
@require_auth
def list_transactions():
    db = get_db()
    child_id = request.args.get('child_id', type=int)
    if g.user['role'] == 'child':
        rows = db.execute(
            'SELECT t.*, u.name AS child_name FROM transactions t '
            'LEFT JOIN users u ON u.id=t.child_id WHERE t.child_id=? ORDER BY t.id DESC',
            (g.user['id'],)).fetchall()
    else:
        if child_id and is_child_of(g.user['id'], child_id):
            rows = db.execute(
                'SELECT t.*, u.name AS child_name FROM transactions t '
                'LEFT JOIN users u ON u.id=t.child_id WHERE t.child_id=? ORDER BY t.id DESC',
                (child_id,)).fetchall()
        else:
            rows = db.execute(
                'SELECT t.*, u.name AS child_name FROM transactions t '
                'LEFT JOIN users u ON u.id=t.child_id '
                'WHERE t.child_id IN (SELECT child_id FROM parent_child WHERE parent_id=?) '
                'ORDER BY t.id DESC', (g.user['id'],)).fetchall()
    return ok(transactions=[dict(r) for r in rows])


@app.route('/api/transactions', methods=['POST'])
@require_child
def create_transaction():
    data = request.get_json(silent=True) or {}
    ttype = data.get('type')
    try:
        amount = round(float(data.get('amount') or 0), 2)
    except (TypeError, ValueError):
        return error('金额格式不正确')
    if amount <= 0:
        return error('金额需大于 0')
    db = get_db()
    acc = get_account(db, g.user['id'])
    try:
        if ttype == 'save':
            goal_id = data.get('goal_id')
            create_pending_credit(db, g.user['id'], amount, 'save',
                                  description=data.get('description') or '存入零花钱', goal_id=goal_id)
            return ok(msg='存款申请已提交，等待家长确认入账', balance=float(acc['balance']))
        if ttype in ('withdraw', 'consume'):
            label = '取款' if ttype == 'withdraw' else '消费'
            create_pending_debit(db, g.user['id'], amount, ttype,
                                 description=data.get('description') or label)
            return ok(msg=f'{label}申请已提交，等待家长审核', balance=float(acc['balance']))
    except ValueError as e:
        return error(str(e))
    return error('不支持的类型')


@app.route('/api/reviews')
@require_parent
def list_pending_reviews():
    """待审核的存取/消费申请。非多租户：任何家长都能看到所有孩子的待审核；
    可传 child_id 只查某个孩子的。"""
    db = get_db()
    child_id = request.args.get('child_id', type=int)
    if child_id:
        rows = db.execute(
            'SELECT t.*, u.name AS child_name FROM transactions t JOIN users u ON u.id=t.child_id '
            'WHERE t.status=? AND t.child_id=? ORDER BY t.id DESC',
            ('pending', child_id)).fetchall()
    else:
        rows = db.execute(
            'SELECT t.*, u.name AS child_name FROM transactions t JOIN users u ON u.id=t.child_id '
            'WHERE t.status=? ORDER BY t.id DESC',
            ('pending',)).fetchall()
    return ok(reviews=[dict(r) for r in rows])


@app.route('/api/transactions/<int:tx_id>/review', methods=['PATCH'])
@require_parent
def review_transaction(tx_id):
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    if action not in ('approve', 'reject'):
        return error('action 须为 approve 或 reject')
    try:
        review_pending_tx(get_db(), tx_id, g.user['id'], action)
    except ValueError as e:
        return error(str(e))
    return ok(msg='处理完成')


@app.route('/api/transactions/<int:tx_id>/cancel', methods=['POST'])
@require_child
def cancel_transaction(tx_id):
    """孩子取消自己的待审批申请（存钱/取钱/消费）。

    仅当单据仍为 pending 时生效（与家长审核并发安全：谁先处理谁生效）。
    """
    db = get_db()
    tx = db.execute('SELECT * FROM transactions WHERE id=? AND child_id=?',
                    (tx_id, g.user['id'])).fetchone()
    if not tx:
        return error('单据不存在')
    cur = db.execute(
        "UPDATE transactions SET status='rejected', cancelled_at=?, reviewed_at=? "
        "WHERE id=? AND status='pending'", (now_str(), now_str(), tx_id))
    if cur.rowcount != 1:
        db.commit()
        return error('该单据已被处理，无法取消')
    if tx['deposit_id'] and tx['type'] in ('term_in', 'term_out', 'term_early_out'):
        _revert_term_deposit(db, tx)
    db.commit()
    return ok(msg='已取消该申请')


# ---------------- 静态前端(PWA) ----------------
@app.route('/')
def index():
    return send_from_directory(PUBLIC_DIR, 'index.html')


@app.route('/<path:path>')
def static_files(path):
    if path.startswith('api/'):
        return error('接口不存在', 404)
    return send_from_directory(PUBLIC_DIR, path)


# ---------------- 定期利息线程 ----------------
def interest_worker():
    while True:
        now = now_dt()
        next_run = (now + datetime.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        time.sleep(max(1, (next_run - now).total_seconds()))
        try:
            settle_all_interest()
            print('[利息] 定期结算完成', now_str())
        except Exception as exc:  # noqa: BLE001
            print('[利息] 结算失败:', exc)


if __name__ == '__main__':
    init_db()
    threading.Thread(target=interest_worker, daemon=True).start()
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, threaded=True)
    print(f'🚀 儿童储蓄系统已启动: http://localhost:{port}')
