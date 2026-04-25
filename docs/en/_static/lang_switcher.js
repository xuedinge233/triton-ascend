// Language switcher injected into the RTD theme sidebar.
// Builds the "other language" URL by swapping /zh/ <-> /en/ in the current path,
// preserving the page-relative position so users land on the same page.
(function () {
  function init() {
    var path = window.location.pathname;
    var isZh = /\/zh\//.test(path);
    var isEn = /\/en\//.test(path);
    if (!isZh && !isEn) return; // root or unknown layout — skip

    var otherPath = isZh
      ? path.replace('/zh/', '/en/')
      : path.replace('/en/', '/zh/');

    // Probe the target so we don't dump users on a 404 when the other locale
    // hasn't translated this page yet. Falls back to the locale's index.
    fetch(otherPath, { method: 'HEAD' }).then(function (r) {
      if (!r.ok) {
        otherPath = isZh
          ? path.replace(/\/zh\/.*/, '/en/index.html')
          : path.replace(/\/en\/.*/, '/zh/index.html');
      }
      mount(isZh, otherPath);
    }).catch(function () {
      mount(isZh, otherPath);
    });
  }

  function mount(isZh, otherPath) {
    var existing = document.getElementById('lang-switcher');
    if (existing) existing.remove();

    var wrap = document.createElement('div');
    wrap.id = 'lang-switcher';
    wrap.style.cssText = [
      'padding:.5rem 1rem',
      'border-bottom:1px solid rgba(255,255,255,.2)',
      'font-size:.9rem',
      'background:#2980b9',
      'color:#fff',
      'text-align:center',
    ].join(';');

    var linkStyle = 'color:#fff;text-decoration:underline;';
    var activeStyle = 'color:#fff;font-weight:600;';

    wrap.innerHTML = isZh
      ? '🌐 <span style="' + activeStyle + '">中文</span> | ' +
        '<a style="' + linkStyle + '" href="' + otherPath + '">English</a>'
      : '🌐 <a style="' + linkStyle + '" href="' + otherPath + '">中文</a> | ' +
        '<span style="' + activeStyle + '">English</span>';

    // RTD theme: insert at the top of the side navigation header so it's
    // visible above the project title.
    var sidebar = document.querySelector('.wy-side-nav-search');
    if (sidebar && sidebar.parentNode) {
      sidebar.parentNode.insertBefore(wrap, sidebar);
    } else {
      document.body.insertBefore(wrap, document.body.firstChild);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
