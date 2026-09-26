/* MAIA Architech — shared behaviour for index.html, daybook.html, finnish-tutor.html */
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var fine = window.matchMedia && window.matchMedia('(pointer: fine)').matches;

  /* ---- theme ---- */
  var root = document.documentElement, btn = document.getElementById('themeBtn');
  try { var s = localStorage.getItem('maia-theme'); if (s === 'light' || s === 'dark') root.setAttribute('data-theme', s); } catch (e) {}
  function isDark() {
    var t = root.getAttribute('data-theme');
    if (t) return t === 'dark';
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function paint() { if (btn) btn.textContent = isDark() ? '☀' : '◐'; }
  paint();
  if (btn) {
    btn.addEventListener('click', function () {
      var next = isDark() ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('maia-theme', next); } catch (e) {}
      paint();
    });
  }

  /* ---- hero lattice ---- */
  var cv = document.getElementById('lattice');
  if (cv && cv.getContext) {
    var ctx = cv.getContext('2d'), pts = [], w = 0, h = 0, dpr = 1, raf = 0, live = true;
    var mouse = { x: -999, y: -999 };
    function size() {
      var r = cv.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = r.width; h = r.height;
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var n = Math.max(16, Math.min(44, Math.round(w * h / 17000)));
      pts = [];
      for (var i = 0; i < n; i++) {
        pts.push({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22 });
      }
    }
    function frame() {
      ctx.clearRect(0, 0, w, h);
      var D = Math.min(190, Math.max(120, w / 7));
      for (var i = 0; i < pts.length; i++) {
        var p = pts[i];
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0 || p.x > w) p.vx *= -1;
        if (p.y < 0 || p.y > h) p.vy *= -1;
      }
      for (i = 0; i < pts.length; i++) {
        for (var j = i + 1; j < pts.length; j++) {
          var a = pts[i], b = pts[j], dx = a.x - b.x, dy = a.y - b.y, d = Math.sqrt(dx * dx + dy * dy);
          if (d < D) {
            ctx.strokeStyle = 'rgba(123,206,236,' + (0.24 * (1 - d / D)).toFixed(3) + ')';
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
          }
        }
      }
      for (i = 0; i < pts.length; i++) {
        var q = pts[i];
        var md = Math.sqrt((q.x - mouse.x) * (q.x - mouse.x) + (q.y - mouse.y) * (q.y - mouse.y));
        if (md < 200) {
          ctx.strokeStyle = 'rgba(123,224,214,' + (0.4 * (1 - md / 200)).toFixed(3) + ')';
          ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(q.x, q.y); ctx.lineTo(mouse.x, mouse.y); ctx.stroke();
        }
        ctx.fillStyle = md < 200 ? 'rgba(160,240,232,.9)' : 'rgba(140,210,240,.55)';
        ctx.beginPath(); ctx.arc(q.x, q.y, md < 200 ? 2.4 : 1.8, 0, 6.283); ctx.fill();
      }
      if (live && !reduce) raf = requestAnimationFrame(frame);
    }
    size(); frame();
    if (reduce) live = false;
    var rt;
    window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(function () { size(); if (reduce) frame(); }, 180); });
    if (fine) {
      cv.parentNode.addEventListener('mousemove', function (e) {
        var r = cv.getBoundingClientRect();
        mouse.x = e.clientX - r.left; mouse.y = e.clientY - r.top;
      });
      cv.parentNode.addEventListener('mouseleave', function () { mouse.x = -999; mouse.y = -999; });
    }
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        es.forEach(function (en) {
          if (en.isIntersecting && !live && !reduce) { live = true; raf = requestAnimationFrame(frame); }
          else if (!en.isIntersecting && live) { live = false; cancelAnimationFrame(raf); }
        });
      }, { threshold: 0 }).observe(cv);
    }
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { live = false; cancelAnimationFrame(raf); }
      else if (!reduce) { live = true; raf = requestAnimationFrame(frame); }
    });
  }

  /* ---- reveal ---- */
  var rev = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); } });
    }, { threshold: .1, rootMargin: '0px 0px -50px 0px' });
    rev.forEach(function (el) { io.observe(el); });
    setTimeout(function () { rev.forEach(function (el) { el.classList.add('in'); }); }, 2500);
  } else { rev.forEach(function (el) { el.classList.add('in'); }); }

  /* ---- counters ---- */
  var nums = document.querySelectorAll('.stats .n');
  function run(el) {
    var to = parseInt(el.getAttribute('data-to'), 10) || 0, sfx = el.getAttribute('data-suffix') || '', t0 = null;
    if (reduce) { el.textContent = to + sfx; return; }
    function step(t) {
      if (!t0) t0 = t;
      var p = Math.min((t - t0) / 1100, 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(to * e) + sfx;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  if ('IntersectionObserver' in window && nums.length) {
    var cio = new IntersectionObserver(function (es) {
      es.forEach(function (en) { if (en.isIntersecting) { run(en.target); cio.unobserve(en.target); } });
    }, { threshold: .5 });
    nums.forEach(function (el) { cio.observe(el); });
  } else {
    nums.forEach(function (el) { el.textContent = (el.getAttribute('data-to') || '0') + (el.getAttribute('data-suffix') || ''); });
  }

  /* ---- data-flow widget(s): highlights the destination box, not a travelling dot ---- */
  document.querySelectorAll('[data-flow]').forEach(function (card) {
    var chips = card.querySelector('.chips');
    var note = card.querySelector('.flow-note');
    var nDev = card.querySelector('.node.n-device'), nOut = card.querySelector('.node.n-out');
    var track = card.querySelector('.track');
    if (!chips) return;
    function flash(el) { if (!el) return; el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
    chips.addEventListener('click', function (e) {
      var b = e.target.closest('.chip');
      if (!b) return;
      chips.querySelectorAll('.chip').forEach(function (c) { c.classList.toggle('on', c === b); });
      var out = b.getAttribute('data-dest') === 'out';
      nDev.classList.add('lit'); flash(nDev);
      nOut.classList.toggle('lit', out);
      if (out) flash(nOut);
      if (track) { track.classList.toggle('out', out); track.classList.toggle('stay', !out); }
      note.innerHTML = b.getAttribute('data-note');
    });
  });

  /* ---- screenshot galleries (autoplay + manual control) ---- */
  document.querySelectorAll('[data-gallery]').forEach(function (g) {
    var shots = g.querySelectorAll('.screen img');
    var navWrap = g.parentNode.querySelector('[data-nav-for]');
    var btns = navWrap ? navWrap.querySelectorAll('button') : [];
    var manual = g.hasAttribute('data-manual');
    var i = 0, timer = null, ms = parseInt(g.getAttribute('data-interval'), 10) || 5000;
    var count = g.querySelector('.desk-count');
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    g.style.setProperty('--dwell', ms + 'ms');
    // restart the countdown ring on the "next" arrow
    function tick() {
      g.classList.remove('ticking');
      if (timer) { void g.offsetWidth; g.classList.add('ticking'); }
    }
    function show(k) {
      i = (k + shots.length) % shots.length;
      shots.forEach(function (s, n) { s.classList.toggle('on', n === i); });
      btns.forEach(function (b, n) { b.classList.toggle('on', n === i); });
      if (count) count.textContent = pad(i + 1) + ' / ' + pad(shots.length);
      tick();
    }
    function play() { if (reduce || manual) return; stop(); timer = setInterval(function () { show(i + 1); }, ms); tick(); }
    function stop() { if (timer) { clearInterval(timer); timer = null; } g.classList.remove('ticking'); }
    btns.forEach(function (b, n) {
      b.addEventListener('click', function () { show(n); play(); });
    });
    // arrows sit over the picture, so the pointer is already on it and hover keeps autoplay paused
    g.querySelectorAll('.gal-arrow').forEach(function (b) {
      b.addEventListener('click', function () { show(i + (+b.getAttribute('data-dir'))); });
    });
    g.addEventListener('mouseenter', stop);
    g.addEventListener('mouseleave', play);
    g.addEventListener('wt-show', function (e) { stop(); show(e.detail); });
    if (manual) {
      show(0);
    } else if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        es.forEach(function (en) { en.isIntersecting ? play() : stop(); });
      }, { threshold: .3 }).observe(g);
    } else { play(); }
  });

  /* ---- Windows screenshots: click to open large, arrows and Esc work ---- */
  document.querySelectorAll('.deskwrap').forEach(function (wrap) {
    var desk = wrap.querySelector('.desk'), lb = wrap.querySelector('dialog.lb');
    if (!desk || !lb || !lb.showModal) return;
    var shots = Array.prototype.slice.call(desk.querySelectorAll('.screen img'));
    var names = Array.prototype.slice.call(wrap.querySelectorAll('.deskrail button')).map(function (b) { return b.getAttribute('aria-label'); });
    var img = lb.querySelector('img'), cap = lb.querySelector('.lb-cap'), k = 0;
    function show(n) {
      k = (n + shots.length) % shots.length;
      img.src = shots[k].getAttribute('data-full') || shots[k].currentSrc || shots[k].src;
      img.alt = shots[k].alt;
      cap.textContent = (k + 1) + ' / ' + shots.length + (names[k] ? '  ·  ' + names[k] : '');
    }
    desk.querySelector('.desk-open').addEventListener('click', function () {
      var cur = 0;
      shots.forEach(function (s, n) { if (s.classList.contains('on')) cur = n; });
      show(cur);
      lb.showModal();
    });
    lb.querySelectorAll('.lb-arrow').forEach(function (b) {
      b.addEventListener('click', function () { show(k + (+b.getAttribute('data-dir'))); });
    });
    lb.querySelector('.lb-x').addEventListener('click', function () { lb.close(); });
    lb.addEventListener('click', function (e) { if (e.target === lb) lb.close(); });
    lb.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight') show(k + 1);
      if (e.key === 'ArrowLeft') show(k - 1);
    });
    // back in the window, stay on the picture that was open
    lb.addEventListener('close', function () { desk.dispatchEvent(new CustomEvent('wt-show', { detail: k })); });
  });

  /* ---- "how to use" walkthrough: syncs a numbered rail to a phone mockup as you scroll ---- */
  var wtState = Array.prototype.slice.call(document.querySelectorAll('.wt-rail')).map(function (rail) {
    var wrap = rail.closest('.wt');
    var galleryId = rail.getAttribute('data-gallery-target');
    return {
      rail: rail,
      steps: Array.prototype.slice.call(rail.querySelectorAll('.wt-step')),
      fill: rail.querySelector('.wt-fill'),
      gallery: galleryId ? document.getElementById(galleryId) : null,
      caption: wrap ? wrap.querySelector('.wt-caption') : null,
      active: -1
    };
  });
  function activateWt(state, idx) {
    if (state.active === idx || !state.steps.length) return;
    state.active = idx;
    state.steps.forEach(function (s, i) { s.classList.toggle('lit', i <= idx); });
    if (state.fill) state.fill.style.height = (state.steps.length <= 1 ? 100 : (idx / (state.steps.length - 1)) * 100) + '%';
    if (state.gallery) state.gallery.dispatchEvent(new CustomEvent('wt-show', { detail: idx }));
    if (state.caption) state.caption.textContent = state.steps[idx].getAttribute('data-caption') || '';
  }
  function updateWt() {
    var trigger = window.innerHeight * 0.42;
    wtState.forEach(function (state) {
      var best = -1, bestDist = Infinity;
      state.steps.forEach(function (s, i) {
        var r = s.getBoundingClientRect();
        if (r.bottom < 0 || r.top > window.innerHeight) return;
        var dist = Math.abs(r.top - trigger);
        if (dist < bestDist) { bestDist = dist; best = i; }
      });
      if (best === -1) {
        var railRect = state.rail.getBoundingClientRect();
        if (railRect.bottom < 0) activateWt(state, state.steps.length - 1);
        return;
      }
      activateWt(state, best);
    });
  }
  wtState.forEach(function (state) {
    state.steps.forEach(function (s, i) {
      s.addEventListener('click', function () {
        activateWt(state, i);
        s.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
      });
    });
    activateWt(state, 0);
  });

  /* ---- contact form (present on the home page only) ---- */
  var form = document.getElementById('contactForm');
  if (form) {
    var statusEl = document.getElementById('cfStatus');
    var sendBtn = document.getElementById('cfSend');
    function address() { try { return atob('YWhtZWQueW9zcmkudTNAZ21haWwuY29t'); } catch (e) { return ''; } }
    function say(msg, kind) { statusEl.textContent = msg; statusEl.className = 'cf-status' + (kind ? ' ' + kind : ''); }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (form.botcheck && form.botcheck.checked) return;

      var name = form.name.value.trim();
      var email = form.email.value.trim();
      var message = form.message.value.trim();
      var firstBad = null;
      [['name', name], ['email', email], ['message', message]].forEach(function (pair) {
        var field = form[pair[0]];
        var bad = !pair[1] || (pair[0] === 'email' && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(pair[1]));
        field.setAttribute('aria-invalid', bad ? 'true' : 'false');
        if (bad && !firstBad) firstBad = field;
      });
      if (firstBad) { firstBad.focus(); say('Please fill in your name, a valid email, and a message.', 'err'); return; }

      var endpoint = form.getAttribute('data-endpoint');
      if (!endpoint) {
        var to = address();
        if (!to) { say('Sorry, the form is unavailable right now. Please try again later.', 'err'); return; }
        window.location.href = 'mailto:' + to
          + '?subject=' + encodeURIComponent('maiaarchitech.com — message from ' + name)
          + '&body=' + encodeURIComponent(message + '\n\n— ' + name + ' (' + email + ')');
        say('Opening your email app so you can send it. Nothing was sent from this page.');
        return;
      }

      sendBtn.disabled = true;
      say('Sending…');
      fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({
          access_key: form.getAttribute('data-key') || undefined,
          subject: 'maiaarchitech.com — message from ' + name,
          from_name: name, name: name, email: email, message: message
        })
      }).then(function (r) { return r.json().catch(function () { return { success: r.ok }; }); })
        .then(function (data) {
          if (data && (data.success || data.ok)) {
            form.reset();
            say('Thank you — your message has been sent. You will get a reply to ' + email + '.', 'ok');
          } else {
            say('That did not go through. Please try again in a moment.', 'err');
          }
        })
        .catch(function () { say('That did not go through — check your connection and try again.', 'err'); })
        .then(function () { sendBtn.disabled = false; });
    });
  }

  /* ---- nav active state, scroll progress, back to top ---- */
  var navLinks = document.querySelectorAll('[data-nav]');
  var curPage = document.body.getAttribute('data-page') || '';
  var secs = Array.prototype.slice.call(navLinks)
    .map(function (a) { var h = a.getAttribute('href'); return h.charAt(0) === '#' ? h.slice(1) : null; })
    .filter(Boolean)
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  var bar = document.getElementById('bar'), topBtn = document.getElementById('topBtn'), hdr = document.getElementById('hdr');
  var tick = false;
  function onScroll() {
    var y = window.scrollY || window.pageYOffset;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    if (bar) bar.style.width = (max > 0 ? (y / max) * 100 : 0) + '%';
    if (topBtn) topBtn.classList.toggle('show', y > 620);
    if (hdr) hdr.classList.toggle('stuck', y > 8);
    var cur = null, probe = y + 140;
    secs.forEach(function (s) { if (s.offsetTop <= probe && s.offsetTop + s.offsetHeight > probe) cur = s.id; });
    navLinks.forEach(function (a) {
      var href = a.getAttribute('href');
      var isHash = href.charAt(0) === '#';
      var on = isHash ? (href === '#' + cur) : (a.getAttribute('data-page') === curPage);
      a.classList.toggle('active', on);
    });
    updateWt();
    tick = false;
  }
  document.addEventListener('scroll', function () {
    if (!tick) { tick = true; requestAnimationFrame(onScroll); }
  }, { passive: true });
  onScroll();
})();

/* ---------- tour video language: Arabic for Arabic-language browsers, switchable ---------- */
(function () {
  var group = document.querySelector('[data-vidlang]');
  if (!group) return;
  var frame = group.parentNode.querySelector('.tourvid iframe');
  var btns = group.querySelectorAll('button[data-lang]');
  function pick(lang) {
    btns.forEach(function (b) {
      var on = b.getAttribute('data-lang') === lang;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on && frame.getAttribute('src') !== b.getAttribute('data-src')) {
        frame.setAttribute('src', b.getAttribute('data-src'));
        frame.setAttribute('title', b.getAttribute('data-title'));
      }
    });
  }
  var langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ''];
  var prefersArabic = /^ar\b/i.test(langs[0] || '');
  if (prefersArabic) pick('ar');
  btns.forEach(function (b) {
    b.addEventListener('click', function () { pick(b.getAttribute('data-lang')); });
  });
})();
