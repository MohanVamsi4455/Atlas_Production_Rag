import logging
import re
import uuid

import psycopg
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from app.observability import initialize, flush
from app.demo_context import principal, Principal
from fastapi.concurrency import run_in_threadpool
from app.aws import delete_object, presigned_put_url, presigned_url, upload_bytes
from app import keyword
from app.vector import delete_document as delete_vectors
from app.config import settings
from app.db import connection, audit
from app.rag import chat
from app.guardrails import classify_input
from app.pii import enabled as pii_enabled, preview as pii_preview, redact
from app.memory import history, save_turn, list_messages, list_conversations, delete_conversation
from worker.ingest import process_s3_key

initialize()
log=logging.getLogger('atlas.api')


BASE=Path(__file__).resolve().parent
app=FastAPI(title='Atlas Enterprise RAG',docs_url='/api/docs')

@app.on_event('shutdown')
def _shutdown(): flush()


@app.exception_handler(psycopg.Error)
async def database_error(request:Request,exc:psycopg.Error):
    """Turn database failures into a readable message instead of a bare 500 'Request failed'."""
    log.error('Database error on %s: %s: %s',request.url.path,type(exc).__name__,str(exc).splitlines()[0] if str(exc) else '')
    if isinstance(exc,psycopg.OperationalError):
        msg=('Could not connect to the database. Check that DATABASE_URL is correct and that the database is running '
             'and its security group allows this server on port 5432.')
        return JSONResponse(status_code=503,content={'detail':msg})
    return JSONResponse(status_code=500,content={'detail':'A database error occurred. See the server logs for details.'})

app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
templates=Jinja2Templates(directory=BASE/'templates')



class Upload(BaseModel):
    filename:str=Field(min_length=1,max_length=180)

class Ask(BaseModel):
    question:str=Field(min_length=3,max_length=2000)
    conversation_id:uuid.UUID|None=None

class Feedback(BaseModel):
    document_id:str|None=None
    rating:int=Field(ge=-1,le=1)
    note:str=Field(default='',max_length=500)



@app.get('/',response_class=HTMLResponse)
def home(request:Request):
    # asset version = newest modification time of the css/js files, so browsers never keep a stale copy
    version=str(int(max((BASE/'static'/'js'/'app.js').stat().st_mtime,(BASE/'static'/'css'/'styles.css').stat().st_mtime)))
    return templates.TemplateResponse('index.html',{'request':request,'v':version,'region':settings().aws_region,
        'mode':'single-workspace demo'})



@app.get('/health/live')
def live(): return {'status':'alive'}



@app.get('/health/ready')
def ready():
    try:
        with connection() as conn: conn.execute('SELECT 1').fetchone()
        return {'status':'ready'}
    except Exception as exc:
        raise HTTPException(503,'Database unavailable') from exc



@app.post('/api/documents/uploads')
def create_upload(body:Upload,p:Principal=Depends(principal)):
    suffix=Path(body.filename).suffix.lower()
    if suffix not in {'.pdf','.docx','.txt','.md'}:
        raise HTTPException(400,'Allowed types: PDF, DOCX, TXT, MD')
    doc_id=str(uuid.uuid4())
    key=f'tenants/{p.tenant}/incoming/{doc_id}/v1{suffix}'
    with connection() as conn:
        conn.execute('''INSERT INTO documents(id,tenant_id,title,owner_sub,allowed_groups,s3_key,status,current_version)
                        VALUES (%s,%s,%s,%s,%s,%s,'pending',1)''',
                        (doc_id,p.tenant,body.filename,p.subject,["workspace"],key))
        audit(conn,p.subject,p.tenant,'upload_requested',{'document_id':doc_id})
    # Browser PUTs to S3; a separate completion request triggers synchronous ingestion.
    url=presigned_put_url(key)
    return {'document_id':doc_id,'upload_url':url,'headers':{'Content-Type':'application/octet-stream'},
            'upload_path':f'/api/documents/{doc_id}/content'}



@app.put('/api/documents/{document_id}/content')
async def upload_content(document_id:uuid.UUID,request:Request,p:Principal=Depends(principal)):
    """Receive the file body and store it in S3. The browser talks only to Atlas, so the S3 bucket needs no CORS rule."""
    limit=settings().max_upload_bytes
    declared=request.headers.get('content-length','')
    if declared.isdigit() and int(declared)>limit: raise HTTPException(413,'File too large (maximum 10 MB).')
    with connection() as conn:
        row=conn.execute("SELECT s3_key,status FROM documents WHERE id=%s AND tenant_id=%s AND owner_sub=%s",
                         (document_id,p.tenant,p.subject)).fetchone()
    if not row: raise HTTPException(404,'Document not found')
    if row['status']!='pending': raise HTTPException(409,'This document was already uploaded')
    data=bytearray()
    async for chunk in request.stream():
        data+=chunk
        if len(data)>limit: raise HTTPException(413,'File too large (maximum 10 MB).')
    if not data: raise HTTPException(400,'The file is empty')
    try:
        await run_in_threadpool(upload_bytes,row['s3_key'],bytes(data))
    except Exception as exc:
        log.exception('Storing upload %s in S3 failed',document_id)
        raise HTTPException(502,'Could not store the file in S3. Check the S3 bucket name and AWS permissions.') from exc
    with connection() as conn:
        audit(conn,p.subject,p.tenant,'document_uploaded',{'document_id':str(document_id),'bytes':len(data)})
    return {'uploaded':True,'bytes':len(data)}



class CompleteUpload(BaseModel):
    document_id:uuid.UUID

@app.post('/api/documents/complete')
def complete_upload(body:CompleteUpload,p:Principal=Depends(principal)):
    with connection() as conn:
        row=conn.execute("""SELECT s3_key,status FROM documents WHERE id=%s AND tenant_id=%s""",
                         (body.document_id,p.tenant)).fetchone()
    if not row: raise HTTPException(404,'Document not found')
    if row['status']=='ready': return {'status':'ready'}
    if row['status']=='processing': raise HTTPException(409,'Already processing')
    try:
        process_s3_key(row['s3_key'])
    except Exception as exc:
        raise HTTPException(502,'Ingestion failed. Check server logs and document status.') from exc
    return {'status':'ready'}



@app.get('/api/documents')
def documents(p:Principal=Depends(principal)):
    with connection() as conn:
        rows=conn.execute('''SELECT id,title,status,current_version,created_at,allowed_groups,pii FROM documents
            WHERE tenant_id=%s AND status<>'retired' AND (allowed_groups && %s::text[] OR owner_sub=%s)
            ORDER BY created_at DESC LIMIT 100''',(p.tenant,list(p.groups),p.subject)).fetchall()
    for r in rows: r['contains_pii']=_is_restricted(r.pop('pii',None))
    return {'documents':rows}




def _is_restricted(pii)->bool:
    """A document that contains personal data (found at upload) is never shown or opened in the UI."""
    return bool(pii_enabled() and pii and sum(pii.values())>0)

def _restrict_sources(p:Principal,sources:list)->list:
    """Hide the text of, and any link to, sources that come from a document containing personal data."""
    ids=[s['document_id'] for s in sources if s.get('document_id')]
    if not ids or not pii_enabled(): return sources
    with connection() as conn:
        rows=conn.execute("SELECT id,pii FROM documents WHERE tenant_id=%s AND id = ANY(%s::uuid[])",(p.tenant,ids)).fetchall()
    blocked={str(r['id']) for r in rows if _is_restricted(r['pii'])}
    for s in sources:
        if s.get('document_id') in blocked:
            s['restricted']=True; s['excerpt']=''
    return sources

def _readable_file_url(document_id:uuid.UUID,p:Principal)->tuple[str,str]:
    """Short-lived link to the original file, only for documents this user may read."""
    with connection() as conn:
        row=conn.execute('''SELECT s3_key,title,pii FROM documents WHERE id=%s AND tenant_id=%s AND status<>'retired'
            AND (allowed_groups && %s::text[] OR owner_sub=%s)''',
            (document_id,p.tenant,list(p.groups),p.subject)).fetchone()
        if not row: raise HTTPException(404,'Document not found')
        blocked=_is_restricted(row['pii'])
        audit(conn,p.subject,p.tenant,'document_open_blocked_pii' if blocked else 'document_opened',{'document_id':str(document_id)})
    if blocked: raise HTTPException(403,'This document contains personal data, so it cannot be opened here.')
    return presigned_url(row['s3_key'],300,row['title']),row['title']

@app.get('/api/documents/{document_id}/url')
def document_url(document_id:uuid.UUID,p:Principal=Depends(principal)):
    url,title=_readable_file_url(document_id,p)
    return {'url':url,'title':title}

@app.get('/api/documents/{document_id}/open')
def document_open(document_id:uuid.UUID,page:int=Query(1,ge=1,le=100000),p:Principal=Depends(principal)):
    """Redirect to the original file; PDFs jump to the cited page. Used as a plain link, so no pop-up blocking."""
    url,title=_readable_file_url(document_id,p)
    # PDF viewer options: jump to the page, fit the width, hide the thumbnail pane and toolbar (the window is small).
    if title.lower().endswith('.pdf'): url+=f'#page={page}&view=FitH&navpanes=0&toolbar=0'
    return RedirectResponse(url,status_code=302)



BLOCK_MESSAGES={
    'prompt_injection_or_secret_request':"I can't act on that. The request tries to override my instructions or to obtain secrets such as keys or passwords.",
    'harmful_request':"I can't help with hacking, malware or other harmful activity. Ask me about your approved documents instead.",
    'pii_in_question':"Your question contains a sensitive ID (card, Aadhaar, PAN or SSN). I don't process these, so nothing was sent to the AI. Please remove it and ask again.",
    'secret_in_question':"Please don't type passwords, PINs or OTPs here. I don't need them, and nothing was sent to the AI.",
    'empty_input':'Please type a question.',
}

@app.post('/api/chat')
def ask(body:Ask,p:Principal=Depends(principal)):
    reason=classify_input(body.question)
    if reason:
        with connection() as conn:
            audit(conn,p.subject,p.tenant,'guardrail_input_blocked',{'reason':reason,'preview':pii_preview(body.question)})
        return {'answer':BLOCK_MESSAGES.get(reason,'This request was blocked by a guardrail.'),'sources':[],
                'status':'guardrail_blocked','reason':reason}
    # Email addresses / phone numbers typed into the question are masked before retrieval, the LLM, history and logs.
    question,q_found=redact(body.question)
    if q_found:
        with connection() as conn:
            audit(conn,p.subject,p.tenant,'guardrail_pii_redacted',{'reason':'pii_redacted','types':q_found,
                  'stage':'question','preview':pii_preview(question)})
    if settings().hitl_enabled and any(x in question.lower() for x in ('delete','export','send to','update policy')):
        with connection() as conn:
            action,target=None,None
            if 'delete' in question.lower():
                action,target=_find_delete_target(conn,p,question)
            row=conn.execute('''INSERT INTO approvals(tenant_id,requester,question,status,action,document_id)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
                (p.tenant,p.subject,question,'pending',action,target['id'] if target else None)).fetchone()
            audit(conn,p.subject,p.tenant,'approval_requested',{'id':str(row['id']),'action':action,
                'document_id':str(target['id']) if target else None})
        if target:
            msg=(f'This request needs administrator approval. If approved, **{target["title"]}** will be permanently deleted '
                 'from the library and search. Open the Approvals tab to review it.')
        elif action=='delete':
            msg=('This request needs administrator approval, but I could not tell which document you mean, so nothing '
                 'would be deleted. Ask again with the document\'s file name.')
        else:
            msg='This request requires administrator approval before any action. Open the Approvals tab to review it.'
        return {'answer':msg,'sources':[],'status':'pending_approval','approval_id':str(row['id'])}
    conversation_id=body.conversation_id or uuid.uuid4()
    prior=history(p.tenant,conversation_id)
    result=chat(p,question,prior)
    result['sources']=_restrict_sources(p,result.get('sources') or [])
    masked=result.pop('pii',None) or {}
    # the chip means: your message was masked, or the answer refers to hidden data
    result['question_masked']=bool(q_found)
    result['pii_masked']=bool(q_found) or 'REDACTED]' in result['answer']
    if masked:
        with connection() as conn:
            audit(conn,p.subject,p.tenant,'guardrail_pii_redacted',{'reason':'pii_redacted','types':masked,
                  'preview':pii_preview(question),'conversation_id':str(conversation_id)})
    save_turn(p.tenant,conversation_id,question,result['answer'],
              result.get('sources') if result.get('status')=='answered' else None)
    if result.get('status')=='declined':
        with connection() as conn:
            audit(conn,p.subject,p.tenant,'guardrail_model_refused',{'reason':'model_declined','preview':pii_preview(question),
                  'conversation_id':str(conversation_id)})
    if result.get('answer')=='The response was blocked by the sensitive-data output guardrail.':
        with connection() as conn:
            audit(conn,p.subject,p.tenant,'guardrail_output_blocked',{'conversation_id':str(conversation_id),'reason':'sensitive_data_in_answer','preview':pii_preview(question)})
        result['status']='guardrail_blocked'
        result['sources']=[]
    return {**result,'conversation_id':str(conversation_id)}

@app.get('/api/conversations')
def conversations(p:Principal=Depends(principal)):
    return {'conversations':list_conversations(p.tenant)}

@app.delete('/api/conversations/{conversation_id}')
def remove_conversation(conversation_id:uuid.UUID,p:Principal=Depends(principal)):
    n=delete_conversation(p.tenant,conversation_id)
    with connection() as conn:
        audit(conn,p.subject,p.tenant,'conversation_deleted',{'conversation_id':str(conversation_id),'messages':n})
    return {'deleted':n}

@app.get('/api/conversations/{conversation_id}/messages')
def conversation_messages(conversation_id:uuid.UUID,p:Principal=Depends(principal)):
    return {'conversation_id':str(conversation_id),'messages':list_messages(p.tenant,conversation_id)}

@app.post('/api/feedback')
def feedback(body:Feedback,p:Principal=Depends(principal)):
    with connection() as conn:
        conn.execute('INSERT INTO feedback(tenant_id,actor,rating,note) VALUES (%s,%s,%s,%s)',
                     (p.tenant,p.subject,body.rating,body.note))
    return {'saved':True}

@app.get('/api/admin/metrics')
def metrics(p:Principal=Depends(principal)):
    with connection() as conn:
        docs=conn.execute('SELECT status,count(*) AS total FROM documents WHERE tenant_id=%s GROUP BY status',(p.tenant,)).fetchall()
        fb=conn.execute('SELECT rating,count(*) AS total FROM feedback WHERE tenant_id=%s GROUP BY rating',(p.tenant,)).fetchall()
    return {'documents':docs,'feedback':fb}




class ApprovalDecision(BaseModel):
    approve:bool
    
@app.get('/api/approvals')
def list_approvals(p:Principal=Depends(principal)):
    if not p.admin: raise HTTPException(403,'Admin only')
    with connection() as conn:
        rows=conn.execute("""SELECT a.id,a.requester,a.question,a.status,a.reviewer,a.created_at,a.reviewed_at,
                a.action,a.executed,a.document_id,d.title AS document_title
            FROM approvals a LEFT JOIN documents d ON d.id=a.document_id
            WHERE a.tenant_id=%s ORDER BY a.created_at DESC LIMIT 100""",(p.tenant,)).fetchall()
    return {'approvals':rows}

_GENERIC={'pdf','docx','txt','doc','file','document','copy','final','the','and','for','with','new','old'}

def _find_delete_target(conn,p:Principal,question:str):
    """Work out which single document a 'delete ...' request means. Returns ('delete', {id,title}) or ('delete', None)."""
    docs=conn.execute('''SELECT id,title FROM documents WHERE tenant_id=%s AND status<>'retired'
        AND (allowed_groups && %s::text[] OR owner_sub=%s)''',(p.tenant,list(p.groups),p.subject)).fetchall()
    words=set(re.findall(r'[a-z0-9]+',question.lower()))
    scored=[]
    for d in docs:
        stem=Path(d['title']).stem.lower()
        tokens={t for t in re.findall(r'[a-z0-9]+',stem) if len(t)>=3 and t not in _GENERIC}
        scored.append((len(tokens&words),d))
    best=max((s for s,_ in scored),default=0)
    top=[d for s,d in scored if s==best and s>0]
    if len(top)==1: return 'delete',top[0]
    if not top and words&{'pdf','document','file','doc','docx','txt'}:
        # "delete the pdf": only safe when exactly one document of that kind exists
        kind={'pdf':'.pdf','docx':'.docx','doc':'.docx','txt':'.txt'}
        suffixes={kind[w] for w in words if w in kind}
        pool=[d for d in docs if not suffixes or Path(d['title']).suffix.lower() in suffixes]
        if len(pool)==1: return 'delete',pool[0]
    return 'delete',None

def _delete_document(conn,p:Principal,document_id)->bool:
    """Remove a document everywhere: Pinecone vectors, the S3 file, and retire its row (kept for audit)."""
    doc=conn.execute("SELECT s3_key,title FROM documents WHERE id=%s AND tenant_id=%s AND status<>'retired'",
                     (document_id,p.tenant)).fetchone()
    if not doc: return False
    try:
        removed=delete_vectors(p.tenant,str(document_id))
        if keyword.enabled():
            try: keyword.delete_document(p.tenant,str(document_id))
            except Exception: log.warning('Elasticsearch delete failed for %s; run scripts/sync_to_elasticsearch.py',document_id,exc_info=True)
        delete_object(doc['s3_key'])
    except Exception as exc:
        log.exception('Deleting document %s failed',document_id)
        raise HTTPException(502,'Deleting the document failed. The request is still pending, so you can retry.') from exc
    conn.execute("UPDATE documents SET status='retired' WHERE id=%s",(document_id,))
    audit(conn,p.subject,p.tenant,'document_deleted',{'document_id':str(document_id),'title':doc['title'],'chunks':removed})
    return True

@app.post('/api/approvals/{approval_id}/decision')
def decide(approval_id:uuid.UUID,body:ApprovalDecision,p:Principal=Depends(principal)):
    if not p.admin: raise HTTPException(403,'Admin only')
    with connection() as conn:
        row=conn.execute("SELECT action,document_id FROM approvals WHERE id=%s AND tenant_id=%s AND status='pending' FOR UPDATE",
                         (approval_id,p.tenant)).fetchone()
        if not row: raise HTTPException(404,'Pending approval not found')
        executed=False
        # Only an explicit, identified delete is carried out; every other request type stays a decision record.
        if body.approve and row['action']=='delete' and row['document_id']:
            executed=_delete_document(conn,p,row['document_id'])
        conn.execute("UPDATE approvals SET status=%s,reviewer=%s,reviewed_at=now(),executed=%s WHERE id=%s",
                     ('approved' if body.approve else 'rejected',p.subject,executed,approval_id))
        audit(conn,p.subject,p.tenant,'approval_decided',{'id':str(approval_id),'approved':body.approve,'executed':executed})
    return {'status':'approved' if body.approve else 'rejected','executed':executed}

@app.get('/api/admin/guardrails')
def guardrail_metrics(p:Principal=Depends(principal)):
    if not p.admin: raise HTTPException(403,'Admin only')
    with connection() as conn:
        rows=conn.execute("""SELECT action,count(*) AS total FROM audit_events
            WHERE tenant_id=%s AND action IN ('guardrail_input_blocked','guardrail_output_blocked','guardrail_model_refused','guardrail_pii_redacted')
            GROUP BY action ORDER BY action""",(p.tenant,)).fetchall()
        recent=conn.execute("""SELECT action,detail,created_at FROM audit_events
            WHERE tenant_id=%s AND action IN ('guardrail_input_blocked','guardrail_output_blocked','guardrail_model_refused','guardrail_pii_redacted')
            ORDER BY id DESC LIMIT 20""",(p.tenant,)).fetchall()
    return {'events':rows,'recent':recent,'mode':'deterministic_demo','managed_guardrails':False}
