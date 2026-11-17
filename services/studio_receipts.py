"""Opt-in authenticated Studio text receipts. Never imports account/config services.

Local at-most-once dispatch, not native exactly-once. UNKNOWN is a tombstone,
never permission to redispatch. Client lifetime is independent of native lifetime.
"""
from __future__ import annotations
import asyncio, hashlib, json, os, re, sqlite3, threading, time, uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

PROTOCOL='studio-receipts-v1'
ID=re.compile(r'^[A-Za-z0-9_.:-]{1,160}$')
TERMINAL={'COMPLETED','FAILED_VERIFIED','UNKNOWN'}
def sha(text:str)->str:return hashlib.sha256(text.encode('utf-8')).hexdigest()
def utc(ts=None):
    from datetime import datetime,timezone
    return datetime.fromtimestamp(time.time() if ts is None else ts,timezone.utc).isoformat()
def canonical_payload(body:dict)->dict:
    # Same algorithm in Studio; attemptId and transport metadata are not semantic inputs.
    request={k:v for k,v in body.items() if k not in {'studio','metadata'}}
    binding={k:v for k,v in body['studio'].items() if k not in {'attemptId','logicalId'}}
    return {'request':request,'binding':binding}
def canonical_hash(body:dict)->str:
    return sha(json.dumps(canonical_payload(body),sort_keys=True,ensure_ascii=False,separators=(',',':')))
def validate(body:dict):
    s=body.get('studio')
    if not isinstance(s,dict) or s.get('protocol')!=PROTOCOL:raise HTTPException(400,'PROTOCOL_REQUIRED')
    fields={'protocol','logicalId','attemptId','role','projectId','revision','promptVersion','hookHash','writerHash'}
    if set(s)!=fields:raise HTTPException(400,'BINDING_FIELDS')
    for f in ['logicalId','attemptId','role','projectId','promptVersion']:
        if not isinstance(s.get(f),str) or not ID.fullmatch(s[f]):raise HTTPException(400,'INVALID_BINDING')
    for f in ['hookHash','writerHash']:
        if not isinstance(s.get(f),str) or s[f] and not re.fullmatch('[0-9a-f]{64}',s[f]):raise HTTPException(400,'INVALID_HASH')
    if type(s.get('revision')) is not int or not 0<=s['revision']<=1000000:raise HTTPException(400,'INVALID_REVISION')
    if not isinstance(body.get('model'),str) or not ID.fullmatch(body['model'].replace('/','_')):raise HTTPException(400,'INVALID_MODEL')
    if not isinstance(body.get('messages'),list) or not body['messages'] or len(body['messages'])>32:raise HTTPException(400,'TEXT_ONLY')
    if any(not isinstance(m,dict) or m.get('role') not in {'system','user','assistant'} or not isinstance(m.get('content'),str) or set(m)-{'role','content'} for m in body['messages']):raise HTTPException(400,'TEXT_ONLY')
    allowed={'model','messages','stream','reasoning_effort','max_tokens','max_completion_tokens','stream_options','metadata','studio'}
    if set(body)-allowed:raise HTTPException(400,'UNSUPPORTED_TEXT_PARAMETER')
    if len(json.dumps(body,ensure_ascii=False).encode())>2*1024*1024:raise HTTPException(413,'REQUEST_TOO_LARGE')
    effort=body.get('reasoning_effort','')
    if effort not in {'','none','low','medium','high','extended','xhigh','ultra'}:raise HTTPException(400,'INVALID_EFFORT')

class ReceiptStore:
    def __init__(self,path:Path,key:bytes,*,max_active=2,max_active_per_owner=1,max_new_per_owner_minute=20,max_rows=10000,max_result_bytes=80*1024*1024,lease_seconds=30,retention_seconds=7*86400):
        self.path=Path(path).resolve();self.path.parent.mkdir(parents=True,exist_ok=True)
        self.cipher=Fernet(key);self.max_active=max_active;self.max_rows=max_rows;self.max_result_bytes=max_result_bytes;self.lease_seconds=lease_seconds;self.retention_seconds=retention_seconds
        self.max_active_per_owner=max_active_per_owner;self.max_new_per_owner_minute=max_new_per_owner_minute
        self.server_id=''
        try:
            with self.connect() as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS studio_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute("INSERT OR IGNORE INTO studio_meta VALUES('server_id',?)",('studio-'+uuid.uuid4().hex,))
                self.server_id=db.execute("SELECT value FROM studio_meta WHERE key='server_id'").fetchone()[0]
                db.execute('''CREATE TABLE IF NOT EXISTS receipts(
                 owner TEXT NOT NULL, logical_id TEXT NOT NULL, input_hash TEXT NOT NULL,
                 binding TEXT NOT NULL, attempt_id TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
                 status TEXT NOT NULL, phase TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                 native_started REAL, first_visible REAL, terminal_at REAL, durable_at REAL,
                 fence TEXT NOT NULL, lease_expires REAL NOT NULL, worker_pid INTEGER NOT NULL,
                 slot_held INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 0,
                 native_count INTEGER NOT NULL DEFAULT 0, frames INTEGER NOT NULL DEFAULT 0,
                 result_cipher BLOB, result_hash TEXT, proof TEXT, error_type TEXT,
                 PRIMARY KEY(owner,logical_id))''')
                db.execute('PRAGMA user_version=1')
        except sqlite3.Error:raise HTTPException(503,'RECEIPT_STORAGE_UNAVAILABLE') from None
    def connect(self):
        db=sqlite3.connect(self.path,timeout=.2);db.row_factory=sqlite3.Row
        db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA busy_timeout=200');return db
    def claim(self,owner,body):
        validate(body);s=body['studio'];now=time.time();fingerprint=canonical_hash(body)
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                existing=db.execute('SELECT * FROM receipts WHERE owner=? AND logical_id=?',(owner,s['logicalId'])).fetchone()
                if existing:
                    if existing['input_hash']!=fingerprint:raise HTTPException(409,'IDEMPOTENCY_INPUT_MISMATCH')
                    return dict(existing),False
                if db.execute('SELECT count(*) FROM receipts').fetchone()[0]>=self.max_rows:raise HTTPException(507,'RECEIPT_QUOTA')
                active=db.execute('SELECT count(*) FROM receipts WHERE slot_held=1').fetchone()[0]
                if active>=self.max_active:raise HTTPException(429,'NATIVE_CAPACITY_HELD',headers={'Retry-After':'5'})
                owner_active=db.execute('SELECT count(*) FROM receipts WHERE owner=? AND slot_held=1',(owner,)).fetchone()[0]
                if owner_active>=self.max_active_per_owner:raise HTTPException(429,'OWNER_CAPACITY_HELD',headers={'Retry-After':'5'})
                recent=db.execute('SELECT count(*) FROM receipts WHERE owner=? AND created>=?',(owner,now-60)).fetchone()[0]
                if recent>=self.max_new_per_owner_minute:raise HTTPException(429,'OWNER_RATE_LIMIT',headers={'Retry-After':'60'})
                used=db.execute('SELECT coalesce(sum(length(result_cipher)),0) FROM receipts').fetchone()[0]
                if used+(active+1)*6*1024*1024>self.max_result_bytes:raise HTTPException(507,'RECEIPT_RESULT_QUOTA')
                fence=uuid.uuid4().hex
                db.execute('''INSERT INTO receipts(owner,logical_id,input_hash,binding,attempt_id,model,effort,status,phase,created,updated,fence,lease_expires,worker_pid)
                 VALUES(?,?,?,?,?,?,?,'RECEIVED','REGISTERED',?,?,?,?,?)''',
                 (owner,s['logicalId'],fingerprint,json.dumps(s,separators=(',',':')),s['attemptId'],body['model'],body.get('reasoning_effort',''),now,now,fence,now+self.lease_seconds,os.getpid()))
                row=dict(db.execute('SELECT * FROM receipts WHERE owner=? AND logical_id=?',(owner,s['logicalId'])).fetchone())
                return row,True
        except sqlite3.Error:raise HTTPException(503,'RECEIPT_STORAGE_UNAVAILABLE') from None
    def update(self,row,**values):
        # Fencing token prevents late workers from committing after UNKNOWN/recovery.
        allowed={'status','phase','native_started','first_visible','terminal_at','durable_at','slot_held','native_count','frames','result_cipher','result_hash','proof','error_type','lease_expires'}
        assert set(values)<=allowed
        values['updated']=time.time()
        try:
            with self.connect() as db:
                pairs=','.join(k+'=?' for k in values)
                cur=db.execute(f'UPDATE receipts SET {pairs},version=version+1 WHERE owner=? AND logical_id=? AND fence=? AND status NOT IN (\'COMPLETED\',\'FAILED_VERIFIED\',\'UNKNOWN\')',(*values.values(),row['owner'],row['logical_id'],row['fence']))
                return cur.rowcount==1
        except sqlite3.Error:raise HTTPException(503,'RECEIPT_STORAGE_UNAVAILABLE') from None
    def release(self,row):
        with self.connect() as db:
            db.execute('UPDATE receipts SET slot_held=0,version=version+1 WHERE owner=? AND logical_id=? AND fence=?',(row['owner'],row['logical_id'],row['fence']))
    def get(self,owner,logical_id):
        if not ID.fullmatch(logical_id):raise HTTPException(404)
        try:
            with self.connect() as db:row=db.execute('SELECT * FROM receipts WHERE owner=? AND logical_id=?',(owner,logical_id)).fetchone()
            if not row:raise HTTPException(404)
            r=dict(row);binding=json.loads(r['binding'])
            out={'protocol':PROTOCOL,'serverId':self.server_id,'logicalId':logical_id,'inputHash':r['input_hash'],'binding':binding,'model':r['model'],'effort':r['effort'],'status':r['status'],'phase':r['phase'],'createdAt':utc(r['created']),'updatedAt':utc(r['updated']),'nativeStartedAt':utc(r['native_started']) if r['native_started'] else None,'firstVisibleAt':utc(r['first_visible']) if r['first_visible'] else None,'terminalAt':utc(r['terminal_at']) if r['terminal_at'] else None,'durableReceiptAt':utc(r['durable_at']) if r['durable_at'] else None,'nativePhysicalDispatchCount':r['native_count'],'frameCount':r['frames'],'resultHash':r['result_hash'],'terminalProof':json.loads(r['proof']) if r['proof'] else None,'errorType':r['error_type'],'slotHeld':bool(r['slot_held'])}
            if r['status']=='COMPLETED':
                if not r['result_cipher']:raise HTTPException(503,'RECEIPT_RESULT_EXPIRED')
                result=self.cipher.decrypt(r['result_cipher']).decode('utf-8')
                if sha(result)!=r['result_hash']:raise HTTPException(503,'RECEIPT_INTEGRITY')
                out['result']=result
            return out
        except (sqlite3.Error,InvalidToken,UnicodeError,ValueError):raise HTTPException(503,'RECEIPT_INTEGRITY') from None
    def complete(self,row,text,proof,frames):
        # Encrypt before transaction. Exact result and terminal state commit atomically.
        cipher=self.cipher.encrypt(text.encode())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT coalesce(sum(length(result_cipher)),0) FROM receipts').fetchone()[0]
            if used+len(cipher)>self.max_result_bytes:raise ValueError('OUTPUT_STORAGE_LIMIT')
            cur=db.execute("UPDATE receipts SET status='COMPLETED',phase='COMPLETE',terminal_at=?,durable_at=?,updated=?,result_cipher=?,result_hash=?,proof=?,frames=?,version=version+1 WHERE owner=? AND logical_id=? AND fence=? AND status IN ('RUNNING','RECEIVED')",(time.time(),time.time(),time.time(),cipher,sha(text),json.dumps(proof),frames,row['owner'],row['logical_id'],row['fence']))
            return cur.rowcount==1
    def recover(self,now=None):
        # Lease expiry NEVER frees slot or redispatches. Native cancellation is unverified.
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute("UPDATE receipts SET status='UNKNOWN',phase='NATIVE_CANCEL_UNVERIFIED',error_type='LEASE_EXPIRED_NATIVE_UNKNOWN',version=version+1,updated=? WHERE status IN ('RECEIVED','RUNNING') AND lease_expires<?",(now,now))
            db.execute("UPDATE receipts SET result_cipher=NULL WHERE status='COMPLETED' AND durable_at<?",(now-self.retention_seconds,))

@dataclass
class NativeEvent:
    kind:str
    text:str=''
    proof:dict=field(default_factory=dict)

class ReceiptEngine:
    def __init__(self,store,adapter:Callable[[dict,threading.Event],Iterator[NativeEvent]],*,heartbeat=1,first_visible=720,idle=180,total=1200):
        self.store=store;self.adapter=adapter;self.heartbeat=heartbeat;self.first_visible=first_visible;self.idle=idle;self.total=total
        self.lock=threading.Lock();self.workers={};self.stopped=False
        self.monitor=threading.Thread(target=self._monitor,daemon=True,name='studio-watchdog');self.monitor.start()
    def _monitor(self):
        while not self.stopped:
            try:self.store.recover()
            except Exception:pass
            with self.lock:keys=list(self.workers)
            for owner,logical_id in keys:
                try:self.tick(owner,logical_id)
                except Exception:pass # Never starts native work; persistent state fails closed.
            time.sleep(max(.01,min(self.heartbeat,1)))
    def submit(self,owner,body):
        if self.stopped:raise HTTPException(503,'ENGINE_SHUTDOWN')
        row,new=self.store.claim(owner,body)
        if new:
            state={'text':'','cancel':threading.Event(),'started':time.monotonic(),'last_visible':None,'frames':0}
            key=(owner,row['logical_id'])
            thread=threading.Thread(target=self._work,args=(row,body,state),daemon=True,name='studio-native-'+sha(row['logical_id'])[:8]);state['thread']=thread
            with self.lock:self.workers[key]=state
            thread.start()
        return row
    def _work(self,row,body,state):
        proof=None
        try:
            # A delayed worker may have lost its lease/fence before it starts.
            # Never dispatch after recovery has already sealed UNKNOWN.
            if not self.store.update(row,status='RUNNING',phase='NATIVE_SUBMITTED',native_started=time.time(),native_count=1):
                return
            for event in self.adapter(body,state['cancel']):
                if not isinstance(event,NativeEvent):raise ValueError('UNSUPPORTED_NATIVE_EVENT')
                if event.kind=='visible':
                    if not isinstance(event.text,str):raise ValueError('UNSUPPORTED_NATIVE_EVENT')
                    if not event.text:continue
                    if proof:raise ValueError('REORDERED_NATIVE_EVENT')
                    state['text']+=event.text;state['frames']+=1;state['last_visible']=time.monotonic()
                    if len(state['text'].encode())>4*1024*1024:raise ValueError('OUTPUT_STORAGE_LIMIT')
                    if state['frames']==1:self.store.update(row,phase='FIRST_VISIBLE',first_visible=time.time(),frames=1)
                elif event.kind=='terminal':
                    if proof:raise ValueError('DUPLICATE_TERMINAL')
                    proof=event.proof
                elif event.kind in {'reasoning','submitted','progress'}:pass
                elif event.kind=='rejected_before_submit':
                    self.store.update(row,status='FAILED_VERIFIED',phase='REJECTED_PRE_DISPATCH',error_type='VERIFIED_NO_NATIVE_SUBMIT',native_count=0);return
                else:raise ValueError('UNSUPPORTED_NATIVE_EVENT')
            if not state['text'].strip():raise ValueError('EMPTY_VISIBLE_OUTPUT')
            if not proof or proof.get('upstreamDone') is not True or proof.get('assistantTerminal') is not True or proof.get('finishReason')!='stop':raise ValueError('NATIVE_TERMINAL_UNVERIFIED')
            self.store.complete(row,state['text'],proof,state['frames'])
        except Exception as exc:
            # 2026-11-12 (owner-authorized): keep the transport facts the web layer already proved.
            # upstream_not_dispatched: no conversation POST left the gateway -> verified failure, safe to run again.
            # upstream_outcome_unknown_no_conversation with no visible frame: keep that code; its 502 status must not hide it.
            native_code=getattr(exc,'code',None)
            # 2026-11-17 (owner-authorized, Studio plan V3.8 U3): chatgpt.com ended the turn with an error and the
            # read-back proved no answer was produced -> verified failure; the client may run the step again.
            if native_code=='upstream_failed_verified_no_answer':
                try:self.store.update(row,status='FAILED_VERIFIED',phase='UPSTREAM_ERROR_VERIFIED',error_type='VERIFIED_UPSTREAM_ERROR_NO_ANSWER',frames=state['frames'])
                except Exception:pass
                return
            if native_code=='upstream_not_dispatched':
                try:self.store.update(row,status='FAILED_VERIFIED',phase='REJECTED_PRE_DISPATCH',error_type='VERIFIED_NO_NATIVE_SUBMIT',native_count=0,frames=state['frames'])
                except Exception:pass
                return
            code=str(exc) if isinstance(exc,ValueError) and str(exc) in {'UNSUPPORTED_NATIVE_EVENT','REORDERED_NATIVE_EVENT','DUPLICATE_TERMINAL','OUTPUT_STORAGE_LIMIT','EMPTY_VISIBLE_OUTPUT','NATIVE_TERMINAL_UNVERIFIED','UPSTREAM_SSE_USAGE_LIMIT','UPSTREAM_SSE_ERROR_UNVERIFIED'} else 'CURL92' if 'curl: (92)' in str(exc) else 'NATIVE_OUTCOME_UNKNOWN'
            # Record allowlisted HTTP facts without copying upstream error bodies.
            status=getattr(exc,'status_code',None)
            if status in {401,403,429,500,502,503,504}:code='UPSTREAM_HTTP_'+str(status)
            if native_code=='upstream_outcome_unknown_no_conversation' and state['frames']==0:code='UPSTREAM_OUTCOME_UNKNOWN_NO_CONVERSATION'
            try:self.store.update(row,status='UNKNOWN',phase='NATIVE_UNKNOWN',error_type=code,frames=state['frames'])
            except Exception:pass # Storage failure is fail-closed; durable RUNNING later orphans to UNKNOWN.
        finally:
            # Only the actual native worker finishing releases capacity.
            try:self.store.release(row)
            except Exception:pass
            with self.lock:self.workers.pop((row['owner'],row['logical_id']),None)
    def tick(self,owner,logical_id):
        with self.lock:state=self.workers.get((owner,logical_id))
        if state:
            now=time.monotonic();elapsed=now-state['started'];last=state['last_visible']
            error='TIMEOUT_TOTAL' if elapsed>self.total else 'TIMEOUT_FIRST_VISIBLE' if last is None and elapsed>self.first_visible else 'TIMEOUT_IDLE_VISIBLE' if last is not None and now-last>self.idle else None
            receipt=self.store.get(owner,logical_id)
            with self.store.connect() as db:r=db.execute('SELECT * FROM receipts WHERE owner=? AND logical_id=?',(owner,logical_id)).fetchone()
            if error:
                state['cancel'].set();self.store.update(dict(r),status='UNKNOWN',phase='NATIVE_CANCEL_UNVERIFIED',error_type=error)
            elif receipt['status'] not in TERMINAL:self.store.update(dict(r),lease_expires=time.time()+self.store.lease_seconds,frames=state['frames'])
        return self.store.get(owner,logical_id)
    async def stream(self,owner,logical_id):
        # First body item precedes first next(native); comments cannot count as text.
        yield ': studio accepted; progress is not AI content\n\n'
        while True:
            r=await asyncio.to_thread(self.tick,owner,logical_id)
            yield 'data: '+json.dumps({'studio_progress':{'logicalId':logical_id,'status':r['status'],'phase':r['phase']}},separators=(',',':'))+'\n\n'
            if r['status']=='COMPLETED':
                yield 'data: '+json.dumps({'model':r['model'],'choices':[{'delta':{'role':'assistant','content':r['result']},'finish_reason':None}]},ensure_ascii=False)+'\n\n'
                yield 'data: '+json.dumps({'studio_receipt':r,'choices':[{'delta':{},'finish_reason':'stop'}]})+'\n\n'
                yield 'data: [DONE]\n\n';return
            if r['status'] in TERMINAL:
                yield 'data: '+json.dumps({'error':{'code':r['errorType'] or 'NATIVE_UNKNOWN','request_id':logical_id,'upstream_outcome':'unknown','retryable':False}})+'\n\n';return
            await asyncio.sleep(self.heartbeat)
    def shutdown(self):
        self.stopped=True
        with self.lock:states=list(self.workers.values())
        for s in states:s['cancel'].set()
        # Never claim cancellation. Native threads retained; on process exit leases orphan.

def make_router(engine,authenticate):
    router=APIRouter()
    def owner(auth):
        identity=authenticate(auth)
        if not identity or not identity.get('id'):raise HTTPException(401)
        return sha(str(identity['id']))
    @router.get('/v1/studio/capabilities')
    async def capabilities(authorization:str|None=Header(default=None)):
        owner(authorization)
        return {'protocol':PROTOCOL,'serverId':engine.store.server_id,'authenticated':True,'atMostOnce':True,'encryptedResults':True,'statusPath':'studio/requests/','submitPath':'studio/completions','nativeExactlyOnce':False}
    @router.get('/v1/studio/requests/{logical_id}')
    async def status(logical_id:str,authorization:str|None=Header(default=None)):
        who=owner(authorization)
        return await asyncio.to_thread(engine.store.get,who,logical_id)
    @router.post('/v1/studio/completions')
    async def completion(request:Request,authorization:str|None=Header(default=None)):
        who=owner(authorization)
        if int(request.headers.get('content-length','0'))>2*1024*1024:raise HTTPException(413)
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>2*1024*1024:raise HTTPException(413)
        try:body=json.loads(raw)
        except Exception:raise HTTPException(400,'INVALID_JSON') from None
        if not isinstance(body,dict):raise HTTPException(400)
        row=await asyncio.to_thread(engine.submit,who,body)
        return StreamingResponse(engine.stream(who,row['logical_id']),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no','X-Studio-Protocol':PROTOCOL,'X-Studio-Server-Id':engine.store.server_id})
    return router
