"""Reference behaviour for the controls, embedded in the cards. This is the interaction contract, executable."""
BEZ = r"""
// cubic-bezier(x1,y1,x2,y2) as a function of time, solved by Newton then bisection; read from the easing token itself.
function bezier(x1,y1,x2,y2){ function cx(t){return 3*x1*t*(1-t)*(1-t)+3*x2*t*t*(1-t)+t*t*t;} function cy(t){return 3*y1*t*(1-t)*(1-t)+3*y2*t*t*(1-t)+t*t*t;}
  function dx(t){return 3*x1*(1-t)*(1-t)+6*(x2-x1)*t*(1-t)+3*(1-x2)*t*t;}
  return function(x){ if(x<=0) return 0; if(x>=1) return 1; var t=x; for(var i=0;i<8;i++){ var d=cx(t)-x; if(Math.abs(d)<1e-6) return cy(t); var s=dx(t); if(Math.abs(s)<1e-6) break; t-=d/s; }
    var lo=0, hi=1; t=x; while(hi-lo>1e-6){ var v=cx(t); if(v<x) lo=t; else hi=t; t=(lo+hi)/2; } return cy(t); }; }
function tokenEase(name){ var v=getComputedStyle(document.documentElement).getPropertyValue('--'+name).trim(); var m=v.match(/cubic-bezier\(([^)]+)\)/); if(!m) return function(x){return x;}; var p=m[1].split(',').map(parseFloat); return bezier(p[0],p[1],p[2],p[3]); }
function tokenMs(name){ return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--'+name)); }
"""
SEG = r"""
// Segmented control: role=radiogroup. Arrow keys move and select (wrapping, skipping disabled), Home/End jump; Tab enters on the checked item.
function kSeg(root, onChange){
  root.classList.add('k-seg-js');
  var thumb=root.querySelector('.k-seg-thumb'), items=[].slice.call(root.querySelectorAll('.k-seg-item'));
  function enabled(){ return items.filter(function(i){ return i.getAttribute('aria-disabled')!=='true'; }); }
  function place(it, animate){ if(!animate){ thumb.style.transition='none'; } thumb.style.width=it.offsetWidth+'px'; thumb.style.transform='translateX('+it.offsetLeft+'px)'; if(!animate){ void thumb.offsetWidth; thumb.style.transition=''; } }
  function current(){ return items.filter(function(i){ return i.getAttribute('aria-checked')==='true'; })[0]; }
  // A font that finishes loading later changes every item's width: re-place without animating. (Not a ResizeObserver on the
  // items: the 500 to 600 weight change on select resizes them by a fraction of a pixel and would cancel the slide.)
  if(document.fonts){ document.fonts.addEventListener('loadingdone', function(){ var c=current(); if(c) place(c,false); }); }
  // quiet: set the state without the slide and without telling onChange (a caller restoring or preparing a control).
  function select(it, focus, quiet){ if(it.getAttribute('aria-disabled')==='true') return; items.forEach(function(i){ var on=i===it; i.setAttribute('aria-checked', on?'true':'false'); i.tabIndex=on?0:-1; }); place(it,!quiet); if(focus) it.focus(); if(onChange && !quiet) onChange(it); }
  items.forEach(function(i){
    i.addEventListener('click', function(){ select(i,false); });
    i.addEventListener('keydown', function(e){ var en=enabled(), k=en.indexOf(i), t=null;
      if(e.key==='ArrowRight'||e.key==='ArrowDown') t=en[(k+1)%en.length]; else if(e.key==='ArrowLeft'||e.key==='ArrowUp') t=en[(k-1+en.length)%en.length];
      else if(e.key==='Home') t=en[0]; else if(e.key==='End') t=en[en.length-1];
      if(t){ e.preventDefault(); select(t,true); } });
  });
  var cur=items.filter(function(i){ return i.getAttribute('aria-checked')==='true'; })[0]||enabled()[0];
  items.forEach(function(i){ i.tabIndex=i===cur?0:-1; }); place(cur,false);
  return {select:select, items:items, place:place};
}
"""
TABS = r"""
// Tabs: role=tablist. Arrow keys move focus and select (automatic activation: views are cheap to show), Home/End jump.
// Left and Right, or Up and Down when the list is aria-orientation="vertical".
function kTabs(root, onChange){
  root.classList.add('k-tabs-js');
  var bar=root.querySelector('.k-tabs-bar'), tabs=[].slice.call(root.querySelectorAll('.k-tab'));
  var vertical=root.getAttribute('aria-orientation')==='vertical', next=vertical?'ArrowDown':'ArrowRight', prev=vertical?'ArrowUp':'ArrowLeft';
  // Horizontal: the bar is the tab's width along the bottom. Vertical: 20px tall on the left edge, centred on the tab.
  function place(t, animate){ if(!animate){ bar.style.transition='none'; }
    if(vertical){ bar.style.height='20px'; bar.style.transform='translateY('+(t.offsetTop+(t.offsetHeight-20)/2)+'px)'; }
    else { bar.style.width=t.offsetWidth+'px'; bar.style.transform='translateX('+t.offsetLeft+'px)'; }
    if(!animate){ void bar.offsetWidth; bar.style.transition=''; } }
  if(document.fonts){ document.fonts.addEventListener('loadingdone', function(){ var c=tabs.filter(function(x){ return x.getAttribute('aria-selected')==='true'; })[0]; if(c) place(c,false); }); }
  function select(t, focus, quiet){ tabs.forEach(function(x){ var on=x===t; x.setAttribute('aria-selected', on?'true':'false'); x.tabIndex=on?0:-1; var p=x.getAttribute('aria-controls'); if(p){ var el=document.getElementById(p); if(el) el.hidden=!on; } }); place(t,!quiet); if(focus) t.focus(); if(onChange && !quiet) onChange(t); }
  tabs.forEach(function(t){
    t.addEventListener('click', function(){ select(t,false); });
    t.addEventListener('keydown', function(e){ var k=tabs.indexOf(t), n=null;
      if(e.key===next) n=tabs[(k+1)%tabs.length]; else if(e.key===prev) n=tabs[(k-1+tabs.length)%tabs.length]; else if(e.key==='Home') n=tabs[0]; else if(e.key==='End') n=tabs[tabs.length-1];
      if(n){ e.preventDefault(); select(n,true); } });
  });
  var cur=tabs.filter(function(t){ return t.getAttribute('aria-selected')==='true'; })[0]||tabs[0];
  tabs.forEach(function(t){ t.tabIndex=t===cur?0:-1; }); place(cur,false);
  return {select:select, tabs:tabs, place:place};
}
"""
TOGGLE = r"""
// Toggle: role=switch on a button. Click, Space or Enter flips it; CSS shows the word that matches aria-checked.
function kToggle(btn, onChange){ btn.addEventListener('click', function(){ if(btn.getAttribute('aria-disabled')==='true') return; var on=btn.getAttribute('aria-checked')!=='true'; btn.setAttribute('aria-checked', on?'true':'false'); if(onChange) onChange(on); }); }
"""
FIT = r"""
// Fit: a band that must stay one line (the header, the Needs strip) gives way in a stated order. Children with
// data-fit-drop="N" are set aside (data-fit-hidden) in ascending N until the band fits; an element hidden for its own
// reason (hidden) is left alone. A need set aside is counted on the band's more item (data-fit-more), which lands on the
// inbox. Runs on resize, when fonts land, and when the caller says the content changed (update).
function kFit(row){
  var more=row.querySelector('[data-fit-more]');
  function over(){ return row.scrollWidth > row.clientWidth + 0.5; }
  function update(){
    var items=[].slice.call(row.querySelectorAll('[data-fit-drop]')).sort(function(a,b){ return a.dataset.fitDrop-b.dataset.fitDrop; });
    items.forEach(function(i){ i.removeAttribute('data-fit-hidden'); }); if(more) more.hidden=true;
    var set=0;
    for(var k=0; k<items.length && over(); k++){ if(items[k].hidden) continue; items[k].setAttribute('data-fit-hidden', '');
      if(more && items[k].classList.contains('k-need')){ set++; more.hidden=false; more.querySelector('b').textContent=set+' more'; } } }
  if(window.ResizeObserver) new ResizeObserver(function(){ update(); }).observe(row);
  if(document.fonts) document.fonts.addEventListener('loadingdone', update);
  update(); return {update:update};
}
"""
