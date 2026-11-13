/* RadarPreço v12 – focused navigation on mobile; price and alert functions unchanged. */
(()=>{'use strict';
const q=s=>document.querySelector(s),mobile=window.matchMedia('(max-width:760px)');
const detail=q('#detail'),results=q('#list'),nav=q('.r9-bottom');if(!detail||!results)return;
document.body.classList.add('rp-ui-ready');
const ids=['mobileHome','mobileSearch','mobileSaved','mobileAlerts','mobileAccount'];
function activate(id){for(const key of ids){const b=document.getElementById(key);if(!b)continue;b.classList.toggle('rp-active',key===id);if(key===id)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')}}
function focused(open){document.body.classList.toggle('rp-detail-open',open&&mobile.matches);if(open&&mobile.matches){activate('');window.scrollTo({top:0,behavior:'instant'})}}
function addBack(){if(detail.querySelector('.rp-mobile-back'))return;const b=document.createElement('button');b.type='button';b.className='rp-mobile-back';b.textContent='← Voltar aos produtos';b.setAttribute('aria-label','Voltar para a lista de produtos');b.addEventListener('click',()=>{focused(false);activate('mobileSearch');q('.toolbar')?.scrollIntoView({behavior:'smooth',block:'start'})});detail.insertBefore(b,detail.firstChild)}
addBack();const watch=new MutationObserver(()=>{if(!detail.querySelector('.rp-mobile-back')){watch.disconnect();addBack();watch.observe(detail,{childList:true})}});watch.observe(detail,{childList:true});
results.addEventListener('click',e=>{if(mobile.matches&&e.target.closest('[data-name]'))focused(true)});
q('#searchNow')?.addEventListener('click',()=>{if(mobile.matches&&q('#q')?.value.trim())focused(true)});
q('#quick')?.addEventListener('click',e=>{if(mobile.matches&&e.target.closest('[data-quick]'))focused(true)});
nav?.addEventListener('click',e=>{const b=e.target.closest('button[id]');if(b){focused(false);activate(b.id)}});
for(const id of ['savedBtn','allBtn'])document.getElementById(id)?.addEventListener('click',()=>focused(false));
q('#categories')?.addEventListener('click',e=>{if(e.target.closest('[data-cat]'))focused(false)});
mobile.addEventListener?.('change',()=>focused(false));activate('mobileHome');
try{if(mobile.matches&&decodeURIComponent(location.hash.slice(1)).trim())focused(true)}catch(e){}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('rp-detail-open'))focused(false)});
})();