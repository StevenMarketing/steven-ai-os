import http from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const agents = [{id:'chatgpt',name:'ChatGPT · GReply Lead',integration:'not_connected'}, {id:'muse',name:'Muse · SEO Lead',integration:'not_connected'}, {id:'hermes',name:'Hermes · Execution',integration:'not_connected'}, {id:'simulator',name:'SIMULATOR · test only',integration:'local_simulator'}];
const allowed = new Set(['queued','running','testing','review_ready','blocked','awaiting_approval','done']);
export class Store {
  constructor(path, now=()=>Date.now()) {
    this.now=now; this.db=new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, author TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, title TEXT NOT NULL, owner TEXT NOT NULL, priority TEXT NOT NULL, state TEXT NOT NULL, risk INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, fence TEXT, lease INTEGER, heartbeat INTEGER, result TEXT, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, task TEXT, at INTEGER NOT NULL);
    `);
  }
  audit(actor,action,task=null){this.db.prepare('INSERT INTO audit(actor,action,task,at) VALUES(?,?,?,?)').run(actor,action,task,this.now());}
  transaction(fn){this.db.exec('BEGIN IMMEDIATE');try{const out=fn();this.db.exec('COMMIT');return out;}catch(e){this.db.exec('ROLLBACK');throw e;}}
  create({title,owner,priority,idem,risk}) {
    if(typeof title!=='string'||!title.trim()||title.length>240||!agents.some(a=>a.id===owner)||!['low','normal','high'].includes(priority)||typeof idem!=='string'||!/^[a-zA-Z0-9-]{8,100}$/.test(idem)||typeof risk!=='boolean')throw Error('invalid_task');
    return this.transaction(()=>{
      const old=this.db.prepare('SELECT * FROM tasks WHERE idem=?').get(idem);
      if(old){if(old.title!==title||old.owner!==owner||old.priority!==priority||old.risk!==Number(risk))throw Error('idempotency_conflict');return old;}
      const id=randomUUID(); this.db.prepare('INSERT INTO tasks(id,idem,title,owner,priority,state,risk,created) VALUES(?,?,?,?,?,?,?,?)').run(id,idem,title,owner,priority,risk?'awaiting_approval':'queued',Number(risk),this.now());
      this.audit('owner','task_created',id);return this.task(id);
    });
  }
  task(id){return this.db.prepare('SELECT * FROM tasks WHERE id=?').get(id);}
  expire(){return this.transaction(()=>{
    const rows=this.db.prepare("SELECT * FROM tasks WHERE state IN ('running','testing') AND lease<=?").all(this.now());
    for(const t of rows){this.db.prepare('UPDATE tasks SET state=?,fence=NULL,lease=NULL WHERE id=?').run(t.attempt<3?'queued':'blocked',t.id);this.audit('system','lease_expired',t.id);}
    return rows.length;
  });}
  claim(id){return this.transaction(()=>{
    const t=this.task(id);if(!t||t.owner!=='simulator'||t.state!=='queued'||t.risk||t.attempt>=3)throw Error('claim_denied');
    const fence=randomUUID();this.db.prepare("UPDATE tasks SET state='running',attempt=attempt+1,fence=?,lease=?,heartbeat=? WHERE id=?").run(fence,this.now()+60000,this.now(),id);this.audit('simulator','claimed',id);return fence;
  });}
  progress(id,fence,state){if(!allowed.has(state)||!['running','testing','review_ready'].includes(state))throw Error('invalid_transition');return this.transaction(()=>{
    const t=this.task(id);if(!t||!fence||t.fence!==fence||t.lease<=this.now()||!['running','testing'].includes(t.state)||t.state==='testing'&&state==='running')throw Error('stale_claim');
    this.db.prepare('UPDATE tasks SET state=?,heartbeat=?,lease=?,result=? WHERE id=?').run(state,this.now(),this.now()+60000,state==='review_ready'?'SIMULATION ONLY: no external work executed':null,id);this.audit('simulator',state,id);
  });}
  message(text){if(typeof text!=='string'||!text.trim()||text.length>2000)throw Error('invalid_message');this.db.prepare('INSERT INTO messages(author,kind,text,at) VALUES(?,?,?,?)').run('owner','human',text,this.now());this.audit('owner','message_created');}
  snapshot(){return {mode:'ISOLATED PROTOTYPE · NO PRODUCTION CONNECTION',agents,tasks:this.db.prepare('SELECT id,title,owner,priority,state,risk,attempt,heartbeat,result,created FROM tasks ORDER BY created DESC LIMIT 200').all(),messages:this.db.prepare('SELECT * FROM messages ORDER BY id DESC LIMIT 100').all().reverse(),audit:this.db.prepare('SELECT * FROM audit ORDER BY id DESC LIMIT 100').all()};}
  close(){this.db.close();}
}
function secureEqual(a,b){const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y);}
export function createApp({store,token,origin}){
  if(typeof token!=='string'||token.length<32)throw Error('ROOM_OWNER_TOKEN must have at least 32 characters');
  const ui=readFileSync(new URL('./index.html',import.meta.url));const streams=new Set();
  const server=http.createServer(async(req,res)=>{
    const headers={'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Cache-Control':'no-store'};
    const send=(status,data)=>{res.writeHead(status,{...headers,'Content-Type':'application/json'});res.end(JSON.stringify(data));};
    try{
      if(req.url==='/'&&req.method==='GET'){res.writeHead(200,{...headers,'Content-Type':'text/html; charset=utf-8'});res.end(ui);return;}
      if(req.url==='/healthz'&&req.method==='GET'){send(200,{ok:true,mode:'prototype'});return;}
      if(!secureEqual(req.headers.authorization||'',`Bearer ${token}`)){send(401,{error:'unauthorized'});return;}
      if(req.headers.origin&&req.headers.origin!==origin){send(403,{error:'origin_denied'});return;}
      if(req.url==='/api/state'&&req.method==='GET'){send(200,store.snapshot());return;}
      if(req.url==='/api/events'&&req.method==='GET'){
        if(streams.size>=10){send(429,{error:'stream_limit'});return;}
        res.writeHead(200,{...headers,'Content-Type':'text/event-stream','X-Accel-Buffering':'no'});streams.add(res);
        const emit=()=>res.write(`event: state\ndata: ${JSON.stringify(store.snapshot())}\n\n`);emit();const timer=setInterval(emit,5000);req.on('close',()=>{clearInterval(timer);streams.delete(res);});return;
      }
      if(req.method!=='POST'){send(404,{error:'not_found'});return;}
      if(!/^application\/json(?:;|$)/i.test(req.headers['content-type']||'')){send(415,{error:'json_required'});return;}
      let body='';for await(const chunk of req){body+=chunk;if(Buffer.byteLength(body)>8192){send(413,{error:'body_too_large'});return;}}
      let data;try{data=JSON.parse(body);}catch{send(400,{error:'invalid_json'});return;}
      if(!data||typeof data!=='object'||Array.isArray(data)){send(400,{error:'invalid_body'});return;}
      if(req.url==='/api/messages'){store.message(data.text);send(201,{ok:true});return;}
      if(req.url==='/api/tasks'){if(Object.keys(data).some(k=>!['title','owner','priority','idem','risk'].includes(k)))throw Error('unknown_field');send(201,store.create(data));return;}
      if(req.url==='/api/simulate'){
        const fence=store.claim(data.id);store.progress(data.id,fence,'testing');store.progress(data.id,fence,'review_ready');send(200,{simulation:true});return;
      }
      send(404,{error:'not_found'});
    }catch(e){send(400,{error:['invalid_task','idempotency_conflict','claim_denied','stale_claim','invalid_transition','invalid_message','unknown_field'].includes(e.message)?e.message:'request_failed'});}
  });
  server.requestTimeout=10000;server.headersTimeout=10000;
  return server;
}
if(process.argv[1]===fileURLToPath(import.meta.url)){
  const token=process.env.ROOM_OWNER_TOKEN;
  if(!token||token.length<32)throw Error('Configure ROOM_OWNER_TOKEN securely before start');
  if(!process.env.ROOM_DB_PATH||process.env.ROOM_DB_PATH===':memory:')throw Error('ROOM_DB_PATH must be a persistent file path');
  const store=new Store(process.env.ROOM_DB_PATH);store.expire();
  const timer=setInterval(()=>store.expire(),10000);
  const server=createApp({store,token,origin:process.env.ROOM_ORIGIN||'http://127.0.0.1:3000'});
  server.listen(Number(process.env.PORT||3000),process.env.HOST||'127.0.0.1',()=>console.log('Collaboration prototype started; integrations disabled'));
  for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{clearInterval(timer);server.close(()=>{store.close();process.exit(0);});});
}
