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
  const asset = rel && rel.assets && rel.assets.find(a => /^agenticcad-.*\.zip$/.test(a.name));
  const ver = rel ? rel.tag_name.replace(/^v/, '') : null;
  const url = asset ? asset.browser_download_url : FALLBACK;
  const size = asset ? `${(asset.size / 1048576).toFixed(1)} MB` : '';
  for (const el of els) {
    el.href = url;
    el.addEventListener('click', () => track('file_download', { file_name: asset ? asset.name : 'releases-page', version: ver || 'unknown', link_url: url }));
  }
  document.querySelectorAll('[data-dl-ver]').forEach(x => x.textContent = ver ? `v${ver}` : 'latest');
  document.querySelectorAll('[data-dl-size]').forEach(x => x.textContent = size);
  document.querySelectorAll('[data-dl-date]').forEach(x => x.textContent = rel ? new Date(rel.published_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : '');
  document.querySelectorAll('[data-dl-count]').forEach(x => x.textContent = asset ? `${asset.download_count} downloads` : '');
  document.querySelectorAll('[data-version]').forEach(x => { if (ver) x.textContent = `v${ver}`; });
}
