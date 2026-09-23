// Google Analytics 4 behind a consent banner.
// - Nothing from Google loads until the visitor clicks Accept (Consent Mode v2 defaults are "denied" and we
//   do not even load gtag.js before consent, so there are no cookieless pings either).
// - The choice is stored in localStorage ("agenticcad-consent" = granted | denied, with a timestamp) and can
//   be changed any time from the "Cookie settings" link in the footer. Consent is re-asked after 12 months.
// - Download clicks are reported as a `file_download` event (only when consent is granted).
// Set GA_ID to your measurement id (G-XXXXXXXXXX); leave it empty and no banner is shown at all.
export const GA_ID = "G-T5BR5HHP3M";
const KEY = 'agenticcad-consent', MAX_AGE = 365 * 864e5;

function stored() { try { const v = JSON.parse(localStorage.getItem(KEY) || 'null'); return v && Date.now() - v.at < MAX_AGE ? v.choice : null; } catch { return null; } }
function store(choice) { try { localStorage.setItem(KEY, JSON.stringify({ choice, at: Date.now() })); } catch {} }

let loaded = false;
function load() {
  if (loaded || !GA_ID) return; loaded = true;
  window.dataLayer = window.dataLayer || []; window.gtag = function () { dataLayer.push(arguments); };
  gtag('consent', 'default', { ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied', analytics_storage: 'granted' });
  gtag('js', new Date()); gtag('config', GA_ID);
  const s = document.createElement('script'); s.async = true; s.src = `https://www.googletagmanager.com/gtag/js?id=${GA_ID}`; document.head.appendChild(s);
}
export function track(name, params = {}) { if (window.gtag && stored() === 'granted') window.gtag('event', name, params); }

export function analytics(root = '') {
  if (!GA_ID) return;
  const choice = stored();
  if (choice === 'granted') load(); else if (choice === null) banner(root);
  document.addEventListener('click', e => { if (e.target.closest('[data-cookie-settings]')) { e.preventDefault(); banner(root, true); } });
}

function banner(root, reopen = false) {
  if (document.querySelector('.cookie')) return;
  const el = document.createElement('div'); el.className = 'cookie'; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', 'Cookie choices');
  el.innerHTML = `<div class="cookie-in">
    <div class="cookie-txt"><b>Cookies for analytics?</b> We'd like to use Google Analytics to count visits and downloads so we know what to improve. It sets cookies and sends usage data to Google. Nothing is set until you choose, and declining changes nothing about the site. <a href="${root}privacy.html">Privacy notice</a></div>
    <div class="cookie-btns"><button class="btn" data-c="denied">Decline</button><button class="btn primary" data-c="granted">Accept</button></div></div>`;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  el.addEventListener('click', e => {
    const b = e.target.closest('[data-c]'); if (!b) return;
    store(b.dataset.c); el.classList.remove('show'); setTimeout(() => el.remove(), 300);
    if (b.dataset.c === 'granted') load();
    else if (reopen && loaded) { // consent withdrawn after analytics ran: stop sending and drop Google's cookies
      window.gtag && window.gtag('consent', 'update', { analytics_storage: 'denied' });
      document.cookie.split(';').map(c => c.trim().split('=')[0]).filter(n => /^_ga/.test(n)).forEach(n => { for (const d of [location.hostname, '.' + location.hostname.split('.').slice(-2).join('.')]) document.cookie = `${n}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; domain=${d}`; });
    }
  });
}
