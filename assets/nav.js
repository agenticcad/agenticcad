// injects the shared header; `root` = relative path to the site root ("" or "../")
import { analytics } from './analytics.js';
import { release } from './release.js';
export function nav(root = '', active = '') {
  analytics(root);
  const L = (h, t, k) => `<a href="${root}${h}" class="${active === k ? 'on' : ''}">${t}</a>`;
  document.currentScript?.remove();
  const el = document.createElement('header'); el.className = 'nav';
  el.innerHTML = `<div class="wrap">
    <a class="brand" href="${root}index.html">${LOGO}<span>AgenticCAD</span><span class="ver" data-version>v0.9.1</span></a>
    <nav>${L('index.html#features', 'Features', 'features')}${L('docs/', 'Docs', 'docs')}${L('tutorials/', 'Tutorials', 'tutorials')}<a href="#" class="dl" data-dl title="Download the latest release (zip)">Download <span data-dl-ver>latest</span></a>
      <a class="gh" href="https://github.com/agenticcad/agenticcad">${GH} GitHub</a></nav></div>`;
  document.body.prepend(el);
  release();
}
export const LOGO = `<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M16 3 28 9.5v13L16 29 4 22.5v-13L16 3Z" stroke="#4ea1ff" stroke-width="1.8" stroke-linejoin="round"/><path d="M16 16 28 9.5M16 16v13M16 16 4 9.5" stroke="#4ea1ff" stroke-width="1.8" stroke-linejoin="round"/><circle cx="16" cy="16" r="3.2" fill="#ff8c42"/></svg>`;
export const GH = `<svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>`;
