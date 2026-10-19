"""Localhost control center; credentials required on every application startup."""
import asyncio
import logging
import httpx
import secrets
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,HTTPException,Request,Response,WebSocket,WebSocketDisconnect,Depends
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from .broker import CapitalBroker
from .stream import CapitalQuoteStream
from .engine import Store,AdaptiveBrain,SingleTradeEngine

store=Store();brain=AdaptiveBrain(store);engine=SingleTradeEngine(store,brain)
SESSION=secrets.token_urlsafe(48)
COOKIE='nta_session'
worker_task=None
stream_task=None
stream=CapitalQuoteStream()
engine.quote_stream=stream

async def worker():
    error_count=0
    while True:
        try:
            if engine.broker:
                if store.unresolved():await engine.reconcile_outstanding()
                await engine.poll()
                error_count=0;engine.last_error=None
                if engine._paused_for_read and engine.mandate and not engine.operator_paused and not store.unresolved() and len(engine.positions)<=1:
                    engine._paused_for_read=False;engine.armed=True;engine.execution_enabled=True
                    store.event('recovery','Read-only broker outage recovered; autonomous execution resumed')
        except asyncio.CancelledError:raise
        except Exception as exc:
            engine.last_error=f'{type(exc).__name__}: {str(exc)[:180]}'
            error_count+=1
            # Record only exception TYPE and source LOCATION, never credentials,
            # API token headers or full HTTP exception/repr payloads.
            frames=traceback.extract_tb(exc.__traceback__)
            local=next((f for f in reversed(frames) if '/app/' in f.filename.replace('\\','/')),
                frames[-1] if frames else None)
            store.event('error','Broker/engine cycle error',{
                'type':type(exc).__name__,'count':error_count,
                'source':Path(local.filename).name if local else None,
                'line':local.lineno if local else None})
            # Reads can recover automatically. Never keep sending real orders after uncertain broker state.
            if error_count>=3:
                if engine.armed or engine.execution_enabled:engine._paused_for_read=True
                engine.armed=False;engine.execution_enabled=False
        await asyncio.sleep(3 if error_count<3 else 10)

@asynccontextmanager
async def lifespan(app):
    global worker_task,stream_task
    worker_task=asyncio.create_task(worker())
    stream_task=asyncio.create_task(stream.run(engine))
    yield
    worker_task.cancel()
    stream_task.cancel()
    for t in (worker_task,stream_task):
        try:await t
        except asyncio.CancelledError:pass
    engine.cancel_peer_refresh()
    engine.cancel_calendar_refresh()
    brain.save()
    if engine.broker:await engine.broker.close()

app=FastAPI(title='Neural Trading AI',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
app.mount('/static',StaticFiles(directory=str(Path(__file__).parent/'web')),name='static')

@app.middleware('http')
async def headers(request:Request,call_next):
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws://127.0.0.1:8765 ws://localhost:8765; img-src 'self' data:"
    return response

def require_auth(request:Request):
    if request.cookies.get(COOKIE)!=SESSION or not engine.broker:
        raise HTTPException(401,'Login required')
    return True

class Credentials(BaseModel):
    api_key:str=Field(min_length=1,max_length=256)
    identifier:str=Field(min_length=1,max_length=256)
    password:str=Field(min_length=1,max_length=256)


def _capital_connect_error(stage: str, exc: Exception) -> HTTPException:
    """Explain the failed step, without ever exposing credentials, headers or response body."""
    stages={'session':'autentifikācija', 'accounts':'konta nolasīšana', 'positions':'pozīciju nolasīšana'}
    label=stages[stage]
    status=getattr(getattr(exc, 'response', None), 'status_code', None)
    # Only error class / numeric status / request stage are logged. No URL, JSON or headers.
    logging.warning('Capital.com connect failed: stage=%s exception=%s http=%s',
                    stage, type(exc).__name__, status if status is not None else '-')
    if status is not None:
        if status==429:
            return HTTPException(429, f'Capital.com pieprasījumu limits (HTTP 429; {label}). Uzgaidi un mēģini vēlāk.')
        if stage=='session' and status in (401,403):
            return HTTPException(401, f'Capital.com nepieņēma LIVE autentifikāciju (HTTP {status}). Pārbaudi LIVE API atslēgu, identifikatoru un paroli.')
        if status in (401,403):
            return HTTPException(502, f'Capital.com sesija izveidota, bet {label} noraidīta (HTTP {status}).')
        if status>=500:
            return HTTPException(503, f'Capital.com servisa kļūda (HTTP {status}; {label}). Mēģini vēlāk.')
        return HTTPException(502, f'Capital.com {label} neizdevās (HTTP {status}).')
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(503, f'Capital.com neatbildēja laikus ({label}).')
    if isinstance(exc, httpx.RequestError):
        return HTTPException(503, f'Neizdevās sasniegt Capital.com ({label}). Pārbaudi interneta savienojumu.')
    return HTTPException(502, f'Capital.com {label} neizdevās ({type(exc).__name__}). Skati konsoli.')

@app.get('/')
async def index():return FileResponse(Path(__file__).parent/'web'/'index.html')

@app.post('/api/connect')
async def connect(data:Credentials):
    # Do not put credentials in logs, DB, or exception details.
    if engine.broker:
        await engine.broker.close();engine.broker=None
    candidate=CapitalBroker(data.api_key,data.identifier,data.password,live=True)
    stage='session'
    try:
        await candidate.connect()
        stage='accounts'
        initial_accounts=await candidate.accounts()
        stage='positions'
        await candidate.positions()
    except Exception as exc:
        error=_capital_connect_error(stage, exc)
        await candidate.close()
        raise error from None
    engine.attach(candidate);engine.account=initial_accounts
    try:
        await engine.reconcile_outstanding()
        await engine.sync()
    except Exception as exc:
        await candidate.close();engine.broker=None
        raise HTTPException(502,'Broker position reconciliation failed') from exc
    store.event('auth','Capital.com LIVE authenticated; automatic cycle initialized')
    response=JSONResponse({'ok':True,'live':True,'automatic':True})
    response.set_cookie(COOKIE,SESSION,httponly=True,samesite='strict',secure=False,path='/')
    return response

@app.get('/api/status')
async def status(_:bool=Depends(require_auth)):
    return {'state':engine.snapshot(),'events':store.recent(50),'signals':store.signals(30),
            'trades':store.trades(20),'code':engine.policy.info(),
            'candles':store.candles(engine.mandate['epic'],120) if engine.mandate else []}

@app.websocket('/ws/live')
async def live_ws(ws:WebSocket):
    if ws.cookies.get(COOKIE)!=SESSION or not engine.broker:
        await ws.close(code=1008);return
    await ws.accept()
    try:
        while True:
            await ws.send_json({'state':engine.snapshot(),'events':store.recent(40),
                'signals':store.signals(20),'trades':store.trades(20),
                'code':engine.policy.info(),
                'candles':store.candles(engine.mandate['epic'],120) if engine.mandate else []})
            await asyncio.sleep(2)
    except (WebSocketDisconnect,RuntimeError):pass

@app.post('/api/refresh')
async def refresh(_:bool=Depends(require_auth)):
    try:await engine.sync()
    except Exception as exc:raise HTTPException(502,'Position read failed') from exc
    return {'positions':engine.positions,'mandate':engine.mandate}

@app.post('/api/pause')
async def pause(_:bool=Depends(require_auth)):
    engine.armed=False;engine.execution_enabled=False;engine.operator_paused=True
    store.event('control','New trading orders paused by operator')
    return {'ok':True}

@app.post('/api/resume')
async def resume(_:bool=Depends(require_auth)):
    await engine.reconcile_outstanding()
    await engine.sync()
    if engine.store.unresolved():raise HTTPException(409,'Unresolved broker operation; reconciliation required')
    if not engine.mandate:raise HTTPException(409,'First mobile position not yet adopted')
    if len(engine.positions)>1:raise HTTPException(409,'Multiple positions')
    engine.armed=True;engine.execution_enabled=True;engine.operator_paused=False;engine._paused_for_read=False
    store.event('control','Autonomous cycle resumed by operator')
    return {'ok':True}

@app.post('/api/prepare-new-market')
async def prepare_new_market(_:bool=Depends(require_auth)):
    try:return await engine.prepare_new_instrument()
    except ValueError as exc:raise HTTPException(409,str(exc)) from None
    except RuntimeError as exc:raise HTTPException(409,str(exc)) from None
    except Exception as exc:raise HTTPException(502,'Capital.com position verification failed') from exc

@app.post('/api/emergency')
async def emergency(_:bool=Depends(require_auth)):
    engine.armed=False;engine.operator_paused=True
    if len(engine.positions)==1:
        # Temporarily retain execution capability ONLY to send the close request.
        engine.execution_enabled=True
        async with engine.lock:
            await engine.exit_position(engine.positions[0]['dealId'])
    engine.execution_enabled=False
    store.event('control','Emergency stop requested; future entries disabled')
    return {'ok':True,'positions':engine.positions}

@app.get('/api/history')
async def history(_:bool=Depends(require_auth)):
    return {'events':store.recent(400),'signals':store.signals(400),'trades':store.trades(400)}

@app.get('/api/code')
async def code(_:bool=Depends(require_auth)):
    return engine.policy.info()

@app.post('/api/logout')
async def logout(_:bool=Depends(require_auth)):
    engine.armed=False;engine.execution_enabled=False
    if engine.broker:await engine.broker.close()
    engine.broker=None;engine.mandate=None
    response=JSONResponse({'ok':True});response.delete_cookie(COOKIE);return response

@app.get('/api/health')
async def health():return {'app':'Neural Trading AI','ready':True,'authenticated':False}
