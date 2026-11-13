/* RadarPreco: freely licensed product photos for *visible* cards, no price-API usage. */
(()=>{
'use strict';
const list=document.getElementById('list');if(!list)return;
const KEY='radarpreco_images_v2',DAY=86400000,cache=new Map(),miss=new Map(),queue=[],pending=new Set();
let running=0,count=0,lastStart=0,timer=null;
const normalize=s=>String(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();
const valid=u=>typeof u==='string'&&/^https:\/\/[^\s<>"']+$/i.test(u)&&u.length<2000;
const generic=new Set(['ssd','memoria','placa','mouse','headset','monitor','notebook','hd','celular','de','da','do','com','para','sem','laptop','ar','condicionado']);
const special=new Set(['fe','pro','ultra','max','plus','mini','air','x','ti','super','se','tkl','lxs','oled','xt']);
function correctModel(name,title){
 const wanted=normalize(name),asset=normalize(title);
 const isDevice=/\b(?:iphone|smartphone|celular|galaxy (?:a\d+|s\d+|m\d+|tab|z fold|z flip)|ipad|tablet|redmi|poco|moto [a-z]\d+)\b/.test(wanted);
 const explicitAccessory=/^(?:kit |combo |pack |conjunto )?(?:capa|capinha|case|pelicula|carregador|cabo|tela|display|bateria|adaptador|acessorio)/.test(wanted);
 const accessory=/\b(?:capa|capas|capinha|capinhas|case|cases|cover|pelicula|peliculas|carregador|carregadores|suporte|tela|display|bateria|bolsa|vidro|protetor|adesivo|cabo|adaptador|acessorio|acessorios|mockup|miniatura|replica|dummy)\b/.test(asset);
 if(isDevice&&!explicitAccessory&&accessory)return false;
 const a=normalize(name).split(' ').filter(w=>w&&!generic.has(w)),b=new Set(normalize(title.replace(/^file:/i,'')).split(' '));
 const nums=a.filter(w=>/\d/.test(w)),qual=a.filter(w=>special.has(w));
 const brand=a.find(w=>w.length>2&&!/\d/.test(w)&&!special.has(w));
 if(nums.length)return nums.every(w=>b.has(w))&&qual.every(w=>b.has(w))&&(!brand||b.has(brand));
 const terms=a.filter(w=>w.length>=3);return terms.length>=2&&terms.slice(0,4).every(w=>b.has(w));
}
function persist(){try{const v={};for(const [k,p] of cache)if(p.exp>Date.now())v[k]=p;for(const [k,exp] of miss)if(exp>Date.now()&&!v[k])v[k]={miss:true,exp};localStorage.setItem(KEY,JSON.stringify(Object.fromEntries(Object.entries(v).slice(-250))))}catch(e){}}
try{const saved=JSON.parse(localStorage.getItem(KEY)||'{}');for(const [key,v] of Object.entries(saved))if(v.exp>Date.now()){if(v.miss)miss.set(key,v.exp);else if(valid(v.url)&&valid(v.source))cache.set(key,v)}}catch(e){}
window.RadarPrecoProductImages=cache;
function apply(name){const p=cache.get(normalize(name));if(!p)return;for(const card of list.querySelectorAll('button.entry[data-name]')){
 if(normalize(card.dataset.name)!==normalize(name))continue;const thumb=card.querySelector('.entry-thumb');if(!thumb||thumb.querySelector('img:not([data-commons])'))continue;
 if(thumb.querySelector('img[data-commons]'))continue;
 const img=document.createElement('img');img.src=p.url;img.alt='';img.dataset.commons='1';img.loading='lazy';img.referrerPolicy='no-referrer';img.title='Foto: '+p.author+' • '+p.license+' (veja fonte no detalhe)';img.onerror=()=>img.remove();thumb.appendChild(img);
 }
 const title=document.querySelector('#detail .r6-head h2'),area=document.querySelector('#detail .r6-image');
 if(title&&area&&normalize(title.textContent)===normalize(name)&&!area.querySelector('img')){
 const img=document.createElement('img');img.src=p.url;img.alt='Foto do produto '+name;img.referrerPolicy='no-referrer';img.onerror=()=>img.remove();area.appendChild(img);const subtitle=document.querySelector('#detail .r6-sub');if(subtitle){subtitle.textContent='Imagem de acervo licenciado • ';const credit=document.createElement('a');credit.href=p.source;credit.target='_blank';credit.rel='noopener noreferrer';credit.className='r11-photo-credit';credit.textContent='Foto: '+p.author+' • '+p.license+' (ver origem)';subtitle.appendChild(credit)}
 }
}
const textOf=s=>{const e=document.createElement('span');e.innerHTML=String(s||'').slice(0,450);return e.textContent.trim().slice(0,100)};
async function request(url){const abort=new AbortController(),t=setTimeout(()=>abort.abort(),6500);try{const r=await fetch(url,{signal:abort.signal,credentials:'omit'});return r.ok?await r.json():null}catch(e){return null}finally{clearTimeout(t)}}
async function commons(name){const p=new URLSearchParams({action:'query',generator:'search',gsrsearch:name,gsrnamespace:'6',gsrlimit:'8',prop:'imageinfo',iiprop:'url|extmetadata',iiurlwidth:'240',format:'json',origin:'*'});
 const d=await request('https://commons.wikimedia.org/w/api.php?'+p);const pages=Object.values(d?.query?.pages||{}).sort((a,b)=>(a.index??50)-(b.index??50));
 for(const page of pages){const meta=page.imageinfo?.[0];if(!meta||!correctModel(name,page.title))continue;
 if(!/\.(?:jpg|jpeg|png|webp)(?:\?|$)/i.test(meta.url||''))continue;
 const url=meta.thumburl||meta.url,source=meta.descriptionurl;if(!valid(url)||!valid(source))continue;
 return {url,source,author:textOf(meta.extmetadata?.Artist?.value)||'autor na fonte',license:textOf(meta.extmetadata?.LicenseShortName?.value)||'ver licença',provider:'Wikimedia Commons'};
 }return null;
}
async function openverse(name){const p=new URLSearchParams({q:name,page_size:'8'}),d=await request('https://api.openverse.org/v1/images/?'+p);
 for(const row of d?.results||[]){if(!correctModel(name,String(row.title||'')))continue;
 if(valid(row.thumbnail)&&valid(row.foreign_landing_url))return {url:row.thumbnail,source:row.foreign_landing_url,author:String(row.creator||'autor na fonte').slice(0,100),license:String(row.license||'ver licença').slice(0,40),provider:'Openverse'};
 }return null;
}
function schedule(){if(timer||!queue.length||running>=2||count>=60)return;timer=setTimeout(()=>{timer=null;drain()},Math.max(0,1000-(Date.now()-lastStart)))}
function drain(){if(running>=2||!queue.length||count>=60)return;const name=queue.shift(),key=normalize(name);pending.delete(key);count++;running++;lastStart=Date.now();
 (async()=>{const p=await commons(name)||await openverse(name);if(p){cache.set(key,{...p,exp:Date.now()+30*DAY});persist();apply(name)}else{miss.set(key,Date.now()+3*DAY);persist()}})().catch(()=>{miss.set(key,Date.now()+3600000)}).finally(()=>{running--;schedule()});schedule();}
function enqueue(name){const key=normalize(name);if(!key||cache.has(key)||pending.has(key)||miss.get(key)>Date.now()||count+queue.length>=60)return;
 const card=[...list.querySelectorAll('button.entry[data-name]')].find(x=>normalize(x.dataset.name)===key);if(!card||card.querySelector('.entry-thumb img'))return;
 pending.add(key);queue.push(name);schedule();}
const observe='IntersectionObserver' in window?new IntersectionObserver(entries=>{for(const item of entries)if(item.isIntersecting){observe.unobserve(item.target);enqueue(item.target.dataset.name)}},{root:list,rootMargin:'65px'}):null;
function scan(){for(const row of list.querySelectorAll('button.entry[data-name]')){const key=normalize(row.dataset.name);if(cache.has(key))apply(row.dataset.name);else if(observe)observe.observe(row);else enqueue(row.dataset.name)}}
let update=null;const watcher=new MutationObserver(()=>{if(update)return;update=setTimeout(()=>{update=null;scan()},90)});watcher.observe(list,{childList:true});scan();
window.RadarPrecoPhotoStatus=()=>({found:cache.size,queries:count,pending:queue.length});
})();