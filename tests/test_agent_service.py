import asyncio, json, tempfile, unittest
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from agent_service import AgentService
from guard import Store, readonly

class Call:
    def __init__(self, ident, name, arguments='{}'):
        self.id, self.function = ident, NS(name=name, arguments=arguments)

    def model_dump(self, **kwargs):
        return {'id': self.id, 'type': 'function', 'function': vars(self.function)}

class Client:
    def __init__(self, responses):
        self.chat = NS(completions=NS(create=AsyncMock(side_effect=responses)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

def response(calls=()):
    return NS(choices=[NS(message=NS(content='Antwort' if not calls else None, tool_calls=list(calls)))])

def tool(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object', 'additionalProperties': False}}}

class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name+'/state.db')
        self.core = NS(store=self.store, locks=defaultdict(asyncio.Lock), readonly=readonly,
                       MCP_URL='test', FHEM_URL='', MODEL='test', TTL=120, SYSTEM={'role': 'system', 'content': 'test'},
                       E=lambda key, default=None: 'key' if key == 'OPENAI_API_KEY' else default,
                       discover=AsyncMock(return_value={n: tool(n) for n in ('fhem_set', 'fhem_get')}),
                       safe_execute=AsyncMock(return_value={'ok': True}))
        self.agent = AgentService(self.core)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def pending(self):
        return self.store.propose('a', {'name': 'fhem_set', 'call_id': 'write', 'args': {}, 'route': 'mcp', 'destination': 'test'}, 120,
                                  messages=[{'role': 'assistant', 'tool_calls': [Call('write', 'fhem_set').model_dump()]}])

    async def test_entire_batch_rejected_for_both_orders(self):
        for names in [('fhem_set', 'fhem_get'), ('fhem_get', 'fhem_set')]:
            self.store.save('a', [])
            client = Client([response([Call(str(i), n) for i, n in enumerate(names)]), response()])
            with patch('agent_service.AsyncOpenAI', return_value=client):
                state = await self.agent.chat('a', 'test')
            self.core.safe_execute.assert_not_awaited()
            self.assertIsNone(state['pending'])
            answers = [m for m in state['messages'] if m['role'] == 'tool']
            self.assertEqual({m['tool_call_id'] for m in answers}, {'0', '1'})
            self.assertTrue(all('error' in json.loads(m['content']) for m in answers))

    async def test_single_write_only_proposed(self):
        with patch('agent_service.AsyncOpenAI', return_value=Client([response([Call('write', 'fhem_set')])])):
            state = await self.agent.chat('a', 'test')
        self.assertIsNotNone(state['pending'])
        self.core.safe_execute.assert_not_awaited()

    async def test_timeout_result_blocks_without_replanning(self):
        p = self.pending()
        self.core.safe_execute.return_value = {'_uncertain': True, 'error': 'TimeoutError'}
        with patch('agent_service.AsyncOpenAI') as llm:
            state = await self.agent.decide('a', p['id'], True)
        llm.assert_not_called()
        self.assertIsNotNone(state['recovery'])
        self.core.safe_execute.assert_awaited_once()

    async def test_mcp_error_blocks_without_replanning(self):
        p = self.pending()
        self.core.safe_execute.return_value = {'isError': True}
        with patch('agent_service.AsyncOpenAI') as llm:
            state = await self.agent.decide('a', p['id'], True)
        llm.assert_not_called()
        self.assertIsNotNone(state['recovery'])

    async def test_cancellation_persists_uncertain(self):
        p = self.pending()
        self.core.safe_execute.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.agent.decide('a', p['id'], True)
        self.assertIsNotNone(self.store.blocked('a'))
        self.assertEqual(json.loads(self.store.load('a')[-1]['content'])['status'], 'uncertain')

    async def test_success_followup_exposes_only_reads(self):
        p = self.pending()
        client = Client([response()])
        with patch('agent_service.AsyncOpenAI', return_value=client):
            state = await self.agent.decide('a', p['id'], True)
        offered = client.chat.completions.create.call_args.kwargs['tools']
        self.assertEqual([t['function']['name'] for t in offered], ['fhem_get'])
        self.assertIsNone(state['recovery'])

    async def test_blocked_chat_then_explicit_new_instruction(self):
        p = self.pending()
        self.store.claim('a', p['id'], True)
        self.store.finish('a', p['id'], {'_uncertain': True}, uncertain=True)
        client = Client([response(), response([Call('new', 'fhem_set')])])
        with patch('agent_service.AsyncOpenAI', return_value=client):
            await self.agent.chat('a', 'Status prüfen')
            first_tools = client.chat.completions.create.call_args.kwargs['tools']
            self.assertEqual([t['function']['name'] for t in first_tools], ['fhem_get'])
            state = await self.agent.chat('a', '/weiter Neuer Schreibauftrag')
        self.assertIsNone(state['recovery'])
        self.assertIsNotNone(state['pending'])
        self.core.safe_execute.assert_not_awaited()

    async def test_reject_does_not_call_llm_or_execute(self):
        p = self.pending()
        with patch('agent_service.AsyncOpenAI') as llm:
            state = await self.agent.decide('a', p['id'], False)
        llm.assert_not_called()
        self.core.safe_execute.assert_not_awaited()
        self.assertIsNone(state['pending'])
        self.assertEqual(len([m for m in state['messages'] if m['role'] == 'tool']), 1)

    async def test_expired_proposal_does_not_block_chat(self):
        p = self.pending()
        self.store.db.execute('UPDATE actions SET expires=0 WHERE id=?', (p['id'],))
        self.store.db.commit()
        with patch('agent_service.AsyncOpenAI', return_value=Client([response()])):
            state = await self.agent.chat('a', 'Neuer Leseauftrag')
        self.assertIsNone(state['pending'])
        self.assertTrue(any(m.get('content') == 'Neuer Leseauftrag' for m in state['messages']))
        self.core.safe_execute.assert_not_awaited()

if __name__ == '__main__':
    unittest.main()
