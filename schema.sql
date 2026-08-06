-- ============================================================
-- 儿童储蓄记账系统 - SQLite 数据库结构
-- 首次启动时由 app.py 自动执行；也可手动执行:
--   sqlite3 data/savings.db < schema.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE,
  password_hash TEXT    NOT NULL,
  name          TEXT    NOT NULL,
  role          TEXT    NOT NULL CHECK (role IN ('parent','child')),
  created_at    TEXT    DEFAULT (datetime('now','localtime'))
);

-- 用户名大小写不敏感唯一（tina / Tina 视为同一个，不允许重复）
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_ci ON users (LOWER(username));

-- 家长-儿童绑定关系
CREATE TABLE IF NOT EXISTS parent_child (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  child_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE (parent_id, child_id)
);

-- 儿童储蓄账户（interest_rate 为年利率，如 0.02 = 2%）
CREATE TABLE IF NOT EXISTS accounts (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  child_id         INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  balance          REAL    NOT NULL DEFAULT 0,
  interest_rate    REAL    NOT NULL DEFAULT 0.02,
  last_interest_at TEXT
);

-- 阶梯利率：按余额区间分段计息
-- 区间 [min_amount, 下一档 min_amount) 使用本档 rate；最高档向上无限
CREATE TABLE IF NOT EXISTS interest_tiers (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  child_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  min_amount REAL    NOT NULL DEFAULT 0,
  rate       REAL    NOT NULL DEFAULT 0.02,
  UNIQUE (child_id, min_amount)
);

-- 定期利率阶梯（时间阶梯）：按存期天数分段（存期越长利率越高）
-- 存期为 N 天时，适用 min_days <= N 的最高档 rate
CREATE TABLE IF NOT EXISTS term_tiers (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  child_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  min_days INTEGER NOT NULL DEFAULT 0,
  rate     REAL    NOT NULL DEFAULT 0.02,
  UNIQUE (child_id, min_days)
);

-- 定期存款：活期转定期锁定，到期返还本金并结算定期利息；也可提前结清（未到期部分按活期折算）
-- status: active=存期中, matured=已结算(到期或提前结清)；settled_at 非空表示提前结清
CREATE TABLE IF NOT EXISTS term_deposits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  child_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  amount     REAL    NOT NULL,
  rate       REAL    NOT NULL,
  term_days  INTEGER NOT NULL,
  start_at   TEXT    NOT NULL,
  mature_at  TEXT    NOT NULL,
  status     TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','matured')),
  settled_at TEXT
);

-- 奖惩模板：每个项目每次的价格
CREATE TABLE IF NOT EXISTS templates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT    NOT NULL,
  type        TEXT    NOT NULL CHECK (type IN ('reward','punish')),
  amount      REAL    NOT NULL DEFAULT 0,
  description TEXT,
  icon        TEXT,
  active      INTEGER NOT NULL DEFAULT 1
);

-- 家务任务/奖惩任务
-- status: pending=孩子发起待审批, active=待完成, completed=待家长确认发放,
--         paid=已发放, rejected=已驳回
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  child_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  initiator     TEXT    NOT NULL DEFAULT 'parent' CHECK (initiator IN ('parent','child')),
  template_id   INTEGER REFERENCES templates(id) ON DELETE SET NULL,
  title         TEXT    NOT NULL,
  description   TEXT,
  reward_amount REAL    NOT NULL DEFAULT 0,
  status        TEXT    NOT NULL DEFAULT 'pending',
  created_at    TEXT    DEFAULT (datetime('now','localtime')),
  approved_at   TEXT,
  completed_at  TEXT,
  reviewed_at   TEXT
);

-- 儿童储蓄目标
CREATE TABLE IF NOT EXISTS goals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  child_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT    NOT NULL,
  target_amount REAL    NOT NULL,
  saved_amount  REAL    NOT NULL DEFAULT 0,
  status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','achieved','cancelled')),
  deadline      TEXT,
  achieved_at   TEXT,
  created_at    TEXT    DEFAULT (datetime('now','localtime'))
);

-- 资金流水：所有存钱/支出/奖励/利息/惩罚均形成记录
-- type: task_reward 任务奖励, punish 惩罚扣款, save 存钱, withdraw 取钱,
--       consume 消费, interest 利息, parent_deposit 家长存入
-- amount 正数=收入, 负数=支出; status: pending=待家长审核, approved=已入账, rejected=已驳回/已取消
CREATE TABLE IF NOT EXISTS transactions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  child_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  goal_id         INTEGER REFERENCES goals(id) ON DELETE SET NULL,
  type            TEXT    NOT NULL,
  amount          REAL    NOT NULL,
  balance_after   REAL,
  description     TEXT,
  status          TEXT    NOT NULL DEFAULT 'approved' CHECK (status IN ('pending','approved','rejected')),
  related_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  reviewed_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  reviewed_at     TEXT,
  cancelled_at    TEXT,
  created_at      TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_transactions_child ON transactions (child_id);
CREATE INDEX IF NOT EXISTS idx_tasks_child ON tasks (child_id);
CREATE INDEX IF NOT EXISTS idx_goals_child ON goals (child_id);
