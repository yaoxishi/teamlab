export default function(component) {
 const {parentElement,data}=component;
 const link=parentElement.querySelector('a'),status=parentElement.querySelector('[role="status"]');
 link.textContent=data.label;link.setAttribute('aria-disabled',String(data.disabled));
 let url;
 try{
  if(data.disabled || !['study.yaml','items.csv','personas_template.csv','anes2016_personas.csv'].includes(data.filename))throw Error();
  const decoded=atob(data.content);if(decoded.length>25000000)throw Error();
  const bytes=Uint8Array.from(decoded,c=>c.charCodeAt(0));
  url=URL.createObjectURL(new Blob([bytes],{type:'application/octet-stream'}));
  link.href=url;link.download=data.filename;
 }catch{link.setAttribute('aria-disabled','true');}
 link.addEventListener('click',event=>{
  if(!url){event.preventDefault();status.textContent='Export unavailable. Check the study and reconnect your private workspace.';}
  else status.textContent='Save your download in a private folder.';
 });
 // Use the browser's native download action; keep bytes alive until unmount.
 return ()=>{if(url)URL.revokeObjectURL(url);};
}
