import json, tempfile, unittest
from unittest.mock import patch
from guard import Store

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name+'/state.db'
        self.store = Store(self.path)
        self.action = {'name': 'fhem_set', 'call_id': 'call-1', 'args': {}, 'route': 'mcp', 'destination': 'test'}
        self.messages = [{'role': 'assistant', 'tool_calls': [{'id': 'call-1'}]}]

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def propose(self, ttl=120):
        return self.store.propose('a', self.action, ttl, messages=self.messages)

    def restart(self):
        self.store.db.close()
        self.store = Store(self.path)

    def test_expiry_without_click_resolves_once(self):
        with patch('guard.time.time', return_value=100):
            self.propose(10)
        with patch('guard.time.time', return_value=111):
            self.assertIsNone(self.store.pending('a'))
            self.assertIsNone(self.store.pending('a'))
        answers = [m for m in self.store.load('a') if m['role'] == 'tool']
        self.assertEqual(len(answers), 1)
        self.assertEqual(json.loads(answers[0]['content'])['status'], 'expired')
        self.assertIsNone(self.store.blocked('a'))

    def test_consumed_restart_blocks_and_does_not_replay(self):
        p = self.propose()
        self.store.claim('a', p['id'], True)
        self.restart()
        self.assertIsNotNone(self.store.blocked('a'))
        self.assertEqual(json.loads(self.store.load('a')[-1]['content'])['status'], 'uncertain')
        with self.assertRaises(ValueError):
            self.store.claim('a', p['id'], True)
        with self.assertRaises(ValueError):
            self.propose()
        self.store.acknowledge('a')
        self.assertIsNone(self.store.blocked('a'))

    def test_completed_restart_does_not_block(self):
        p = self.propose()
        self.store.claim('a', p['id'], True)
        self.store.finish('a', p['id'], {'ok': True})
        self.restart()
        self.assertIsNone(self.store.blocked('a'))
        self.assertEqual(len([m for m in self.store.load('a') if m['role'] == 'tool']), 1)

    def test_pending_restart_cancels_and_resolves(self):
        self.propose()
        self.restart()
        self.assertIsNone(self.store.pending('a'))
        self.assertIsNone(self.store.blocked('a'))
        self.assertEqual(json.loads(self.store.load('a')[-1]['content'])['status'], 'cancelled')

    def test_uncertain_survives_reset_and_restart(self):
        p = self.propose()
        self.store.claim('a', p['id'], True)
        self.store.finish('a', p['id'], {'_uncertain': True}, uncertain=True)
        self.store.save('a', [])
        self.restart()
        self.assertIsNotNone(self.store.blocked('a'))
        self.assertIsNone(self.store.blocked('other'))

    def test_running_cannot_acknowledge(self):
        p = self.propose()
        self.store.claim('a', p['id'], True)
        with self.assertRaises(ValueError):
            self.store.acknowledge('a')

    def test_wrong_owner_cannot_finish(self):
        p = self.propose()
        self.store.claim('a', p['id'], True)
        with self.assertRaises(ValueError):
            self.store.finish('other', p['id'], {})
        self.assertIsNotNone(self.store.blocked('a'))

    def test_proposal_and_messages_rollback_together(self):
        self.store.db.execute("CREATE TRIGGER fail_action BEFORE INSERT ON actions BEGIN SELECT RAISE(ABORT, 'test'); END")
        self.store.db.commit()
        with self.assertRaises(Exception):
            self.propose()
        self.assertEqual(self.store.load('a'), [])
        self.assertIsNone(self.store.pending('a'))

if __name__ == '__main__':
    unittest.main()
