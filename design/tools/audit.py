"""Component audit: the mechanical half of 'done'. Renders a card in each theme and checks it.

Checks, per theme:
  contrast  every visible text run against its composited background (4.5:1; 3:1 at >=24px or >=18.66px bold)
  focus     every interactive element takes focus by script and then shows an outline of >=2px at >=3:1 against the surface it sits on,
            or a box-shadow that focus adds (an underline, a ring) with a part of >=2px at >=3:1
  glyph     every character (text, ::before/::after content, input value and placeholder) exists in the shipped font its element asks for first
  boundary  every input's edge (field box, toggle off track) is >=3:1 on its own fill and on the surface around it
  truncated no text we wrote is cut by an ellipsis (kstrl's own run-time text may be, when marked data-runtime), and no token
            (search-index) breaks across lines at its hyphen
  collision no two runs of text overlap (per line box)
  fit       a card's content (the children of .c-wrap, else its .c-tile tiles) has equal top and bottom margins (within 4px), so nothing is clipped or left dead
  disabled  no native disabled attribute: an unavailable control stays focusable (aria-disabled, or readonly for a value) and says why
  target    every interactive element is at least 24x24 CSS px (WCAG 2.2, 2.5.8)
  overflow  no descendant escapes an element marked data-box (or a .k-tile / .k-card)
  painted   no box the page paints itself (a fill, image, shadow or edge its own CSS adds, ::before and ::after included): it is
            a component, the card's ground, the documentation base, a 1px line rule, or inside data-illustration="what it draws"
            (listed). A page rule that recolours a component's classless part fails too
  leak      a page's own rule or style attribute sets only layout (margin, grid and flex placement, position, size, overflow) and
            custom properties on an element that carries a component class: a page restyling a component's part is how its copies drift
Static checks on the CSS the card uses (bundle component layer + the card's own <style>):
  rawcolor  no hex/rgb/hsl literal outside tokens
  motion    every selector that animates or transitions is neutralised under prefers-reduced-motion
  docbase   c-tile, c-ink and c-stage, which the painted check exempts, are defined exactly as card.py writes them
  type      every text run is a step of the type scale (scale.py): family, size, an allowed weight, the step's line height and tracking
  baseline  text beside text in one row sits on one baseline (a run in a box, a control cluster, a title beside a caption excepted)
            when the run sets its own lines (inline runs and SVG text take theirs from around them)
  hygiene   no page stylesheet redefines an un-namespaced class the bundle defines (grow, sep, q, ...): that restyles it page-wide
"""
from __future__ import annotations
import json, re, subprocess, sys
from pathlib import Path

from browsers import HEADLESS as B
assert B, 'no Chromium found: set KSTRL_DESIGN_HEADLESS (see browsers.py)'
ROOT = Path('../system/project').resolve()

JS = r"""
<script>
// Runs after load, fonts and the card's own scripts (which wait on document.fonts.ready and were registered first), so it sees what a reader sees.
window.addEventListener('load', function(){ (document.fonts ? document.fonts.ready : Promise.resolve()).then(function(){ setTimeout(function(){
function parse(c){ const m=c.match(/rgba?\(([^)]+)\)/); if(!m) return null; const p=m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; }
function over(top,bot){ const a=top.a+bot.a*(1-top.a); if(a===0) return {r:0,g:0,b:0,a:0}; return {r:(top.r*top.a+bot.r*bot.a*(1-top.a))/a,g:(top.g*top.a+bot.g*bot.a*(1-top.a))/a,b:(top.b*top.a+bot.b*bot.a*(1-top.a))/a,a:a}; }
function lum(c){ const f=v=>{v/=255; return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)}; return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b); }
function ratio(a,b){ const l1=lum(a), l2=lum(b); return (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05); }
function bgOf(el){ const stack=[]; let e=el; while(e && e.nodeType===1){ const cs=getComputedStyle(e); const c=parse(cs.backgroundColor); if(c && c.a>0) stack.push(c); if(c && c.a>=1) break; e=e.parentElement; } let base={r:255,g:255,b:255,a:1}; const rootBg=parse(getComputedStyle(document.body).backgroundColor); if(rootBg&&rootBg.a>0) base=over(rootBg,base); for(let i=stack.length-1;i>=0;i--) base=over(stack[i],base); return base; }
function opacityOf(el){ let o=1, e=el; while(e && e.nodeType===1){ o*=parseFloat(getComputedStyle(e).opacity); e=e.parentElement; } return o; }
function path(el){ const p=[]; let e=el; while(e && e.nodeType===1 && p.length<4){ p.unshift(e.tagName.toLowerCase()+(e.className&&typeof e.className==='string'?'.'+e.className.trim().split(/\s+/).join('.'):'')); e=e.parentElement; } return p.join(' > '); }
function visible(el){ const r=el.getBoundingClientRect(); const cs=getComputedStyle(el); return r.width>0&&r.height>0&&cs.visibility!=='hidden'&&cs.display!=='none'; }
const out={contrast:[],focus:[],target:[],overflow:[],disabled:[],glyph:[],fit:[],boundary:[],collision:[],truncated:[],leak:[],painted:[],illustration:[],type:[],baseline:[],counts:{text:0,interactive:0}};
const CM={}; for(const k in (window.__CMAP||{})) CM[k]=new Set(window.__CMAP[k]);
const gseen=new Set();
function fam(el, pe){ return getComputedStyle(el, pe||null).fontFamily.split(',')[0].trim().replace(/^["']|["']$/g,''); }
function glyphs(el, text, where){ const f=fam(el, where&&where.startsWith('::')?where:null); const set=CM[f];
  if(!set){ const key='family|'+f; if(!gseen.has(key)){ gseen.add(key); out.glyph.push({el:path(el),problem:'font family not shipped',family:f}); } return; }
  for(const ch of text){ const cp=ch.codePointAt(0); if(cp<=32||/\s/.test(ch)||set.has(cp)) continue; const key=f+'|'+cp; if(gseen.has(key)) continue; gseen.add(key);
    out.glyph.push({el:path(el),problem:'missing glyph',ch:ch,cp:'U+'+cp.toString(16).toUpperCase().padStart(4,'0'),family:f,where:where}); } }
// aria-hidden content is still seen: it is skipped only by the checks about operating a control (focus, target)
// visually hidden for screen readers (clip-path inset(50%) on a 1px box): not drawn, so no contrast, overflow or collision
function srOnly(el){ for(let e=el; e && e.nodeType===1; e=e.parentElement){ const cs=getComputedStyle(e); if(cs.clipPath && cs.clipPath.indexOf('inset(50%')===0) return true; } return false; }
const skip=el=>el.closest('[data-audit-skip]') || srOnly(el);
// inert content (the page under an open modal) cannot be operated at that moment, by design: its controls are checked where they are live
const skipCtl=el=>el.closest('[aria-hidden="true"],[data-audit-skip],[inert]') || managedOption(el);
// an option in a listbox that a combobox drives with aria-activedescendant is operated from the combobox, never focused itself
function managedOption(el){ if(el.getAttribute('role')!=='option') return false; const lb=el.closest('[role=listbox]'); return !!(lb && lb.id && document.querySelector('[aria-controls~="'+lb.id+'"][aria-activedescendant]')); }
// the audit measures end states: a transition would show the first frame of a change instead of where it lands
{ const st=document.createElement('style'); st.setAttribute('data-audit-own',''); st.textContent='*, *::before, *::after { transition:none !important; }'; document.head.appendChild(st); }
// contrast
const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
const seen=new Set();
while(walker.nextNode()){ const t=walker.currentNode; if(!t.nodeValue.trim()) continue; const el=t.parentElement; if(el&&!skip(el)&&visible(el)&&el.tagName!=='STYLE'&&el.tagName!=='SCRIPT') glyphs(el, t.nodeValue, 'text'); if(!el||seen.has(el)||skip(el)||!visible(el)) continue;
  // a motion filmstrip's frames are partly transparent on purpose (a fade-in starts at nothing): no contrast there
  if(el.closest('[data-audit-film]')) continue; seen.add(el); out.counts.text++;
  const cs=getComputedStyle(el); let fg=parse(cs.color); if(!fg) continue; const bg=bgOf(el); const op=opacityOf(el); fg={...fg,a:fg.a*op}; const eff=over(fg,bg);
  const size=parseFloat(cs.fontSize), w=parseInt(cs.fontWeight); const large=size>=24||(size>=18.66&&w>=700); const need=large?3:4.5; const r=ratio(eff,bg);
  if(r<need-0.005) out.contrast.push({el:path(el),text:t.nodeValue.trim().slice(0,40),ratio:+r.toFixed(2),need}); }
// pseudo-element content and input values/placeholders render text too
for(const el of document.body.querySelectorAll('*')){ if(skip(el)||!visible(el)) continue;
  for(const pe of ['::before','::after']){ const c=getComputedStyle(el,pe).content; if(c&&c!=='none'&&c!=='normal'&&c.startsWith('"')) glyphs(el, JSON.parse(c), pe); }
  if(el.tagName==='INPUT'||el.tagName==='TEXTAREA'){ if(el.value) glyphs(el, el.value, 'value'); if(el.placeholder) glyphs(el, el.placeholder, 'placeholder'); } }
// interactive: focus + target
const I=[...document.querySelectorAll('button,a[href],input,select,textarea,[role=button],[role=tab],[role=switch],[role=radio],[role=checkbox],[role=option],[role=menuitem],[tabindex]:not([tabindex="-1"])')].filter(e=>!skipCtl(e)&&visible(e));
for(const el of I){ out.counts.interactive++; const r=el.getBoundingClientRect(); if(r.width<24-0.01||r.height<24-0.01) out.target.push({el:path(el),w:+r.width.toFixed(1),h:+r.height.toFixed(1)});
  // native disabled drops the control out of the tab order and hides its reason: use aria-disabled or readonly
  if(el.disabled){ out.disabled.push({el:path(el),problem:'native disabled attribute'}); continue; }
  const chain=[]; for(let e=el,k=0; e && k<4; e=e.parentElement,k++) chain.push(e);
  const before=chain.map(e=>getComputedStyle(e).boxShadow);
  el.focus({preventScroll:true});
  if(document.activeElement!==el){ out.focus.push({el:path(el),problem:'not focusable'}); continue; }
  // the ring may be drawn by the element or, for a field, by its box (:focus-within) up to 3 levels up
  let ringEl=null, e2=el; for(let k=0;k<4&&e2;k++){ const c2=getComputedStyle(e2); if(c2.outlineStyle!=='none'&&(parseFloat(c2.outlineWidth)||0)>=2&&parse(c2.outlineColor)){ ringEl=e2; break; } e2=e2.parentElement; }
  if(!ringEl){
    // a box-shadow that focus adds (an underline, a ring) counts when one of its parts is at least 2px and 3:1 on the surface
    let shadowOk=false;
    chain.forEach((e,k)=>{ const after=getComputedStyle(e).boxShadow; if(after===before[k]||after==='none') return;
      after.split(/,(?![^(]*\))/).forEach(part=>{ const m=part.match(/rgba?\([^)]*\)/); if(!m) return; const col=parse(m[0]); const nums=(part.replace(m[0],'').match(/-?[\d.]+px/g)||[]).map(v=>Math.abs(parseFloat(v)));
        if(nums.length && Math.max(...nums)>=2-0.01){ const surf=bgOf(e); if(ratio(over(col,surf),surf)>=3-0.005) shadowOk=true; } }); });
    if(!shadowOk){ out.focus.push({el:path(el),problem:'no outline >=2px on focus',outline:getComputedStyle(el).outline}); }
    el.blur(); continue; }
  const oc=parse(getComputedStyle(ringEl).outlineColor);
  const surface=bgOf(ringEl.parentElement||ringEl); const rr=ratio(over(oc,surface),surface); if(rr<3-0.005) out.focus.push({el:path(el),problem:'focus ring below 3:1',ratio:+rr.toFixed(2)}); el.blur(); }
// overflow
// element boxes, and the text itself: a nowrap line can spill past a box whose own rect still fits
for(const box of document.querySelectorAll('[data-box],.k-tile,.k-card,.c-tile,.c-anat')){ const b=box.getBoundingClientRect(); let hit=null;
  // measured after clipping by anything between the element and the box: a clipped part is not drawn, so cannot overflow
  const out_of=(el,q0)=>{ if(!q0.width) return false; const q=clipTo(el.nodeType===1?el:el.parentElement, q0, box); return !!q&&(q.right>b.right+0.5||q.bottom>b.bottom+0.5||q.left<b.left-0.5||q.top<b.top-0.5); };
  for(const d of box.querySelectorAll('*')){ if(skip(d)) continue; if(out_of(d.parentElement===box?box:d.parentElement, d.getBoundingClientRect())){ hit={box:path(box),el:path(d)}; break; } }
  if(!hit){ const tw=document.createTreeWalker(box,NodeFilter.SHOW_TEXT); while(tw.nextNode()){ const t=tw.currentNode; if(!t.nodeValue.trim()||skip(t.parentElement)||!visible(t.parentElement)) continue; const r=document.createRange(); r.selectNodeContents(t); if(out_of(t.parentElement, r.getBoundingClientRect())){ hit={box:path(box),el:path(t.parentElement),text:t.nodeValue.trim().slice(0,30)}; break; } } }
  if(hit) out.overflow.push(hit); }
// text a drawing clips: an SVG clips its own contents at its viewport, so a label that runs past the edge is cut, not hidden on
// purpose. The box check above counts a clipped part as not drawn, which is right for boxes and wrong for words.
for(const tx of document.querySelectorAll('svg text')){ const s=tx.ownerSVGElement; if(!s||skip(tx)||!visible(tx)||!tx.textContent.trim()) continue;
  if(getComputedStyle(s).overflow==='visible') continue; const r=tx.getBoundingClientRect(), b=s.getBoundingClientRect();
  if(r.right>b.right+0.5||r.left<b.left-0.5||r.bottom>b.bottom+0.5||r.top<b.top-0.5) out.overflow.push({box:path(s), el:path(tx), problem:'text clipped by its drawing', text:tx.textContent.trim().slice(0,24), by:+Math.max(r.right-b.right, b.left-r.left, r.bottom-b.bottom, b.top-r.top).toFixed(1)}); }
// boundary: an input's edge is what shows where it is (a fill 1.09:1 on its ground identifies nothing), so the edge is >=3:1
// against both its own fill and the surface around it. Locked (readonly) fields and aria-disabled toggles are exempt.
for(const el of document.querySelectorAll('.k-field-box,.k-toggle-track')){ if(skipCtl(el)||!visible(el)) continue;
  if(el.closest('[data-locked="true"],[aria-disabled="true"],[aria-checked="true"],[data-invalid="true"]')) continue;
  const m=getComputedStyle(el).boxShadow.match(/rgba?\([^)]*\)/); const edge=m?parse(m[0]):null;
  if(!edge){ out.boundary.push({el:path(el),problem:'no edge'}); continue; }
  const fill=bgOf(el), around=bgOf(el.parentElement.closest('*:not(.k-field):not(.k-toggle)')||el.parentElement);
  const r1=ratio(over(edge,fill),fill), r2=ratio(over(edge,around),around);
  if(Math.min(r1,r2)<3-0.005) out.boundary.push({el:path(el),problem:'input edge below 3:1',on_fill:+r1.toFixed(2),on_surface:+r2.toFixed(2)}); }
// the part of a rect that overflow-clipping ancestors (below `stop`) leave drawn; null when nothing is left
function clipTo(el, q, stop){ let r={left:q.left,top:q.top,right:q.right,bottom:q.bottom}; for(let e=el; e && e.nodeType===1 && e!==stop; e=e.parentElement){ const o=getComputedStyle(e);
  if(o.overflowX!=='visible'||o.overflowY!=='visible'){ const b=e.getBoundingClientRect(); r={left:Math.max(r.left,b.left),top:Math.max(r.top,b.top),right:Math.min(r.right,b.right),bottom:Math.min(r.bottom,b.bottom)}; } }
  return (r.right-r.left>0.5&&r.bottom-r.top>0.5) ? r : null; }
// is the lower of two overlapping texts hidden at (x,y) by an opaque element stacked between them (a panel over the map)?
function occluded(ea, eb, x, y){ const st=document.elementsFromPoint(x,y); const ia=st.indexOf(ea), ib=st.indexOf(eb); if(ia<0||ib<0) return false;
  const top=ia<ib?ea:eb, bot=ia<ib?eb:ea; for(let i=Math.min(ia,ib); i<Math.max(ia,ib); i++){ const e=st[i]; if(e.contains(bot)) continue;
    const c=parse(getComputedStyle(e).backgroundColor); if(c&&c.a>=0.99) return true; } return false; }
// collision: no two runs of text overlap (a label running into its neighbour). Per line box, via Range client rects.
// Occlusion is found by hit-testing, and inert content (the page under an open window) is not hit-testable: inert is
// lifted while this block measures and put back after, or a window over the page reads as text over text.
{ const inertEls=[...document.querySelectorAll('[inert]')]; inertEls.forEach(e=>{ e.inert=false; });
  const runs=[]; const tw=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  while(tw.nextNode()){ const t=tw.currentNode; const el=t.parentElement; if(!t.nodeValue.trim()||!el||skip(el)||!visible(el)) continue;
    // a run's box is its font's content area; ink rarely fills the top and bottom 15% (above caps, below a descender-free line)
    // what an overflow-clipping ancestor hides is not drawn
    const r=document.createRange(); r.selectNodeContents(t); for(const q0 of r.getClientRects()){ if(q0.width>0.5&&q0.height>0.5){ const d=q0.height*0.15;
      const q=clipTo(el, {left:q0.left,right:q0.right,top:q0.top+d,bottom:q0.bottom-d}, null); if(q) runs.push({q,el,t:t.nodeValue.trim().slice(0,24)}); } } }
  const seenC=new Set();
  for(let i=0;i<runs.length;i++) for(let j=i+1;j<runs.length;j++){ const a=runs[i], b=runs[j]; if(a.el===b.el) continue;
    const w=Math.min(a.q.right,b.q.right)-Math.max(a.q.left,b.q.left), h=Math.min(a.q.bottom,b.q.bottom)-Math.max(a.q.top,b.q.top);
    if(w>1&&h>2){ const x=(Math.max(a.q.left,b.q.left)+Math.min(a.q.right,b.q.right))/2, y=(Math.max(a.q.top,b.q.top)+Math.min(a.q.bottom,b.q.bottom))/2;
      if(occluded(a.el,b.el,x,y)) continue;
      const k=a.t+'|'+b.t; if(seenC.has(k)) continue; seenC.add(k); out.collision.push({a:a.t,b:b.t,el:path(a.el),overlap:[+w.toFixed(1),+h.toFixed(1)]}); } }
  inertEls.forEach(e=>{ e.inert=true; }); }
// broken token: a line that ends at a hyphen with the word going on below (search- / index) splits a name in two. Found by
// comparing where the hyphen and the next character sit; a token in prose carries v-token (nowrap) instead.
{ const tw=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); const rg=document.createRange();
  while(tw.nextNode()){ const t=tw.currentNode, s=t.nodeValue, el=t.parentElement; if(!el || skip(el) || !visible(el) || el.closest('[data-runtime],[data-audit-film],svg')) continue;
    // any hyphen with more of the token after it: search-|index, and a flag's own hyphens (--|to, -|-ack), which the first
    // version skipped because the character before was not a letter
    for(let i=s.indexOf('-'); i>=0 && i<s.length-1; i=s.indexOf('-', i+1)){ if(/\s/.test(s[i+1])) continue;
      rg.setStart(t,i); rg.setEnd(t,i+1); const a=rg.getBoundingClientRect(); rg.setStart(t,i+1); rg.setEnd(t,i+2); const b=rg.getBoundingClientRect();
      if(a.height && b.height && b.top > a.top + a.height/2){ out.truncated.push({el:path(el), problem:'token broken at its hyphen', text:s.slice(Math.max(0,s.lastIndexOf(' ', i)+1), s.indexOf(' ', i)>0?s.indexOf(' ', i):undefined)}); break; } } } }
// truncated: an ellipsis is a failure of writing, allowed only on text kstrl supplies at run time (marked data-runtime)
for(const el of document.body.querySelectorAll('*')){ if(skip(el)||!visible(el)) continue; const cs=getComputedStyle(el);
  if(cs.textOverflow==='ellipsis' && el.scrollWidth>el.clientWidth+1 && !el.closest('[data-runtime]')) out.truncated.push({el:path(el),text:el.textContent.trim().slice(0,40)}); }
// fit: a component card frames its tiles with equal top and bottom margins, nothing clipped
{ const w=document.querySelector('.c-wrap'); const ts=w?[...w.children].filter(e=>visible(e)):[...document.querySelectorAll('.c-tile')]; if(ts.length){ const top=Math.min(...ts.map(t=>t.getBoundingClientRect().top)), bot=Math.max(...ts.map(t=>t.getBoundingClientRect().bottom)), H=window.innerHeight;
  if(Math.abs((H-bot)-top)>4) out.fit.push({problem: bot>H ? 'tiles clipped by the card' : 'bottom margin differs from top', top:Math.round(top), bottom_margin:Math.round(H-bot), height_for_equal:Math.ceil(bot+top)}); } }
// balance: tiles in a row share its height, so the shorter one ends short. Under text or controls that reads as unfinished,
// so the gap is at most 40px (a footnote moves to the tile's foot instead); under an anatomy drawing, whitespace frames the
// diagram, up to 80px. Before this check Keycap and Ring each ended 110px short and Button's anatomy tile 68px.
for(const row of document.querySelectorAll('.c-two, .c-three')){ const tiles=[...row.children].filter(t=>t.matches('.c-tile, .k-tile') && visible(t)); if(tiles.length<2) continue;
  for(const tl of tiles){ const r=tl.getBoundingClientRect(), cs=getComputedStyle(tl); const inner=r.bottom-parseFloat(cs.paddingBottom)-parseFloat(cs.borderBottomWidth);
    let b=-1e9; const w=document.createTreeWalker(tl,NodeFilter.SHOW_ELEMENT); while(w.nextNode()){ const e=w.currentNode; const q=e.getBoundingClientRect(); if(!q.width||!q.height) continue; if(e.closest('svg') && e.tagName!=='svg') continue; if(getComputedStyle(e).position==='absolute' && !e.closest('.c-anat')) continue; b=Math.max(b,q.bottom); }
    const last=[...tl.children].filter(k=>k.getBoundingClientRect().height>0).pop(); const anat=!!(last && last.classList.contains('c-anat'));
    const dead=Math.round(inner-b), limit=anat?80:40;
    if(dead>limit) out.fit.push({problem:'a tile ends '+dead+'px short of its row, under '+(anat?'an anatomy drawing':'text'), tile:path(tl), limit}); } }
// scroll: at its declared size a card never scrolls inside its frame. The fit check allows 4px, and 1px of overflow was
// enough to draw a scrollbar and narrow the whole layout by its width (Chrome and Stat in version 36).
{ const d=document.documentElement; const over=Math.max(d.scrollHeight-d.clientHeight, d.scrollWidth-d.clientWidth);
  if(over>0) out.fit.push({problem:'the card scrolls inside its frame', by:over, height_for_no_scroll:d.scrollHeight});
  // This also catches a card that fits only at full width. The renderer lays out once before the window has its height, so a
  // page starts with a scrollbar and keeps it when the narrower layout overflows (a line rewraps): Meter at 362px. A gallery that
  // sizes its frame after the first layout can do the same. The bistable fixture holds the renderer to this.
}
// leak: the page's own rules (every stylesheet but the bundle and the audit's own) may set only layout on an element that carries a
// component class. Longhands are compared, so a shorthand (font, background) is caught by its parts. Style attributes are checked
// statically, in the source, because at this point the components' own scripts have written theirs (a thumb's transform).
{ const LAYOUT=/^(margin|grid|flex|order|align-self|justify-self|place-self|position|top|right|bottom|left|inset|z-index|width|min-width|max-width|height|min-height|max-height|overflow|container|vertical-align|row-gap|column-gap|--)/;
  // k-ink says the surface is ink (the focus ring follows); it is a context, not a component
  const isComp=el=>[...el.classList].some(c=>/^k-/.test(c) && c!=='k-ink');
  const own=[...document.styleSheets].filter(sh=>sh.ownerNode && !sh.ownerNode.hasAttribute('data-audit-bundle') && !sh.ownerNode.hasAttribute('data-audit-own'));
  function rulesOf(list, acc){ for(const r of list){ if(r.selectorText) acc.push(r); else if(r.cssRules) rulesOf(r.cssRules, acc); } return acc; }
  const rules=own.flatMap(sh=>{ try{ return rulesOf(sh.cssRules, []); }catch(e){ return []; } });
  for(const r of rules){ const props=[...r.style].filter(p=>!LAYOUT.test(p)); if(!props.length) continue;
    let els; try{ els=document.querySelectorAll(r.selectorText); }catch(e){ continue; }
    for(const el of els){ if(skip(el) || !isComp(el) || el.closest('[data-audit-film]')) continue; out.leak.push({el:path(el), problem:'page rule on a component', rule:r.selectorText, sets:props.slice(0,5)}); break; } }
}
// painted: a box the page draws itself (a fill, an image, a shadow or an edge its own CSS adds) is either a copy of a component,
// which drifts (eight pages drew a meter at four heights and three radii), or an illustration, which says so. Found by effect:
// the page's own stylesheets and style attributes are turned off and every element's paint, ::before and ::after included,
// is compared. Allowed without a word: the card's ground (the viewport's size, canvas and nothing else), the documentation base
// (c-tile, c-ink, c-stage, painted by their own rules only; the static check holds those rules to card.py's), and a rule line
// (one side, 1px solid line). Anything inside data-illustration="what it draws" (three words at least) is listed, not failed.
// A colour-only change to paint the bundle already draws recolours a component's part. That includes a colour inherited from a
// page-coloured ancestor: none exists today (the only bundle paint in currentColor is .k-spin::before, coloured by its button),
// so it fails rather than being exempted untested.
{ const own=[...document.styleSheets].filter(sh=>sh.ownerNode && !sh.ownerNode.hasAttribute('data-audit-bundle') && !sh.ownerNode.hasAttribute('data-audit-own'));
  function alphaOf(c){ if(!c||c==='none') return 0; let m=c.match(/^rgba?\(([^)]+)\)$/); if(m){ const p=m[1].split(/[ ,\/]+/).filter(Boolean); return p.length>3?parseFloat(p[3]):1; } m=c.match(/\/\s*([\d.]+)\s*\)$/); return m?parseFloat(m[1]):1; }
  const shadowOf=s=>s==='none'?'':s.split(/,(?![^(]*\))/).filter(p=>{ const m=p.match(/(rgba?|color)\([^)]*\)/); const lens=(p.replace(m?m[0]:'','').match(/-?[\d.]+px/g)||[]).map(parseFloat); return (!m||alphaOf(m[0])>0) && lens.some(v=>v!==0); }).join(',');
  function paintOf(el, pe){ const cs=getComputedStyle(el, pe||null); if(pe && (cs.content==='none'||cs.content==='normal')) return null;
    const edges=['Top','Right','Bottom','Left'].map(s=>{ const w=parseFloat(cs['border'+s+'Width'])||0, st=cs['border'+s+'Style'], c=cs['border'+s+'Color']; return (w>0&&st!=='none'&&st!=='hidden'&&alphaOf(c)>0)?w+'px '+st+' '+c:''; });
    return {fill:alphaOf(cs.backgroundColor)>0?cs.backgroundColor:'', image:cs.backgroundImage==='none'?'':cs.backgroundImage, shadow:shadowOf(cs.boxShadow), edges, radius:cs.borderRadius}; }
  const paints=p=>!!p && !!(p.fill||p.image||p.shadow||p.edges.some(Boolean));
  const shape=p=>JSON.stringify(p).replace(/(rgba?|color)\([^)]*\)/g,'C');
  const PAINT=/^(background|box-shadow|border-(top|right|bottom|left)-(width|style|color)|border-(width|style|color)|border-image|outline)/;
  function rulesOf(list, acc){ for(const r of list){ if(r.selectorText) acc.push(r); else if(r.cssRules) rulesOf(r.cssRules, acc); } return acc; }
  const rules=own.flatMap(sh=>{ try{ return rulesOf(sh.cssRules, []); }catch(e){ return []; } }).filter(r=>[...r.style].some(p=>PAINT.test(p)));
  // the page's own sources of paint on an element (or its pseudo-element): the selectors of matching rules that set paint, and its style attribute
  function sources(el, pe, style){ const out2=[]; for(const r of rules){ for(const sel of r.selectorText.split(/,(?![^(]*\))/).map(s=>s.trim())){ const m=sel.match(/::?(before|after)$/);
      if((pe && (!m || '::'+m[1]!==pe)) || (!pe && m)) continue; let hit=false; try{ hit=el.matches(pe?sel.slice(0, m.index):sel); }catch(e){ hit=false; } if(hit){ out2.push(sel); break; } } }
    if(!pe && style && style.split(';').some(d=>PAINT.test(d.split(':')[0].trim()))) out2.push('style attribute'); return out2; }
  const els=[...document.body.querySelectorAll('*')].filter(e=>!(e instanceof SVGElement) && !skip(e) && visible(e));
  const PES=[null,'::before','::after'];
  const on=els.map(e=>PES.map(pe=>paintOf(e,pe)));
  own.forEach(sh=>sh.disabled=true); const saved=els.map(e=>e.getAttribute('style')); els.forEach(e=>e.removeAttribute('style'));
  const off=els.map(e=>PES.map(pe=>paintOf(e,pe)));
  own.forEach(sh=>sh.disabled=false); els.forEach((e,i)=>{ if(saved[i]!==null) e.setAttribute('style', saved[i]); });
  const probe=document.createElement('i'); probe.style.color='var(--line)'; document.body.appendChild(probe); const LINE=getComputedStyle(probe).color; probe.style.color='var(--canvas)'; const CANVAS=getComputedStyle(probe).color; probe.remove();
  const DOC=new Set(['.c-tile','.c-ink','.c-stage']), seenP=new Set(), seenI=new Set();
  out.painted=[]; out.illustration=[];
  els.forEach((el,i)=>PES.forEach((pe,j)=>{ const a=on[i][j], b=off[i][j]; if(!paints(a) || JSON.stringify(a)===JSON.stringify(b)) return;
    if(!pe && [...el.classList].some(c=>/^k-/.test(c) && c!=='k-ink')) return;
    if(el.closest('[data-audit-film]')) return;
    const src=sources(el, pe, saved[i]);
    const recolour=paints(b) && shape(a)===shape(b);
    const r=el.getBoundingClientRect(); if(!pe && a.fill===CANVAS && !a.image && !a.shadow && !a.edges.some(Boolean) && Math.abs(r.left)<1 && Math.abs(r.top)<1 && Math.abs(r.width-document.documentElement.clientWidth)<1 && Math.abs(r.height-document.documentElement.clientHeight)<1) return;
    if(src.length && src.every(s=>DOC.has(s))) return;
    const lines=a.edges.filter(Boolean); if(!a.fill && !a.image && !a.shadow && lines.length===1 && lines[0]==='1px solid '+LINE) return;
    const il=el.closest('[data-illustration]');
    if(il){ const why=(il.getAttribute('data-illustration')||'').trim(); if(why.split(/\s+/).filter(Boolean).length>=3){ if(!seenI.has(il)){ seenI.add(il); out.illustration.push({region:path(il), draws:why}); } return; }
      const k='why|'+path(il); if(!seenP.has(k)){ seenP.add(k); out.painted.push({el:path(il), problem:'illustration without a reason (data-illustration names what it draws, in three words or more)'}); } return; }
    const what=[a.fill&&'fill', a.image&&'image', a.shadow&&'shadow', lines.length&&('edge '+lines.length)].filter(Boolean).join(' ');
    const problem=recolour?'recolours a component part':(pe?'pseudo-element painted by the page':'painted by the page');
    const k=problem+'|'+(src.join(',')||path(el).split(' > ').pop())+'|'+(pe||''); if(seenP.has(k)) return; seenP.add(k);
    out.painted.push({el:path(el)+(pe||''), problem, paints:what, from:src.slice(0,2).join(', ')||'(no page rule matched)'}); })); }
// type: every text run is a step of the type scale (scale.py): its family and size are a step's, its weight one the step allows,
// and, when it sets its own lines (not an inline run inside a paragraph, not SVG), its line height the step's, or its own size
// for a single-line control (a run on one line box; wrapped text at line height 1 is not a control). A census found 155 styles
// on 3,226 runs, 8.1% of them on a token. Illustrations draw other apps;
// the wordmark is the logo set in type, at the size its lockup needs (the Cover's 120px), so it is not a run of the product.
// A .v-measure sits one step below its context and keeps the context's leading, so it passes the line-height test only when
// that leading is inherited unchanged from a parent whose own family, size and line height are a step.
// A .v-measure is also smaller than the words around it: Geist Mono at the same size reads larger than Instrument Sans.
// Tracking is the step's too, and letter-spacing inherits as a length: a unit inside a tracked number resets it.
{ const SC=__SCALE__; out.type=[]; const seenT=new Set();
  const lead=c=>{ const f=c.fontFamily.split(',')[0].trim().replace(/^["']|["']$/g,''), s=parseFloat(c.fontSize), l=parseFloat(c.lineHeight); return SC.some(x=>x[0]===f && Math.abs(x[1]-s)<0.01 && Math.abs(x[2]-l)<0.01); };
  const inherits=(el,lh)=>{ const p=el.parentElement; if(!p||!el.classList.contains('v-measure')) return false; const pc=getComputedStyle(p); return pc.lineHeight===lh && lead(pc); };
  const tw=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  while(tw.nextNode()){ const t=tw.currentNode; const el=t.parentElement; if(!t.nodeValue.trim()||!el||el.tagName==='SCRIPT'||el.tagName==='STYLE'||el.id==='audit-out'||skip(el)||!visible(el)) continue;
    if(el.closest('[data-illustration],[data-wordmark]')) continue;
    const cs=getComputedStyle(el), fam=cs.fontFamily.split(',')[0].trim().replace(/^["']|["']$/g,''), size=parseFloat(cs.fontSize), w=parseInt(cs.fontWeight), lh=cs.lineHeight;
    const cands=SC.filter(x=>x[0]===fam && Math.abs(x[1]-size)<0.01);
    let problem=null;
    if(!cands.length) problem='off the scale: no step is '+fam+' at '+size+'px';
    else if(!cands.some(x=>x[3].indexOf(w)>=0)) problem=cands[0][4]+' at weight '+w+' (it allows '+cands[0][3].join(', ')+')';
    else if(!(el instanceof SVGElement) && cs.display!=='inline' && !inherits(el,lh)){ const l=parseFloat(lh);
      const rg=document.createRange(); rg.selectNodeContents(t); const oneLine=new Set([...rg.getClientRects()].filter(r=>r.width>0).map(r=>Math.round(r.top))).size<=1;
      if(!(lh!=='normal' && cands.some(x=>Math.abs(x[2]-l)<0.01 || (oneLine && Math.abs(size-l)<0.01)))) problem=cands[0][4]+' with line height '+lh+' (it sets '+cands[0][2]+'px)'; }
    if(!problem){ const step=cands.find(x=>x[3].indexOf(w)>=0 && Math.abs(x[2]-parseFloat(lh))<0.01) || cands.find(x=>x[3].indexOf(w)>=0);
      const ls=cs.letterSpacing==='normal'?0:parseFloat(cs.letterSpacing)/size;
      if(step && Math.abs(ls-step[5])>0.0015) problem=step[4]+' tracked '+(+ls.toFixed(3))+'em (it sets '+step[5]+'em)'; }
    if(!problem && el.classList.contains('v-measure') && el.parentElement){ const pc=getComputedStyle(el.parentElement), ps=parseFloat(pc.fontSize);
      if(!/Geist Mono/.test(pc.fontFamily) && size>=ps) problem='a measurement at '+size+'px in '+ps+'px text: it sits a step below the text it is in'; }
    if(!problem) continue;
    const k=problem+'|'+path(el).split(' > ').pop(); if(seenT.has(k)) continue; seenT.add(k);
    out.type.push({el:path(el), problem, text:t.nodeValue.trim().slice(0,30)}); } }
// baseline: text beside text in one row sits on one baseline. Two neighbouring children of a flex or grid row, each holding a run of
// at most two lines whose text is the first thing in it, on the same line, are compared by their first baselines (the line
// fragment's top plus the font's ascent). A run inside a box (a chip, a key, anything with a fill or two edges) is centred in its
// box on purpose, and so is a cluster of controls; a big number or a title beside a caption (sizes more than 1.5 apart) is
// composition, not a line, and is not checked. Rows centred on a taller box keep that choice: the check sees text beside text.
{ const cv=document.createElement('canvas').getContext('2d'); const seenB=new Set();
  const firstLine=el=>{ const w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT); while(w.nextNode()){ const t=w.currentNode; if(!t.nodeValue.trim()) continue; const p=t.parentElement;
      if(p.closest('[data-illustration],[aria-hidden="true"]')||skip(p)) continue; const rg=document.createRange(); rg.selectNodeContents(t); const rs=[...rg.getClientRects()].filter(r=>r.width>0); if(!rs.length) continue;
      const cs=getComputedStyle(p); cv.font=cs.fontWeight+' '+cs.fontSize+' '+cs.fontFamily; const m=cv.measureText('Hxg');
      return {base:rs[0].top+m.fontBoundingBoxAscent, top:rs[0].top, bottom:rs[0].bottom, size:parseFloat(cs.fontSize), el:p, text:t.nodeValue.trim().slice(0,24)}; } return null; };
  const boxed=e=>{ const cs=getComputedStyle(e); return /(^|\s)k-(chip|button|key|keys|seg|tab|toggle|mk|dot|ref|ring|bar|meter|field|kg)/.test(typeof e.className==='string'?e.className:'') || cs.backgroundColor!=='rgba(0, 0, 0, 0)' || cs.backgroundImage!=='none' || ['Top','Right','Bottom','Left'].filter(s=>parseFloat(cs['border'+s+'Width'])>0).length>=2 || e instanceof SVGElement; };
  const inBox=(k,te)=>{ if(boxed(k)) return true; for(let e=te; e && e!==k; e=e.parentElement){ if(boxed(e)) return true; } return false; };
  document.querySelectorAll('body *').forEach(c=>{ const cs=getComputedStyle(c); if(!/flex|grid/.test(cs.display) || c.closest('[data-illustration],[data-audit-film]')) return; if(cs.display.includes('flex') && cs.flexDirection.startsWith('column')) return;
    const kids=[...c.children]; const L=kids.map(k=>({k, f:firstLine(k)})).filter(x=>{ if(!x.f) return false; const r=x.k.getBoundingClientRect(); return r.width>0 && !inBox(x.k,x.f.el) && r.height<=2.5*(parseFloat(getComputedStyle(x.f.el).lineHeight)||20); });
    for(let i=0;i+1<L.length;i++){ const A=L[i], B=L[i+1]; if(kids.indexOf(B.k)-kids.indexOf(A.k)!==1) continue; const a=A.f, b=B.f;
      if(Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)<=0) continue;
      const ra=A.k.getBoundingClientRect(), rb=B.k.getBoundingClientRect(); if(Math.abs(ra.top-rb.top)>40) continue;
      if(Math.max(rb.left-ra.right, ra.left-rb.right)>48 && kids.length>2) continue;
      if(a.top-ra.top>8 || b.top-rb.top>8) continue;
      if(Math.max(a.size,b.size)/Math.min(a.size,b.size)>1.5) continue;
      if([A.k,B.k].some(k=>k.querySelector('.k-tabs, .k-seg, .k-button, .k-toggle, .k-field'))) continue;
      const d=b.base-a.base; if(Math.abs(d)<1) continue;
      const key=path(c)+'|'+a.text+'|'+b.text; if(seenB.has(key)) continue; seenB.add(key);
      out.baseline.push({row:path(c)+' ['+cs.display+', align-items '+cs.alignItems+']', a:a.text+' '+a.size+'px', b:b.text+' '+b.size+'px', off:+d.toFixed(1)}); } }); }
const pre=document.createElement('pre'); pre.id='audit-out'; pre.textContent=JSON.stringify(out); document.body.appendChild(pre);
}, 60); }); });
</script>"""

def tokens_css() -> str:
    tok = json.loads((ROOT / 'tokens.json').read_text())
    themes = [t['id'] for t in tok['color']['themes']]
    def fam(name):
        o = {t: [] for t in themes}
        for t in tok.get(name, {}).get('tokens', []):
            for th in themes:
                v = t['value'] if isinstance(t['value'], str) else t['value'].get(th, t['value'][themes[0]])
                if isinstance(v, str) and v.startswith('{') and v.endswith('}'): v = f'var(--{v[1:-1]})'
                o[th].append(f"--{t['name']}: {v};")
        return o
    c, s = fam('color'), fam('shadow')
    css = [f':root, [data-theme="{themes[0]}"] {{ ' + ' '.join(c[themes[0]] + s[themes[0]]) + ' }']
    for th in themes[1:]:
        css.append(f'[data-theme="{th}"] {{ ' + ' '.join(c[th] + s[th]) + ' }')
    flat = []
    for k, v in tok.items():
        if isinstance(v, dict) and 'tokens' in v and k not in ('color', 'shadow'):
            flat += [f"--{t['name']}: {t['value']};" for t in v['tokens']]
    flat += [f'--font-{k}: {v};' for k, v in tok['type']['families'].items()]
    css.append(':root { ' + ' '.join(flat) + ' }')
    for f in tok['type']['fonts']:
        css.append(f"@font-face {{ font-family:'{f['family']}'; src:url('file://{ROOT}/{f['file']}') format('woff2'); font-weight:{f['weight']}; font-style:{f.get('style','normal')}; }}")
    return '\n'.join(css)

_CM: dict | None = None
def cmaps() -> dict:
    # code points each shipped family can draw, from the font files themselves (union over a family's files)
    global _CM
    if _CM is None:
        from fontTools.ttLib import TTFont
        tok = json.loads((ROOT / 'tokens.json').read_text())
        out: dict = {}
        for f in tok['type']['fonts']:
            out.setdefault(f['family'], set()).update(TTFont(ROOT / f['file']).getBestCmap())
        _CM = {k: sorted(v) for k, v in out.items()}
    return _CM

def card_path(comp: str) -> Path:
    f = Path('audit_fixtures') / comp / 'preview.html'
    return f if f.exists() else ROOT / 'components' / comp / 'preview.html'

def render(comp: str, theme: str) -> dict:
    src = card_path(comp).read_text()
    first = src.splitlines()[0]
    h = int(re.search(r'height=(\d+)', first).group(1)); w = re.search(r'width=(\d+)', first); w = int(w.group(1)) if w else 960
    style = '<style data-audit-bundle>' + tokens_css() + '\n' + (ROOT / 'components/bundle.css').read_text() + '</style>'
    html = src.replace('<head>', '<head>' + style, 1) if '<head>' in src else style + src
    html = re.sub(r'<html', f'<html data-theme="{theme}"', html, count=1) if '<html' in html else f'<html data-theme="{theme}">' + html
    from scale import js_table
    js = '<script>window.__CMAP=' + json.dumps(cmaps()) + ';</script>' + JS.replace('__SCALE__', js_table())
    html = html.replace('</body>', js + '</body>') if '</body>' in html else html + js
    f = Path(f'out/render/_audit_{comp}_{theme}.html'); f.parent.mkdir(parents=True, exist_ok=True); f.write_text(html)
    out = subprocess.run([B, '--headless', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files', '--virtual-time-budget=4000', f'--window-size={w},{h}', '--dump-dom', f'file://{f.resolve()}'], capture_output=True, text=True, timeout=120).stdout
    f.unlink()
    m = re.search(r'<pre id="audit-out">(.*?)</pre>', out, re.S)
    if not m:
        return {'error': 'audit script produced no output'}
    return json.loads(m.group(1).replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))

def static_checks(comp: str) -> dict:
    src = card_path(comp).read_text()
    own = '\n'.join(re.findall(r'<style>(.*?)</style>', src, re.S))
    lib = (ROOT / 'components/bundle.css').read_text()
    layer = lib.split('/* @components */', 1)[1] if '/* @components */' in lib else ''
    css = layer + '\n' + own
    nocomments = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    raw = sorted(set(re.findall(r'#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)|\bhsla?\([^)]*\)', nocomments)))
    # motion: selectors whose block animates or transitions
    rm_blocks = re.findall(r'@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}', nocomments, re.S)
    rm_text = ' '.join(rm_blocks)
    animated = []
    # An animation whose keyframes change only opacity or colour moves nothing, which reduced motion keeps ("colour changes
    # stay"): it needs no guard. Anything else in its keyframes (transform, size, position) does.
    still_props = {'opacity', 'color', 'background-color', 'background', 'fill', 'stroke', 'border-color', 'outline-color'}
    still = {name for name, inner in re.findall(r'@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}', nocomments)
             if {pr.strip() for pr in re.findall(r'([\w-]+)\s*:', inner)} <= still_props}
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', re.sub(r'@media[^{]*\{', '', nocomments)):
        m = re.search(r'\banimation\s*:\s*([\w-]+)', body)
        if m and m.group(1) in still and not re.search(r'\btransition\s*:', body):
            continue
        if re.search(r'\b(animation|transition)\s*:', body) and not re.search(r'\b(animation|transition)\s*:\s*none', body):
            for s in sel.split(','):
                s = s.strip()
                if s and s not in rm_text:
                    animated.append(s)
    # hygiene: the bundle's un-namespaced bare classes (no k-, v-, t-, c- prefix) are global; a page rule that names one
    # redefines it for everything on the page that uses it. Namespaced component classes may be composed (layout only).
    base = lib.split('/* @components */', 1)[0]
    bare = {m.group(1) for sel in re.findall(r'([^{}]+)\{', re.sub(r'/\*.*?\*/', '', base, flags=re.S)) for part in sel.split(',')
            for m in [re.fullmatch(r'\s*\.([a-z][a-z0-9-]*)\s*', part)] if m}
    short = {c for c in bare if not re.match(r'(k|v|t|c)-', c) and c not in ('t2', 't3')}
    own_css = re.sub(r'/\*.*?\*/', '', own, flags=re.S)
    clash = sorted({c for sel in re.findall(r'([^{}]+)\{', own_css) for c in re.findall(r'\.([a-z][a-z0-9-]*)', sel) if c in short})
    # style attributes in the source, on an element carrying a component class (k-ink excepted), may set only layout
    layout = re.compile(r'^(margin|grid|flex|order|align-self|justify-self|place-self|position|top|right|bottom|left|inset|z-index|width|min-width|max-width|height|min-height|max-height|overflow|container|vertical-align|row-gap|column-gap|gap|--)')
    from html.parser import HTMLParser
    class Walk(HTMLParser):
        VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
        def __init__(self) -> None:
            super().__init__(); self.film: list[bool] = []; self.leak: list[str] = []
        def handle_starttag(self, tag, attrs):
            a = dict(attrs); in_film = (bool(self.film) and self.film[-1]) or 'data-audit-film' in a
            cls = (a.get('class') or '').split(); sty = a.get('style') or ''
            if sty and not in_film and any(c.startswith('k-') and c != 'k-ink' for c in cls):
                bad = [d.split(':')[0].strip() for d in sty.split(';') if ':' in d and not layout.match(d.split(':')[0].strip())]
                if bad:
                    self.leak.append(f'style attribute on a component: .{cls[0]} sets {", ".join(bad)}')
            if tag not in self.VOID and not tag.endswith('/'):
                self.film.append(in_film)
        def handle_endtag(self, tag):
            if tag not in self.VOID and self.film:
                self.film.pop()
    w = Walk(); w.feed(re.sub(r'<(style|script)>.*?</\1>', '', src, flags=re.S)); leak = w.leak
    # the documentation base is exempt from the painted check only as card.py writes it: a card that redefines c-tile, c-ink
    # or c-stage would otherwise paint anything under an exempt name
    from card import BASE_CSS
    def rules_of(css: str) -> list[tuple[str, str]]:
        css = re.sub(r'@media[^{]*\{', '', re.sub(r'/\*.*?\*/', '', css, flags=re.S))
        return [(' '.join(a.split()), ' '.join(b.split())) for a, b in re.findall(r'([^{}]+)\{([^{}]*)\}', css)]
    canon = dict(rules_of(BASE_CSS))
    docbase = [f'{sel} is not the rule card.py writes' for sel, body in rules_of(own) if sel in ('.c-tile', '.c-ink', '.c-stage') and body != canon.get(sel)]
    return {'rawcolor': raw, 'motion_unguarded': sorted(set(animated)), 'hygiene': clash, 'leak_static': leak, 'docbase': docbase}

import os
SHOW = 10 ** 6 if os.environ.get('AUDIT_SHOW_ALL') else 12  # failures printed per check; the self-test prints all, so no plant hides behind the cut

def audit(comp: str) -> bool:
    ok = True
    st = static_checks(comp)
    for k, v in st.items():
        if v:
            ok = False
            print(f'  FAIL {k}: {v}')
    for theme in ('day', 'night'):
        r = render(comp, theme)
        if 'error' in r:
            print(f'  FAIL {theme}: {r["error"]}'); ok = False; continue
        c = r['counts']
        bad = {k: r[k] for k in ('contrast', 'focus', 'target', 'overflow', 'disabled', 'glyph', 'fit', 'boundary', 'collision', 'truncated', 'leak', 'painted', 'type', 'baseline') if r[k]}
        print(f'  {theme}: {c["text"]} text runs, {c["interactive"]} interactive' + ('' if not bad else ''))
        if theme == 'day':
            for it in r.get('illustration', []):
                print(f'    illustration: {it["region"]}: {it["draws"]}')
        for k, v in bad.items():
            ok = False
            for item in v[:SHOW]:
                print(f'    FAIL {k}: {item}')
            if len(v) > SHOW:
                print(f'    ... {len(v) - SHOW} more {k}')
    print(f'{"PASS" if ok else "FAIL"} {comp}')
    return ok

def selftest() -> bool:
    import contextlib, io
    global SHOW
    SHOW = 10 ** 6
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        plant_ok = audit('_AuditPlant'); clean_ok = audit('_AuditClean'); bistable_ok = audit('_AuditBistable')
    text = buf.getvalue()
    expected = ['rawcolor', 'motion_unguarded', 'hygiene', 'contrast', 'no outline', 'below 3:1', 'target', 'overflow', 'native disabled', 'not focusable', 'missing glyph', 'not shipped', 'clipped by the card', 'input edge below 3:1', 'collision', 'truncated', 'page rule on a component', 'style attribute on a component', 'token broken at its hyphen', "'.p-kpaint .k-chip'", "'from': '.p-well'", 'pseudo-element painted by the page', 'recolours a component part', 'illustration without a reason', "'from': 'style attribute'", "'from': '.p-ground'", "'from': '.p-rule2'", "'from': '.p-rulecol'", "'from': '.p-inkextra, .c-ink'", 'is not the rule card.py writes', 'the card scrolls inside its frame', "'text': '--flag'",
                'off the scale: no step is Instrument Sans at 15px', 'small with line height 20px', 'label at weight 700', 'title tracked 0.05em', 'a measurement at 12px in 12px text', 'label with line height 20px', 'small with line height 13px', 'measure-inline with line height 19px', "'a': 'top name 14px'", 'text clipped by its drawing', 'short of its row, under text']
    missing = [e for e in expected if e not in text]
    good = (not plant_ok) and clean_ok and (not bistable_ok) and not missing
    print(f'selftest: plant {"caught" if not plant_ok else "MISSED"}, clean {"passed" if clean_ok else "FAILED"}, missing checks: {missing or "none"}')
    return good

if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        sys.exit(0 if selftest() else 1)
    results = [audit(c) for c in sys.argv[1:]]
    sys.exit(0 if all(results) else 1)
