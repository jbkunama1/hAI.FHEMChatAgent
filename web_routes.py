import asyncio
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from device_service import DeviceError, search
from diagnostics_service import llm_check, mcp_checks
from menu import TEMPLATES

def make_router(core, devices):
    router = APIRouter()

    @router.get('/', response_class=HTMLResponse)
    async def index(owner=Depends(core.auth)):
        return Path(__file__).with_name('index.html').read_text(encoding='utf-8')

    @router.get('/api/devices')
    async def find_devices(q: str = Query(default='', max_length=120), owner=Depends(core.auth)):
        try:
            matches = search(await devices.devices(), q)
            return {'devices': matches[:30], 'total': len(matches), 'cache_seconds': 30}
        except DeviceError as exc:
            raise HTTPException(503, detail=exc.detail()) from None
        except Exception:
            raise HTTPException(503, detail={'phase': 'device_check', 'code': 'failed'}) from None

    @router.get('/api/menu')
    async def menu(owner=Depends(core.auth)):
        return {'templates': TEMPLATES}

    @router.post('/api/diagnostics')
    async def diagnostics(owner=Depends(core.auth)):
        checks, llm = await asyncio.gather(mcp_checks(core, devices), llm_check(core))
        mcp, fhem = checks
        return {'app': {'status': 'ok'}, 'mcp': mcp, 'fhem_via_mcp': fhem, 'llm': llm,
                'direct_fhem': {'status': 'not_tested', 'message': 'Kein automatischer FHEMWEB-Aufruf'}}

    return router
