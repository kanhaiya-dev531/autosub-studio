/* Shared helpers: API calls with CSRF, the live-caption renderer, and the home-page demo. */
(function () {
  'use strict';

  // ---------------------------------------------------------------- API
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  /** Call the JSON API. `body` is an object (sent as JSON) or FormData. Throws Error(message) on failure. */
  async function api(method, url, body) {
    var options = { method: method, headers: { 'X-CSRF-Token': csrfToken() }, credentials: 'same-origin' };
    if (body instanceof FormData) {
      options.body = body;
    } else if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    var response;
    try {
      response = await fetch(url, options);
    } catch (e) {
      throw new Error('Could not reach the server. Check your connection and try again.');
    }
    var data = null;
    try { data = await response.json(); } catch (e) { /* not JSON */ }
    if (!response.ok) {
      throw new Error((data && data.error) || 'Something went wrong (error ' + response.status + ').');
    }
    return data;
  }

  // ------------------------------------------------------ live captions
  // These limits mirror autosub/styles.py so the in-page caption breaks lines like the real renderer.
  var CAPTION = {
    classic: { maxWords: Infinity, maxChars: Infinity, highlight: false },
    pop: { maxWords: 3, maxChars: 18, highlight: true },
    neon: { maxWords: 6, maxChars: 30, highlight: true }
  };

  /** Split words ({t, s, e}) into short groups, never merging across the limits. */
  function chunkWords(words, maxWords, maxChars) {
    var chunks = [], current = [], chars = 0;
    words.forEach(function (w) {
      var projected = chars + (current.length ? 1 : 0) + w.t.length;
      if (current.length && (current.length >= maxWords || projected > maxChars)) {
        chunks.push(current);
        current = [];
        chars = 0;
        projected = w.t.length;
      }
      current.push(w);
      chars = projected;
    });
    if (current.length) chunks.push(current);
    return chunks;
  }

  /** Show one chunk in `el`, lighting up word number `active` (or none when -1). */
  function paintCaption(el, template, chunk, active) {
    var cfg = CAPTION[template] || CAPTION.classic;
    el.className = el.className.replace(/\bcap--\w+/g, '').trim() + ' cap--' + template;
    var key = template + '|' + chunk.map(function (w) { return w.t; }).join(' ');
    if (el.dataset.key !== key) {
      el.textContent = '';
      chunk.forEach(function (w) {
        var span = document.createElement('span');
        span.className = 'w';
        span.textContent = w.t;
        el.appendChild(span);
      });
      el.dataset.key = key;
    }
    for (var i = 0; i < el.children.length; i++) {
      el.children[i].classList.toggle('on', cfg.highlight && i === active);
      el.children[i].classList.toggle('done', cfg.highlight && i < active);
    }
  }

  function clearCaption(el) {
    el.textContent = '';
    delete el.dataset.key;
  }

  function pad(n, size) { return String(n).padStart(size, '0'); }
  function formatTime(ms) {
    ms = Math.max(0, Math.round(ms));
    var h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, s = Math.floor(ms / 1000) % 60;
    return h + ':' + pad(m, 2) + ':' + pad(s, 2) + '.' + pad(ms % 1000, 3);
  }

  window.AutoSub = {
    api: api, csrfToken: csrfToken, CAPTION: CAPTION,
    chunkWords: chunkWords, paintCaption: paintCaption, clearCaption: clearCaption, formatTime: formatTime
  };

  // ------------------------------------------------------ home page demo
  function startDemo() {
    var stage = document.getElementById('demo-stage');
    if (!stage) return;
    var caption = document.getElementById('demo-caption');
    var clock = document.getElementById('demo-time');
    var buttons = Array.prototype.slice.call(document.querySelectorAll('.seg [data-template]'));
    var sentence = 'Your words appear on screen exactly when you say them'.split(' ');
    var WORD_MS = 430, HOLD_MS = 1100;
    var groupSize = { classic: 5, pop: 3, neon: 5 };
    var words = sentence.map(function (t, i) { return { t: t, s: i * WORD_MS, e: (i + 1) * WORD_MS }; });
    var total = words.length * WORD_MS;
    var template = 'pop';
    var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var started = performance.now();

    function show(elapsed) {
      var chunks = chunkWords(words, groupSize[template], Infinity);
      var index = Math.min(words.length - 1, Math.floor(elapsed / WORD_MS));
      var seen = 0;
      for (var c = 0; c < chunks.length; c++) {
        if (index < seen + chunks[c].length) {
          paintCaption(caption, template, chunks[c], index - seen);
          break;
        }
        seen += chunks[c].length;
      }
      clock.textContent = formatTime(elapsed);
    }

    function tick() {
      var elapsed = (performance.now() - started) % (total + HOLD_MS);
      show(Math.min(elapsed, total - 1));
    }

    buttons.forEach(function (button) {
      button.addEventListener('click', function () {
        template = button.dataset.template;
        buttons.forEach(function (b) { b.setAttribute('aria-checked', String(b === button)); });
        started = performance.now();
        show(reduced ? WORD_MS : 0);
      });
    });

    if (reduced) {
      show(WORD_MS);  // a still frame: second word lit
    } else {
      show(0);
      setInterval(tick, 80);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', startDemo);
  else startDemo();
})();
