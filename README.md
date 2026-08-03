# 🐷 儿童储蓄银行

前后端一体的轻量**儿童储蓄记账系统**：家长管理奖惩、发布家务任务、审核取钱；孩子存钱、定目标、看流水。柔和卡通风格，适配手机浏览器，支持 **PWA**（可添加到主屏幕，类似手机 APP）。

技术栈：**Python + Flask + SQLite**（后端）、**原生 HTML/CSS/JS**（前端，无框架）、**Docker** 一键部署。

---

## ✨ 功能

- **家长绑定多名儿童**：家长账号可绑定/解绑多个儿童账号
- **家务任务 + 奖惩**：
  - 家长发布家务任务 → 孩子完成 → 家长审核后发放零花钱
  - 孩子也可申请任务 → 家长审核通过 → 孩子完成 → 家长确认发放
  - **奖惩模板**：可维护「奖励/惩罚」项目，每个项目设定**每次价格**；惩罚可直接对孩子账户扣款
- **储蓄目标**：孩子建立目标（金额/截止日），查看进度条；存钱可关联目标，达标自动标记「已达成」
- **资金流水**：所有存钱 / 取钱 / 消费 / 奖励 / 惩罚 / 利息都形成流水记录
- **取钱/消费需家长审核**：孩子提交申请，家长通过才扣款
- **利息**：**活期 + 定期**双轨利息
  - 活期：按余额**阶梯年利率**分段计息（如 ≥0→1%、≥100→2%），**每日自动结算**
  - 定期：可把活期转存**定期**（锁定 N 天），**存期越长利率越高**（时间阶梯），到期自动还本付息
  - 均可手动结算/提前手动结算到期定期
- **PWA**：离线可用、可安装到手机桌面，类 APP 体验
- 柔和卡通 UI，手机浏览器自适应

---

## 📁 目录结构

```
├── app.py                    # Flask 后端（API + 静态前端 + 定时利息线程）
├── schema.sql                # SQLite 表结构
├── requirements.txt          # Python 依赖
├── public/                   # 前端（PWA）
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   ├── manifest.json
│   ├── sw.js
│   └── icons/                # 应用图标
├── tools/generate_icons.py   # 图标生成脚本（纯标准库）
├── tests/test_api.py         # 后端冒烟测试
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/docker-build.yml   # GitHub Actions 自动构建镜像
```

---

## 🚀 快速开始

### 方式一：Docker 运行（推荐）

```bash
docker compose up -d --build
# 访问 http://localhost:8000
```

数据持久化在命名卷 `savings-data`（SQLite 数据库位于容器内 `/app/data/savings.db`）。

### 方式二：本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 生成 PWA 图标（可选，仓库已内置）
python tools/generate_icons.py

# 3. 启动（首次自动建库并写入演示数据）
python app.py

# 4. 访问 http://localhost:8000
```

### 运行测试

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

---

## 🧪 演示账号（首次启动自动创建）

| 角色 | 用户名 | 密码 |
|------|--------|------|
| 家长 | `parent1` | `123456` |
| 儿童 | `child1`（小明） | `123456` |
| 儿童 | `child2`（小红） | `123456` |

家长 `parent1` 已绑定 `child1`、`child2`。

---

## ⚙️ 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8000` | 服务端口 |
| `DATA_DIR` | `./data` | 数据目录 |
| `DB_PATH` | `{DATA_DIR}/savings.db` | SQLite 数据库路径 |

---

## 📡 API 一览（前缀 `/api`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/register` | 注册（家长/儿童） |
| POST | `/auth/login` | 登录，返回 token |
| POST | `/auth/change-password` | 修改密码（需当前密码，改后其它设备 token 失效） |
| GET | `/me` / `/me/account` | 当前用户 / 儿童账户 |
| GET/POST | `/children` `/children/bind` | 家长查看/绑定孩子 |
| PATCH/DELETE | `/children/<id>/rate` `/children/<id>` | 设单利率 / 解绑 |
| GET/PUT | `/children/<id>/tiers` | 查看/保存活期阶梯利率 |
| GET/PUT | `/children/<id>/term-tiers` | 查看/保存定期（时间）阶梯利率 |
| POST | `/children/<id>/interest` `/interest/settle` | 结息 / 全局结息 |
| POST | `/term-deposits` | 转存定期（活期转定期） |
| GET | `/term-deposits` | 定期存款列表 |
| POST | `/term-deposits/settle` | 结算到期定期 |
| GET/POST | `/templates` | 奖惩模板列表/新增 |
| PATCH/DELETE | `/templates/<id>` | 修改/删除模板 |
| POST | `/children/<id>/punish` | 惩罚扣款（模板或自定义） |
| GET/POST | `/tasks` | 任务列表 / 创建（家长布置或孩子申请） |
| PATCH | `/tasks/<id>/complete` | 孩子标记完成 |
| PATCH | `/tasks/<id>/review` | 家长审批 / 确认发放 |
| GET/POST | `/goals` | 储蓄目标列表 / 新建 |
| PATCH | `/goals/<id>/cancel` | 取消目标 |
| GET/POST | `/transactions` | 流水列表 / 存钱·取钱·消费 |
| GET | `/reviews` | 家长待审核（取钱/消费） |
| PATCH | `/transactions/<id>/review` | 家长审核取钱/消费 |

认证方式：登录后携带 `Authorization: Bearer <token>` 请求头（局域网轻量方案）。

---

## 📦 数据库

首次启动自动执行 `schema.sql` 建表，并写入演示数据。表：

- `users` 用户（parent / child）
- `parent_child` 家长-儿童绑定
- `accounts` 储蓄账户（余额、年利率、上次结息）
- `templates` 奖惩模板（每次价格）
- `tasks` 家务/奖惩任务（双方向发起 + 状态机）
- `goals` 储蓄目标
- `transactions` 资金流水（取钱/消费待审核）

手动初始化（可选）：

```bash
python -c "import app; app.init_db()"
```

---

## 🐳 Docker 镜像（GitHub Actions）

`.github/workflows/docker-build.yml` 会在推送到 `main` 或打 `v*` 标签时自动：

1. 运行单元测试
2. 构建镜像并推送到 **GHCR**（`ghcr.io/<owner>/saving`）
3. 标签：`main`、`latest`、`sha-xxxx`、语义化版本

拉取运行：

```bash
docker run -d -p 8000:8000 -v savings-data:/app/data ghcr.io/<owner>/saving:latest
```

> 提示：首次使用需在 GitHub 仓库 Settings → Actions 中允许 Workflow 权限写入 Packages（默认即可）。
