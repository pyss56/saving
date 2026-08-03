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
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------- 路径/配置 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA_DIR, 'savings.db'))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
TOKEN_TTL = 60 * 60 * 24 * 30  # token 有效期 30 天

os.makedirs(DATA_DIR, exist_ok=True)

# 显式指定 instance_path，避免 Python 3.14 移除 pkgutil.get_loader 导致的自动探测崩溃
app = Flask(__name__, static_folder=None,
            instance_path=os.path.join(DATA_DIR, 'instance'))

# 内存 token 存储: token -> {user_id, expires}
TOKENS = {}


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


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
    db.executescript(load_schema())
    db.commit()
    db.close()
    seed_if_empty()


def reset_db():
    """测试/重置用：清空 token 并重建数据库"""
    TOKENS.clear()
    try:
        os.remove(DB_PATH)
    except OSError:
        pass
    init_db()


def seed_if_empty():
    """首次运行时写入演示账号与示例奖惩模板"""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    n = db.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c']
    if n > 0:
        db.close()
        return
    now = now_str()
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
    """取钱/消费：创建待家长审核的扣款单，不立即扣账。"""
    acc = get_account(db, child_id)
    if float(acc['balance']) < amount:
        raise ValueError('余额不足')
    cur = db.execute(
        "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, description, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (child_id, acc['id'], type_, -amount, None, description, 'pending', now_str()),
    )
    db.commit()
    return cur.lastrowid


def review_pending_tx(db, tx_id, parent_id, action):
    """家长审核待处理的取款/消费单。"""
    tx = db.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
    if not tx or tx['status'] != 'pending':
        raise ValueError('该单据不可审核')
    if not is_child_of(parent_id, tx['child_id']):
        raise ValueError('无权审核该单据')
    if action == 'approve':
        acc = get_account(db, tx['child_id'])
        new_balance = round(float(acc['balance']) + float(tx['amount']), 2)  # amount 为负数
        db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
        db.execute("UPDATE transactions SET status='approved', balance_after=?, reviewed_by=?, "
                   "reviewed_at=? WHERE id=?", (new_balance, parent_id, now_str(), tx_id))
    else:
        db.execute("UPDATE transactions SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?",
                   (parent_id, now_str(), tx_id))
    db.commit()


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
    return ok(msg='ok')


@app.route('/api/auth/register', methods=['POST'])
def register():
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
    if db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
        return error('用户名已存在')
    cur = db.execute(
        'INSERT INTO users (username, password_hash, name, role, created_at) VALUES (?,?,?,?,?)',
        (username, generate_password_hash(password), name, role, now_str()),
    )
    uid = cur.lastrowid
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
    row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
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


@app.route('/api/me/account')
@require_child
def my_account():
    db = get_db()
    acc = get_account(db, g.user['id'])
    tiers = get_tiers(db, g.user['id'])
    eff = effective_annual_rate(db, g.user['id'], float(acc['balance']))
    term_tiers = get_term_tiers(db, g.user['id'])
    deposits = [dict(r) for r in db.execute(
        'SELECT * FROM term_deposits WHERE child_id=? ORDER BY id DESC',
        (g.user['id'],)).fetchall()]
    term_balance = round(sum(float(d['amount']) for d in deposits if d['status'] == 'active'), 2)
    return ok(account={'id': acc['id'], 'child_id': acc['child_id'],
                       'balance': acc['balance'], 'interest_rate': acc['interest_rate'],
                       'last_interest_at': acc['last_interest_at'],
                       'tiers': tiers, 'effective_rate': eff,
                       'term_tiers': term_tiers, 'term_deposits': deposits,
                       'term_balance': term_balance})


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
    if float(acc['balance']) < amount:
        return error('活期余额不足')
    rate = term_rate_for_days(db, g.user['id'], term_days)
    start = now_str()
    mature = (datetime.datetime.now() + datetime.timedelta(days=term_days)).strftime('%Y-%m-%d %H:%M:%S')
    new_balance = round(float(acc['balance']) - amount, 2)
    db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
    db.execute(
        'INSERT INTO term_deposits (child_id, account_id, amount, rate, term_days, start_at, '
        'mature_at, status) VALUES (?,?,?,?,?,?,?,?)',
        (g.user['id'], acc['id'], amount, rate, term_days, start, mature, 'active'))
    db.execute(
        "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, description, "
        "status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (g.user['id'], acc['id'], 'term_in', -amount, new_balance,
         f'转存定期 {term_days} 天（年利率 {rate*100:.1f}%）', 'approved', start))
    db.commit()
    return ok(msg=f'已转存定期 {term_days} 天，年利率 {rate*100:.1f}%，到期自动还本付息',
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
    db = get_db()
    child_id = g.user['id'] if g.user['role'] == 'child' else None
    count, interest = mature_due_deposits(db, child_id)
    db.commit()
    if count:
        return ok(msg=f'已结算 {count} 笔到期定期，利息 {interest:.2f} 元',
                  count=count, interest=round(interest, 2))
    return ok(msg='没有到期的定期存款', count=0)


# ---------------- 家长-孩子绑定 ----------------
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
    child = db.execute('SELECT * FROM users WHERE username=? AND role=?', (username, 'child')).fetchone()
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
        interest = round(float(d['amount']) * float(d['rate']) * int(d['term_days']) / 365, 2)
        total = round(float(d['amount']) + interest, 2)
        acc = db.execute('SELECT * FROM accounts WHERE id=?', (d['account_id'],)).fetchone()
        new_balance = round(float(acc['balance']) + total, 2)
        db.execute('UPDATE accounts SET balance=? WHERE id=?', (new_balance, acc['id']))
        db.execute("UPDATE term_deposits SET status='matured' WHERE id=?", (d['id'],))
        db.execute(
            "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, "
            "description, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'], 'term_out', float(d['amount']), new_balance,
             '定期到期·本金返还', 'approved', now))
        db.execute(
            "INSERT INTO transactions (child_id, account_id, type, amount, balance_after, "
            "description, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (d['child_id'], d['account_id'], 'term_interest', interest, new_balance,
             '定期利息', 'approved', now))
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
    if not title:
        return error('请填写任务名称')
    db = get_db()
    reward = 0.0
    template_id = data.get('template_id')
    if template_id:
        tpl = db.execute('SELECT * FROM templates WHERE id=? AND type=?',
                         (template_id, 'reward')).fetchone()
        if not tpl:
            return error('奖励模板不存在')
        reward = round(float(tpl['amount']), 2)
        if not title:
            title = tpl['name']
    try:
        reward = round(float(data.get('reward_amount') or reward or 0), 2)
    except (TypeError, ValueError):
        return error('奖励金额格式不正确')

    if g.user['role'] == 'parent':
        child_id = data.get('child_id')
        if not child_id or not is_child_of(g.user['id'], int(child_id)):
            return error('请选择已绑定的孩子')
        db.execute(
            "INSERT INTO tasks (parent_id, child_id, initiator, template_id, title, description, "
            "reward_amount, status, created_at, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (g.user['id'], int(child_id), 'parent', template_id, title, data.get('description'),
             reward, 'active', now_str(), now_str()),
        )
    else:
        db.execute(
            "INSERT INTO tasks (parent_id, child_id, initiator, template_id, title, description, "
            "reward_amount, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (None, g.user['id'], 'child', template_id, title, data.get('description'),
             reward, 'pending', now_str()),
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
            credit(db, g.user['id'], amount, 'save',
                   description=data.get('description') or '存入零花钱', goal_id=goal_id)
            return ok(msg='存款成功', balance=round(float(acc['balance']) + amount, 2))
        if ttype in ('withdraw', 'consume'):
            if float(acc['balance']) < amount:
                return error('余额不足')
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
    db = get_db()
    rows = db.execute(
        'SELECT t.*, u.name AS child_name FROM transactions t JOIN users u ON u.id=t.child_id '
        'WHERE t.status=? AND t.child_id IN '
        '(SELECT child_id FROM parent_child WHERE parent_id=?) ORDER BY t.id DESC',
        ('pending', g.user['id'])).fetchall()
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
        now = datetime.datetime.now()
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
