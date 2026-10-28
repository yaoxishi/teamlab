export default function(component) {
 const {parentElement,data}=component;
 const button=parentElement.querySelector('button');
 const status=parentElement.querySelector('[role="status"]');
 let disposed=false;
 button.addEventListener('click',async()=>{
  button.disabled=true;status.textContent='Checking access and downloading…';
  let url;
  try{
   if(!/^https:\/\/[a-z0-9]{20}\.supabase\.co\/functions\/v1\/private-download$/.test(data.endpoint))throw Error();
   const response=await fetch(data.endpoint,{method:'POST',headers:{Authorization:'Bearer '+data.token,
    apikey:data.publicKey,'Content-Type':'application/json'},body:JSON.stringify({execution_id:data.execution}),
    credentials:'omit',cache:'no-store',redirect:'error'});
   if(!response.ok)throw Error();
   const blob=await response.blob();if(blob.size>25000000)throw Error();
   if(disposed)return;
   url=URL.createObjectURL(blob);
   const a=document.createElement('a');a.href=url;a.download='TeamLab-'+data.execution+'.zip';
   parentElement.appendChild(a);a.click();a.remove();
   status.textContent='Download requested. Save this file; website access expires after 24 hours.';
  }catch{if(!disposed)status.textContent='Download unavailable. Reconnect or refresh your private workspace.';}
  // Native downloads consume Blob URLs after the click has returned.
  finally{if(url)setTimeout(()=>URL.revokeObjectURL(url),1000);if(!disposed)button.disabled=false;}
 });
 return ()=>{disposed=true;};
}
