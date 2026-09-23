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

  // 这台设备的稳定标识：存在 localStorage 里，换 IP、换网络、关掉页面再打开
  // 都还是它。以前网页版的身份是"地址 + User-Agent"，手机换一次 IP 就变成
  // 另一台设备，会话也跟着多一条 —— 用户看到的正是"同一台手机冒出来好几台"。
  var DEVICE_KEY = 'eversend-device-id';
  var DEVICE_NAME_KEY = 'eversend-device-name';
  function deviceId() {
    var existing = localStorage.getItem(DEVICE_KEY);
    if (existing) return existing;
    var fresh = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : ('web-' + Date.now() + '-' + Math.random().toString(16).slice(2));
    localStorage.setItem(DEVICE_KEY, fresh);
    return fresh;
  }

  /** 这台设备叫什么：安卓手机写型号，电脑浏览器写“浏览器”。 */
  function deviceName() {
    var existing = localStorage.getItem(DEVICE_NAME_KEY);
    if (existing) return existing;
    var ua = navigator.userAgent || '';
    var guess = /Android/i.test(ua) ? '安卓手机' : (/iPhone|iPad/i.test(ua) ? 'iPhone/iPad' : '电脑浏览器');
    var match = /Android[^;)]*;\s*([^;)]+?)\s*(?:Build|\))/i.exec(ua);
    if (match && match[1]) guess = match[1].trim();
    localStorage.setItem(DEVICE_NAME_KEY, guess);
    return guess;
  }

  function deviceHeaders() {
    return { 'X-EverSend-Device': deviceId(), 'X-EverSend-Name': deviceName() };
  }

  function api(path, options) {
    var opts = options || {};
    var headers = Object.assign({ Accept: 'application/json' }, deviceHeaders(), opts.headers || {});
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
    // 记住的手机（网页版 / 安卓 App），带 online 与 secondsAgo：设备列表要
    // 靠它打「已连接 / 未连接 · 最后在线」的标记。
    knownClients: [],
    selectedDeviceId: null,
  shares: [],
  chat: { selfId: '', conversations: [], unread: 0 },
  conversationId: '',
  messages: [],
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

  /** 「已连接 / 未连接 · 最后在线 X 前」——手机端也要一眼看出谁在线。
   *
   *  手机的设备列表其实有两拨人：协议对端（电脑，来自 /api/state 的 devices，
   *  在发现表里就说明刚刚还听见它）和设备列表里记住的手机（knownClients，带
   *  online 与 secondsAgo）。两拨人以前长一个样，用户分不清谁连着了。
   */
  function statusChip(online, secondsAgo, isSelf) {
    if (isSelf) return h('span', { class: 'tag is-self', text: '本机' });
    if (online) return h('span', { class: 'tag is-online', text: '已连接' });
    var ago = Number(secondsAgo || 0);
    var when = ago < 60 ? Math.round(ago) + ' 秒前'
      : ago < 3600 ? Math.round(ago / 60) + ' 分钟前'
        : Math.round(ago / 3600) + ' 小时前';
    return h('span', { class: 'tag is-offline', text: '未连接 · 最后在线 ' + when });
  }

  /** 名字后面跟"系统 · 版本 · IP"，规范到哪儿都一样。 */
  function deviceFacts(device) {
    var parts = [];
    if (device.platform) parts.push(platformName(device.platform));
    if (device.version) parts.push('韧传 ' + device.version);
    if (device.address) parts.push(device.address + (device.webPort ? ':' + device.webPort : ''));
    return parts.join(' · ');
  }

  function platformName(platform) {
    var text = String(platform || '').toLowerCase();
    if (text === 'android') return '安卓';
    if (text === 'browser') return '网页版';
    if (text === 'windows') return 'Windows';
    if (text === 'linux') return 'Linux';
    if (text === 'darwin' || text === 'macos') return 'macOS';
    return platform || '';
  }

  function renderDevices() {
    // The local machine is served first by /api/state: "send it to this
    // computer" is the common case, so it must never be filtered away.
    var devices = store.devices;
    var phones = (store.knownClients || []).filter(function (c) { return c && c.deviceId !== store.selfId; });
    var signature = devices
      .map(function (d) { return d.id + '|' + d.name + '|' + d.trusted + '|' + d.address + '|' + d.online; })
      .join(',')
      + '#' + phones.map(function (c) {
        return c.key + '|' + c.online + '|' + Math.round(c.secondsAgo || 0);
      }).join(',')
      + '#' + store.selectedDeviceId;
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
      else tags.push(statusChip(device.online !== false, device.secondsAgo, false));
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
          h('span', { class: 'addr', text: (device.address || '未知地址') + ':' + (device.tcpPort || '?') }),
          h('span', { class: 'facts muted', text: deviceFacts(device) })
        )
      );
      list.appendChild(button);
    });
  }

  /** 手机行：并进同一个「设备列表」，只是它不可点（文件由电脑转交）。
   *
   *  以前手机单独占一张卡片，用户得在两个地方找设备；现在一处看全 ——
   *  谁在线、是安卓 App 还是网页版、地址是多少，一眼扫完。
   */
  function appendPhoneRows(list, phones) {
    phones.forEach(function (client) {
      var isApp = client.clientKind === 'app';
      list.appendChild(h('div', { class: 'device is-peer' },
        h('span', { class: 'icon', text: isApp ? '📱' : '🌐' }),
        h('span', { class: 'meta' },
          h('span', { class: 'name' }, h('span', { text: client.label || '手机' }),
            h('span', { class: 'tag ' + (isApp ? 'is-app' : 'is-web'), text: isApp ? '安卓 App' : '网页版' }),
            statusChip(client.online, client.secondsAgo, client.isLocal)),
          h('span', { class: 'addr', text: client.address || '未知地址' }),
          h('span', { class: 'facts muted', text: client.version ? ('韧传 ' + client.version) : '' })
        )
      ));
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
    // Spell out whose files these are.  The heading used to read 「已接收的文件」
    // which, on a phone, reads as "files I received" -- it is the opposite:
    // these live on the computer and are here to be downloaded.
    $('#receive-dir').textContent = '这些文件在电脑上；点「下载」就能取到手机里。';
    if (!store.files.length) {
      list.appendChild(h('li', { class: 'file-item' }, h('span', { class: 'fname muted', text: '电脑上还没有文件' })));
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

  function noteShareDownload(share) {
    // The browser handles the actual download (and writes it to the phone's
    // Downloads folder); all this can do is tell the user where it went.
    toast('开始下载：' + share.name + '（保存在手机的「下载」目录里）');
  }

  // ------------------------------------------------------------------ chat

  var emojiOpen = false;
  var recorder = { media: null, chunks: [], started: 0, timer: null, state: 'idle' };

  function chatUnread() {
    return (store.chat && store.chat.unread) || 0;
  }

  function openConversation(convId) {
    store.conversationId = convId || '';
    refreshChat();
  }

  function refreshChat() {
    var query = '/api/chat' + (store.conversationId
      ? '?conv=' + encodeURIComponent(store.conversationId) : '');
    return api(query).then(function (data) {
      store.chat = {
        selfId: data.selfId || (store.chat && store.chat.selfId) || '',
        conversations: data.conversations || [],
        unread: (data.conversations || []).reduce(function (sum, c) { return sum + (c.unread || 0); }, 0)
      };
      if (data.conversationId) store.conversationId = data.conversationId;
      store.messages = data.messages || [];
      renderChat();
    }).catch(function () { /* the connection badge already says it */ });
  }

  function renderChat() {
    var badge = $('#chat-badge');
    var unread = chatUnread();
    badge.hidden = unread === 0;
    badge.textContent = String(unread);

    var list = $('#conv-list');
    var conversations = (store.chat && store.chat.conversations) || [];
    clear(list);
    if (!conversations.length) {
      list.appendChild(h('li', { class: 'conv-item muted', text: '还没有会话。点右上角「和电脑聊天」。' }));
    }
    conversations.forEach(function (conv) {
      var active = conv.id === store.conversationId;
      list.appendChild(h('li', {
        class: 'conv-item' + (active ? ' is-active' : ''),
        onclick: function () { openConversation(conv.id); }
      },
        h('span', { class: 'conv-title', text: conv.title || conv.id.slice(0, 12) }),
        conv.unread ? h('span', { class: 'badge', text: String(conv.unread) }) : null,
        h('span', { class: 'conv-last muted', text: (conv.kind === 'group' ? '群 · ' : '') + (conv.lastText || '') })
      ));
    });

    var thread = $('#chat-thread');
    thread.hidden = !store.conversationId;
    if (!store.conversationId) return;
    var current = conversations.find(function (c) { return c.id === store.conversationId; }) || {};
    $('#chat-title').textContent = (current.id || '').indexOf('g:') === 0
      ? ('👥 ' + (current.title || '群聊'))
      : (current.title || '会话');
    // 规范显示成员：名字（系统 · 韧传 版本 · IP），和设备列表用同一套字段。
    var members = (current.members || []).map(function (id) {
      if (id === (store.chat.selfId || '')) return '我';
      var known = (store.knownClients || []).find(function (c) { return 'web:' + c.key === id; });
      if (known) {
        var parts = [known.clientKind === 'app' ? '安卓 App' : '网页版'];
        if (known.version) parts.push('韧传 ' + known.version);
        if (known.address) parts.push(known.address);
        return (known.label || '手机') + '（' + parts.join(' · ') + '）';
      }
      var device = (store.devices || []).find(function (d) { return d.id === id; });
      if (device) {
        var bits = [platformName(device.platform) || '电脑'];
        if (device.version) bits.push('韧传 ' + device.version);
        if (device.address) bits.push(device.address);
        return device.name + '（' + bits.join(' · ') + '）';
      }
      return id.slice(0, 8);
    });
    $('#chat-members').textContent = members.join(' · ');

    var box = $('#msg-list');
    clear(box);
    if (!store.messages.length) {
      box.appendChild(h('li', { class: 'msg-system', text: '还没有消息。' }));
    }
    store.messages.forEach(function (message) {
      box.appendChild(messageNode(message));
    });
    box.scrollTop = box.scrollHeight;
  }

  function messageNode(message) {
    var mine = message.direction === 'out';
    var body = [];
    if (message.kind === 'text' || message.kind === 'system') {
      body.push(h('span', { class: 'msg-text', text: message.text || '' }));
    } else {
      body.push(attachmentNode(message));
    }
    return h('li', { class: 'msg' + (mine ? ' is-mine' : '') },
      h('div', { class: 'bubble' },
        h('span', { class: 'msg-meta muted',
          text: (message.senderName || (mine ? '我' : '对方')) + ' · ' + formatClock(message.ts) }),
        appendNodes(h('div', {}), body),
        message.state === 'failed' ? h('span', { class: 'state-err', text: '发送失败（对方不在线）' }) : null
      )
    );
  }

  // Not named ``append``: the page already has one, and in JavaScript the last
  // declaration in a scope wins -- shadowing it broke renderSelf(), which broke
  // the whole render loop (the send tab's upload card silently stopped updating,
  // and the Android CI job caught it).
  function appendNodes(node, children) {
    children.forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function attachmentNode(message) {
    var url = '/api/chat/media/' + encodeURIComponent(message.id);
    var name = message.mediaName || '附件';
    // 附件一律给一个下载链接：能看的东西也要能存下来（用户明确要的）。
    function downloadLink(label) {
      return h('a', { class: 'msg-save', href: url, download: name, text: '⬇ ' + label });
    }
    if (message.kind === 'image') {
      return h('div', { class: 'msg-media' },
        h('a', { href: url, target: '_blank', rel: 'noopener' },
          h('img', { class: 'msg-image', src: url, alt: name, loading: 'lazy' })),
        h('span', { class: 'msg-actions' }, downloadLink('保存图片')),
      );
    }
    if (message.kind === 'video') {
      return h('div', { class: 'msg-media' },
        h('video', { class: 'msg-video', src: url, controls: 'controls', preload: 'metadata' }),
        h('span', { class: 'msg-actions' }, downloadLink('保存视频')),
      );
    }
    if (message.kind === 'voice') {
      var seconds = message.durationMs ? Math.round(message.durationMs / 1000) : 0;
      return h('div', { class: 'msg-voice' },
        h('audio', { src: url, controls: 'controls', preload: 'metadata' }),
        h('span', { class: 'muted', text: '语音' + (seconds ? ' · ' + seconds + ' 秒' : '') }),
        downloadLink('保存语音')
      );
    }
    return h('a', { class: 'msg-file', href: url, download: name },
      h('span', { text: '📄 ' + name }),
      h('span', { class: 'muted', text: ' ' + fmtBytes(message.mediaSize || 0) })
    );
  }

  // Likewise: the page already formats times; keep a distinct name.
  function formatClock(ts) {
    if (!ts) return '';
    var date = new Date(ts * 1000);
    return ('0' + date.getHours()).slice(-2) + ':' + ('0' + date.getMinutes()).slice(-2);
  }

  function onChatFile(event) {
    var file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    var kind = guessKind(file);
    toast('正在发送 ' + file.name + '…');
    uploadAttachment(file, kind, 0).then(refreshChat).catch(function (error) {
      toast('发送失败：' + error.message, 'error');
    });
  }

  function sendText() {
    var input = $('#chat-input');
    var text = input.value.trim();
    if (!text || !store.conversationId) return;
    input.value = '';
    postChat('/api/chat/send', { conv: store.conversationId, text: text })
      .then(refreshChat)
      .catch(function (error) { toast('发送失败：' + error.message, 'error'); });
  }

  function postChat(path, payload) {
    return api(path, { method: 'POST', json: payload }).then(function (data) {
      if (!data || !data.ok) throw new Error((data && data.error) || '服务器拒绝了');
      return data;
    });
  }

  function uploadAttachment(file, kind, durationMs) {
    if (!store.conversationId) return;
    var query = '/api/chat/upload?name=' + encodeURIComponent(file.name || ('voice-' + Date.now() + '.webm')) +
      '&conv=' + encodeURIComponent(store.conversationId) +
      '&kind=' + encodeURIComponent(kind || 'file') +
      '&duration=' + encodeURIComponent(durationMs || 0);
    return fetch(query, {
      method: 'POST',
      headers: Object.assign(
        { 'X-EverSend-Token': TOKEN, 'Content-Type': file.type || 'application/octet-stream' },
        deviceHeaders(),
      ),
      body: file,
      credentials: 'same-origin'
    }).then(function (response) {
      return response.text().then(function (text) {
        var data = null;
        try { data = JSON.parse(text); } catch (err) { data = null; }
        if (!response.ok || !data || !data.ok) {
          throw new Error((data && data.error) || ('HTTP ' + response.status));
        }
        return data;
      });
    });
  }

  function guessKind(file) {
    var type = (file.type || '').toLowerCase();
    var name = (file.name || '').toLowerCase();
    if (type.indexOf('image/') === 0 || /\.(png|jpe?g|gif|webp|heic|heif|bmp)$/.test(name)) return 'image';
    if (type.indexOf('video/') === 0 || /\.(mp4|mov|mkv|webm|3gp|avi)$/.test(name)) return 'video';
    if (type.indexOf('audio/') === 0 || /\.(m4a|aac|opus|ogg|mp3|wav|amr|weba)$/.test(name)) return 'voice';
    return 'file';
  }

  /** 表情面板：整份表情表（三端共用，来自 /api/emoji），分组、可上下滑动。
   *
   *  以前是写死在页面里的 48 个，用户说太少。现在拉 /api/emoji（服务端从
   *  core/emoji.py 生成），分组渲染在一个可滚动的容器里，往下滑就是更多。
   */
  var emojiGroups = null;

  function insertEmoji(emoji) {
    var input = $('#chat-input');
    input.value += emoji;
    input.focus();
  }

  function renderEmojiGroups(pad, groups) {
    clear(pad);
    groups.forEach(function (group) {
      pad.appendChild(h('div', { class: 'emoji-title', text: group.title + '（' + group.emoji.length + '）' }));
      var grid = h('div', { class: 'emoji-grid' });
      group.emoji.forEach(function (emoji) {
        grid.appendChild(h('button', {
          type: 'button', text: emoji, title: emoji,
          onclick: function () { insertEmoji(emoji); }
        }));
      });
      pad.appendChild(grid);
    });
  }

  function toggleEmoji() {
    var pad = $('#emoji-pad');
    emojiOpen = !emojiOpen;
    pad.hidden = !emojiOpen;
    if (!emojiOpen) return;
    if (emojiGroups) {
      if (!pad.childElementCount) renderEmojiGroups(pad, emojiGroups);
      return;
    }
    // 先用内置的一小组顶上，接口回来再换成整份表（离线也不至于没有表情）。
    if (!pad.childElementCount) {
      renderEmojiGroups(pad, [{ title: '常用', emoji: [
        '😀','😂','🥹','😊','😍','😘','🤔','😴','😎','🤩','😭','😅','🙃','😇','🥳','🤝',
        '👍','👎','👌','🙏','👏','💪','🤙','✌️','❤️','💔','🔥','✨','🎉','🎁','⭐','💡'] }]);
    }
    fetch('/api/emoji', { headers: { 'Accept': 'application/json' } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        emojiGroups = (data && data.groups) || [];
        if (emojiGroups.length) renderEmojiGroups(pad, emojiGroups);
      })
      .catch(function () { /* 拉不到就用内置的那一小份 */ });
  }

  // --- 语音消息：浏览器只在安全上下文里给麦克风 --------------------------

  function secureContext() {
    return window.isSecureContext === true ||
      location.protocol === 'https:' || location.hostname === 'localhost' ||
      location.hostname === '127.0.0.1';
  }

  function canRecord() {
    return secureContext() && !!navigator.mediaDevices && !!navigator.mediaDevices.getUserMedia &&
      typeof MediaRecorder !== 'undefined' && !!store.conversationId;
  }

  /** 录音状态机：idle → starting → recording → sending → idle。
   *
   *  以前只看 MediaRecorder 自己的 state：getUserMedia 还没回来时按钮是灰的、
   *  再点一下又去开第二个录音器，于是界面卡在"正在录音…"而语音根本没发出去。
   *  现在以自己的状态为准，并且**每条路径都会把提示清掉**。
   */
  function setVoiceHint(text) {
    var hint = $('#voice-hint');
    if (!hint) return;
    hint.hidden = !text;
    hint.textContent = text || '';
    $('#btn-voice').classList.toggle('is-recording', recorder.state === 'recording' || recorder.state === 'starting');
  }

  function startRecording() {
    if (!canRecord()) {
      toast('这个地址不能录音：手机浏览器只在 HTTPS 页面里给麦克风。请用「设置」里那个 https:// 地址打开本页。', 'error');
      return;
    }
    if (recorder.state !== 'idle') return;
    recorder.state = 'starting';
    setVoiceHint('正在准备麦克风…');
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      if (recorder.state !== 'starting') {           // 用户在等待期间又点了一下
        stream.getTracks().forEach(function (track) { track.stop(); });
        recorder.state = 'idle';
        setVoiceHint('');
        return;
      }
      recorder.media = new MediaRecorder(stream);
      recorder.chunks = [];
      recorder.started = Date.now();
      recorder.media.ondataavailable = function (event) {
        if (event.data && event.data.size) recorder.chunks.push(event.data);
      };
      recorder.media.onstop = function () {
        stream.getTracks().forEach(function (track) { track.stop(); });
        var seconds = (Date.now() - recorder.started) / 1000;
        var type = recorder.media.mimeType || 'audio/webm';
        recorder.media = null;
        recorder.state = 'idle';
        if (seconds < 0.6 || !recorder.chunks.length) {
          setVoiceHint('');
          toast('太短了，没发出去');
          return;
        }
        var blob = new Blob(recorder.chunks, { type: type });
        var ext = type.indexOf('mp4') >= 0 ? 'm4a' : 'webm';
        var file = new File([blob], 'voice-' + Date.now() + '.' + ext, { type: blob.type });
        recorder.state = 'sending';
        setVoiceHint('正在发送语音…（' + seconds.toFixed(1) + ' 秒）');
        uploadAttachment(file, 'voice', Math.round(seconds * 1000))
          .then(function () { refreshChat(); })
          .catch(function (error) { toast('语音发送失败：' + error.message, 'error'); })
          .then(function () {                        // 成功失败都要收尾
            recorder.state = 'idle';
            setVoiceHint('');
          });
      };
      recorder.media.start();
      recorder.state = 'recording';
      setVoiceHint('正在录音…再按一次 🎤 结束并发送');
    }).catch(function (error) {
      recorder.state = 'idle';
      setVoiceHint('');
      toast('拿不到麦克风：' + error.message, 'error');
    });
  }

  function stopRecording() {
    // 'starting'（麦克风还没准备好）时点一下，就当作"别录了"。
    if (recorder.state === 'starting') {
      recorder.state = 'idle';
      setVoiceHint('');
      return;
    }
    if (recorder.media && recorder.media.state !== 'inactive') {
      setVoiceHint('正在结束录音…');
      recorder.media.stop();          // onstop 负责发送与清提示
      return;
    }
    recorder.state = 'idle';
    setVoiceHint('');
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
        // Say plainly whether this phone already took it.  The download itself
        // goes straight to the phone's Downloads folder, which a web page
        // cannot look at -- so this marker is the only "received" record the
        // page can honestly show.
        share.downloaded
          ? h('span', { class: 'state-ok', text: '✔ 已下载' })
          : null,
        h('a', {
          href: '/api/share/' + encodeURIComponent(share.id),
          download: share.name,
          text: share.downloaded ? '再下载' : '下载',
          onclick: function () { noteShareDownload(share); }
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

  /** Show the installer card only when this computer actually has the file.
   *
   *  It has its own change key rather than riding on renderSelf(): that one
   *  bails out as soon as the device info is unchanged, so a card that appears
   *  when the file is dropped into the folder would never show up.
   */
  function renderApk() {
    var card = $('#app-card');
    if (!card) return;
    var apk = (store.app && store.app.apk) || {};
    if (!changed('apk', JSON.stringify(apk))) return;
    if (!apk.available) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    var link = $('#apk-link');
    if (link && apk.url) link.setAttribute('href', apk.url);
    var label = $('#apk-size');
    if (label) {
      label.textContent = apk.name
        ? (apk.name + ' · ' + fmtBytes(apk.size))
        : fmtBytes(apk.size);
    }
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
    renderApk();
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
    store.knownClients = state.knownClients || [];
    store.shares = state.shares || [];
    store.chat = state.chat || store.chat;
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

  // ------------------------------------------------------- 连接保持 / 断开

  // A silent, looping audio element is the only thing a *web page* can do to
  // stay alive when the phone's screen goes off: Chrome freezes a background
  // tab, but a tab that is playing media keeps running (that is how web music
  // players survive).  It costs a little battery, so it is opt-in and the
  // switch says so.
  var SILENT_WAV =
    'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=';

  var keepalive = { audio: null, wakeLock: null, manual: false };
  var disconnected = false;

  function keepaliveSupported() {
    return typeof Audio !== 'undefined';
  }

  function startKeepalive() {
    if (keepalive.audio || !keepaliveSupported()) return;
    try {
      keepalive.audio = new Audio(SILENT_WAV);
      keepalive.audio.loop = true;
      keepalive.audio.volume = 0.001;   // not silent-silent: some systems drop audio at 0
      var played = keepalive.audio.play();
      if (played && played.catch) {
        played.catch(function () {
          // Autoplay policy: it needs a user gesture, which the switch click is.
          keepalive.audio = null;
          setKeepaliveState('浏览器拒绝了后台播放，熄屏后可能仍会断开。');
        });
      }
    } catch (err) {
      keepalive.audio = null;
    }
    // Screen Wake Lock would be better (no battery cost from audio) but it is
    // only available in a secure context, and this page is plain http:// on a
    // LAN address -- so on a phone it is almost never there.
    if (navigator.wakeLock && navigator.wakeLock.request) {
      navigator.wakeLock.request('screen').then(function (lock) {
        keepalive.wakeLock = lock;
      }).catch(function () { /* not available; the audio is the fallback */ });
    }
    setKeepaliveState('已开启：页面会尽量留在后台（有声音图标是正常的，那是无声音频）。');
  }

  function stopKeepalive() {
    if (keepalive.audio) {
      try { keepalive.audio.pause(); } catch (err) { /* already gone */ }
      keepalive.audio = null;
    }
    if (keepalive.wakeLock) {
      try { keepalive.wakeLock.release(); } catch (err) { /* already released */ }
      keepalive.wakeLock = null;
    }
  }

  function setKeepaliveState(text) {
    var node = $('#keepalive-state');
    if (node) node.textContent = text || '';
  }

  function disconnect() {
    // Tell the desktop first (so its device list updates at once), then stop
    // talking to it: close the stream, stop the timers.
    api('/api/leave', { method: 'POST', json: {} }).catch(function () { /* best effort */ });
    closeEvents();
    disconnected = true;
    setConnected('down');
    setKeepaliveState('');
    toast('已断开。点「重新连接」可以再连上。');
    renderAll();
  }

  function reconnect() {
    disconnected = false;
    setConnected('poll');
    refreshState().then(refreshFiles).catch(function () { /* the badge shows it */ });
    connectEvents();
    toast('正在重新连接…');
  }

  // ---------------------------------------------------------------- events

  var SSE_KINDS_REFRESH = {
    transfer_started: 1, transfer_finished: 1, transfer_cancelled: 1, transfer_accepted: 1,
    transfer_rejected: 1, file_done: 1, file_failed: 1, offer_received: 1,
    device_found: 1, device_updated: 1, engine_started: 1, web_upload_failed: 1,
    chat_message: 1, chat_sent: 1
  };

  var eventSource = null;

  function closeEvents() {
    if (eventSource) {
      try { eventSource.close(); } catch (err) { /* already closed */ }
      eventSource = null;
    }
  }

  function connectEvents() {
    if (!window.EventSource) { setConnected('poll'); return; }
    closeEvents();
    var source = new EventSource('/api/events');
    eventSource = source;
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
      case 'chat_message':
        var chat = event.message || {};
        toast('💬 ' + (chat.senderName || '对方') + '：' +
          (chat.text || { image: '[图片]', video: '[视频]', voice: '[语音]', file: '[文件]' }[chat.kind] || '新消息'));
        break;
      case 'warning':
        toast(event.message || '警告', 'error');
        break;
    }
    if (SSE_KINDS_REFRESH[kind]) {
      samples.delete(event.transfer_id);
      scheduleRefresh(kind === 'send_progress' ? 1000 : 250);
    }
    if (kind === 'chat_message' || kind === 'chat_sent') refreshChat();
  }

  // ---------------------------------------------------------------- upload

  var uploader = {
    queue: [],
    active: null,
    loaded: 0,
    done: [],
    //: The file whose upload died mid-flight (usually a screen-off freeze).
    interrupted: null
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
    request.setRequestHeader('X-EverSend-Device', deviceId());
    request.setRequestHeader('X-EverSend-Name', deviceName());
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
      // Almost always the phone's screen went off and the system froze the
      // page mid-upload.  Keep the file at the head of the queue so it resumes
      // when the user comes back, instead of losing the upload silently.
      uploader.interrupted = file;
      uploader.done.push({ name: file.name, ok: false, error: '网络中断（熄屏？）' });
      toast('上传中断：' + file.name + '——回到页面会自动重试', 'error');
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

  function retryInterrupted() {
    // Called when the page comes back to the foreground (and when the network
    // returns): an upload that died while the phone was asleep is put back in
    // the queue.  The server resumes from the byte count it already has.
    var file = uploader.interrupted;
    if (!file) return;
    uploader.interrupted = null;
    uploader.queue.unshift(file);
    renderUpload();
    // A retry starts the upload over (the spool file is written fresh), so say
    // 重试 rather than 继续 -- promising a resume we do not do would be worse
    // than the extra wait.
    toast('重试上传：' + file.name);
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
    ['send', 'receive', 'chat', 'about'].forEach(function (name) {
      var section = $('#view-' + name);
      if (section) section.hidden = name !== view;
      var tab = document.querySelector('.tab[data-view="' + name + '"]');
      if (tab) {
        tab.classList.toggle('is-active', name === view);
        tab.setAttribute('aria-selected', name === view ? 'true' : 'false');
      }
    });
    if (view === 'about') loadQr();
    if (view === 'chat') refreshChat();
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

    // ---- chat
    $('#btn-chat-send').addEventListener('click', sendText);
    $('#chat-input').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); sendText(); }
    });
    $('#btn-emoji').addEventListener('click', toggleEmoji);
    $('#btn-chat-back').addEventListener('click', function () {
      store.conversationId = '';
      refreshChat();
    });
    $('#btn-new-chat').addEventListener('click', function () {
      // A phone's only protocol-free partner is the computer serving this page,
      // so "new chat" is just the 1:1 with it.
      api('/api/chat/send', { method: 'POST', json: { text: '你好' } })
        .then(function () { store.conversationId = ''; refreshChat(); })
        .catch(function (error) { toast('建立会话失败：' + error.message, 'error'); });
    });
    $('#btn-attach').addEventListener('click', function () { $('#chat-file').click(); });
    $('#chat-camera') && $('#chat-camera').addEventListener('change', onChatFile);
    $('#chat-file').addEventListener('change', onChatFile);
    $('#btn-voice').addEventListener('click', function () {
      // 自己的状态说了算：不看 MediaRecorder 的内部状态（它可能还没 start）。
      if (recorder.state === 'recording' || recorder.state === 'starting') stopRecording();
      else if (recorder.state === 'idle') startRecording();
    });
    $('#pin').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') pump();
    });
    window.addEventListener('online', function () { scheduleRefresh(0); retryInterrupted(); });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden || disconnected) return;
      // Back from a locked screen: reconnect *now* rather than waiting for the
      // next poll, so the desktop sees the phone again within a second or two.
      scheduleRefresh(0);
      refreshFiles();
      if (!eventSource || eventSource.readyState === 2) connectEvents();
      retryInterrupted();
    });
    var keepaliveBox = $('#keepalive');
    if (keepaliveBox) {
      keepaliveBox.checked = localStorage.getItem('eversend-keepalive') === '1';
      if (keepaliveBox.checked) startKeepalive();
      keepaliveBox.addEventListener('change', function () {
        if (keepaliveBox.checked) {
          localStorage.setItem('eversend-keepalive', '1');
          startKeepalive();
        } else {
          localStorage.setItem('eversend-keepalive', '0');
          stopKeepalive();
          setKeepaliveState('已关闭：手机熄屏后连接会断开（文件不会丢，回来再传即可）。');
        }
      });
    }
    var disconnectButton = $('#btn-disconnect');
    if (disconnectButton) {
      disconnectButton.addEventListener('click', function () {
        if (disconnected) { reconnect(); disconnectButton.textContent = '断开与电脑的连接'; return; }
        disconnect();
        disconnectButton.textContent = '重新连接';
      });
    }
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
      if (disconnected) return;
      refreshState().catch(function () { /* the connection badge says it all */ });
    }, POLL_MS);
    setInterval(paint, TICK_MS);
    setInterval(refreshFiles, 15000);
    setInterval(function () { if (!disconnected) refreshChat(); }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
