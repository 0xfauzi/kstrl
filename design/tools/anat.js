/* Anatomy overlay: measures the real element and draws dimension lines in an SVG, so every number shown is what renders.
   anat(host, target, specs): specs are {kind:'h'|'w'|'gap'|'pad'|'note', ...}. Lines 1px text-3, end ticks 5px, labels measure-small (11px) at 400 in text-2. */
function anat(host, target, specs){
  var H = host.getBoundingClientRect(), T = target.getBoundingClientRect();
  var ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'c-anat-svg'); svg.setAttribute('width', H.width); svg.setAttribute('height', H.height); svg.setAttribute('aria-hidden', 'true');
  svg.style.position = 'absolute'; svg.style.left = '0'; svg.style.top = '0'; svg.style.overflow = 'visible';
  function el(n, a){ var e = document.createElementNS(ns, n); for (var k in a) e.setAttribute(k, a[k]); svg.appendChild(e); return e; }
  function line(x1, y1, x2, y2){ el('line', {x1:x1, y1:y1, x2:x2, y2:y2, 'class':'c-dl'}); }
  function text(x, y, s, anchor){ var t = el('text', {x:x, y:y, 'class':'c-dt', 'text-anchor':anchor || 'middle'}); t.textContent = s; }
  function rel(r){ return {l:r.left - H.left, t:r.top - H.top, r:r.right - H.left, b:r.bottom - H.top, w:r.width, h:r.height}; }
  var t = rel(T);
  specs.forEach(function(s){
    var r = s.of ? rel(s.of.getBoundingClientRect()) : t, v;
    if (s.kind === 'h') { var x = r.l - 16; line(x, r.t, x, r.b); line(x - 3, r.t, x + 3, r.t); line(x - 3, r.b, x + 3, r.b); text(x - 8, (r.t + r.b) / 2 + 3.5, s.label || Math.round(r.h) + '', 'end'); }
    if (s.kind === 'hr') { var xr = r.r + 14; line(xr, r.t, xr, r.b); line(xr - 3, r.t, xr + 3, r.t); line(xr - 3, r.b, xr + 3, r.b); text(xr + 8, (r.t + r.b) / 2 + 3.5, s.label || Math.round(r.h) + '', 'start'); }
    if (s.kind === 'w') { var y = r.b + 16; line(r.l, y, r.r, y); line(r.l, y - 3, r.l, y + 3); line(r.r, y - 3, r.r, y + 3); text((r.l + r.r) / 2, y + 14, s.label || Math.round(r.w) + ''); }
    if (s.kind === 'pad') { var y2 = r.t - 12, p = s.value; line(r.l, y2, r.l + p, y2); line(r.l, y2 - 3, r.l, y2 + 3); line(r.l + p, y2 - 3, r.l + p, y2 + 3); text(r.l + p / 2, y2 - 6, s.label || p + ''); }
    if (s.kind === 'gap') { var a = rel(s.a.getBoundingClientRect()), b = rel(s.b.getBoundingClientRect()), y3 = t.b + 14; line(a.r, y3, b.l, y3); line(a.r, y3 - 3, a.r, y3 + 3); line(b.l, y3 - 3, b.l, y3 + 3); text((a.r + b.l) / 2, y3 + 14, s.label || Math.round(b.l - a.r) + ''); }
    if (s.kind === 'note') { var nx = t.r + (s.dx || 28), ny = t.t + (s.row || 0) * 16 + 4; if (s.lead) { line(t.r - 2, t.t + 3, nx - 6, ny - 3.5); } text(nx, ny, s.label, 'start'); }
  });
  host.appendChild(svg);
}
