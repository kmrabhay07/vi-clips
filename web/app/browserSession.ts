export async function browserSession(action:'ping'|'sync'):Promise<void>{
 return new Promise((resolve,reject)=>{
  const id=crypto.randomUUID();
  const timer=window.setTimeout(()=>{window.removeEventListener('message',receive);reject(new Error('Install the VI Clips browser extension, then reload this page. Open Browser session setup below for instructions.'))},action==='ping'?1200:15000);
  function receive(event:MessageEvent){
   if(event.source!==window||event.origin!==window.location.origin||event.data?.channel!=='VI_CLIPS_SESSION_RESULT'||event.data.id!==id)return;
   window.clearTimeout(timer);window.removeEventListener('message',receive);
   if(event.data.ok)resolve();else reject(new Error(event.data.error||'Browser session sharing failed.'));
  }
  window.addEventListener('message',receive);
  window.postMessage({channel:'VI_CLIPS_SESSION_REQUEST',id,action},window.location.origin);
 });
}
