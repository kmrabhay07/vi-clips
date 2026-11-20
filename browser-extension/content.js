(() => {
  const origin = window.location.origin;
  if (!['http://localhost:3166','http://127.0.0.1:3166'].includes(origin)) return;
  let syncing = false;
  window.addEventListener('message', async event => {
    if (event.source !== window || event.origin !== origin) return;
    const request = event.data;
    if (request?.channel !== 'VI_CLIPS_SESSION_REQUEST' ||
        typeof request.id !== 'string' || !/^[a-f0-9-]{36}$/.test(request.id)) return;
    const respond = result => window.postMessage({channel:'VI_CLIPS_SESSION_RESULT',id:request.id,...result}, origin);
    if (request.action === 'ping') { respond({ok:true}); return; }
    if (request.action !== 'sync' || syncing) return;
    syncing = true;
    try {
      const session = await chrome.runtime.sendMessage({type:'VI_YOUTUBE_SESSION_READ'});
      if (!session?.ok) throw new Error(session?.error || 'YouTube session could not be read.');
      const form = new FormData();
      form.append('file', new Blob([session.text], {type:'text/plain'}), 'youtube-session.txt');
      // Same-origin app proxy carries this over the existing private SSH tunnel.
      // Raw cookie values never enter page messages, DOM, logs, or extension storage.
      const response = await fetch('/api/youtube/import-cookies', {method:'POST',body:form,redirect:'error'});
      if (!response.ok) throw new Error('Your VI Clips server could not save the YouTube session.');
      respond({ok:true});
    } catch (error) {
      respond({ok:false,error:error instanceof Error ? error.message : 'Browser session sharing failed.'});
    } finally { syncing = false; }
  });
})();
