// Latest release: fills every [data-dl] link with the zip asset of the newest GitHub release and reports
// clicks to analytics. Falls back to the releases page if the API is unavailable (rate limit, offline).
import { track } from './analytics.js';
const REPO = 'agenticcad/agenticcad';
const FALLBACK = `https://github.com/${REPO}/releases/latest`;
export async function release() {
  const els = [...document.querySelectorAll('[data-dl]')];
  if (!els.length) return;
  let rel = null;
  try { const r = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, { headers: { Accept: 'application/vnd.github+json' } }); if (r.ok) rel = await r.json(); } catch {}
  const assets = (rel && rel.assets) || [];
  const find = re => assets.find(a => re.test(a.name));
  const kinds = {
    mac: { asset: find(/\.pkg$/i) || find(/\.dmg$/i), label: 'macOS (Apple Silicon)', note: 'macOS: the installer is not notarised yet, so if macOS blocks it, right-click → Open, or System Settings → Privacy & Security → Open Anyway.' },
    win: { asset: find(/\.msi$/i), label: 'Windows (x64)', note: 'Windows: the installer is unsigned, so SmartScreen shows "More info → Run anyway".' },
    src: { asset: find(/^agenticcad-.*\.zip$/), label: 'source zip', note: 'Source: Python 3.12, unzip, then the three install lines.' },
  };
  const ua = navigator.platform + ' ' + navigator.userAgent;
  const mine = /Mac/i.test(ua) ? 'mac' : /Win/i.test(ua) ? 'win' : 'src';
  const ver = rel ? rel.tag_name.replace(/^v/, '') : null;
  const sizeOf = a => a ? `${(a.size / 1048576).toFixed(0)} MB` : '';
  for (const el of els) {
    const want = el.dataset.dl || mine;                       // data-dl="mac|win|src", or auto-detect
    const missing = el.dataset.dl && want !== 'src' && !kinds[want].asset;   // explicit platform button, no installer yet
    const k = kinds[want] && kinds[want].asset ? kinds[want] : kinds.src;
    const url = missing ? `https://github.com/${REPO}/releases` : (k.asset ? k.asset.browser_download_url : FALLBACK);
    el.href = url;
    if (missing) { el.classList.remove('primary'); el.title = 'Installer not attached to this release yet'; el.querySelectorAll('[data-dl-asset-size]').forEach(x => x.textContent = 'soon'); continue; }
    el.querySelectorAll('[data-dl-label]').forEach(x => x.textContent = k.label);
    el.querySelectorAll('[data-dl-asset-size]').forEach(x => x.textContent = sizeOf(k.asset));
    el.addEventListener('click', () => track('file_download', { file_name: k.asset ? k.asset.name : 'releases-page', version: ver || 'unknown', link_url: url, platform: want }));
  }
  document.querySelectorAll('[data-dl-note]').forEach(x => { const k = kinds[x.dataset.dlNote || mine]; x.textContent = k && k.asset ? k.note : ''; });
  const asset = kinds.src.asset;
  const size = sizeOf(asset);
  document.querySelectorAll('[data-dl-ver]').forEach(x => x.textContent = ver ? `v${ver}` : 'latest');
  document.querySelectorAll('[data-dl-size]').forEach(x => x.textContent = size);
  document.querySelectorAll('[data-dl-date]').forEach(x => x.textContent = rel ? new Date(rel.published_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : '');
  document.querySelectorAll('[data-dl-count]').forEach(x => x.textContent = asset && asset.download_count >= 25 ? `${asset.download_count} downloads` : '');
  document.querySelectorAll('[data-version]').forEach(x => { if (ver) x.textContent = `v${ver}`; });
}
