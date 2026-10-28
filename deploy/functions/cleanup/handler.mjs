// Independent scheduled maintenance. Actual object deletion uses Storage API.
export function cleanupHandler(config, fetcher=fetch) {
 return async request=>{
  const headers={'Cache-Control':'no-store','Content-Type':'application/json'};
  if(request.method!=='POST'||!config.cleanupSecret||request.headers.get('Authorization')!=='Bearer '+config.cleanupSecret)
   return new Response('{}',{status:403,headers});
  if(!/^https:\/\/[a-z0-9]{20}\.supabase\.co$/.test(config.url))return new Response('{}',{status:503,headers});
  const admin={apikey:config.secretKey,'Content-Type':'application/json'};
  if(config.secretKey.split('.').length===3)admin.Authorization='Bearer '+config.secretKey;
  const send=async(path,body,method='POST')=>{
   const r=await fetcher(config.url+path,{method,headers:admin,body:JSON.stringify(body),redirect:'error',signal:AbortSignal.timeout(30000)});
   if(!r.ok)throw Error();return r.json();
  };
  try {
   await send('/rest/v1/rpc/convolab_reconcile',{});
   const keys=await send('/rest/v1/rpc/convolab_cleanup_objects',{});
   if(!Array.isArray(keys)||keys.length>100)throw Error();
   let deleted=0,failed=0;
   for(const item of keys){
    if(!item||!/^([0-9a-f-]{36})\/([0-9a-f-]{36})\/checkpoints\/([0-9a-f-]{36})\.zip$/.test(item.object_key)){failed++;continue;}
    try{
     await send('/storage/v1/object/convolab-private',{prefixes:[item.object_key]},'DELETE');
     if(await send('/rest/v1/rpc/convolab_cleanup_ack',{p_key:item.object_key})!==true)throw Error();
     deleted++;
    }catch{failed++;} // Keep metadata for the next scheduled attempt.
   }
   const rows=await send('/rest/v1/rpc/convolab_cleanup_rows',{});
   const identities=await send('/rest/v1/rpc/convolab_cleanup_identities',{});
   if(!Number.isInteger(identities)||identities<0||identities>100)throw Error();
   return new Response(JSON.stringify({deleted,failed,rows,identities}),{headers});
  }catch{return new Response('{"maintenance":"unconfirmed"}',{status:503,headers});}
 };
}
