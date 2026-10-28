// Portable handler; run under the Supabase Edge runtime after Stage 6 setup.
// No SDK, signed URL, Streamlit media, cookies, or logs of request content.
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const digest = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), n=>n.toString(16).padStart(2,'0')).join('');
export function downloadHandler(config, fetcher=fetch) {
 return async request => {
  const origin=request.headers.get('Origin');
  const headers={'Cache-Control':'no-store, private, max-age=0',Pragma:'no-cache',
   'X-Content-Type-Options':'nosniff',Vary:'Origin, Authorization'};
  const fail=()=>new Response('Private result unavailable. Reconnect or refresh your workspace.',{status:403,headers});
  if (!/^https:\/\/[a-z0-9]{20}\.supabase\.co$/.test(config.url) ||
      !/^https:\/\/[^/]+$/.test(config.origin) || (origin && origin!==config.origin)) return fail();
  if(origin) Object.assign(headers,{'Access-Control-Allow-Origin':origin,
    'Access-Control-Allow-Methods':'POST, OPTIONS','Access-Control-Allow-Headers':'authorization, content-type, apikey'});
  if(request.method==='OPTIONS') return new Response(null,{status:204,headers});
  if(request.method!=='POST' || new URL(request.url).search) return fail();
  try {
   const auth=request.headers.get('Authorization');
   if(!auth || !/^Bearer [^\s]{1,16384}$/.test(auth)) return fail();
   const raw=await request.text(); if(raw.length>1024) return fail();
   const body=JSON.parse(raw);
   if(Object.keys(body).join(',')!=='execution_id' || !uuid.test(body.execution_id)) return fail();
   const userHeaders={apikey:config.publicKey,Authorization:auth,'Cache-Control':'no-store','Content-Type':'application/json'};
   const json=async(path,opts={})=>{
    const r=await fetcher(config.url+path,{...opts,headers:userHeaders,redirect:'error',cache:'no-store',signal:AbortSignal.timeout(15000)});
    if(!r.ok) throw Error(); return r.json();
   };
   // Never trust submitted owner IDs or locally decoded JWTs.
   const user=await json('/auth/v1/user');
   if(!uuid.test(user.id) || user.is_anonymous!==true || user.role!=='authenticated') return fail();
   const live=async()=>await json('/rest/v1/rpc/convolab_session_active',{method:'POST',body:'{}'})===true;
   if(!await live()) return fail();
   if(await json('/rest/v1/rpc/convolab_touch',{method:'POST',body:'{}'})!==true)return fail();
   const row=async()=>{
    const rows=await json('/rest/v1/convolab_executions?select=id,owner_id,status,save_status,object_key,sha256,expires_at&id=eq.'+body.execution_id+'&owner_id=eq.'+user.id);
    if(!Array.isArray(rows)||rows.length!==1) throw Error();
    const r=rows[0];
    if(r.id!==body.execution_id||r.owner_id!==user.id||!['complete','failed','interrupted'].includes(r.status)||
      !new RegExp('^'+user.id+'/'+r.id+'/checkpoints/[0-9a-f-]{36}\\.zip$').test(r.object_key)||
      !/^[0-9a-f]{64}$/.test(r.sha256)||!Number.isFinite(Date.parse(r.expires_at))||Date.parse(r.expires_at)<=Date.now()) throw Error();
    return r; // RLS database time is authoritative; edge-clock guard is additional.
   };
   const first=await row();
   const admin={apikey:config.secretKey,'Cache-Control':'no-store'};
   if(config.secretKey.split('.').length===3) admin.Authorization='Bearer '+config.secretKey;
   const response=await fetcher(config.url+'/storage/v1/object/convolab-private/'+first.object_key,
     {headers:admin,redirect:'error',cache:'no-store',signal:AbortSignal.timeout(30000)});
   if(!response.ok) return fail();
   const chunks=[]; let size=0;
   const reader=response.body.getReader();
   try {for(;;){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>25000000)throw Error();chunks.push(value);}}
   finally{await reader.cancel();}
   const bytes=new Uint8Array(size);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length;}
   if(await digest(bytes)!==first.sha256 || !await live()) return fail();
   const last=await row(); // No transfer after deadline/revocation during retrieval.
   if(last.object_key!==first.object_key||last.sha256!==first.sha256) return fail();
   return new Response(bytes,{headers:{...headers,'Content-Type':'application/zip',
    'Content-Disposition':'attachment; filename="TeamLab-'+body.execution_id+'.zip"'}});
  }catch{return fail();}
 };
}
