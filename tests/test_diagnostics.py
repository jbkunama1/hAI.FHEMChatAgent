import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from device_service import DeviceError, DeviceService, normalize, payload, search, unpack
from diagnostics_service import mcp_checks
from guard import readonly

class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def core(self, response):
        session = NS(call_tool=AsyncMock(return_value=response))
        @asynccontextmanager
        async def connect():
            yield session
        return NS(MCP_URL='https://example.invalid', readonly=readonly, discover=AsyncMock(return_value={'fhem_list_devices':{'function':{'parameters':{'type':'object'}}}}), mcp_session=connect), session

    async def test_single_discovery_and_fresh_device_read(self):
        c, session = self.core(NS(isError=False, structuredContent={'Results':[{'Name':'Lamp'}]}, content=[]))
        service = DeviceService(c)
        await service.devices()
        c.discover.reset_mock(); session.call_tool.reset_mock()
        mcp, fhem = await mcp_checks(c,service)
        c.discover.assert_awaited_once()
        session.call_tool.assert_awaited_once()
        self.assertEqual((mcp['status'],fhem['devices']),('ok',1))

    async def test_connection_error_keeps_phase_and_code(self):
        c, _ = self.core(NS(isError=False, structuredContent={'result':'connection refused'}, content=[]))
        mcp, fhem = await mcp_checks(c,DeviceService(c))
        self.assertEqual(mcp['status'],'ok')
        self.assertEqual(fhem['phase'],'fhem_connection')
        self.assertEqual(fhem['code'],'connection_refused')

    async def test_discovery_failure_not_retried(self):
        c, session = self.core(None)
        c.discover.side_effect = TimeoutError
        mcp, fhem = await mcp_checks(c,DeviceService(c))
        c.discover.assert_awaited_once()
        session.call_tool.assert_not_awaited()
        self.assertEqual(fhem['code'],'timeout')

    async def test_invalid_payload_returns_parsing_error(self):
        c, _ = self.core(NS(isError=False, structuredContent=None, content=[NS(type='text',text='no json')]))
        with self.assertRaises(DeviceError) as ctx:
            await DeviceService(c).devices()
        self.assertEqual(ctx.exception.phase,'parsing')

    def test_wrapped_response_and_search(self):
        r = NS(isError=False,structuredContent={'result':'{"Results":[{"Name":"Lamp","Attributes":{"room":"Wohnzimmer","alias":"Licht"}}]}'},content=[])
        rows = normalize(unpack(payload(r)))
        self.assertEqual(search(rows,'wohn licht')[0]['name'],'Lamp')
        self.assertEqual(unpack({}),[])
