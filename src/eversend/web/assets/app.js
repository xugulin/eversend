/* EverSend web client (vanilla JS, no build step, no CDN).
 *
 * The page is served by the engine's own web UI, so everything it needs is
 * already on the LAN.  Structure:
 *
 *   1. helpers            formatting, DOM building, toasts
 *   2. api()              fetch wrapper that attaches the CSRF token
 *   3. store + render     one state object, pure render functions
 *   4. events             SSE for immediacy, /api/state polling as a fallback
 *   5. upload             XHR (not fetch) so we get real upload progress
 *   6. wiring             tabs, drag & drop, buttons
 *
 * All user-facing strings are Chinese; comments are English.
 */
(function () {
  'use strict';

  // --------------------------------------------------------------- helpers

  var TOKEN = (document.querySelector('meta[name="eversend-token"]') || {}).content || '';
  var POLL_MS = 3000;
  var TICK_MS = 250;

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  /** Terse element builder.  Text goes through textContent, never innerHTML,
   *  so a hostile device name cannot inject markup into the page. */
  function h(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'dataset') Object.keys(value).forEach(function (k) { node.dataset[k] = value[k]; });
        else if (key.slice(0, 2) === 'on') node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? '' : value);
      });
    }
    for (var i = 2; i < arguments.length; i++) {
      append(node, arguments[i]);
    }
    return node;
  }

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(node, c); }); return; }
    node.appendChild(child.nodeType ? child : document.createTextNode(String(child)));
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // Mirrors eversend.core.model.human_bytes / human_speed / human_duration so
  // the phone and the desktop show identical numbers.
  function fmtBytes(value) {
    var units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    var size = Number(value) || 0;
    for (var i = 0; i < units.length; i++) {
      if (Math.abs(size) < 1024 || i === units.length - 1) {
        return units[i] === 'B' ? Math.round(size) + ' B' : size.toFixed(2) + ' ' + units[i];
      }
      size /= 1024;
    }
    return size.toFixed(2) + ' PB';
  }

  function fmtSpeed(bps) { return fmtBytes(bps) + '/s'; }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds)) return '--:--';
    var total = Math.max(0, Math.round(seconds));
    if (total < 60) return total + 's';
    var minutes = Math.floor(total / 60);
    if (minutes < 60) return minutes + 'm' + String(total % 60).padStart(2, '0') + 's';
    var hours = Math.floor(minutes / 60);
    return hours + 'h' + String(minutes % 60).padStart(2, '0') + 'm';
  }

  function fmtTime(epochSeconds) {
    try {
      return new Date(epochSeconds * 1000).toLocaleString();
    } catch (err) { return ''; }
  }

  var toastTimers = [];
  function toast(message, kind) {
    var box = $('#toasts');
    var node = h('div', { class: 'toast' + (kind ? ' is-' + kind : ''), text: message });
    box.appendChild(node);
    var timer = setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, kind === 'error' ? 7000 : 4000);
    toastTimers.push(timer);
    while (box.children.length > 4) box.removeChild(box.firstChild);
  }

  // ------------------------------------------------------------------- api

  function api(path, options) {
    var opts = options || {};
    var headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});
    var body = opts.body;
    if (opts.method === 'POST') headers['X-EverSend-Token'] = TOKEN;
    if (opts.json !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.json);
    }
    return fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      body: body,
      cache: 'no-store',
      credentials: 'same-origin'
    }).then(function (response) {
      return response.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (err) { data = null; } }
        if (!response.ok) {
          var message = (data && data.error) || ('请求失败（HTTP ' + response.status + '）');
          var error = new Error(message);
          error.status = response.status;
          throw error;
        }
        return data || {};
      });
    });
  }

  // ----------------------------------------------------------------- store

  var store = {
    ready: false,
    device: null,
    app: null,
    devices: [],
    transfers: [],
    offers: [],
    uploads: [],
    recentUploads: [],
    files: [],
    receiveDir: '',
    freeSpace: 0,
    selectedDeviceId: null,
  shares: [],
    picked: [],
    view: 'send'
  };

  // Live byte samples per transfer (and per upload) used to interpolate
  // between events so the bar moves smoothly instead of stepping every poll.
  var samples = new Map();
  var painted = new Map();

  function noteBytes(id, bytes) {
    if (!id || typeof bytes !== 'number') return;
    var now = performance.now();
    var entry = samples.get(id);
    if (!entry) { samples.set(id, { bytes: bytes, at: now, rate: 0 }); return; }
    var dt = (now - entry.at) / 1000;
    if (dt < 0.2) return;
    var instant = (bytes - entry.bytes) / dt;
    if (instant >= 0) entry.rate = entry.rate > 0 ? entry.rate * 0.65 + instant * 0.35 : instant;
    entry.bytes = bytes;
    entry.at = now;
  }

  function projected(id, fallbackBytes, total) {
    var entry = samples.get(id);
    if (!entry || !(entry.rate > 0)) return { bytes: fallbackBytes, rate: 0 };
    var bytes = Math.min(total || Infinity, entry.bytes + entry.rate * (performance.now() - entry.at) / 1000);
    return { bytes: Math.max(fallbackBytes, bytes), rate: entry.rate };
  }

  // ---------------------------------------------------------------- render

  // The poll runs every 3 s; rebuilding a list that did not change would drop
  // focus, reset the "trust this device" checkbox and flicker the page.  Each
  // renderer therefore compares a cheap signature first and returns early.
  var signatures = {};

  function changed(key, value) {
    if (signatures[key] === value) return false;
    signatures[key] = value;
    return true;
  }

  function platformIcon(platform, kind) {
    var tag = String(platform || '').toLowerCase();
    if (tag === 'android') return '🤖';
    if (tag === 'ios' || tag === 'ipados' || tag === 'macos') return '🍎';
    if (tag === 'windows') return '🪟';
    if (tag === 'linux') return '🐧';
    if (kind === 'mobile') return '📱';
    if (kind === 'server') return '🖥️';
    return '💻';
  }

  function deviceLabel(device) {
    return device.name || device.id || '未知设备';
  }

  function renderDevices() {
    // The local machine is served first by /api/state: "send it to this
    // computer" is the common case, so it must never be filtered away.
    var devices = store.devices;
    var signature = devices
      .map(function (d) { return d.id + '|' + d.name + '|' + d.trusted + '|' + d.address; })
      .join(',') + '#' + store.selectedDeviceId;
    if (!changed('devices', signature)) return;

    var list = $('#device-list');
    var empty = $('#device-empty');
    clear(list);
    empty.hidden = devices.length > 0;

    if (!store.selectedDeviceId && devices.length) {
      var preferred = devices.find(function (d) { return d.local; }) ||
        devices.find(function (d) { return d.trusted; }) || devices[0];
      store.selectedDeviceId = preferred.id;
    }
    if (store.selectedDeviceId && !devices.some(function (d) { return d.id === store.selectedDeviceId; })) {
      store.selectedDeviceId = devices.length ? devices[0].id : null;
    }

    devices.forEach(function (device) {
      var selected = device.id === store.selectedDeviceId;
      var tags = [];
      if (device.local) tags.push(h('span', { class: 'tag is-self', text: '本机' }));
      if (device.trusted && !device.local) tags.push(h('span', { class: 'tag is-trusted', text: '已信任' }));
      if (device.webUrl && !device.local) tags.push(h('span', { class: 'tag', text: '可网页打开' }));
      var button = h('button', {
        class: 'device',
        type: 'button',
        'aria-pressed': selected ? 'true' : 'false',
        onclick: function () {
          store.selectedDeviceId = device.id;
          renderDevices();
          updateSendButton();
        }
      },
        h('span', { class: 'icon', text: platformIcon(device.platform, device.kind) }),
        h('span', { class: 'meta' },
          h('span', { class: 'name' }, deviceLabel(device), tags),
          h('span', { class: 'addr', text: (device.address || '未知地址') + ':' + (device.tcpPort || '?') })
        )
      );
      list.appendChild(button);
    });
  }

  function renderPicked() {
    var signature = store.picked.map(function (f) { return f.name + '|' + f.size; }).join(',');
    if (!changed('picked', signature)) return;
    var list = $('#file-list');
    clear(list);
    var total = 0;
    store.picked.forEach(function (file, index) {
      total += file.size;
      list.appendChild(h('li', { class: 'file-item' },
        h('span', { class: 'fname', title: file.name, text: file.name }),
        h('span', { class: 'fsize', text: fmtBytes(file.size) }),
        h('button', {
          class: 'remove', type: 'button', 'aria-label': '移除 ' + file.name, text: '×',
          onclick: function () { store.picked.splice(index, 1); renderPicked(); updateSendButton(); }
        })
      ));
    });
    $('#file-total').textContent = store.picked.length
      ? store.picked.length + ' 个文件 · ' + fmtBytes(total)
      : '';
  }

  function updateSendButton() {
    var button = $('#btn-send');
    var device = store.devices.find(function (d) { return d.id === store.selectedDeviceId; });
    button.disabled = !store.picked.length || !device || !!uploader.active;
    button.textContent = uploader.active ? '正在发送…' : (store.picked.length ? '发送 ' + store.picked.length + ' 个文件' : '发送');
  }

  function renderUpload() {
    var card = $('#upload-card');
    var body = $('#upload-body');
    var job = uploader.active;
    var signature = (job ? job.file.name + '|' + job.file.size : '') + '#' +
      uploader.queue.length + '#' + uploader.done.length;
    if (!changed('upload', signature)) return;
    clear(body);
    if (!job && !uploader.queue.length && !uploader.done.length) { card.hidden = true; return; }
    card.hidden = false;

    if (job) {
      var bar = h('i');
      body.appendChild(h('div', { class: 'upload-head' },
        h('span', { class: 'fname', text: job.file.name }),
        h('span', { id: 'upload-percent', text: '0%' })
      ));
      body.appendChild(h('div', { class: 'bar' }, bar));
      body.appendChild(h('div', { class: 'stats' },
        h('span', { id: 'upload-bytes', text: '0 B / ' + fmtBytes(job.file.size) }),
        h('span', { id: 'upload-speed', text: '' })
      ));
      painted.set('__upload__', {
        bar: bar,
        total: job.file.size,
        bytesNode: $('#upload-bytes'),
        speedNode: $('#upload-speed'),
        percentNode: $('#upload-percent')
      });
    } else {
      painted.delete('__upload__');
    }

    if (uploader.queue.length) {
      var queue = h('ul', { class: 'queue' });
      uploader.queue.forEach(function (file) {
        queue.appendChild(h('li', {}, h('span', { text: file.name }), h('span', { text: '等待 ' + fmtBytes(file.size) })));
      });
      body.appendChild(h('p', { class: 'hint', text: '队列中还有 ' + uploader.queue.length + ' 个文件' }));
      body.appendChild(queue);
    }

    if (uploader.done.length) {
      var done = h('ul', { class: 'queue' });
      uploader.done.slice(-6).forEach(function (item) {
        done.appendChild(h('li', {},
          h('span', { text: item.name }),
          h('span', { class: item.ok ? 'state-ok' : 'state-err', text: item.ok ? '已发送' : (item.error || '失败') })
        ));
      });
      body.appendChild(done);
    }
  }

  function progressCard(options) {
    var id = options.id;
    var bar = h('i');
    var percentNode = h('span', { text: '0%' });
    var bytesNode = h('span', { text: '0 B / ' + fmtBytes(options.total) });
    var speedNode = h('span', { text: '' });
    var card = h('div', { class: 'transfer' },
      h('div', { class: 'head' },
        h('span', { class: 'who' },
          h('span', { class: 'arrow', text: options.direction === 'send' ? '↑' : '↓' }),
          options.title
        ),
        percentNode
      ),
      options.subtitle ? h('div', { class: 'sub', text: options.subtitle }) : null,
      h('div', { class: 'bar' + (options.failed ? ' is-failed' : '') }, bar),
      h('div', { class: 'stats' }, bytesNode, speedNode),
      options.actions ? h('div', { class: 'actions' }, options.actions) : null
    );
    painted.set(id, {
      bar: bar,
      total: options.total,
      bytesNode: bytesNode,
      speedNode: speedNode,
      percentNode: percentNode
    });
    return card;
  }

  function renderTransfers() {
    var signature = store.transfers
      .map(function (t) {
        return [t.transferId, t.direction, t.status, t.error, t.files, (t.peer || {}).name].join('|');
      })
      .join(',');
    if (!changed('transfers', signature)) return;

    var sendBox = $('#send-transfers');
    var receiveBox = $('#receive-transfers');
    clear(sendBox);
    clear(receiveBox);
    painted.forEach(function (value, key) { if (key !== '__upload__') painted.delete(key); });

    store.transfers.forEach(function (transfer) {
      var peer = transfer.peer || {};
      var failed = transfer.status === 'failed';
      var subtitle = transfer.files + ' 个文件';
      if (transfer.error) subtitle += ' · ' + transfer.error;
      var actions = h('button', {
        class: 'danger', type: 'button', text: '取消',
        onclick: function () { cancelTransfer(transfer.transferId); }
      });
      var card = progressCard({
        id: transfer.transferId,
        direction: transfer.direction,
        title: (transfer.direction === 'send' ? '发送到 ' : '来自 ') + deviceLabel(peer),
        subtitle: subtitle,
        total: transfer.total,
        failed: failed,
        actions: transfer.status === 'active' ? actions : null
      });
      (transfer.direction === 'send' ? sendBox : receiveBox).appendChild(card);
      noteBytes(transfer.transferId, transfer.bytes);
    });
  }

  function renderOffers() {
    var signature = store.offers
      .map(function (o) { return [o.request_id, o.total, (o.peer || {}).name, (o.files || []).length].join('|'); })
      .join(',');
    // Guarded so a 3 s poll cannot reset the "信任此设备" checkbox while the
    // user is looking at it.
    if (!changed('offers', signature)) return;

    var box = $('#offer-list');
    var badge = $('#offer-badge');
    clear(box);
    store.offers.forEach(function (offer) {
      var peer = offer.peer || {};
      var files = offer.files || [];
      var list = h('ul', { class: 'offer-files' });
      files.slice(0, 8).forEach(function (file) {
        list.appendChild(h('li', {},
          h('span', { class: 'fname', title: file.name, text: file.name }),
          h('span', { text: fmtBytes(file.size) })
        ));
      });
      if (files.length > 8) list.appendChild(h('li', { text: '…还有 ' + (files.length - 8) + ' 个文件' }));

      var trust = h('input', { type: 'checkbox' });
      var card = h('div', { class: 'transfer' },
        h('div', { class: 'head' },
          h('span', { class: 'who' }, h('span', { class: 'arrow', text: '↓' }), '来自 ' + deviceLabel(peer)),
          h('span', { class: 'muted', text: fmtBytes(offer.total) })
        ),
        h('div', { class: 'sub', text: files.length + ' 个文件' + (offer.resume_bytes ? '（可续传 ' + fmtBytes(offer.resume_bytes) + '）' : '') + (offer.authenticated ? ' · 已加密验证' : '') }),
        list,
        h('label', { class: 'trust' }, trust, '信任此设备（以后自动接收）'),
        h('div', { class: 'actions' },
          h('button', {
            class: 'primary', type: 'button', text: '接受',
            onclick: function (event) { respondOffer(offer.request_id, true, trust.checked, event.target); }
          }),
          h('button', {
            class: 'danger', type: 'button', text: '拒绝',
            onclick: function (event) { respondOffer(offer.request_id, false, false, event.target); }
          })
        )
      );
      box.appendChild(card);
    });
    $('#offer-count').textContent = store.offers.length ? store.offers.length + ' 个待确认' : '';
    badge.hidden = store.offers.length === 0;
    badge.textContent = String(store.offers.length);
  }

  function renderFiles() {
    var signature = store.files.map(function (f) { return f.path + '|' + f.size + '|' + f.mtime; }).join(',');
    if (!changed('files', signature)) return;
    var list = $('#received-files');
    clear(list);
    if (!store.files.length) {
      list.appendChild(h('li', { class: 'file-item' }, h('span', { class: 'fname muted', text: '还没有收到文件' })));
      return;
    }
    store.files.forEach(function (file) {
      list.appendChild(h('li', { class: 'file-item' },
        h('span', { class: 'fname', title: file.path, text: file.name }),
        h('span', { class: 'fsize', text: fmtBytes(file.size) }),
        h('a', {
          href: '/api/download?path=' + encodeURIComponent(file.path),
          download: file.name,
          text: '下载'
        })
      ));
    });
  }

  function renderShares() {
    // Files the desktop handed over for this phone.  A browser cannot be
    // pushed to, so this list *is* the "receive" direction: the computer puts
    // the file here and the phone pulls it with one tap.
    var signature = store.shares.map(function (s) {
      return s.id + '|' + s.size + '|' + (s.downloaded ? 1 : 0);
    }).join(',');
    if (!changed('shares', signature)) return;
    var card = $('#share-card');
    var list = $('#share-list');
    clear(list);
    card.hidden = store.shares.length === 0;
    if (!store.shares.length) return;
    store.shares.forEach(function (share) {
      list.appendChild(h('li', { class: 'file-item' },
        h('span', { class: 'fname', title: share.name, text: share.name }),
        h('span', { class: 'fsize', text: fmtBytes(share.size) }),
        h('a', {
          href: '/api/share/' + encodeURIComponent(share.id),
          download: share.name,
          text: share.downloaded ? '再下载' : '下载'
        })
      ));
    });
  }

  function renderSelf() {
    var device = store.device || {};
    var signature = [device.id, device.name, device.platform, store.receiveDir, store.freeSpace,
      (device.urls || []).join(',')].join('|');
    if (!changed('self', signature)) return;
    var info = $('#self-info');
    clear(info);
    function row(term, value) {
      info.appendChild(h('dt', { text: term }));
      append(info.appendChild(document.createElement('dd')), value);
    }
    row('设备名称', device.name || '—');
    row('设备编号', h('code', { text: device.id || '—' }));
    row('系统', (device.platform || '—') + ' · ' + (device.kind || '—'));
    row('接收目录', store.receiveDir || '—');
    row('剩余空间', fmtBytes(store.freeSpace));
    if (device.urls && device.urls.length) {
      var links = h('span');
      device.urls.forEach(function (url, index) {
        if (index) links.appendChild(document.createTextNode('、'));
        links.appendChild(h('a', { href: url, text: url }));
      });
      row('访问地址', links);
    }
    $('#about-version').textContent = store.app
      ? (store.app.nameCn + ' ' + store.app.name + ' v' + store.app.version)
      : '';
  }

  function renderAll() {
    renderDevices();
    renderPicked();
    renderUpload();
    renderTransfers();
    renderOffers();
    renderFiles();
    renderShares();
    renderSelf();
    updateSendButton();
  }

  // -------------------------------------------------------------- painting

  function paint() {
    painted.forEach(function (nodes, id) {
      var fallback = 0;
      if (id === '__upload__') {
        fallback = uploader.loaded || 0;
      } else {
        var transfer = store.transfers.find(function (t) { return t.transferId === id; });
        fallback = transfer ? transfer.bytes : 0;
      }
      var total = nodes.total || 0;
      var projectedValue = projected(id, fallback, total);
      var bytes = projectedValue.bytes;
      var percent = total > 0 ? Math.min(100, (bytes / total) * 100) : (fallback > 0 ? 100 : 0);
      nodes.bar.style.width = percent.toFixed(1) + '%';
      if (nodes.percentNode) nodes.percentNode.textContent = percent.toFixed(0) + '%';
      if (nodes.bytesNode) nodes.bytesNode.textContent = fmtBytes(bytes) + ' / ' + fmtBytes(total);
      if (nodes.speedNode) {
        var rate = projectedValue.rate;
        if (rate > 0) {
          var eta = total > bytes ? (total - bytes) / rate : 0;
          nodes.speedNode.textContent = fmtSpeed(rate) + ' · 剩余 ' + fmtDuration(eta);
        } else {
          nodes.speedNode.textContent = '';
        }
      }
    });
  }

  // --------------------------------------------------------------- loading

  function applyState(state) {
    store.ready = true;
    store.app = state.app || store.app;
    store.device = state.device || store.device;
    store.devices = state.devices || [];
    store.shares = state.shares || [];
    store.transfers = state.transfers || [];
    store.offers = state.offers || [];
    store.uploads = state.uploads || [];
    store.recentUploads = state.recentUploads || [];
    store.receiveDir = state.receiveDir || '';
    store.freeSpace = state.freeSpace || 0;
    setConnected('live');
  }

  function refreshState() {
    return api('/api/state').then(function (state) {
      applyState(state);
      renderAll();
      paint();
    }).catch(function (error) {
      setConnected('down');
      throw error;
    });
  }

  var refreshFiles = function () {
    return api('/api/files').then(function (data) {
      store.files = data.files || [];
      store.receiveDir = data.dir || store.receiveDir;
      store.freeSpace = data.free || store.freeSpace;
      renderFiles();
      renderSelf();
    }).catch(function () { /* the toast from the poller is enough */ });
  };

  // Throttled refresh used by the event stream: a burst of events must not
  // turn into a burst of requests.
  var refreshTimer = null;
  function scheduleRefresh(delay) {
    if (refreshTimer) return;
    refreshTimer = setTimeout(function () {
      refreshTimer = null;
      refreshState().catch(function (error) { toast(error.message, 'error'); });
    }, delay === undefined ? 250 : delay);
  }

  function setConnected(mode) {
    var node = $('#conn');
    node.classList.toggle('is-live', mode === 'live');
    node.classList.toggle('is-poll', mode === 'poll');
    node.classList.toggle('is-down', mode === 'down');
    $('#conn-text').textContent = mode === 'live' ? '已连接'
      : mode === 'poll' ? '轮询中' : '已断开';
  }

  // ---------------------------------------------------------------- events

  var SSE_KINDS_REFRESH = {
    transfer_started: 1, transfer_finished: 1, transfer_cancelled: 1, transfer_accepted: 1,
    transfer_rejected: 1, file_done: 1, file_failed: 1, offer_received: 1,
    device_found: 1, device_updated: 1, engine_started: 1, web_upload_failed: 1
  };

  function connectEvents() {
    if (!window.EventSource) { setConnected('poll'); return; }
    var source = new EventSource('/api/events');
    source.addEventListener('open', function () { setConnected('live'); });
    source.addEventListener('hello', function () { setConnected('live'); });
    source.addEventListener('error', function () {
      // EventSource reconnects by itself; the 3 s poll keeps the UI truthful
      // in the meantime, which is also what happens behind a buffering proxy.
      setConnected(source.readyState === 2 ? 'poll' : 'poll');
    });
    source.addEventListener('message', function (message) {
      var event;
      try { event = JSON.parse(message.data); } catch (err) { return; }
      handleEvent(event);
    });
  }

  function handleEvent(event) {
    var kind = event.kind;
    switch (kind) {
      case 'send_progress':
        noteBytes(event.transfer_id, event.bytes);
        var transfer = store.transfers.find(function (t) { return t.transferId === event.transfer_id; });
        if (transfer) { transfer.bytes = event.bytes; transfer.total = event.total || transfer.total; }
        return;
      case 'offer_received':
        toast('收到来自 ' + deviceLabel(event.peer || {}) + ' 的 ' + ((event.files || []).length) + ' 个文件');
        switchView('receive');
        break;
      case 'file_done':
        if (event.path) toast('已接收：' + event.name, 'ok');
        refreshFiles();
        break;
      case 'file_failed':
        toast('文件传输失败：' + (event.error || ''), 'error');
        break;
      case 'transfer_finished':
        if (event.status && event.status !== 'done') {
          toast('传输未完成：' + (event.error || event.status), 'error');
        }
        break;
      case 'web_upload_failed':
        toast('发送失败：' + (event.error || ''), 'error');
        break;
      case 'warning':
        toast(event.message || '警告', 'error');
        break;
    }
    if (SSE_KINDS_REFRESH[kind]) {
      samples.delete(event.transfer_id);
      scheduleRefresh(kind === 'send_progress' ? 1000 : 250);
    }
  }

  // ---------------------------------------------------------------- upload

  var uploader = {
    queue: [],
    active: null,
    loaded: 0,
    done: []
  };

  function enqueueFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    files.forEach(function (file) { uploader.queue.push(file); });
    renderUpload();
    updateSendButton();
    pump();
  }

  function pump() {
    if (uploader.active || !uploader.queue.length) {
      if (!uploader.active) updateSendButton();
      return;
    }
    var device = store.devices.find(function (d) { return d.id === store.selectedDeviceId; });
    if (!device) {
      toast('请先选择目标设备', 'error');
      return;
    }
    var file = uploader.queue.shift();
    var pin = $('#pin').value.trim();
    uploader.active = { file: file, device: device };
    uploader.loaded = 0;
    updateSendButton();
    renderUpload();

    var query = '/api/upload?name=' + encodeURIComponent(file.name) +
      '&deviceId=' + encodeURIComponent(device.id) +
      '&pin=' + encodeURIComponent(pin);

    // XHR rather than fetch: only XHR exposes upload.onprogress, and a real
    // progress bar is the difference between "is it stuck?" and "it is 60%".
    var request = new XMLHttpRequest();
    request.open('POST', query, true);
    request.setRequestHeader('X-EverSend-Token', TOKEN);
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.upload.onprogress = function (progressEvent) {
      if (!progressEvent.lengthComputable) return;
      uploader.loaded = progressEvent.loaded;
      noteBytes('__upload__', progressEvent.loaded);
    };
    request.onload = function () {
      var payload = null;
      try { payload = JSON.parse(request.responseText); } catch (err) { payload = null; }
      var ok = request.status >= 200 && request.status < 300 && payload && payload.ok;
      uploader.done.push({
        name: file.name,
        ok: ok,
        error: ok ? '' : ((payload && payload.error) || ('HTTP ' + request.status))
      });
      if (!ok) toast('上传失败：' + file.name + '（' + ((payload && payload.error) || request.status) + '）', 'error');
      else if (payload.transferId) samples.delete(payload.transferId);
      finishOne(ok);
    };
    request.onerror = function () {
      uploader.done.push({ name: file.name, ok: false, error: '网络中断' });
      toast('上传中断：' + file.name, 'error');
      finishOne(false);
    };
    request.ontimeout = request.onerror;
    request.onabort = function () { finishOne(false); };
    uploader.request = request;
    request.send(file);
  }

  function finishOne(ok) {
    uploader.active = null;
    uploader.request = null;
    uploader.loaded = 0;
    painted.delete('__upload__');
    samples.delete('__upload__');
    renderUpload();
    updateSendButton();
    if (ok) scheduleRefresh(300);
    pump();
  }

  // ---------------------------------------------------------------- actions

  function respondOffer(requestId, accept, trust, button) {
    if (button) button.disabled = true;
    api('/api/offer/respond', {
      method: 'POST',
      json: { requestId: requestId, accept: accept, trust: trust }
    }).then(function () {
      toast(accept ? '已接受，开始接收' : '已拒绝', accept ? 'ok' : undefined);
      return refreshState();
    }).catch(function (error) {
      toast(error.message, 'error');
      return refreshState();
    });
  }

  function cancelTransfer(transferId) {
    api('/api/cancel', { method: 'POST', json: { transferId: transferId } })
      .then(function () { toast('已取消'); return refreshState(); })
      .catch(function (error) { toast(error.message, 'error'); });
  }

  function triggerScan() {
    toast('正在扫描局域网…');
    api('/api/scan', { method: 'POST' })
      .then(function () { return api('/api/announce', { method: 'POST' }); })
      .then(function () { setTimeout(function () { scheduleRefresh(0); }, 1500); })
      .catch(function (error) { toast(error.message, 'error'); });
  }

  function switchView(view) {
    store.view = view;
    ['send', 'receive', 'about'].forEach(function (name) {
      var section = $('#view-' + name);
      if (section) section.hidden = name !== view;
      var tab = document.querySelector('.tab[data-view="' + name + '"]');
      if (tab) {
        tab.classList.toggle('is-active', name === view);
        tab.setAttribute('aria-selected', name === view ? 'true' : 'false');
      }
    });
    if (view === 'about') loadQr();
  }

  var qrLoadedFor = '';
  function loadQr() {
    var url = (store.device && store.device.urls && store.device.urls[0]) || '';
    if (!url) return;
    $('#qr-url').textContent = url;
    if (qrLoadedFor === url) return;
    qrLoadedFor = url;
    $('#qr').src = '/api/qr.svg?ts=' + Date.now();
  }

  // ----------------------------------------------------------------- wiring

  function bind() {
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () { switchView(tab.dataset.view); });
    });

    var input = $('#file-input');
    input.addEventListener('change', function () {
      enqueueFiles(input.files);
      input.value = '';
    });

    var dropzone = $('#dropzone');
    ['dragenter', 'dragover'].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.add('is-over');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.remove('is-over');
      });
    });
    dropzone.addEventListener('drop', function (event) {
      if (event.dataTransfer && event.dataTransfer.files) enqueueFiles(event.dataTransfer.files);
    });
    // Dropping anywhere else must not make the browser navigate away.
    window.addEventListener('dragover', function (e) { e.preventDefault(); });
    window.addEventListener('drop', function (e) { e.preventDefault(); });

    $('#btn-send').addEventListener('click', function () { pump(); });
    $('#btn-rescan').addEventListener('click', triggerScan);
    $('#btn-refresh-devices').addEventListener('click', function () {
      api('/api/announce', { method: 'POST' })
        .then(function () { return refreshState(); })
        .catch(function (error) { toast(error.message, 'error'); });
    });
    $('#btn-refresh-files').addEventListener('click', function () { refreshFiles(); });
    $('#pin').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') pump();
    });
    window.addEventListener('online', function () { scheduleRefresh(0); });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { scheduleRefresh(0); refreshFiles(); }
    });
  }

  // ------------------------------------------------------------------ boot

  function boot() {
    bind();
    switchView('send');
    refreshState().then(refreshFiles).catch(function (error) {
      setConnected('down');
      toast('无法连接服务器：' + error.message, 'error');
    });
    connectEvents();
    setInterval(function () {
      refreshState().catch(function () { /* the connection badge says it all */ });
    }, POLL_MS);
    setInterval(paint, TICK_MS);
    setInterval(refreshFiles, 15000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
