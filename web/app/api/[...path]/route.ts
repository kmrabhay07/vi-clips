import {NextRequest} from 'next/server';
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
async function proxy(request:NextRequest) {
  const path = request.nextUrl.pathname + request.nextUrl.search;
  try {
    const headers = new Headers();
    for(const key of ['content-type','range','if-range','cookie','origin','x-vi-upload']) {const value=request.headers.get(key);if(value)headers.set(key,value)}
    const upstream = await fetch(`${process.env.API_URL || 'http://127.0.0.1:8000'}${path}`, {method:request.method,headers,body:['GET','HEAD'].includes(request.method)?undefined:request.body, duplex:'half',cache:'no-store',redirect:'manual'} as RequestInit & {duplex:string});
    const responseHeaders = new Headers();
    for(const key of ['content-type','content-length','content-disposition','accept-ranges','content-range','location','set-cookie','cache-control']){const value=upstream.headers.get(key);if(value)responseHeaders.set(key,value)}
    return new Response(upstream.body,{status:upstream.status,headers:responseHeaders});
  } catch {return Response.json({detail:'The video API is offline. Start the Docker Compose services to process footage.'},{status:503})}
}
export {proxy as GET,proxy as POST,proxy as PUT,proxy as DELETE};
