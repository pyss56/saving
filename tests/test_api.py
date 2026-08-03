# -*- coding: utf-8 -*-
"""后端 API 冒烟测试（使用 Flask 测试客户端，独立临时数据库）"""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ['DATA_DIR'] = _tmp
os.environ['DB_PATH'] = os.path.join(_tmp, 'test.db')
# 测试依赖 parent1/child1/child2 演示数据（生产默认不写入，只初始化一个家长账号）
os.environ['SEED_DEMO'] = '1'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402


class SavingsApiTest(unittest.TestCase):
    def setUp(self):
        # 每个用例使用全新数据库，保证相互独立、顺序无关
        appmod.reset_db()
        self.client = appmod.app.test_client()

    def auth(self, username, password='123456'):
        r = self.client.post('/api/auth/login', json={'username': username, 'password': password})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()['token']

    def header(self, token):
        return {'Authorization': 'Bearer ' + token}

    def test_login_and_children(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')
        # 家长看到绑定的两个孩子
        r = c.get('/api/children', headers=self.header(p))
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(len(r.get_json()['children']), 2)
        # 儿童账户
        r = c.get('/api/me/account', headers=self.header(ch))
        self.assertTrue(r.get_json()['ok'])
        self.assertGreaterEqual(r.get_json()['account']['balance'], 0)
        # 错误密码
        r = c.post('/api/auth/login', json={'username': 'parent1', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_task_reward_flow(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')
        children = c.get('/api/children', headers=self.header(p)).get_json()['children']
        child1 = next(x for x in children if x['username'] == 'child1')

        # 建奖励模板
        r = c.post('/api/templates', headers=self.header(p),
                   json={'name': '擦桌子', 'type': 'reward', 'amount': 2.5, 'icon': '🧽'})
        self.assertTrue(r.get_json()['ok'])
        tpl_id = r.get_json()['id']

        # 家长发布任务（用模板）
        r = c.post('/api/tasks', headers=self.header(p),
                   json={'child_id': child1['id'], 'template_id': tpl_id, 'title': '擦桌子'})
        self.assertTrue(r.get_json()['ok'])

        # 孩子看到并完成
        tasks = c.get('/api/tasks', headers=self.header(ch)).get_json()['tasks']
        task = next(t for t in tasks if t['title'] == '擦桌子')
        self.assertEqual(task['status'], 'active')
        r = c.patch('/api/tasks/%d/complete' % task['id'], headers=self.header(ch), json={})
        self.assertTrue(r.get_json()['ok'])

        # 家长确认发放
        r = c.patch('/api/tasks/%d/review' % task['id'], headers=self.header(p),
                    json={'action': 'approve'})
        self.assertTrue(r.get_json()['ok'])
        acc = c.get('/api/me/account', headers=self.header(ch)).get_json()['account']
        self.assertAlmostEqual(acc['balance'], 22.5, places=2)  # 20 + 2.5

    def test_parent_records_completed_task(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')
        children = c.get('/api/children', headers=self.header(p)).get_json()['children']
        child1 = next(x for x in children if x['username'] == 'child1')

        # 家长直接登记一条“已完成”的任务（completed=true）
        r = c.post('/api/tasks', headers=self.header(p),
                   json={'child_id': child1['id'], 'title': '整理书包', 'reward_amount': 3,
                         'completed': True})
        self.assertTrue(r.get_json()['ok'])
        tasks = c.get('/api/tasks', headers=self.header(p)).get_json()['tasks']
        task = next(t for t in tasks if t['title'] == '整理书包')
        self.assertEqual(task['status'], 'completed')
        self.assertIsNotNone(task['completed_at'])

        # 家长确认发放
        r = c.patch('/api/tasks/%d/review' % task['id'], headers=self.header(p),
                    json={'action': 'approve'})
        self.assertTrue(r.get_json()['ok'])
        acc = c.get('/api/me/account', headers=self.header(ch)).get_json()['account']
        self.assertAlmostEqual(acc['balance'], 20 + 3, places=2)  # 初始20 + 3

    def test_child_applies_completed_task(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')

        # 儿童申请任务并勾选“已完成”
        r = c.post('/api/tasks', headers=self.header(ch),
                   json={'title': '帮爸爸擦车', 'reward_amount': 3, 'completed': True})
        self.assertTrue(r.get_json()['ok'])
        tasks = c.get('/api/tasks', headers=self.header(p)).get_json()['tasks']
        task = next(t for t in tasks if t['title'] == '帮爸爸擦车')
        self.assertEqual(task['status'], 'pending')
        self.assertIsNotNone(task['completed_at'])

        # 家长审批通过 → 直接完成并发放奖励
        r = c.patch('/api/tasks/%d/review' % task['id'], headers=self.header(p),
                    json={'action': 'approve'})
        self.assertTrue(r.get_json()['ok'])
        tasks = c.get('/api/tasks', headers=self.header(p)).get_json()['tasks']
        task = next(t for t in tasks if t['title'] == '帮爸爸擦车')
        self.assertEqual(task['status'], 'paid')
        acc = c.get('/api/me/account', headers=self.header(ch)).get_json()['account']
        self.assertAlmostEqual(acc['balance'], 20 + 3, places=2)  # 初始20 + 3

    def test_goal_and_withdraw(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')
        children = c.get('/api/children', headers=self.header(p)).get_json()['children']
        child1 = next(x for x in children if x['username'] == 'child1')

        # 建目标
        r = c.post('/api/goals', headers=self.header(ch),
                   json={'name': '买乐高', 'target_amount': 50})
        self.assertTrue(r.get_json()['ok'])
        goal_id = r.get_json()['id']

        # 存钱并关联目标
        r = c.post('/api/transactions', headers=self.header(ch),
                   json={'type': 'save', 'amount': 10, 'goal_id': goal_id})
        self.assertTrue(r.get_json()['ok'])
        goal = c.get('/api/goals', headers=self.header(ch)).get_json()['goals'][0]
        self.assertAlmostEqual(goal['saved_amount'], 10, places=2)

        # 取钱 → 待审核
        r = c.post('/api/transactions', headers=self.header(ch),
                   json={'type': 'withdraw', 'amount': 5, 'description': '买文具'})
        self.assertTrue(r.get_json()['ok'])
        reviews = c.get('/api/reviews', headers=self.header(p)).get_json()['reviews']
        self.assertTrue(any(t['type'] == 'withdraw' for t in reviews))

        # 家长通过
        tx = next(t for t in reviews if t['type'] == 'withdraw')
        r = c.patch('/api/transactions/%d/review' % tx['id'], headers=self.header(p),
                    json={'action': 'approve'})
        self.assertTrue(r.get_json()['ok'])

    def test_punish_and_interest(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')
        children = c.get('/api/children', headers=self.header(p)).get_json()['children']
        child1 = next(x for x in children if x['username'] == 'child1')

        # 建惩罚模板并执行
        r = c.post('/api/templates', headers=self.header(p),
                   json={'name': '超时玩手机', 'type': 'punish', 'amount': 5})
        self.assertTrue(r.get_json()['ok'])
        tpl_id = r.get_json()['id']
        r = c.post('/api/children/%d/punish' % child1['id'], headers=self.header(p),
                   json={'template_id': tpl_id})
        self.assertTrue(r.get_json()['ok'])

        # 设置较高利率，再手动结息（同时覆盖利率配置接口）
        r = c.patch('/api/children/%d/rate' % child1['id'], headers=self.header(p),
                    json={'interest_rate': 1.0})
        self.assertTrue(r.get_json()['ok'])
        r = c.post('/api/interest/settle', headers=self.header(p), json={})
        self.assertTrue(r.get_json()['ok'])

        # 流水包含惩罚与利息
        txs = c.get('/api/transactions', headers=self.header(ch)).get_json()['transactions']
        types = {t['type'] for t in txs}
        self.assertIn('punish', types)
        self.assertIn('interest', types)


    def test_tier_interest(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')
        children = c.get('/api/children', headers=self.header(p)).get_json()['children']
        child1 = next(x for x in children if x['username'] == 'child1')

        # 设置阶梯：≥0 → 50%，≥10 → 100%（余额 20 → 10*50%/365 + 10*100%/365 ≈ 0.0411）
        r = c.put('/api/children/%d/tiers' % child1['id'], headers=self.header(p),
                  json={'tiers': [{'min_amount': 0, 'rate': 0.5},
                                  {'min_amount': 10, 'rate': 1.0}]})
        self.assertTrue(r.get_json()['ok'])
        tiers = c.get('/api/children/%d/tiers' % child1['id'],
                      headers=self.header(p)).get_json()['tiers']
        self.assertEqual(len(tiers), 2)

        # 综合年利率 ≈ 0.0411*365/20 = 0.75
        acc = c.get('/api/me/account', headers=self.header(ch)).get_json()['account']
        self.assertAlmostEqual(acc['effective_rate'], 0.75, places=3)

        # 结息后余额 = 20.04
        r = c.post('/api/interest/settle', headers=self.header(p), json={})
        self.assertTrue(r.get_json()['ok'])
        acc = c.get('/api/me/account', headers=self.header(ch)).get_json()['account']
        self.assertAlmostEqual(acc['balance'], 20.04, places=2)

    def test_term_deposit(self):
        c = self.client
        p = self.auth('parent1')
        ch = self.auth('child1')
        children = c.get('/api/children', headers=self.header(p)).get_json()['children']
        child1 = next(x for x in children if x['username'] == 'child1')

        # 配置定期利率（时间阶梯）：≥1天 50%，≥30天 100%
        r = c.put('/api/children/%d/term-tiers' % child1['id'], headers=self.header(p),
                  json={'tiers': [{'min_days': 1, 'rate': 0.5}, {'min_days': 30, 'rate': 1.0}]})
        self.assertTrue(r.get_json()['ok'])

        # 活期 20 → 转存 10 元定期 30 天（利率应取 100%）
        r = c.post('/api/term-deposits', headers=self.header(ch),
                   json={'amount': 10, 'term_days': 30})
        self.assertTrue(r.get_json()['ok'])
        self.assertAlmostEqual(r.get_json()['rate'], 1.0, places=3)
        acc = c.get('/api/me/account', headers=self.header(ch)).get_json()['account']
        self.assertAlmostEqual(acc['balance'], 10.0, places=2)      # 活期减少
        self.assertAlmostEqual(acc['term_balance'], 10.0, places=2)  # 定期增加

        # 把到期时间改到过去，模拟到期后结算
        import sqlite3
        db = sqlite3.connect(appmod.DB_PATH)
        db.execute("UPDATE term_deposits SET mature_at='2000-01-01 00:00:00' WHERE status='active'")
        db.commit()
        db.close()

        r = c.post('/api/term-deposits/settle', headers=self.header(ch), json={})
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(r.get_json()['count'], 1)

        # 本金+利息回活期：10 + 10*100%*30/365 ≈ 20.82
        acc = c.get('/api/me/account', headers=self.header(ch)).get_json()['account']
        self.assertAlmostEqual(acc['balance'], 20.82, places=2)
        self.assertAlmostEqual(acc['term_balance'], 0.0, places=2)

    def test_change_password(self):
        c = self.client
        token = self.auth('child1')
        h = self.header(token)
        # 旧密码错误
        r = c.post('/api/auth/change-password', headers=h,
                   json={'old_password': 'wrong', 'new_password': '654321'})
        self.assertEqual(r.status_code, 400)
        # 正确修改
        r = c.post('/api/auth/change-password', headers=h,
                   json={'old_password': '123456', 'new_password': '654321'})
        self.assertTrue(r.get_json()['ok'])
        # 旧密码失效，新密码可登录
        r = c.post('/api/auth/login', json={'username': 'child1', 'password': '123456'})
        self.assertEqual(r.status_code, 401)
        r = c.post('/api/auth/login', json={'username': 'child1', 'password': '654321'})
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
