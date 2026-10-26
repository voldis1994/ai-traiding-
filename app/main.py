"""Neural Trading AI — operator console + multi-tenant admin/client portals."""
import asyncio
import logging
import os
import httpx
import secrets
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,HTTPException,Request,WebSocket,WebSocketDisconnect,Depends
from fastapi.responses import FileResponse,JSONResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from .broker import CapitalBroker
from .brokers import create_broker, list_brokers
from .stream import CapitalQuoteStream
from .engine import Store,AdaptiveBrain,SingleTradeEngine
from .storage import DATA
from .platform import PlatformStore
from .runtime_hub import RuntimeHub

store=Store();brain=AdaptiveBrain(store);engine=SingleTradeEngine(store,brain)
SESSION=secrets.token_urlsafe(48)
COOKIE='nta_session'
ADMIN_COOKIE='nta_admin'
CLIENT_COOKIE='nta_client'
worker_task=None
stream_task=None
stream=CapitalQuoteStream()
engine.quote_stream=stream

platform=PlatformStore()
hub=RuntimeHub(platform)
WEB=Path(__file__).parent/'web'


def _bootstrap_admin():
    user=(os.getenv('NTA_ADMIN_USER') or 'admin').strip() or 'admin'
    password=os.getenv('NTA_ADMIN_PASSWORD') or 'admin123'
    if platform.admin_count()==0:
        platform.ensure_admin(user,password)
        logging.warning('Bootstrapped admin user %r — change NTA_ADMIN_PASSWORD in production',user)
    else:
        # Ensure configured admin exists (idempotent username).
        platform.ensure_admin(user,password)


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
            frames=traceback.extract_tb(exc.__traceback__)
            local=next((f for f in reversed(frames) if '/app/' in f.filename.replace('\\','/')),
                frames[-1] if frames else None)
            store.event('error','Broker/engine cycle error',{
                'type':type(exc).__name__,'count':error_count,
                'source':Path(local.filename).name if local else None,
                'line':local.lineno if local else None})
            if error_count>=3:
                if engine.armed or engine.execution_enabled:engine._paused_for_read=True
                engine.armed=False;engine.execution_enabled=False
        await asyncio.sleep(3 if error_count<3 else 10)

@asynccontextmanager
async def lifespan(app):
    global worker_task,stream_task
    _bootstrap_admin()
    worker_task=asyncio.create_task(worker())
    stream_task=asyncio.create_task(stream.run(engine))
    try:
        await hub.start_all_active()
    except Exception as exc:
        logging.warning('Client runtime autostart issue: %s',type(exc).__name__)
    yield
    worker_task.cancel()
    stream_task.cancel()
    for t in (worker_task,stream_task):
        try:await t
        except asyncio.CancelledError:pass
    await hub.shutdown()
    engine.cancel_peer_refresh()
    engine.cancel_calendar_refresh()
    brain.save()
    if engine.broker:await engine.broker.close()

app=FastAPI(title='Neural Trading AI',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
app.mount('/static',StaticFiles(directory=str(WEB)),name='static')
app.mount('/app/static',StaticFiles(directory=str(WEB/'client')),name='client-static')
app.mount('/admin/static',StaticFiles(directory=str(WEB/'admin')),name='admin-static')

@app.middleware('http')
async def headers(request:Request,call_next):
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    host=request.headers.get('host') or '127.0.0.1:8765'
    ws=f"ws://{host} wss://{host}"
    response.headers['Content-Security-Policy']=(
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {ws}; img-src 'self' data:")
    return response

def require_auth(request:Request):
    if request.cookies.get(COOKIE)!=SESSION or not engine.broker:
        raise HTTPException(401,'Login required')
    return True

def require_admin(request:Request):
    token=request.cookies.get(ADMIN_COOKIE)
    session=platform.get_session(token)
    if not session or session.get('role')!='admin':
        raise HTTPException(401,'Admin login required')
    return session

def require_client(request:Request):
    token=request.cookies.get(CLIENT_COOKIE)
    session=platform.get_session(token)
    if not session or session.get('role')!='client':
        raise HTTPException(401,'Client login required')
    client=platform.get_client(int(session['subject_id']))
    if not client or not client.get('active'):
        raise HTTPException(401,'Client inactive')
    return client

class Credentials(BaseModel):
    api_key:str=Field(min_length=1,max_length=256)
    identifier:str=Field(min_length=1,max_length=256)
    password:str=Field(min_length=1,max_length=256)

class UserPass(BaseModel):
    username:str=Field(min_length=1,max_length=64)
    password:str=Field(min_length=1,max_length=256)

class ClientCreate(BaseModel):
    username:str=Field(min_length=1,max_length=64)
    password:str=Field(min_length=4,max_length=256)
    display_name:str=Field(default='',max_length=120)
    language:str=Field(default='lv',max_length=8)

class BrokerAttach(BaseModel):
    broker_type:str=Field(default='capital_com',max_length=64)
    display_name:str=Field(default='',max_length=120)
    api_key:str=Field(min_length=1,max_length=256)
    identifier:str=Field(min_length=1,max_length=256)
    password:str=Field(min_length=1,max_length=256)

class LanguageBody(BaseModel):
    language:str=Field(min_length=2,max_length=8)

class PasswordBody(BaseModel):
    password:str=Field(min_length=4,max_length=256)


def _capital_connect_error(stage: str, exc: Exception) -> HTTPException:
    """Explain the failed step, without ever exposing credentials, headers or response body."""
    stages={'session':'autentifikācija', 'accounts':'konta nolasīšana', 'positions':'pozīciju nolasīšana'}
    label=stages.get(stage,stage)
    status=getattr(getattr(exc, 'response', None), 'status_code', None)
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

# ---------- pages ----------
@app.get('/')
async def index():return FileResponse(WEB/'index.html')

@app.get('/admin')
async def admin_page():return FileResponse(WEB/'admin'/'index.html')

@app.get('/app')
async def client_page():return FileResponse(WEB/'client'/'index.html')

@app.get('/app/')
async def client_page_slash():return RedirectResponse('/app')

# ---------- legacy operator Capital.com console ----------
@app.post('/api/connect')
async def connect(data:Credentials):
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
    store.event('auth','Capital.com LIVE authenticated; automatic cycle initialized',
                {'data_dir':str(DATA),'sqlite':str(store.path),
                 'mandate_requires_new_manual_trade':engine.mandate is None})
    response=JSONResponse({'ok':True,'live':True,'automatic':True,
                           'data_dir':str(DATA),'mandate':engine.mandate})
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
    store.set('mandate',None)
    response=JSONResponse({'ok':True});response.delete_cookie(COOKIE);return response

@app.get('/api/health')
async def health():
    return {'app':'Neural Trading AI','ready':True,'authenticated':False,
            'portals':{'operator':'/','admin':'/admin','client':'/app'},
            'clients':len(platform.list_clients()),
            'running_robots':len(hub.all_runtimes())}

# ---------- admin portal API ----------
@app.post('/api/admin/login')
async def admin_login(data:UserPass):
    admin=platform.verify_admin(data.username,data.password)
    if not admin:raise HTTPException(401,'Invalid admin credentials')
    token=platform.create_session('admin',admin['id'])
    response=JSONResponse({'ok':True,'username':admin['username']})
    response.set_cookie(ADMIN_COOKIE,token,httponly=True,samesite='strict',secure=False,path='/')
    return response

@app.post('/api/admin/logout')
async def admin_logout(request:Request):
    platform.delete_session(request.cookies.get(ADMIN_COOKIE))
    response=JSONResponse({'ok':True});response.delete_cookie(ADMIN_COOKIE,path='/');return response

@app.get('/api/admin/me')
async def admin_me(session=Depends(require_admin)):
    return {'ok':True,'role':'admin','subject_id':session['subject_id']}

@app.get('/api/admin/brokers')
async def admin_brokers(_:dict=Depends(require_admin)):
    return {'brokers':list_brokers()}

@app.get('/api/admin/clients')
async def admin_clients(_:dict=Depends(require_admin)):
    rows=[]
    for c in platform.list_clients():
        rt=hub.get(c['id'])
        rows.append({**c,'robot_running':bool(rt and rt.started and rt.engine.broker),
                     'phase':(rt.client_view()['phase'] if rt and rt.started else None)})
    return {'clients':rows}

@app.post('/api/admin/clients')
async def admin_create_client(data:ClientCreate,_:dict=Depends(require_admin)):
    try:
        client=platform.create_client(data.username,data.password,data.display_name,data.language)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None
    return {'ok':True,'client':client}

@app.post('/api/admin/clients/{client_id}/password')
async def admin_reset_password(client_id:int,data:PasswordBody,_:dict=Depends(require_admin)):
    if not platform.get_client(client_id):raise HTTPException(404,'Client not found')
    try:client=platform.reset_client_password(client_id,data.password)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None
    return {'ok':True,'client':client}

@app.post('/api/admin/clients/{client_id}/broker')
async def admin_attach_broker(client_id:int,data:BrokerAttach,_:dict=Depends(require_admin)):
    if not platform.get_client(client_id):raise HTTPException(404,'Client not found')
    # Validate credentials against the live broker before saving.
    stage='session'
    try:
        candidate=create_broker(data.broker_type,data.api_key,data.identifier,data.password,live=True)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from None
    try:
        await candidate.connect()
        stage='accounts';await candidate.accounts()
        stage='positions';await candidate.positions()
    except Exception as exc:
        await candidate.close()
        raise _capital_connect_error(stage,exc) from None
    await candidate.close()
    account=platform.attach_broker(client_id,data.broker_type,data.display_name,
                                   data.api_key,data.identifier,data.password)
    try:
        await hub.start_account(account)
    except Exception as exc:
        raise HTTPException(502,f'Broker saved but robot failed to start ({type(exc).__name__})') from None
    return {'ok':True,'broker':{'id':account['id'],'broker_type':account['broker_type'],
                                'display_name':account['display_name']},
            'client':platform.get_client(client_id)}

@app.post('/api/admin/clients/{client_id}/start')
async def admin_start_client(client_id:int,_:dict=Depends(require_admin)):
    try:
        rt=await hub.ensure_client_running(client_id)
    except Exception as exc:
        raise HTTPException(502,f'Could not start robot ({type(exc).__name__})') from None
    if not rt:raise HTTPException(409,'Client has no broker account')
    return {'ok':True,'view':rt.client_view()}

@app.post('/api/admin/clients/{client_id}/stop')
async def admin_stop_client(client_id:int,_:dict=Depends(require_admin)):
    await hub.stop_client(client_id)
    return {'ok':True}

# ---------- client portal API ----------
@app.post('/api/client/login')
async def client_login(data:UserPass):
    client=platform.verify_client(data.username,data.password)
    if not client:raise HTTPException(401,'Invalid login')
    token=platform.create_session('client',client['id'])
    # Best-effort start so the guide/robot screens have live data.
    try:await hub.ensure_client_running(client['id'])
    except Exception as exc:
        logging.warning('Client %s robot start on login: %s',client['id'],type(exc).__name__)
    response=JSONResponse({'ok':True,'client':{
        'id':client['id'],'username':client['username'],
        'display_name':client['display_name'],'language':client['language']}})
    response.set_cookie(CLIENT_COOKIE,token,httponly=True,samesite='strict',secure=False,path='/')
    return response

@app.post('/api/client/logout')
async def client_logout(request:Request):
    platform.delete_session(request.cookies.get(CLIENT_COOKIE))
    response=JSONResponse({'ok':True});response.delete_cookie(CLIENT_COOKIE,path='/');return response

@app.get('/api/client/me')
async def client_me(client=Depends(require_client)):
    return {'ok':True,'client':client}

@app.post('/api/client/language')
async def client_language(data:LanguageBody,client=Depends(require_client)):
    try:updated=platform.set_client_language(client['id'],data.language)
    except ValueError as exc:raise HTTPException(400,str(exc)) from None
    return {'ok':True,'client':updated}

@app.get('/api/client/status')
async def client_status(client=Depends(require_client)):
    rt=hub.get(client['id'])
    if not rt or not rt.started:
        broker=platform.get_broker_for_client(client['id'])
        return {
            'client':client,
            'phase':'setup' if not broker else 'starting',
            'view':None,
            'has_broker':bool(broker),
        }
    return {'client':client,'phase':rt.client_view()['phase'],'view':rt.client_view(),'has_broker':True}

@app.post('/api/client/refresh')
async def client_refresh(client=Depends(require_client)):
    rt=hub.get(client['id'])
    if not rt or not rt.engine.broker:raise HTTPException(409,'Robot not connected')
    try:await rt.engine.sync()
    except Exception as exc:raise HTTPException(502,'Position read failed') from exc
    view=rt.client_view()
    return {'ok':True,'phase':view['phase'],'view':view}

@app.websocket('/ws/client')
async def client_ws(ws:WebSocket):
    token=ws.cookies.get(CLIENT_COOKIE)
    session=platform.get_session(token)
    if not session or session.get('role')!='client':
        await ws.close(code=1008);return
    client=platform.get_client(int(session['subject_id']))
    if not client or not client.get('active'):
        await ws.close(code=1008);return
    await ws.accept()
    try:
        while True:
            rt=hub.get(client['id'])
            payload={'client':platform.get_client(client['id']),
                     'has_broker':bool(platform.get_broker_for_client(client['id'])),
                     'view':rt.client_view() if rt and rt.started else None}
            if payload['view']:
                payload['phase']=payload['view']['phase']
            elif payload['has_broker']:
                payload['phase']='starting'
            else:
                payload['phase']='setup'
            await ws.send_json(payload)
            await asyncio.sleep(2)
    except (WebSocketDisconnect,RuntimeError):pass

@app.post('/api/client/pause')
async def client_pause(client=Depends(require_client)):
    rt=hub.get(client['id'])
    if not rt or not rt.engine.broker:raise HTTPException(409,'Robot not connected')
    rt.engine.armed=False;rt.engine.execution_enabled=False;rt.engine.operator_paused=True
    rt.store.event('control','Paused by client')
    return {'ok':True,'view':rt.client_view()}

@app.post('/api/client/resume')
async def client_resume(client=Depends(require_client)):
    rt=hub.get(client['id'])
    if not rt or not rt.engine.broker:raise HTTPException(409,'Robot not connected')
    await rt.engine.reconcile_outstanding()
    await rt.engine.sync()
    if rt.store.unresolved():raise HTTPException(409,'Unresolved broker operation')
    if not rt.engine.mandate:raise HTTPException(409,'Complete the first manual trade first')
    if len(rt.engine.positions)>1:raise HTTPException(409,'Multiple positions')
    rt.engine.armed=True;rt.engine.execution_enabled=True
    rt.engine.operator_paused=False;rt.engine._paused_for_read=False
    rt.store.event('control','Resumed by client')
    return {'ok':True,'view':rt.client_view()}
