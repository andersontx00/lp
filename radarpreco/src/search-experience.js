/* RadarPreco search suggestions; use the existing local catalog, no price API requests. */
(() => {
  'use strict';
  const input=document.getElementById('q');
  const raw=document.getElementById('catalog-data');
  const box=input?.closest('.searchbox');
  if(!input||!raw||!box)return;
  let products=[];
  try{products=JSON.parse(raw.textContent)||[]}catch{return}
  const normalize=s=>String(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().trim();
  const escape=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const suggestions=document.createElement('div');
  suggestions.id='rp-model-suggestions';
  suggestions.setAttribute('role','listbox');
  suggestions.setAttribute('aria-label','Modelos encontrados');
  suggestions.hidden=true;
  box.appendChild(suggestions);
  input.setAttribute('aria-controls','rp-model-suggestions');
  input.setAttribute('aria-autocomplete','list');
  let active=-1,visible=[];
  const canonical=q=>normalize(q).replace(/\bcore(\s*[345])\b/g,'corre$1');
  const close=()=>{suggestions.hidden=true;visible=[];active=-1;input.setAttribute('aria-expanded','false')};
  const open=()=>{suggestions.hidden=false;input.setAttribute('aria-expanded','true')};
  function choose(i){
    if(!visible[i])return;
    const chosen=visible[i].name;
    input.value=chosen;
    close();
    document.getElementById('searchNow')?.click();
  }
  function update(){
    const query=canonical(input.value);
    if(query.length<2){close();return}
    const tokens=query.split(/\s+/).filter(Boolean);
    visible=products.filter(p=>{
      const hay=normalize(p.name+' '+p.category+' '+p.subcategory);
      return tokens.every(t=>hay.includes(t));
    }).sort((a,b)=>{
      const nA=normalize(a.name),nB=normalize(b.name);
      const score=n=>n===query?100:n.startsWith(query)?70:n.includes(query)?40:10;
      return score(nB)-score(nA)||nA.length-nB.length;
    }).slice(0,7);
    if(!visible.length){close();return}
    active=-1;
    suggestions.innerHTML=visible.map((p,i)=>'<button type="button" role="option" id="rp-suggest-'+i+'" data-option="'+i+'" aria-selected="false">'+
      '<span class="rp-suggest-icon" aria-hidden="true">'+escape(p.icon||'📦')+'</span>'+
      '<span><strong>'+escape(p.name)+'</strong><small>'+escape(p.category)+' · '+escape(p.subcategory)+'</small></span>'+
      '<span class="rp-suggest-chevron" aria-hidden="true">›</span></button>').join('');
    open();
  }
  function activate(next){
    active=Math.max(0,Math.min(visible.length-1,next));
    suggestions.querySelectorAll('[data-option]').forEach((b,i)=>{
      b.classList.toggle('active',i===active);b.setAttribute('aria-selected',String(i===active));
    });
  }
  input.addEventListener('input',update);
  input.addEventListener('focus',()=>{if(input.value.trim().length>=2)update()});
  input.addEventListener('keydown',event=>{
    if(suggestions.hidden)return;
    if(event.key==='ArrowDown'){event.preventDefault();activate(active+1)}
    else if(event.key==='ArrowUp'){event.preventDefault();activate(active<=0?visible.length-1:active-1)}
    else if(event.key==='Enter'&&active>=0){event.preventDefault();event.stopImmediatePropagation();choose(active)}
    else if(event.key==='Escape'){event.preventDefault();close()}
  },true);
  suggestions.addEventListener('pointerdown',e=>{if(e.target.closest('[data-option]'))e.preventDefault()});
  suggestions.addEventListener('click',e=>{
    const b=e.target.closest('[data-option]');if(b)choose(Number(b.dataset.option))
  });
  document.addEventListener('click',e=>{if(!box.contains(e.target))close()});
  document.getElementById('searchNow')?.addEventListener('click',close);
})();