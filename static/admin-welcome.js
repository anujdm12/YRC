(() => {
  const overlay = document.querySelector('[data-admin-welcome]');
  if (!overlay) return;
  const nonce = overlay.dataset.adminWelcome;
  const key = `yrc-admin-welcome-${nonce}`;
  const read = () => {
    try { return localStorage.getItem(key); }
    catch (_) { try { return sessionStorage.getItem(key); } catch (__) { return null; } }
  };
  const write = () => {
    try { localStorage.setItem(key, 'shown'); }
    catch (_) { try { sessionStorage.setItem(key, 'shown'); } catch (__) {} }
  };
  if (read() === 'shown') return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const previous = document.activeElement;
  let closed = false;
  overlay.hidden = false;
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => overlay.classList.add('is-visible'));
  const enter = overlay.querySelector('.admin-welcome-enter');
  const skip = overlay.querySelector('.admin-welcome-skip');
  function close() {
    if (closed) return;
    closed = true;
    write();
    document.body.style.overflow = '';
    overlay.classList.add('is-closing');
    window.setTimeout(() => { overlay.hidden = true; previous?.focus?.(); }, reduced ? 0 : 420);
  }
  enter.addEventListener('click', close);
  skip.addEventListener('click', close);
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  overlay.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const controls = [...overlay.querySelectorAll('button:not([disabled])')];
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus(); }
    }
  });
  skip.focus();
})();
