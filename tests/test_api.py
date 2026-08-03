# -*- coding: utf-8 -*-
"""后端 API 冒烟测试（使用 Flask 测试客户端，独立临时数据库）"""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ['DATA_DIR'] = _tmp
os.environ['DB_PATH'] = os.path.join(_tmp, 'test.db')

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
