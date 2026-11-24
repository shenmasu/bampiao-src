#!/usr/bin/env node
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const SIZES = {'1:1':'1024x1024','3:4':'1024x1536','4:3':'1536x1024','9:16':'1024x1792','16:9':'1792x1024','2:3':'1024x1536','3:2':'1536x1024'};
const args = process.argv.slice(2);
function opt(name, fallback='') { const i=args.indexOf(name); return i>=0 ? (args[i+1]||'') : fallback; }
function has(name) { return args.includes(name); }
function positional() { const valued=new Set(['--ratio','--n','--ref','--out','--resume','--timeout','--url','--key','--save-key']); for(let i=0;i<args.length;i++){ if(valued.has(args[i])){i++;continue;} if(!args[i].startsWith('--')) return args[i]; } return ''; }
const base=(opt('--url',process.env.BAMPIAO_URL||'https://hezhuzi.com')).replace(/\/$/,'');
const keyPath=path.join(os.homedir(),'.bampiao','key');
async function readKey(){ if(opt('--key')||process.env.BAMPIAO_KEY) return (opt('--key')||process.env.BAMPIAO_KEY).trim(); try{return (await fs.readFile(keyPath,'utf8')).trim();}catch{throw new Error('未找到半瓢密钥，请先运行安装脚本或设置 BAMPIAO_KEY');} }
function headers(key){return {'Authorization':'Bearer '+key,'Content-Type':'application/json'};}
async function requestJson(key,p,method='GET',payload=null){const r=await fetch(base+p,{method,headers:headers(key),body:payload==null?undefined:JSON.stringify(payload)});const text=await r.text();let body={};try{body=text?JSON.parse(text):{};}catch{body={error:{message:text.slice(0,300)}};}if(!r.ok) throw new Error(`HTTP ${r.status}：${body?.error?.message||'请求失败'}`);return body;}
async function check(key){const r=await fetch(base+'/bp/me',{method:'POST',headers:headers(key),body:'{}'});if(!r.ok)throw new Error(`自检失败：HTTP ${r.status}`);const me=await r.json();console.log(`半瓢连接正常｜${me.name||'已登录'}｜今日 ${me.used||0} / ${me.unlimited?'不限':me.limit}｜并发上限 ${me.concurrency_unlimited?'不限':me.concurrency}`);}
async function refUri(file){const p=path.resolve(file.replace(/^~(?=$|[/\\])/,os.homedir()));const st=await fs.stat(p);if(st.size>10*1024*1024)throw new Error(`参考图超过 10MB：${p}`);const ext=path.extname(p).toLowerCase();const mime=ext==='.jpg'||ext==='.jpeg'?'image/jpeg':ext==='.webp'?'image/webp':ext==='.gif'?'image/gif':'image/png';return `data:${mime};base64,${(await fs.readFile(p)).toString('base64')}`;}
async function download(key,url,target){if(url.startsWith('/'))url=base+url;const r=await fetch(url,{headers:{Authorization:'Bearer '+key}});if(!r.ok)throw new Error(`下载失败：HTTP ${r.status}`);const data=Buffer.from(await r.arrayBuffer());const suffix=data.subarray(0,6).toString()==='GIF87a'||data.subarray(0,6).toString()==='GIF89a'?'.gif':'.png';let out=target;if(!['.png','.gif'].includes(path.extname(out).toLowerCase())||suffix==='.gif')out=out.replace(/\.[^.\/\\]+$/,'')+suffix;await fs.mkdir(path.dirname(out),{recursive:true});await fs.writeFile(out,data);return path.resolve(out);}
async function waitTask(key,id,timeout){const started=Date.now();for(;;){const t=await requestJson(key,'/v1/tasks/'+encodeURIComponent(id));const elapsed=Math.floor((Date.now()-started)/1000);if(t.status==='queued')console.log(`[半瓢] 排队第 ${t.queue_position||1} 位，${elapsed}s`);else if(t.status==='running')console.log(`[半瓢] 正在生成 ${t.completed_count||0}/${t.n||1} · 第 ${t.attempt_count||0} 次尝试 · ${elapsed}s`);if(['done','failed','cancelled'].includes(t.status))return t;if(elapsed>=timeout)throw new Error(`等待超时；可运行 --resume ${id}`);await new Promise(r=>setTimeout(r,2500));}}
try {
  if(opt('--save-key')){const p=path.join(os.homedir(),'.bampiao','key');await fs.mkdir(path.dirname(p),{recursive:true});await fs.writeFile(p,opt('--save-key').trim()+'\n',{mode:0o600});console.log(`密钥已保存到 ${p}`);process.exit(0);}
  const key=await readKey();
  if(has('--check')){await check(key);process.exit(0);}
  let id=opt('--resume');
  if(!id){const prompt=positional().trim();if(!prompt)throw new Error('缺少提示词');const ratio=opt('--ratio','1:1');if(!SIZES[ratio])throw new Error('比例不支持');const n=Math.max(1,Math.min(4,Number(opt('--n','1'))));const payload={prompt,n,size:SIZES[ratio],timeout:Math.max(30,Math.min(600,Number(opt('--timeout','300'))))};const refs=[];for(let i=0;i<args.length;i++)if(args[i]==='--ref'&&args[i+1])refs.push(await refUri(args[++i]));if(refs.length)payload.ref_images=refs;if(has('--gif'))Object.assign(payload,{gif:true,anim:true,n:1,size:'1024x1024'});const submitted=await requestJson(key,'/v1/tasks','POST',payload);id=submitted.task_id;if(!id)throw new Error('服务没有返回 task_id');console.log(`任务已提交：${id}`);}
  const task=await waitTask(key,id,Number(opt('--timeout','300')));if(task.status!=='done')throw new Error(task.reason||'生成失败');const urls=task.image_urls||[];if(!urls.length)throw new Error('任务已完成但没有可下载图片');const target=path.resolve(opt('--out','bampiao-output'));const stem=path.extname(target)?target.replace(/\.[^.\/\\]+$/,''):target;const files=[];for(let i=0;i<urls.length;i++)files.push(await download(key,urls[i],urls.length===1?target:`${stem}_${i+1}`));if(task.partial)console.error(`部分完成：${task.reason||''}`);console.log(JSON.stringify({task_id:id,status:task.status,partial:!!task.partial,files},null,2));
} catch(e) { console.error(`半瓢出图失败：${e.message||e}`); process.exit(1); }
