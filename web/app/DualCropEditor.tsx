'use client';
import {useEffect,useRef,useState} from 'react';
export type CropRect={x:number;y:number;width:number;height:number};
export type DualCrop={face:CropRect;gameplay:CropRect;top_fraction:number;face_on_top:boolean};
export const defaultDualCrop: DualCrop={face:{x:0,y:0,width:.3,height:.35},gameplay:{x:.3,y:0,width:.7,height:1},top_fraction:.3,face_on_top:true};
type Props={sourceId:string;value:DualCrop;onChange:(value:DualCrop)=>void;disabled?:boolean;start?:number;end?:number;snapshot?:boolean;onReady?:(ready:boolean)=>void};
const clamp=(v:number,min:number,max:number)=>Math.max(min,Math.min(max,v));
export default function DualCropEditor({sourceId,value,onChange,disabled=false,start=0,end,snapshot=false,onReady}:Props){
 const image=useRef<HTMLImageElement>(null);
 const video=useRef<HTMLVideoElement>(null),canvas=useRef<HTMLCanvasElement>(null),surface=useRef<HTMLDivElement>(null);
 const drag=useRef<{id:number;part:'face'|'gameplay';resize:boolean;x:number;y:number;rect:CropRect}|null>(null);
 const [selected,setSelected]=useState<'face'|'gameplay'>('face'),[ratio,setRatio]=useState(16/9),[loaded,setLoaded]=useState(false),[failed,setFailed]=useState(false),[duration,setDuration]=useState(0);
 const latest=useRef(value);latest.current=value;
 function draw(){const v=snapshot?image.current:video.current,c=canvas.current;if(!v||!c)return;const sourceWidth=snapshot?image.current?.naturalWidth:video.current?.videoWidth,sourceHeight=snapshot?image.current?.naturalHeight:video.current?.videoHeight;if(!sourceWidth||!sourceHeight||(!snapshot&&video.current!.readyState<2))return;const ctx=c.getContext('2d');if(!ctx)return;const layout=latest.current,top=Math.round(c.height*layout.top_fraction/2)*2;
  ctx.fillStyle='#100e18';ctx.fillRect(0,0,c.width,c.height);
  const parts=layout.face_on_top?[layout.face,layout.gameplay]:[layout.gameplay,layout.face];
  parts.forEach((rect,index)=>{let sw=rect.width*sourceWidth,sh=rect.height*sourceHeight,sx=rect.x*sourceWidth,sy=rect.y*sourceHeight;const h=index?c.height-top:top,target=c.width/h;if(sw/sh>target){sx+=(sw-sh*target)/2;sw=sh*target}else{sy+=(sh-sw/target)/2;sh=sw/target}ctx.drawImage(v,sx,sy,sw,sh,0,index?top:0,c.width,h)});
 }
 useEffect(()=>{draw()},[value,loaded]);
 useEffect(()=>{if(snapshot)return;let frame=0;const tick=()=>{if(video.current&&!video.current.paused)draw();frame=requestAnimationFrame(tick)};frame=requestAnimationFrame(tick);return()=>cancelAnimationFrame(frame)},[sourceId,snapshot]);
 useEffect(()=>{if(video.current&&loaded){video.current.currentTime=start}},[start,loaded]);
 const rect=value[selected];
 function updateRect(part:'face'|'gameplay',next:CropRect){onChange({...value,[part]:next})}
 function numeric(field:keyof CropRect,n:number){if(!Number.isFinite(n))return;const next={...rect,[field]:n/100};if(field==='x')next.x=clamp(next.x,0,1-next.width);if(field==='y')next.y=clamp(next.y,0,1-next.height);if(field==='width')next.width=clamp(next.width,.02,1-next.x);if(field==='height')next.height=clamp(next.height,.02,1-next.y);updateRect(selected,next)}
 return <div className="dual-editor">
  <div className="dual-instructions"><strong>1. Select two areas from your recording</strong><p>Choose Facecam or Gameplay. Drag its box to move it; drag the corner to resize. Each panel fills its space without stretching.</p></div>
  <div className="dual-region-tabs">{(['face','gameplay'] as const).map(part=><button key={part} disabled={disabled} aria-pressed={selected===part} className={selected===part?'selected':''} onClick={()=>setSelected(part)}>{part==='face'?'Facecam':'Gameplay'}</button>)}</div>
  <div className="dual-workspace"><div>
   <div ref={surface} className="dual-source" style={{aspectRatio:ratio}}>
    {snapshot?<img ref={image} src={`/api/sources/${sourceId}/layout-frame`} alt="Captured video frame for facecam and gameplay crop selection" onLoad={e=>{setRatio(e.currentTarget.naturalWidth/e.currentTarget.naturalHeight);setLoaded(true);onReady?.(true);draw()}} onError={()=>{setFailed(true);onReady?.(false)}}/>:<video ref={video} src={`/api/media/${sourceId}.mp4`} playsInline preload="auto" onLoadedMetadata={e=>{setRatio(e.currentTarget.videoWidth/e.currentTarget.videoHeight);setDuration(e.currentTarget.duration);e.currentTarget.currentTime=start;setLoaded(true)}} onLoadedData={draw} onSeeked={draw} onError={()=>setFailed(true)} onTimeUpdate={e=>{if(end&&e.currentTarget.currentTime>=end){e.currentTarget.pause();e.currentTarget.currentTime=start}draw()}}/>}
    {loaded&&(['face','gameplay'] as const).map(part=>{const r=value[part];return <div key={part} className={`dual-selection ${part} ${selected===part?'active':''}`} style={{left:`${r.x*100}%`,top:`${r.y*100}%`,width:`${r.width*100}%`,height:`${r.height*100}%`}} onPointerDown={e=>{if(disabled)return;e.preventDefault();video.current?.pause();setSelected(part);drag.current={id:e.pointerId,part,resize:(e.target as HTMLElement).dataset.resize==='true',x:e.clientX,y:e.clientY,rect:{...r}};e.currentTarget.setPointerCapture(e.pointerId)}} onPointerMove={e=>{const d=drag.current,b=surface.current?.getBoundingClientRect();if(!d||d.id!==e.pointerId||!b)return;const dx=(e.clientX-d.x)/b.width,dy=(e.clientY-d.y)/b.height;updateRect(d.part,d.resize?{...d.rect,width:clamp(d.rect.width+dx,.02,1-d.rect.x),height:clamp(d.rect.height+dy,.02,1-d.rect.y)}:{...d.rect,x:clamp(d.rect.x+dx,0,1-d.rect.width),y:clamp(d.rect.y+dy,0,1-d.rect.height)})}} onPointerUp={()=>{drag.current=null}} onPointerCancel={()=>{drag.current=null}}><span>{part==='face'?'Facecam':'Gameplay'}</span><i data-resize="true" aria-label={`Resize ${part} crop`} /></div>})}
   </div>
   {failed&&<p className="error-text">The source frame could not load. Check the cloud connection.</p>}
   {!snapshot&&<div className="dual-playback"><button disabled={!loaded} onClick={()=>{const v=video.current;if(!v)return;if(v.paused)v.play().catch(()=>{});else v.pause()}}>Play / pause</button><input type="range" aria-label="Choose source frame" min={start} max={end??duration??1} step=".1" defaultValue={start} onChange={e=>{if(video.current){video.current.pause();video.current.currentTime=Number(e.target.value)}}}/></div>}
   <div className="dual-coordinates">{(['x','y','width','height'] as const).map(field=><label key={field}>{field==='x'?'Left':field==='y'?'Top':field==='width'?'Width':'Height'} %<input type="number" min={field==='width'||field==='height'?2:0} max={100} step={.5} disabled={disabled} value={Math.round(rect[field]*1000)/10} onChange={e=>numeric(field,Number(e.target.value))}/></label>)}</div>
  </div><div className="dual-result"><strong>9:16 layout preview</strong><canvas ref={canvas} width={270} height={480} aria-label="Live stacked facecam and gameplay preview"/><small>{value.face_on_top?'Facecam above gameplay':'Gameplay above facecam'}</small>{end&&<small>Captions and watermark appear in the saved rendered preview.</small>}</div></div>
  <div className="dual-stack-controls"><strong>2. Arrange the stack</strong><label>Top panel height: {Math.round(value.top_fraction*100)}%<input type="range" aria-label="Top panel height" min={.2} max={.6} step={.01} disabled={disabled} value={value.top_fraction} onChange={e=>onChange({...value,top_fraction:Number(e.target.value)})}/></label><button disabled={disabled} onClick={()=>onChange({...value,face_on_top:!value.face_on_top})}>Swap top and bottom</button><button disabled={disabled} onClick={()=>onChange(structuredClone(defaultDualCrop))}>Reset areas</button></div>
 </div>
}
