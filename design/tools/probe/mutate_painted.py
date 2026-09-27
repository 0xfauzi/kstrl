"""Mutation run for the painted, docbase, scroll, type and baseline checks: each mutation breaks one clearing rule or detection step; the self-test must go red.
Outcomes: caught (self-test exit 1), STILL GREEN (a hole), COULD-NOT-PLANT (anchor not found exactly once)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

M = [
 ('collision: inert content not lifted before hit-testing', "inertEls.forEach(e=>{ e.inert=false; });", ""),
 ('pseudo-elements not read', "const PES=[null,'::before','::after'];", "const PES=[null];"),
 ('style attributes left on', "els.forEach(e=>e.removeAttribute('style'));\n", ""),
 ('page sheets left on', "own.forEach(sh=>sh.disabled=true); const saved", "const saved"),
 ('every paint counts as page paint', "if(!paints(a) || JSON.stringify(a)===JSON.stringify(b)) return;", "if(!paints(a)) return;"),
 ('ground: any colour', "a.fill===CANVAS && ", ""),
 ('ground exemption removed', "if(!pe && a.fill===CANVAS", "if(false && a.fill===CANVAS"),
 ('rule line: any width or style', "lines[0]==='1px solid '+LINE", "lines[0].endsWith(LINE)"),
 ('rule line: any colour', "lines[0]==='1px solid '+LINE", "lines[0].startsWith('1px solid ')"),
 ('rule line exemption removed', "lines.length===1 && lines[0]==='1px solid '+LINE) return;", "lines.length===1 && false) return;"),
 ('illustration: one word is a reason', "filter(Boolean).length>=3", "filter(Boolean).length>=1"),
 ('illustration exemption removed', "const il=el.closest('[data-illustration]');", "const il=null;"),
 ('doc base exemption removed', "if(src.length && src.every(s=>DOC.has(s))) return;", ""),
 ('doc base: some instead of every', "src.every(s=>DOC.has(s))", "src.some(s=>DOC.has(s))"),
 ('docbase static check removed', "'leak_static': leak, 'docbase': docbase}", "'leak_static': leak, 'docbase': []}"),
 ('recolour exempted', "const recolour=paints(b) && shape(a)===shape(b);", "const recolour=paints(b) && shape(a)===shape(b); if(recolour) return;"),
 ('film skip removed', "if(el.closest('[data-audit-film]')) return;", ""),
 ('component skip removed', "if(!pe && [...el.classList].some(c=>/^k-/.test(c) && c!=='k-ink')) return;", ""),
 ('scroll: 999px allowed', "if(over>0) out.fit.push({problem:'the card scrolls inside its frame'", "if(over>999) out.fit.push({problem:'the card scrolls inside its frame'"),
 ('broken token: the old letter-before rule', "if(/\\s/.test(s[i+1])) continue;", "if(!/[A-Za-z0-9]/.test(s[i+1]) || i===0 || !/[A-Za-z0-9]/.test(s[i-1])) continue;"),
 ('painted not a failure', "'leak', 'painted', 'type', 'baseline') if r[k]}", "'leak', 'type', 'baseline') if r[k]}"),
 ('type not a failure', "'leak', 'painted', 'type', 'baseline') if r[k]}", "'leak', 'painted', 'baseline') if r[k]}"),
 ('type: off-scale sizes pass', "if(!cands.length) problem='off the scale", "if(false) problem='off the scale"),
 ('type: any weight', "else if(!cands.some(x=>x[3].indexOf(w)>=0)) problem=", "else if(false) problem="),
 ('type: line height test removed', "if(!(lh!=='normal' && cands.some(x=>Math.abs(x[2]-l)<0.01 || (oneLine && Math.abs(size-l)<0.01)))) problem=", "if(false) problem="),
 ('type: line height 1 on many lines', "(oneLine && Math.abs(size-l)<0.01)", "Math.abs(size-l)<0.01"),
 ('type: inherited leading for every run', "if(!p||!el.classList.contains('v-measure')) return false;", "if(!p) return false;"),
 ('type: inherited leading from any parent', "return pc.lineHeight===lh && lead(pc); };", "return pc.lineHeight===lh; };"),
 ('type: tracking test removed', "if(step && Math.abs(ls-step[5])>0.0015) problem=", "if(false) problem="),
 ('type: tracking within 0.1em', "Math.abs(ls-step[5])>0.0015", "Math.abs(ls-step[5])>0.1"),
 ('type: measurement rule removed', "if(!/Geist Mono/.test(pc.fontFamily) && size>=ps) problem=", "if(false) problem="),
 ('type: measurement may equal its text', "&& size>=ps) problem=", "&& size>ps) problem="),
 ('svg clip: never checked', "for(const tx of document.querySelectorAll('svg text')){", "for(const tx of []){"),
 ('svg clip: 20px allowed', "if(r.right>b.right+0.5||r.left<b.left-0.5||r.bottom>b.bottom+0.5||r.top<b.top-0.5) out.overflow.push({box:path(s), el:path(tx), problem:'text clipped by its drawing'", "if(r.right>b.right+20||r.left<b.left-20||r.bottom>b.bottom+20||r.top<b.top-20) out.overflow.push({box:path(s), el:path(tx), problem:'text clipped by its drawing'"),
 ('balance: never checked', "for(const row of document.querySelectorAll('.c-two, .c-three')){ const tiles=", "for(const row of []){ const tiles="),
 ('balance: 999px allowed', "const dead=Math.round(inner-b), limit=anat?80:40;", "const dead=Math.round(inner-b), limit=999;"),
 ('balance: text gets the anatomy allowance', "const dead=Math.round(inner-b), limit=anat?80:40;", "const dead=Math.round(inner-b), limit=80;"),
 ('balance: anatomy gets the text limit', "const dead=Math.round(inner-b), limit=anat?80:40;", "const dead=Math.round(inner-b), limit=40;"),
 ('baseline not a failure', "'type', 'baseline') if r[k]}", "'type') if r[k]}"),
 ('baseline: 3px allowed', "const d=b.base-a.base; if(Math.abs(d)<1) continue;", "const d=b.base-a.base; if(Math.abs(d)<4) continue;"),
 ('baseline: every run counts as boxed', "const inBox=(k,tgt)=>{ if(boxed(k)) return true;", "const inBox=(k,tgt)=>{ return true;"),
 ('baseline: boxed exemption removed', "const inBox=(k,tgt)=>{ if(boxed(k)) return true;", "const inBox=(k,tgt)=>{ return false;"),
 ('baseline: size ratio 1.1', "if(Math.max(a.size,b.size)/Math.min(a.size,b.size)>1.5) continue;", "if(Math.max(a.size,b.size)/Math.min(a.size,b.size)>1.1) continue;"),
 ('baseline: size ratio removed', "if(Math.max(a.size,b.size)/Math.min(a.size,b.size)>1.5) continue;", ""),
 ('baseline: two-line limit lifted', "r.height<=2.5*(parseFloat(getComputedStyle(x.f.el).lineHeight)||20)", "r.height<=99*(parseFloat(getComputedStyle(x.f.el).lineHeight)||20)"),
 ('baseline: gap bound lifted', "if(Math.max(rb.left-ra.right, ra.left-rb.right)>48 && kids.length>2) continue;", ""),
 ('baseline: first-thing test removed', "if(a.top-ra.top>8 || b.top-rb.top>8) continue;", ""),
 ('baseline: control cluster exemption removed', "if([A.k,B.k].some(k=>k.querySelector('.k-tabs, .k-seg, .k-button, .k-toggle, .k-field'))) continue;", ""),
 ('baseline: flex-start rows skipped', "if(cs.display.includes('flex') && cs.flexDirection.startsWith('column')) return;", "if(cs.display.includes('flex') && (cs.flexDirection.startsWith('column') || cs.alignItems==='flex-start')) return;"),
 ('type: wordmark exemption reaches every run', "if(el.closest('[data-illustration],[data-wordmark]')) continue;", "if(el.closest('[data-illustration],[data-wordmark],body')) continue;"),
]
src = Path('audit.py').read_text()
bad = 0
for name, old, new in M:
    if src.count(old) != 1:
        print(f'COULD-NOT-PLANT  {name} (anchor found {src.count(old)} times)'); bad += 1; continue
    Path('audit_m.py').write_text(src.replace(old, new))
    shutil.rmtree('__pycache__', ignore_errors=True)
    r = subprocess.run([sys.executable, '-B', 'audit_m.py', '--selftest'], capture_output=True, text=True, timeout=600)
    last = (r.stdout.strip().splitlines() or ['(no output)'])[-1]
    outcome = 'caught' if r.returncode != 0 else 'STILL GREEN'
    if outcome != 'caught': bad += 1
    print(f'{outcome:16} {name}: {last}')
Path('audit_m.py').unlink(missing_ok=True)
print('holes:', bad)
sys.exit(1 if bad else 0)
