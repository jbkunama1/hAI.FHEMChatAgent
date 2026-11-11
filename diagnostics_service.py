import asyncio
import httpx
from device_service import DeviceError

async def llm_check(core):
    if not core.E('OPENAI_API_KEY', ''):
        return {'status': 'missing', 'message': 'OPENAI_API_KEY fehlt'}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            r = await client.get(core.E('OPENAI_BASE_URL', 'https://9router.arbeitermili.eu/v1').rstrip('/')+'/models', headers={'Authorization': 'Bearer '+core.E('OPENAI_API_KEY')})
        return {'status': 'ok' if r.is_success else 'error', 'http_status': r.status_code, 'message': 'Nur Modellliste, kein Generierungs- oder Tool-Calling-Test'}
    except Exception:
        return {'status': 'error', 'message': 'LLM-Modellliste nicht erreichbar'}

async def mcp_checks(core, service):
    if not core.MCP_URL:
        failure = {'status': 'missing', 'phase': 'configuration', 'code': 'missing_mcp_url'}
        return failure, dict(failure)
    try:
        tools = await core.discover()
    except Exception as exc:
        failure = {'status': 'error', 'phase': 'discovery', 'code': 'timeout' if isinstance(exc, TimeoutError) else 'failed'}
        return failure, dict(failure)
    mcp = {'status': 'ok' if tools else 'error', 'fhem_tools': len(tools), 'tools': [{'name': n, 'required': t['function']['parameters'].get('required', [])} for n, t in sorted(tools.items())]}
    try:
        rows = await service.devices(tools=tools, refresh=True)
        fhem = {'status': 'ok', 'devices': len(rows), 'message': 'Frischer MCP-Lesezugriff, kein Cache-Erfolg'}
    except DeviceError as exc:
        fhem = {'status': 'error', **exc.detail()}
    except Exception:
        fhem = {'status': 'error', 'phase': 'device_check', 'code': 'failed'}
    return mcp, fhem
