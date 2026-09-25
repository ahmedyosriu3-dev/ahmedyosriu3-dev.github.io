/* Daybook page only: film chapters that drive the YouTube player, the screens reel, the privacy short. */
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var smooth = reduce ? 'auto' : 'smooth';

  function inView(el) {
    var r = el.getBoundingClientRect();
    return r.top >= 60 && r.bottom <= window.innerHeight;
  }

  /* ---- the film: language switch, chapter cards, segmented timeline ----
     Talks to the embedded player through YouTube's postMessage protocol (enablejsapi=1),
     so no extra script is loaded. If the player never answers, a click reloads it at that time. */
  var film = document.querySelector('[data-film]');
  var seekChapter = function () {};
  if (film) (function () {
    var frame = film.querySelector('iframe');
    var btns = film.querySelectorAll('.vidlang button[data-lang]');
    var lists = film.querySelectorAll('.chapters');
    var bar = film.querySelector('.chbar');
    var id = '', items = [], starts = [], end = 0, segs = [];
    var ready = false, active = -1, now = 0, tries = 0, knock = 0;

    function base() { return 'https://www.youtube-nocookie.com/embed/' + id + '?rel=0&enablejsapi=1'; }
    function post(msg) {
      try { frame.contentWindow.postMessage(JSON.stringify(msg), '*'); } catch (e) {}
    }
    function listen() {
      clearInterval(knock); tries = 0;
      knock = setInterval(function () {
        if (ready || ++tries > 40) { clearInterval(knock); return; }
        post({ event: 'listening', id: 1, channel: 'widget' });
      }, 400);
    }
    frame.addEventListener('load', function () { ready = false; listen(); });

    function setActive(k) {
      if (k === active) return;
      active = k;
      items.forEach(function (a, n) { a.classList.toggle('on', n === k); });
      segs.forEach(function (s, n) { s.classList.toggle('on', n === k); });
      // keep the playing card visible when the row scrolls sideways (narrow screens)
      var list = items[k] && items[k].closest('.chapters');
      if (list && list.scrollWidth > list.clientWidth + 4) {
        var dx = items[k].getBoundingClientRect().left - list.getBoundingClientRect().left;
        if (dx < 0 || dx > list.clientWidth - items[k].offsetWidth) list.scrollBy({ left: dx - 8, behavior: smooth });
      }
    }
    function paint(t) {
      now = t;
      var k = -1;
      for (var n = 0; n < starts.length; n++) if (t >= starts[n]) k = n;
      segs.forEach(function (s, n) {
        var a = starts[n], b = n + 1 < starts.length ? starts[n + 1] : end;
        var p = Math.max(0, Math.min(1, (t - a) / (b - a)));
        s.firstChild.style.width = (p * 100).toFixed(2) + '%';
      });
      setActive(k);
    }

    function build(list) {
      items = Array.prototype.slice.call(list.querySelectorAll('a[data-t]'));
      starts = items.map(function (a) { return +a.getAttribute('data-t'); });
      end = +list.getAttribute('data-end');
      bar.innerHTML = '';
      bar.setAttribute('dir', list.getAttribute('dir') || 'ltr');
      segs = items.map(function (a, n) {
        var s = document.createElement('span');
        var b = n + 1 < starts.length ? starts[n + 1] : end;
        s.style.flexGrow = String(b - starts[n]);
        s.title = a.querySelector('b').textContent;
        s.appendChild(document.createElement('i'));
        s.addEventListener('click', function () { seek(n); });
        bar.appendChild(s);
        return s;
      });
      active = -1;
      paint(0);
    }

    function seek(k) {
      var t = starts[k];
      if (t == null) return;
      if (ready) {
        post({ event: 'command', func: 'seekTo', args: [t, true] });
        post({ event: 'command', func: 'playVideo', args: [] });
      } else {
        frame.setAttribute('src', base() + '&start=' + t + '&autoplay=1');
      }
      paint(t);
      var box = film.querySelector('.tourvid');
      if (!inView(box)) box.scrollIntoView({ behavior: smooth, block: 'center' });
    }
    seekChapter = function (n) { seek(n - 1); };

    function pick(lang) {
      btns.forEach(function (b) {
        var on = b.getAttribute('data-lang') === lang;
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        if (on) {
          id = b.getAttribute('data-id');
          if (frame.getAttribute('src').indexOf('/embed/' + id + '?') === -1) {
            ready = false;
            frame.setAttribute('src', base());
            frame.setAttribute('title', b.getAttribute('data-title'));
          }
        }
      });
      lists.forEach(function (l) {
        var on = l.getAttribute('data-lang') === lang;
        l.hidden = !on;
        if (on) build(l);
      });
      film.classList.remove('playing');
      // the "In the film" buttons on the screens reel show this language's times
      document.querySelectorAll('.reel-go[data-chapter]').forEach(function (g) {
        var a = items[+g.getAttribute('data-chapter') - 1], at = g.querySelector('.at');
        if (a && at) at.textContent = a.querySelector('.tc').textContent;
      });
    }

    lists.forEach(function (l) {
      l.addEventListener('click', function (e) {
        var a = e.target.closest('a[data-t]');
        if (!a || e.ctrlKey || e.metaKey || e.shiftKey) return;
        e.preventDefault();
        seek(items.indexOf(a));
      });
    });
    btns.forEach(function (b) { b.addEventListener('click', function () { pick(b.getAttribute('data-lang')); }); });

    window.addEventListener('message', function (e) {
      if (e.source !== frame.contentWindow || typeof e.data !== 'string') return;
      var d; try { d = JSON.parse(e.data); } catch (x) { return; }
      if (!d || !d.event) return;
      ready = true;
      var info = d.info || {};
      if (typeof info.currentTime === 'number') paint(info.currentTime);
      var st = typeof info.playerState === 'number' ? info.playerState : (d.event === 'onStateChange' ? d.info : null);
      if (typeof st === 'number') film.classList.toggle('playing', st === 1);
    });

    var langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ''];
    pick(/^ar\b/i.test(langs[0] || '') ? 'ar' : 'en');
    listen();
  })();

  /* ---- the screens reel: a coverflow driven by the scroll position, gently auto-advancing ---- */
  var reel = document.querySelector('[data-reel]');
  if (reel) (function () {
    var figs = Array.prototype.slice.call(reel.querySelectorAll('figure'));
    var dotsBox = document.querySelector('.reel-dots');
    var cur = -1, tick = false, timer = 0, visible = false, rest = 0, DWELL = 5200;
    reel.style.setProperty('--dwell', DWELL + 'ms');
    dotsBox.style.setProperty('--dwell', DWELL + 'ms');
    var dots = figs.map(function (f, n) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('aria-label', 'Screen ' + (n + 1) + ' of ' + figs.length);
      b.addEventListener('click', function () { user(); go(n); });
      dotsBox.appendChild(b);
      return b;
    });
    function go(n, instant) {
      n = (n + figs.length) % figs.length;
      var f = figs[n];
      reel.scrollTo({ left: f.offsetLeft - (reel.clientWidth - f.offsetWidth) / 2, behavior: instant ? 'auto' : smooth });
    }
    function paint() {
      tick = false;
      var mid = reel.scrollLeft + reel.clientWidth / 2, best = 0, bd = Infinity;
      figs.forEach(function (f, n) {
        var w = f.offsetWidth, c = f.offsetLeft + w / 2;
        var d = (c - mid) / (w + 26);                 // signed distance from the centre, in cards
        var a = Math.min(Math.abs(d), 2.2);
        if (Math.abs(c - mid) < bd) { bd = Math.abs(c - mid); best = n; }
        f.style.transform = 'perspective(1400px) translateZ(' + (-a * 90).toFixed(1) + 'px) rotateY(' +
          (Math.max(-1.4, Math.min(1.4, d)) * -26).toFixed(2) + 'deg) scale(' + (1 - Math.min(a, 1.5) * .1).toFixed(3) + ')';
        f.style.filter = 'brightness(' + (1 - Math.min(a, 1.4) * .42).toFixed(3) + ') saturate(' + (1 - Math.min(a, 1) * .35).toFixed(3) + ')';
        f.style.zIndex = String(10 - Math.round(a * 3));
      });
      if (best === cur) return;
      cur = best;
      figs.forEach(function (f, n) { f.classList.toggle('on', n === cur); });
      dots.forEach(function (d, n) {
        d.classList.remove('on'); if (n === cur) { void d.offsetWidth; d.classList.add('on'); }
      });
      arm();
    }
    // auto-advance: only while on screen, and it waits a while after the visitor takes over
    function arm() {
      clearTimeout(timer);
      var on = visible && Date.now() > rest;
      dotsBox.classList.toggle('auto', on);
      if (on) timer = setTimeout(function () { go(cur + 1); }, DWELL);
    }
    function user() { rest = Date.now() + 14000; arm(); setTimeout(arm, 14100); }
    reel.addEventListener('scroll', function () { if (!tick) { tick = true; requestAnimationFrame(paint); } }, { passive: true });
    ['pointerdown', 'wheel', 'touchstart', 'keydown'].forEach(function (ev) { reel.addEventListener(ev, user, { passive: true }); });
    reel.addEventListener('mouseenter', function () { rest = Infinity; arm(); });
    reel.addEventListener('mouseleave', function () { rest = Date.now() + 1500; arm(); setTimeout(arm, 1600); });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) { visible = es[0].isIntersecting; arm(); }, { threshold: .5 }).observe(reel);
    }
    window.addEventListener('resize', function () { var k = cur; go(k, true); cur = -1; paint(); });
    figs.forEach(function (f, n) {
      f.addEventListener('click', function (e) { if (n !== cur && !e.target.closest('.reel-go')) { e.preventDefault(); go(n); } });
    });
    document.querySelectorAll('.reel-arrow').forEach(function (b) {
      b.addEventListener('click', function () { user(); go(cur + (+b.getAttribute('data-dir'))); });
    });
    reel.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight') { e.preventDefault(); go(cur + 1); }
      if (e.key === 'ArrowLeft') { e.preventDefault(); go(cur - 1); }
    });
    reel.querySelectorAll('.reel-go[data-chapter]').forEach(function (g) {
      g.addEventListener('click', function () { user(); seekChapter(+g.getAttribute('data-chapter')); });
    });
    go(0, true);
    paint();
  })();

  /* ---- hero: the recording loops; result cards pop out and the story rail fills in time with it ---- */
  var hv = document.querySelector('video[data-hero]');
  if (hv) (function () {
    var box = hv.closest('.livebox');
    var pops = Array.prototype.slice.call(box.querySelectorAll('.pop'));
    var steps = Array.prototype.slice.call(box.querySelectorAll('.story button'));
    var at = steps.map(function (b) { return +b.getAttribute('data-at'); });
    var held = false, raf = 0;
    hv.muted = true;
    function play() { var p = hv.play(); if (p && p.catch) p.catch(function () {}); }
    function frame() {
      var t = hv.currentTime, len = hv.duration || 37.4;
      pops.forEach(function (p) { p.classList.toggle('show', t >= +p.getAttribute('data-from') && t < +p.getAttribute('data-to')); });
      steps.forEach(function (b, n) {
        var a = at[n], z = n + 1 < at.length ? at[n + 1] : len;
        var on = t >= a && t < z;
        b.classList.toggle('on', on);
        b.classList.toggle('done', t >= z);
        b.firstChild.style.width = on ? ((t - a) / (z - a) * 100).toFixed(1) + '%' : '';
      });
      if (!hv.paused) raf = requestAnimationFrame(frame);
    }
    hv.addEventListener('play', function () { cancelAnimationFrame(raf); raf = requestAnimationFrame(frame); });
    hv.addEventListener('seeked', frame);
    steps.forEach(function (b, n) {
      b.addEventListener('click', function () { hv.currentTime = at[n] + .05; held = false; box.classList.remove('held'); play(); });
    });
    box.querySelector('.live-phone').addEventListener('click', function () {
      held = !hv.paused;
      held ? hv.pause() : play();
      box.classList.toggle('held', held);
    });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        if (es[0].isIntersecting) { if (!held) play(); } else hv.pause();
      }, { threshold: .2 }).observe(hv);
    } else play();
  })();

  /* ---- Windows screenshots: click to open large, arrows and Esc work ---- */
  var desk = document.querySelector('.desk');
  var lb = document.querySelector('dialog.lb');
  if (desk && lb && lb.showModal) (function () {
    var shots = Array.prototype.slice.call(desk.querySelectorAll('.screen img'));
    var names = Array.prototype.slice.call(document.querySelectorAll('.deskrail button')).map(function (b) { return b.getAttribute('aria-label'); });
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
  })();

  /* ---- the privacy short: plays muted while on screen (captions are burned in), sound on request.
     It autoplays even with reduced motion set, since it was asked for; a click pauses it. ---- */
  document.querySelectorAll('video[data-autoplay]').forEach(function (v) {
    var fig = v.closest('.short'), snd = fig && fig.querySelector('.short-sound');
    var held = false, seen = false;
    v.muted = true;
    function play() { var p = v.play(); if (p && p.catch) p.catch(function () { v.controls = true; }); }
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        es.forEach(function (en) {
          seen = en.isIntersecting;
          if (seen) { if (v.preload === 'none') v.preload = 'auto'; if (!held) play(); }
          else v.pause();
        });
      }, { threshold: .35 }).observe(v);
    } else {
      v.controls = true;
    }
    v.addEventListener('click', function () {
      if (v.controls) return;
      held = !v.paused;
      held ? v.pause() : play();
      fig.classList.toggle('held', held);
    });
    if (snd) snd.addEventListener('click', function () {
      v.muted = !v.muted;
      snd.setAttribute('aria-pressed', v.muted ? 'false' : 'true');
      snd.setAttribute('aria-label', v.muted ? 'Turn sound on' : 'Turn sound off');
      if (!v.muted) { held = false; fig.classList.remove('held'); if (v.currentTime > v.duration - 1) v.currentTime = 0; play(); }
    });
  });
})();
