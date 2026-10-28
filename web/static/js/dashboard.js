/* Dashboard: upload with progress, live status of running projects, delete. */
(function () {
  'use strict';
  var A = window.AutoSub;
  var dash = document.getElementById('dash');
  if (!dash) return;

  var maxMb = parseInt(dash.dataset.maxMb || '200', 10);
  var drop = document.getElementById('drop');
  var input = document.getElementById('file');
  var language = document.getElementById('language');
  var title = document.getElementById('drop-title');
  var hint = document.getElementById('drop-hint');
  var bar = document.getElementById('drop-bar');
  var fill = document.getElementById('drop-bar-fill');
  var errorBox = document.getElementById('upload-error');
  var ALLOWED = /\.(mp4|mov|mkv|webm|avi|m4v|mpg|mpeg|3gp|wmv|flv)$/i;
  var defaultTitle = title.textContent;

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function resetDrop() {
    drop.classList.remove('is-busy');
    title.textContent = defaultTitle;
    bar.hidden = true;
    fill.style.width = '0';
    input.value = '';
  }

  function upload(file) {
    showError('');
    if (!file) return;
    if (!ALLOWED.test(file.name)) {
      showError('That file type is not supported. Use MP4, MOV, MKV, WebM or AVI.');
      return;
    }
    if (file.size > maxMb * 1024 * 1024) {
      showError('That file is ' + Math.round(file.size / 1048576) + ' MB. The limit is ' + maxMb + ' MB.');
      return;
    }
    var form = new FormData();
    form.append('video', file);
    form.append('language', language.value);

    drop.classList.add('is-busy');
    title.textContent = 'Uploading ' + file.name;
    bar.hidden = false;

    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-CSRF-Token', A.csrfToken());
    xhr.upload.onprogress = function (event) {
      if (event.lengthComputable) {
        var pct = Math.round((event.loaded / event.total) * 100);
        fill.style.width = pct + '%';
        if (pct >= 100) title.textContent = 'Checking ' + file.name;
      }
    };
    xhr.onerror = function () {
      resetDrop();
      showError('The upload failed. Check your connection, and that the file is under ' + maxMb + ' MB.');
    };
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status === 201 && data && data.url) {
        window.location.href = data.url;
        return;
      }
      resetDrop();
      showError((data && data.error) || 'The upload failed (error ' + xhr.status + ').');
    };
    xhr.send(form);
  }

  input.addEventListener('change', function () { upload(input.files[0]); });
  ['dragenter', 'dragover'].forEach(function (name) {
    drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.add('is-over'); });
  });
  ['dragleave', 'drop'].forEach(function (name) {
    drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.remove('is-over'); });
  });
  drop.addEventListener('drop', function (event) {
    if (event.dataTransfer && event.dataTransfer.files.length) upload(event.dataTransfer.files[0]);
  });
  // a file dropped outside the zone should not make the browser navigate away
  window.addEventListener('dragover', function (event) { event.preventDefault(); });
  window.addEventListener('drop', function (event) { event.preventDefault(); });

  // ---- project list ------------------------------------------------------
  function updateRow(row, job) {
    var chip = row.querySelector('[data-role="status"]');
    chip.className = 'chip chip--' + job.status;
    if (job.status === 'ready') chip.textContent = 'Ready';
    else if (job.status === 'failed') chip.textContent = 'Failed';
    else chip.textContent = 'Transcribing ' + job.progress + '%';
    row.dataset.active = (job.status === 'ready' || job.status === 'failed') ? '0' : '1';
  }

  function pollActive() {
    var rows = Array.prototype.slice.call(document.querySelectorAll('.prow[data-active="1"]'));
    if (!rows.length) return;
    Promise.all(rows.map(function (row) {
      return A.api('GET', '/api/p/' + row.dataset.id).then(function (job) { updateRow(row, job); }, function () {});
    })).then(function () { setTimeout(pollActive, 2000); });
  }
  pollActive();

  var list = document.getElementById('plist');
  if (list) {
    list.addEventListener('click', function (event) {
      var button = event.target.closest('[data-delete]');
      if (!button) return;
      var row = button.closest('.prow');
      var name = row.querySelector('.prow-title').textContent;
      if (!window.confirm('Delete "' + name + '"? The subtitles and the uploaded video will be removed.')) return;
      button.disabled = true;
      A.api('DELETE', '/api/p/' + row.dataset.id).then(function () {
        row.remove();
        if (!list.children.length) window.location.reload();
      }, function (error) {
        button.disabled = false;
        showError(error.message);
      });
    });
  }
})();
