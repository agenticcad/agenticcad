// shared bits: version badge, reveal-on-scroll, nav highlight
(function () {
  const v = document.querySelector('[data-version]');
  if (v) fetch('https://raw.githubusercontent.com/agenticcad/agenticcad/main/version.py').then(r => r.text()).then(t => { const m = t.match(/__version__\s*=\s*"([^"]+)"/); if (m) v.textContent = 'v' + m[1]; }).catch(() => {});
  const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }), { threshold: .12 });
  document.querySelectorAll('.reveal').forEach(el => io.observe(el));
})();
