/* Project workspace: progress, subtitle editor, live captions, style previews, rendering, timing tools. */
(function () {
  'use strict';
  var root = document.getElementById('workspace');
  if (!root) return;

  var A = window.AutoSub;
  var jobId = root.dataset.jobId;
  var base = '/api/p/' + jobId;
  var $ = function (id) { return document.getElementById(id); };

  var el = {
    chip: $('status-chip'), panel: $('status-panel'), panelTitle: $('status-title'), panelBar: $('status-bar'),
    panelFill: $('status-fill'), panelText: $('status-text'), panelActions: $('status-actions'),
    body: $('ws-body'), player: $('player'), video: $('video'), live: $('live-caption'),
    note: $('video-note'), liveNote: $('live-note'),
    list: $('cue-list'), empty: $('cue-empty'), count: $('cue-count'), save: $('save-state'), add: $('add-line'),
    assLink: $('download-ass'),
    renderBtn: $('render-btn'), renderBar: $('render-bar'), renderFill: $('render-fill'),
    renderText: $('render-text'), renders: $('renders'),
    syncResult: $('sync-result')
  };

  var TEMPLATE_NAMES = {};
  document.querySelectorAll('input[name="template"]').forEach(function (radio) {
    TEMPLATE_NAMES[radio.value] = radio.closest('.tpl').querySelector('.tpl-name').textContent;
  });

  var state = {
    job: null,
    cues: [],
    template: root.dataset.defaultTemplate || 'pop',
    rev: 0,              // increases with every edit, so a slow save never overwrites newer typing
    savedRev: 0,         // the `rev` that the server has already stored
    saving: null,        // promise of the save in flight
    saveTimer: null,
    previewTimer: null,
    needsRerender: false,
    activeRow: -1
  };

  // ------------------------------------------------------------ helpers
  function h(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === 'text') node.textContent = attrs[key];
      else node.setAttribute(key, attrs[key]);
    });
    for (var i = 2; i < arguments.length; i++) {
      var child = arguments[i];
      if (child) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
  }

  /** "1:02:03.456", "02:03.4", "3.5" or "3,5" -> milliseconds, or null if it is not a time. */
  function parseTime(text) {
    var match = /^\s*(?:(?:(\d+):)?(\d{1,2}):)?(\d+)(?:[.,](\d{1,3}))?\s*$/.exec(text);
    if (!match) return null;
    var hours = match[1] ? parseInt(match[1], 10) : 0;
    var minutes = match[2] ? parseInt(match[2], 10) : 0;
    var seconds = parseInt(match[3], 10);
    var millis = match[4] ? parseInt((match[4] + '00').slice(0, 3), 10) : 0;
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis;
  }

  /** Spread the words of a line over its time range (used for lines that were edited and not saved yet). */
  function spread(text, start, end) {
    var tokens = text.split(/\s+/).filter(Boolean);
    var weights = tokens.map(function (t) { return t.length + 1; });
    var sum = weights.reduce(function (a, b) { return a + b; }, 0) || 1;
    var cursor = 0;
    return tokens.map(function (t, i) {
      var s = start + Math.round(cursor / sum * (end - start));
      cursor += weights[i];
      return { t: t, s: s, e: start + Math.round(cursor / sum * (end - start)) };
    });
  }

  function wordsOf(cue) {
    if (cue.words && cue.words.length && cue.words.map(function (w) { return w[0]; }).join(' ') === cue.text.trim().split(/\s+/).join(' ')) {
      return cue.words.map(function (w) { return { t: w[0], s: w[1], e: w[2] }; });
    }
    return spread(cue.text, cue.start, cue.end);
  }

  function setSave(text, isError) {
    el.save.textContent = text;
    el.save.classList.toggle('is-error', !!isError);
  }

  function setBar(bar, fill, pct) {
    pct = Math.max(0, Math.min(100, Math.round(pct)));
    fill.style.width = pct + '%';
    bar.setAttribute('aria-valuenow', String(pct));
  }

  function setChip(job) {
    el.chip.className = 'chip chip--' + job.status;
    if (job.status === 'ready') el.chip.textContent = 'Ready';
    else if (job.status === 'failed') el.chip.textContent = 'Failed';
    else el.chip.textContent = 'Transcribing ' + job.progress + '%';
  }

  // ------------------------------------------------------------ progress / failure
  function showStatus(title, text, pct, failed) {
    el.body.hidden = true;
    el.panel.hidden = false;
    el.panelTitle.textContent = title;
    el.panelText.textContent = text || '';
    el.panelBar.hidden = !!failed;
    setBar(el.panelBar, el.panelFill, pct);
    el.panelActions.hidden = !failed;
  }

  async function poll() {
    try {
      state.job = await A.api('GET', base);
    } catch (error) {
      showStatus('We could not load this project', error.message, 0, true);
      return;
    }
    setChip(state.job);
    if (state.job.status === 'ready') return startReady();
    if (state.job.status === 'failed') {
      return showStatus('We could not make subtitles', state.job.message || 'Something went wrong.', 100, true);
    }
    showStatus('Listening to your video', state.job.message || 'This can take a few minutes for long videos.', state.job.progress, false);
    setTimeout(poll, 1500);
  }

  // ------------------------------------------------------------ ready state
  async function startReady() {
    var data;
    try {
      data = await A.api('GET', base + '/cues');
    } catch (error) {
      return showStatus('We could not load the subtitles', error.message, 0, true);
    }
    state.cues = data.cues;
    el.panel.hidden = true;
    el.body.hidden = false;

    if (state.job.has_video) {
      el.video.src = root.dataset.videoUrl;
    } else {
      el.player.hidden = true;
      el.liveNote.hidden = true;
      el.note.hidden = false;
      el.renderBtn.disabled = true;
    }
    if (state.job.message) el.syncResult.textContent = state.job.message;
    renderCues();
    selectTemplate(state.template);
    loadPreviews();
    loadRenders();
  }

  // ------------------------------------------------------------ cue editor
  function timeInput(cue, field, index) {
    var input = h('input', { type: 'text', class: 'time', 'data-field': field, inputmode: 'decimal', autocomplete: 'off',
      'aria-label': (field === 'start' ? 'Start' : 'End') + ' time of line ' + (index + 1) });
    input.value = A.formatTime(cue[field]);
    return input;
  }

  function buildRow(cue, i) {
    var textarea = h('textarea', { rows: '2', maxlength: '500', 'aria-label': 'Text of line ' + (i + 1) });
    textarea.value = cue.text;
    return h('div', { class: 'cue', role: 'listitem', 'data-i': String(i) },
      h('button', { class: 'cue-idx', type: 'button', 'data-act': 'seek', 'aria-label': 'Play line ' + (i + 1), text: String(i + 1) }),
      h('div', { class: 'cue-times' }, timeInput(cue, 'start', i), h('span', { 'aria-hidden': 'true', text: '→' }), timeInput(cue, 'end', i)),
      textarea,
      h('div', { class: 'cue-actions' },
        h('button', { type: 'button', 'data-act': 'split', text: 'Split at cursor' }),
        h('button', { type: 'button', 'data-act': 'merge', text: 'Merge with next' }),
        h('button', { type: 'button', 'data-act': 'add', text: 'Add below' }),
        h('button', { type: 'button', 'data-act': 'delete', text: 'Delete' })));
  }

  function renderCues() {
    state.needsRerender = false;
    state.activeRow = -1;
    var scroll = el.list.scrollTop;
    el.list.textContent = '';
    var fragment = document.createDocumentFragment();
    state.cues.forEach(function (cue, i) { fragment.appendChild(buildRow(cue, i)); });
    el.list.appendChild(fragment);
    el.list.scrollTop = scroll;
    el.empty.hidden = state.cues.length > 0;
    el.count.textContent = state.cues.length ? '(' + state.cues.length + ')' : '';
  }

  function rowIndex(node) {
    var row = node.closest('.cue');
    return row ? parseInt(row.dataset.i, 10) : -1;
  }

  el.list.addEventListener('input', function (event) {
    if (event.target.tagName !== 'TEXTAREA') {
      event.target.classList.remove('is-bad');
      return;
    }
    var cue = state.cues[rowIndex(event.target)];
    if (!cue) return;
    cue.text = event.target.value;
    markDirty();
  });

  el.list.addEventListener('change', function (event) {
    var input = event.target;
    if (!input.classList.contains('time')) return;
    var index = rowIndex(input), cue = state.cues[index];
    var ms = parseTime(input.value);
    var field = input.dataset.field;
    var other = field === 'start' ? cue.end : cue.start;
    var invalid = ms === null || (field === 'start' ? ms >= other : ms <= other);
    if (invalid) {
      input.classList.add('is-bad');
      setSave(ms === null ? 'Use a time like 0:01:02.500' : 'The end must come after the start', true);
      return;
    }
    cue[field] = ms;
    input.value = A.formatTime(ms);
    input.classList.remove('is-bad');
    markDirty();
  });

  el.list.addEventListener('click', function (event) {
    var button = event.target.closest('button[data-act]');
    if (!button) return;
    var i = rowIndex(button), cue = state.cues[i];
    if (!cue) return;
    var act = button.dataset.act;
    if (act === 'seek') seekTo(cue.start);
    else if (act === 'split') splitCue(i, button.closest('.cue').querySelector('textarea'));
    else if (act === 'merge') mergeCue(i);
    else if (act === 'add') addAfter(i);
    else if (act === 'delete') { state.cues.splice(i, 1); renderCues(); markDirty(); }
  });

  el.list.addEventListener('focusout', function () {
    // apply a re-render that had to wait until typing stopped
    setTimeout(function () {
      if (state.needsRerender && !el.list.contains(document.activeElement)) renderCues();
    }, 0);
  });

  el.add.addEventListener('click', function () {
    var last = state.cues[state.cues.length - 1];
    var start = last ? last.end : 0;
    state.cues.push({ orig: null, start: start, end: start + 2000, text: '', words: [] });
    renderCues();
    var rows = el.list.querySelectorAll('.cue');
    var row = rows[rows.length - 1];
    row.scrollIntoView({ block: 'nearest' });
    row.querySelector('textarea').focus();
  });

  function splitCue(i, textarea) {
    var cue = state.cues[i], text = cue.text;
    var pos = textarea.selectionStart;
    if (!(pos > 0 && pos < text.length)) {
      var middle = Math.floor(text.length / 2), best = -1;
      for (var k = 0; k < text.length; k++) {
        if (text[k] === ' ' && (best < 0 || Math.abs(k - middle) < Math.abs(best - middle))) best = k;
      }
      pos = best;
    }
    var left = pos > 0 ? text.slice(0, pos).trim() : '', right = pos > 0 ? text.slice(pos).trim() : '';
    if (!left || !right) {
      setSave('Put the cursor inside the text where the line should split', true);
      return;
    }
    var at = cue.start + Math.round((cue.end - cue.start) * pos / text.length);
    state.cues.splice(i, 1,
      { orig: cue.orig, start: cue.start, end: at, text: left, words: [] },
      { orig: null, start: at, end: cue.end, text: right, words: [] });
    renderCues();
    markDirty();
  }

  function mergeCue(i) {
    var cue = state.cues[i], next = state.cues[i + 1];
    if (!next) {
      setSave('There is no line after this one to merge with', true);
      return;
    }
    cue.text = (cue.text + ' ' + next.text).trim();
    cue.end = Math.max(cue.end, next.end);
    cue.words = [];
    state.cues.splice(i + 1, 1);
    renderCues();
    markDirty();
  }

  function addAfter(i) {
    var cue = state.cues[i], next = state.cues[i + 1];
    var start = cue.end, end = start + 2000;
    if (next && next.start < end) end = Math.max(start + 300, next.start);
    state.cues.splice(i + 1, 0, { orig: null, start: start, end: end, text: '', words: [] });
    renderCues();
    var row = el.list.querySelectorAll('.cue')[i + 1];
    row.scrollIntoView({ block: 'nearest' });
    row.querySelector('textarea').focus();
  }

  // ------------------------------------------------------------ saving
  function markDirty() {
    state.rev++;
    setSave('Unsaved changes');
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(save, 900);
  }

  function save() {
    clearTimeout(state.saveTimer);
    if (state.saving) {
      // run again once the current request ends, so nothing typed in the meantime is lost
      return state.saving.then(function () { return state.rev !== state.savedRev ? save() : undefined; });
    }
    if (state.rev === state.savedRev) return Promise.resolve();  // nothing to save
    var rev = state.rev;
    var items = state.cues.filter(function (c) { return c.text.trim(); })
      .map(function (c) { return { orig: c.orig, start: c.start, end: c.end, text: c.text }; });
    setSave('Saving…');
    state.saving = A.api('PUT', base + '/cues', { cues: items }).then(function (result) {
      state.savedRev = rev;
      state.job = result.job;
      if (state.rev === rev) {
        state.cues = result.cues;
        if (el.list.contains(document.activeElement)) state.needsRerender = true; else renderCues();
        setSave('All changes saved');
        schedulePreviews();
      }
    }, function (error) {
      setSave(error.message, true);
    }).then(function () { state.saving = null; });
    return state.saving;
  }

  // ------------------------------------------------------------ video + live caption
  function seekTo(ms) {
    if (!state.job || !state.job.has_video) return;
    el.video.currentTime = ms / 1000;
    el.video.play().catch(function () { /* autoplay can be blocked; seeking still worked */ });
  }

  function cueIndexAt(ms) {
    for (var i = 0; i < state.cues.length; i++) {
      if (ms >= state.cues[i].start && ms < state.cues[i].end) return i;
    }
    return -1;
  }

  var chunkCache = new WeakMap();
  function chunksFor(cue) {
    var key = state.template + '|' + cue.text + '|' + cue.start + '|' + cue.end;
    var hit = chunkCache.get(cue);
    if (hit && hit.key === key) return hit.chunks;
    var cfg = A.CAPTION[state.template];
    var chunks = A.chunkWords(wordsOf(cue), cfg.maxWords, cfg.maxChars);
    chunkCache.set(cue, { key: key, chunks: chunks });
    return chunks;
  }

  function paintLive(index, ms) {
    if (index < 0 || !state.cues[index].text.trim()) return A.clearCaption(el.live);
    var chunks = chunksFor(state.cues[index]);
    var chunk = chunks[0];
    for (var c = 0; c < chunks.length; c++) if (chunks[c][0].s <= ms) chunk = chunks[c];
    var active = 0;
    for (var w = 0; w < chunk.length; w++) if (chunk[w].s <= ms) active = w;
    A.paintCaption(el.live, state.template, chunk, active);
  }

  function highlightRow(index) {
    if (index === state.activeRow) return;
    var rows = el.list.children;
    if (rows[state.activeRow]) rows[state.activeRow].classList.remove('is-active');
    state.activeRow = index;
    if (rows[index]) {
      rows[index].classList.add('is-active');
      if (!el.list.matches(':hover') && !el.list.contains(document.activeElement)) rows[index].scrollIntoView({ block: 'nearest' });
    }
  }

  function tick() {
    var ms = el.video.currentTime * 1000;
    var index = cueIndexAt(ms);
    highlightRow(index);
    paintLive(index, ms);
  }

  var raf = null;
  function loop() {
    tick();
    raf = el.video.paused || el.video.ended ? null : requestAnimationFrame(loop);
  }
  el.video.addEventListener('play', function () { if (!raf) raf = requestAnimationFrame(loop); });
  ['pause', 'ended', 'seeked', 'loadeddata'].forEach(function (name) { el.video.addEventListener(name, tick); });

  // ------------------------------------------------------------ templates + previews
  function selectTemplate(name) {
    state.template = name;
    el.renderBtn.textContent = 'Render video with ' + TEMPLATE_NAMES[name];
    el.assLink.href = el.assLink.dataset.base + '?template=' + encodeURIComponent(name);
    A.clearCaption(el.live);
    tick();
  }

  document.querySelectorAll('input[name="template"]').forEach(function (radio) {
    radio.addEventListener('change', function () { if (radio.checked) selectTemplate(radio.value); });
  });

  function loadPreviews() {
    if (!state.job.has_video) return;
    document.querySelectorAll('img[data-preview]').forEach(function (img) {
      var thumb = img.closest('.tpl-thumb');
      thumb.classList.add('is-loading');
      img.onload = function () { thumb.classList.remove('is-loading'); };
      img.onerror = function () { thumb.classList.remove('is-loading'); img.removeAttribute('src'); };
      img.src = base + '/preview/' + img.dataset.preview + '.jpg?v=' + state.job.updated + '-' + state.rev;
    });
  }

  function schedulePreviews() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(loadPreviews, 2500);
  }

  // ------------------------------------------------------------ rendering
  function loadRenders() {
    return A.api('GET', base + '/renders').then(function (data) {
      el.renders.textContent = '';
      data.renders.slice(0, 5).forEach(function (r) {
        var label = (TEMPLATE_NAMES[r.template] || r.template) + ' style';
        var item = h('li', {}, h('span', { text: label }));
        if (r.status === 'done') {
          item.appendChild(h('span', {},
            h('a', { href: r.url + '?inline=1', target: '_blank', rel: 'noopener', text: 'Play' }), ' · ',
            h('a', { href: r.url, text: 'Download MP4' })));
        } else if (r.status === 'failed') {
          item.appendChild(h('span', { class: 'muted', text: r.message || 'Failed' }));
        } else {
          item.appendChild(h('span', { class: 'muted', text: 'In progress… ' + r.progress + '%' }));
        }
        el.renders.appendChild(item);
      });
    }, function () { /* the list is a convenience; ignore errors */ });
  }

  async function pollRender(id) {
    el.renderBar.hidden = false;
    for (;;) {
      var render;
      try {
        render = await A.api('GET', '/api/renders/' + id);
      } catch (error) {
        el.renderText.textContent = error.message;
        break;
      }
      setBar(el.renderBar, el.renderFill, render.progress);
      if (render.status === 'done') {
        el.renderText.textContent = 'Your video is ready.';
        break;
      }
      if (render.status === 'failed') {
        el.renderText.textContent = render.message || 'The render failed.';
        break;
      }
      el.renderText.textContent = 'Rendering… ' + render.progress + '%';
      await new Promise(function (resolve) { setTimeout(resolve, 1000); });
    }
    el.renderBar.hidden = true;
    el.renderBtn.disabled = false;
    loadRenders();
  }

  el.renderBtn.addEventListener('click', async function () {
    el.renderBtn.disabled = true;
    el.renderText.textContent = 'Starting…';
    try {
      await save();
      var render = await A.api('POST', base + '/render', { template: state.template });
      setBar(el.renderBar, el.renderFill, 0);
      pollRender(render.id);
    } catch (error) {
      el.renderText.textContent = error.message;
      el.renderBtn.disabled = false;
    }
  });

  // ------------------------------------------------------------ timing tools
  function adopt(result) {
    state.cues = result.cues;
    state.job = result.job;
    state.savedRev = state.rev;
    renderCues();
    tick();
    schedulePreviews();
    setSave('All changes saved');
    el.syncResult.textContent = result.message || '';
  }

  async function runSync(payload) {
    el.syncResult.textContent = 'Working…';
    try {
      await save();
      adopt(await A.api('POST', base + '/sync', payload));
    } catch (error) {
      el.syncResult.textContent = error.message;
    }
  }

  document.querySelectorAll('[data-nudge]').forEach(function (button) {
    button.addEventListener('click', function () { runSync({ mode: 'offset', seconds: parseFloat(button.dataset.nudge) }); });
  });
  $('shift-apply').addEventListener('click', function () {
    var seconds = parseFloat($('shift-seconds').value);
    if (isNaN(seconds)) { el.syncResult.textContent = 'Enter the number of seconds to shift.'; return; }
    runSync({ mode: 'offset', seconds: seconds });
  });
  $('stretch-preset').addEventListener('change', function (event) {
    if (event.target.value) $('stretch-factor').value = event.target.value;
  });
  $('stretch-apply').addEventListener('click', function () {
    var factor = parseFloat($('stretch-factor').value);
    if (isNaN(factor)) { el.syncResult.textContent = 'Enter a stretch factor, for example 1.0427.'; return; }
    runSync({ mode: 'stretch', factor: factor });
  });
  $('auto-apply').addEventListener('click', function () { runSync({ mode: 'auto' }); });
  $('srt-apply').addEventListener('click', async function () {
    var file = $('srt-file').files[0];
    if (!file) { el.syncResult.textContent = 'Choose an .srt file first.'; return; }
    var form = new FormData();
    form.append('srt', file);
    form.append('auto', $('srt-auto').checked ? '1' : '0');
    el.syncResult.textContent = 'Importing…';
    try {
      await save();
      adopt(await A.api('POST', base + '/import-srt', form));
    } catch (error) {
      el.syncResult.textContent = error.message;
    }
  });

  // switching tabs or apps (common on phones) may be the last chance to store pending edits
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden' && state.rev !== state.savedRev) save();
  });

  window.addEventListener('beforeunload', function (event) {
    if (state.saving || el.save.textContent === 'Unsaved changes') {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  poll();
})();
