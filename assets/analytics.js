// Google Analytics 4. Set GA_ID to your measurement id (G-XXXXXXXXXX); leave empty to disable entirely.
// Download clicks are reported as a `file_download` event with the release version, so they show up
// under Events in GA4 (and as a conversion if you mark it as one).
const GA_ID = '';
export function analytics() {
  if (!GA_ID) return;
  const s = document.createElement('script'); s.async = true; s.src = `https://www.googletagmanager.com/gtag/js?id=${GA_ID}`; document.head.appendChild(s);
  window.dataLayer = window.dataLayer || []; window.gtag = function () { dataLayer.push(arguments); };
  gtag('js', new Date()); gtag('config', GA_ID, { anonymize_ip: true });
}
export function track(name, params = {}) { if (window.gtag) window.gtag('event', name, params); }
