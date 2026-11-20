const {readFileSync}=require('node:fs');
const {join}=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const root=join(__dirname,'..','browser-extension');

(async()=>{
 let listener;
 const cookies=[
  {domain:'.youtube.com',path:'/',name:'SID',value:'fixture-secret',httpOnly:true,secure:true},
  {domain:'.google.com',path:'/',name:'SID',value:'unrelated-secret'},
  {domain:'.youtube.com',path:'/',name:'old',value:'expired-secret',expirationDate:1},
  {domain:'.youtube.com',path:'/',name:'partition',value:'partition-secret',partitionKey:{topLevelSite:'https://example.com'}}
 ];
 let cookieReads=0;
 vm.runInNewContext(readFileSync(join(root,'background.js'),'utf8'),{
  URL,Date,Set,chrome:{runtime:{onMessage:{addListener:fn=>listener=fn}},cookies:{getAll:async query=>{assert.equal(query.domain,'youtube.com');cookieReads++;return cookies}}}
 });
 const message={type:'VI_YOUTUBE_SESSION_READ'};
 const denied={url:'https://untrusted.example/',frameId:0,tab:{id:1}};
 assert.equal(listener(message,denied,()=>assert.fail('Cross-origin reply')),undefined);
 assert.equal(listener(message,{...denied,url:'http://localhost:9999/'},()=>assert.fail('Wrong-port reply')),undefined);
 assert.equal(listener(message,{...denied,url:'http://localhost:3166/',frameId:1},()=>assert.fail('Frame reply')),undefined);
 assert.equal(cookieReads,0);
 const result=await new Promise(resolve=>listener(message,{url:'http://localhost:3166/',frameId:0,tab:{id:1}},resolve));
 assert.equal(result.ok,true);
 assert.ok(result.text.includes('#HttpOnly_.youtube.com'));
 assert.ok(result.text.includes('fixture-secret'));
 for(const secret of ['unrelated-secret','expired-secret','partition-secret'])assert.ok(!result.text.includes(secret));

 let receive,upload;
 const posts=[];
 const window={location:{origin:'http://localhost:3166'},addEventListener:(name,fn)=>receive=fn,postMessage:(value,origin)=>posts.push({value,origin})};
 vm.runInNewContext(readFileSync(join(root,'content.js'),'utf8'),{
  window,FormData,Blob,Error,chrome:{runtime:{sendMessage:async()=>result}},
  fetch:async(path,options)=>{upload={path,options};return {ok:true}}
 });
 const data={channel:'VI_CLIPS_SESSION_REQUEST',id:'01234567-89ab-cdef-0123-456789abcdef',action:'sync'};
 await receive({source:window,origin:'https://untrusted.example',data});assert.equal(upload,undefined);
 await receive({source:window,origin:window.location.origin,data});
 assert.equal(upload.path,'/api/youtube/import-cookies');assert.equal(upload.options.redirect,'error');
 assert.equal(await upload.options.body.get('file').text(),result.text);
 assert.equal(posts.length,1);assert.equal(posts[0].value.ok,true);
 assert.ok(!JSON.stringify(posts).includes('fixture-secret'));
 const manifest=JSON.parse(readFileSync(join(root,'manifest.json'),'utf8'));
 assert.deepEqual(manifest.permissions,['cookies']);
 assert.deepEqual(manifest.host_permissions,['https://*.youtube.com/*']);
 console.log('Browser bridge checks passed: exact origin, top frame, domain filter, expiry, partition filter, fixed upload route, no cookie values in page messages.');
})().catch(error=>{console.error(error);process.exitCode=1});
