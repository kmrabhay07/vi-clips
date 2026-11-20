'use client';
import {useEffect,useRef,useState} from 'react';
export default function YouTubeCallback(){
 const [message,setMessage]=useState('Connecting your YouTube channel…');
 const started=useRef(false);
 useEffect(()=>{if(started.current)return;started.current=true;const params=new URLSearchParams(window.location.search);const code=params.get('code'),state=params.get('state'),error=params.get('error');window.history.replaceState({},'',window.location.pathname);if(error||!code||!state){setMessage('Connection was cancelled or expired. Return to Publishing queue and try again.');return}fetch('/api/youtube/callback',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({code,state})}).then(async response=>{const result=await response.json();if(!response.ok)throw Error(result.detail||'Could not connect');window.location.replace('/?tab=publishing&youtube=connected')}).catch(error=>setMessage(error.message))},[]);
 return <main style={{margin:'80px auto',maxWidth:600,width:'auto',padding:30}}><h1>Connect YouTube</h1><p style={{marginTop:20,lineHeight:1.8}}>{message}</p><a href="/?tab=publishing" style={{display:'inline-block',marginTop:24}}>Return to Publishing queue</a></main>
}
