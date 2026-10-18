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
 *   6. wiring             sidebar, pages, drag & drop, buttons
 *
 * The layout is the one in docs/UI_LAYOUT.md: a fixed sidebar on the left
 * (a drawer at ≤720px) and a three-part main column -- peer header, body,
 * composer.  The body swaps between the session page, the transfers page
 * (with its own 接收 / 发送 tables) and the settings page; nothing navigates
 * away from the page itself.
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
  // Divisor is 1024 => IEC unit labels (KiB/MiB/GiB…).  Labelling 1024-based
  // sizes as KB/MB under-reports every number by 4.6%; all three front ends
  // (desktop Qt, this page, Android) must speak this same vocabulary.
  function fmtBytes(value) {
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    var size = Number(value) || 0;
    for (var i = 0; i < units.length; i++) {
      if (Math.abs(size) < 1024 || i === units.length - 1) {
        return units[i] === 'B' ? Math.round(size) + ' B' : size.toFixed(2) + ' ' + units[i];
      }
      size /= 1024;
    }
    return size.toFixed(2) + ' PiB';
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

  // HTTP 头部只收 ISO-8859-1：设备名里有「电脑浏览器」「安卓手机」这种中文
  // 时，fetch() 会直接抛 "String contains non ISO-8859-1 code point" ——
  // 一个头部就等于整页报废（页面上只剩「无法连接服务器」）。所以名字按
  // URL 编码后再放进去，纯 ASCII 的名字编码后跟原样一样，不影响老客户端。
  function encodeHeader(value) {
    try { return encodeURIComponent(value); } catch (error) { return 'browser'; }
  }

  function deviceHeaders() {
    return { 'X-EverSend-Device': deviceId(), 'X-EverSend-Name': encodeHeader(deviceName()) };
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
    // 传输页的筛选药丸（全部 / 进行中 / 已完成 / 失败）与搜索词（§3.2）。
    filter: 'all',
    search: '',
    // 侧栏设备搜索词（设备 > 6 台时那个搜索框）。
    deviceSearch: '',
    // 侧栏设备列表的两段是不是展开的：在线默认展开，离线默认折叠
    // （docs/UI_LAYOUT.md §2）。
    devSections: { online: true, offline: false },
    shares: [],
    chat: { selfId: '', conversations: [], unread: 0 },
    conversationId: '',
    messages: [],
    // 等着发出去的文件（输入框上面那一行的方块）；点「发送」才真的开始传。
    picked: [],
    // 主区现在显示哪一页：会话 / 传输 / 设置。
    page: 'session',
    // 传输页里选中的那张表：接收 / 发送。
    transferKind: 'receive',
    // 打开的群聊 id。不为空时主区是群聊，而不是侧栏选中的设备。
    groupId: '',
    // 选中的设备没有聊天通道（只能传文件）时为 true。
    chatUnavailable: false
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

  /** 名字后面跟「系统 · 韧传 版本 · 发现方式」，规范到哪儿都一样。 */
  function deviceFacts(device) {
    var parts = [];
    if (device && device.isClient) {
      // 手机行：类型（安卓 App / 网页版）写在这一行，行内只留状态徽标 ——
      // 288px 的侧栏里塞不下"名字 + 三个徽标"，名字会被截成「An…」。
      parts.push(device.clientKind === 'app' ? '安卓 App' : '网页版');
    } else if (device && device.platform) {
      parts.push(platformName(device.platform));
    }
    if (device.version) parts.push('韧传 ' + device.version);
    var source = sourceName(device);
    if (source) parts.push(source);
    return parts.join(' · ');
  }

  /** 对方是怎么被发现的：组播 / 网页客户端 / 手动 / 本机。 */
  function sourceName(device) {
    if (!device) return '';
    if (device.local) return '本机';
    var source = String(device.source || '').toLowerCase();
    if (source === 'mdns') return '组播 · mDNS';
    if (source === 'broadcast' || source === 'udp' || source === 'announce') return '组播 · 广播';
    if (source === 'manual') return '手动添加';
    if (source === 'web' || source === 'app') return '网页客户端';
    // 协议里对端主动连过来时 source 是 incoming，别把英文原样摆到界面上。
    if (source === 'incoming') return '对方连过来';
    if (source === 'probe' || source === 'scan') return '扫描到的';
    return source ? '局域网' : '';
  }

  /** 地址 + 端口：设备列表和会话顶部都用这一句。 */
  function deviceAddress(device) {
    if (!device) return '未知地址';
    return (device.address || '未知地址') + ':' + (device.tcpPort || '?');
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

  /** 一台设备算不算「在线」。
   *
   *  /api/state 的 devices 里已经带了 online（发现表里的设备服务端写死 True，
   *  记下来的手机客户端带真实值），所以判据就是 online !== false。唯一的例外是
   *  只有历史、没有实时状态的条目（source === 'history' 这种，连 online 字段
   *  都没有）—— 它们只是"上次见过"，按离线算。
   */
  function deviceOnline(device) {
    if (!device) return false;
    if (String(device.source || '') === 'history') return false;
    return device.online !== false;
  }

  // ------------------------------------------------- 一台设备只有一行（T3）
  //
  // 服务端（web/server.py 的 identity_key）给每条客户端记录一个稳定身份
  // `identityKey`：有设备号就是 `web:<设备号>` / `android:<设备号>`（跟地址无关，
  // 换网络、换 IP 都不变），确实没有设备号的老式浏览器只能退回它自己的
  // `地址|UA摘要`。判定规则是"identityKey 相同 = 同一台物理设备"。
  //
  // 页面这边还兜一层：同一台设备的两条记录（换过网络的旧记录、App 与它自己的
  // 浏览器、手工改过的 web_clients.json）在界面上必须只有一行，三个计数
  // （设备列表 N 台 / 在线 N / 离线 N）都按**去重后的行数**算。
  //
  // 前缀（web: / android:）只说明"谁在说话"：同一台手机的两种写法指的是同一台，
  // 所以比对身份时把前缀去掉；记录上被归并掉的旧身份（`aliases`，例如
  // `127.0.0.1|67f382af`）也算同一台。

  /** `web:<设备号>` / `android:<设备号>` → `<设备号>`；其它写法原样返回。 */
  function bareIdentity(value) {
    var text = String(value === null || value === undefined ? '' : value).trim();
    var cut = text.indexOf(':');
    if (cut > 0) {
      var head = text.slice(0, cut).toLowerCase();
      if (head === 'web' || head === 'android') return text.slice(cut + 1);
    }
    return text;
  }

  /** 一条记录（协议对端或客户端）身上所有"指同一台设备"的写法。 */
  function identityVariants(record) {
    var out = [];
    function add(value) {
      var text = bareIdentity(value);
      if (text && out.indexOf(text) < 0) out.push(text);
    }
    if (!record) return out;
    add(record.identityKey);
    add(record.deviceId);
    add(record.key);
    add(record.id);
    var aliases = record.aliases;
    if (Object.prototype.toString.call(aliases) === '[object Array]') aliases.forEach(add);
    return out;
  }

  /** 这条客户端记录算不算"一台手机"。
   *
   *  和服务端的 is_mobile_client 同一套判据：名字里写着 Android / iPhone / iPad
   *  的永远算（手机经 adb reverse 过来时地址就是 127.0.0.1）；其余回环地址上的
   *  是**给你这个页面的那台电脑的浏览器** —— 把它列进设备列表，就是把"看页面
   *  的自己"当成另一台设备（用户看到的重复正是这么来的）。
   */
  function isPhoneClient(client) {
    if (!client) return false;
    if (/android|iphone|ipad/i.test(String(client.label || ''))) return true;
    return !client.isLocal;
  }

  /** 这一页自己的客户端记录不用列（设备列表列的是"别的设备"）。 */
  function isPageItself(client) {
    var mine = deviceId();
    var id = String(client.deviceId || '');
    if (id && id === mine) return true;
    var key = String(client.key || '');
    return key === 'web:' + mine || key === 'android:' + mine;
  }

  /** 协议对端（电脑）→ 侧栏的一行。 */
  function peerRow(device) {
    return {
      id: String(device.id || ''),
      name: String(device.name || ''),
      kind: device.kind || '',
      platform: device.platform || '',
      version: device.version || '',
      address: device.address || '',
      tcpPort: device.tcpPort,
      online: deviceOnline(device),
      secondsAgo: typeof device.secondsAgo === 'number' ? device.secondsAgo : Infinity,
      trusted: !!device.trusted,
      local: !!device.local,
      source: device.source || '',
      webUrl: device.webUrl || '',
      isClient: false,
      record: device
    };
  }

  /** 配对过的手机（knownClients）→ 侧栏的一行。
   *
   *  手机行**不可点**：它不走文件协议，发给手机是由电脑转交的
   *  （docs/UI_LAYOUT.md §2，网页版一直是这个约定）。所以只展示、不选中。
   */
  function clientRow(client) {
    var isApp = String(client.clientKind || '') === 'app';
    var id = String(client.identityKey || client.key || '').trim();
    if (!id && client.deviceId) id = (isApp ? 'android:' : 'web:') + client.deviceId;
    return {
      id: id,
      name: String(client.label || client.name || (isApp ? '安卓 App' : '网页客户端')),
      kind: isApp ? 'mobile' : 'browser',
      clientKind: isApp ? 'app' : 'browser',
      platform: isApp ? 'android' : 'browser',
      version: client.version || '',
      address: client.address || '',
      tcpPort: 0,
      online: deviceOnline(client),
      secondsAgo: typeof client.secondsAgo === 'number' ? client.secondsAgo : Infinity,
      trusted: true,
      local: false,
      source: '',
      webUrl: '',
      isClient: true,
      record: client
    };
  }

  /** 按稳定身份把行分组：写法有交集的记录连成一组（并查集）。 */
  function groupByIdentity(rows) {
    var parent = rows.map(function (_row, index) { return index; });
    function find(index) {
      var root = index;
      while (parent[root] !== root) root = parent[root];
      while (parent[index] !== root) { var next = parent[index]; parent[index] = root; index = next; }
      return root;
    }
    var owner = {};
    rows.forEach(function (row, index) {
      identityVariants(row.record).forEach(function (name) {
        if (Object.prototype.hasOwnProperty.call(owner, name)) {
          var a = find(owner[name]);
          var b = find(index);
          if (a !== b) parent[b] = a;
        } else {
          owner[name] = index;
        }
      });
    });
    var groups = [];
    var seat = {};
    rows.forEach(function (row, index) {
      var root = find(index);
      if (!Object.prototype.hasOwnProperty.call(seat, root)) {
        seat[root] = groups.length;
        groups.push([]);
      }
      groups[seat[root]].push(row);
    });
    return groups;
  }

  /** 一组记录（同一台设备）→ 一行。
   *
   *  规则（与服务端/桌面端一致）：名字取非空、地址取**最新那条**的、`online`
   *  取"任一条在线"；能收发的协议对端优先当这一行的身份 —— 手机行点了也发不出去。
   */
  function mergeRows(group) {
    if (group.length === 1) return group[0];
    var newest = group.slice().sort(function (a, b) { return a.secondsAgo - b.secondsAgo; });
    var base = newest.find(function (row) { return !row.isClient; }) || newest[0];
    var live = newest.find(function (row) { return row.online; });
    var fresh = live || newest[0];
    var merged = Object.assign({}, base);
    var name = '';
    newest.forEach(function (row) { if (!name && row.name) name = row.name; });
    merged.name = name || base.name;
    merged.address = fresh.address || base.address;
    merged.online = group.some(function (row) { return row.online; });
    merged.secondsAgo = merged.online
      ? (live ? live.secondsAgo : 0)
      : Math.min.apply(null, group.map(function (row) { return row.secondsAgo; }));
    merged.trusted = group.some(function (row) { return row.trusted; });
    merged.local = group.some(function (row) { return row.local; });
    merged.records = group.map(function (row) { return row.record; });
    return merged;
  }

  /** 侧栏「设备列表」要画的每一行：协议对端 + 配对过的手机，已按稳定身份去重。
   *
   *  服务端已经保证同一台设备在 devices / knownClients 里各只有一条（T1）；
   *  这里再兜一层，页面自己也不会把一台设备画成两行。
   */
  function deviceRows() {
    var rows = [];
    (store.devices || []).forEach(function (device) { rows.push(peerRow(device)); });
    (store.knownClients || []).forEach(function (client) {
      if (!isPhoneClient(client) || isPageItself(client)) return;
      rows.push(clientRow(client));
    });
    return groupByIdentity(rows).map(mergeRows);
  }

  // 上一次"因为选中而自动展开"的是哪一段（键是 设备 id + 段名）：只在选中项
  // 换了段的那一次自动展开，用户手动折叠之后不会再被弹开。
  var revealedFor = '';

  /** 列表里重名的行要在标题上分得开：没有这一句，两行"Android 上的 Chrome"
   *  （两台不同的手机，或者同一台手机的 App 与它的浏览器）用户根本分不清。
   *  补地址，没有地址就补系统。 */
  function displayName(device, ambiguous) {
    var title = deviceLabel(device);
    if (!ambiguous) return title;
    var hint = device.address || platformName(device.platform);
    if (hint && title.indexOf(hint) < 0) title += ' · ' + hint;
    return title;
  }

  /** 每一行标题的出现次数：> 1 的行就是"重名"，画的时候要补区分信息。 */
  function nameCounts(rows) {
    var count = {};
    rows.forEach(function (row) {
      var label = deviceLabel(row);
      count[label] = (count[label] || 0) + 1;
    });
    return count;
  }

  /** 设备行：头像 / 名字 / 状态徽标 / 副标题（地址 · 端口 · 发现方式）。
   *  在线段和离线段用的是同一个函数，所以两段里的行长得一模一样。
   *
   *  `ambiguous`：列表里还有另一行同名 —— 标题上补一句能区分的（地址，没有地址
   *  就系统），否则两行一模一样，用户根本不知道点的是哪一台。
   */
  function deviceRow(device, ambiguous) {
    var selected = !device.isClient && device.id === store.selectedDeviceId;
    var tags = [];
    if (device.local) tags.push(h('span', { class: 'tag is-self', text: '本机' }));
    else tags.push(statusChip(deviceOnline(device), device.secondsAgo, false));
    // 配对过的手机一定是"已信任"的：这个徽标对手机行没有信息量，省下的宽度
    // 留给名字（见下面 client 分支的注释）。
    if (device.trusted && !device.local && !device.isClient) {
      tags.push(h('span', { class: 'tag is-trusted', text: '已信任' }));
    }
    var title = displayName(device, ambiguous);
    // 手机走不了文件协议（发给它是由电脑转交的），所以它不是"可点的目标"。
    // 行内只留状态徽标：名字 +「已连接 + 已信任 + 网页版」三个徽标在 288px 的
    // 侧栏里会把名字挤成「An…」，同名的那两行就还是分不清。类型（安卓 App /
    // 网页版）挪到下面的 facts 行 —— 同一个词，看得见才算数。
    if (device.isClient) {
      return h('li', {
        class: 'device is-peer' + (ambiguous ? ' is-same-name' : '')
      },
        h('span', { class: 'icon', text: platformIcon(device.platform, device.kind) }),
        h('span', { class: 'meta' },
          // 名字一行放不下就 CSS 省略号，**全名在 title 里**（§2.2 / C4）——
          // 悬停、长按都能看到完整名字，不靠猜。
          h('span', { class: 'name', title: title },
            h('span', { text: title, title: title }), tags),
          h('span', { class: 'addr', title: deviceSubtitle(device), text: deviceSubtitle(device) })
        )
      );
    }
    // 设备行是列表项 <li>：点它 = 选中并进入会话（键盘 Enter/Space 一样）。
    var row = h('li', {
      class: 'device' + (ambiguous ? ' is-same-name' : ''),
      role: 'button',
      tabindex: '0',
      'aria-pressed': selected ? 'true' : 'false',
      onclick: function () { selectDevice(device.id); },
      onkeydown: function (event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        selectDevice(device.id);
      }
    },
      h('span', { class: 'icon', text: platformIcon(device.platform, device.kind) }),
      h('span', { class: 'meta' },
        h('span', { class: 'name', title: title },
          h('span', { text: title, title: title }), tags),
        h('span', { class: 'addr', title: deviceSubtitle(device), text: deviceSubtitle(device) })
      )
    );
    bindDeviceMenu(row, device);
    return row;
  }

  /** 一段（在线 / 离线）的展开状态：标题的 ▾/▸、整段的显隐、空状态文案。 */
  function paintDeviceSection(head, list, empty, open, count) {
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    var caret = head.querySelector('.caret');
    if (caret) caret.textContent = open ? '▾' : '▸';
    list.hidden = !open;
    // 这一段是空的（标题上的数量写着 0）：展开了就给一句空状态文案。
    empty.hidden = !(open && count === 0);
  }

  /** 点一次段标题：整段展开 / 折叠（状态在 store.devSections 里）。 */
  function toggleDeviceSection(which) {
    store.devSections[which] = !store.devSections[which];
    // 重画一次：renderDevices 的签名里带着展开状态，所以这里会真的画。
    renderDevices();
  }

  /** 侧栏设备行：在线 / 离线**两段**，每段可展开折叠（docs/UI_LAYOUT.md §2）。 */
  function renderDevices() {
    // The local machine is served first by /api/state: "send it to this
    // computer" is the common case, so it must never be filtered away.
    // 本机没有 online 字段，deviceOnline 认它是在线（就在眼前）。
    //
    // rows 已经按稳定身份去过重（deviceRows）：同一台设备的多条记录在这里只剩
    // 一行，下面三个计数全部按**行数**算，徽标永远等于看得见的行数。
    var allRows = deviceRows();
    var searchBox = $('#device-search');
    // 设备 > 6 台才出现搜索框（§3.1）：三五行设备时它只是噪音。
    if (searchBox) searchBox.hidden = allRows.length <= 6;
    var needle = String(store.deviceSearch || '').trim().toLowerCase();
    var rows = allRows;
    if (needle) {
      rows = allRows.filter(function (row) {
        var hay = (deviceLabel(row) + ' ' + (row.address || '') + ' ' + platformName(row.platform) + ' ' +
          (row.facts || '')).toLowerCase();
        return hay.indexOf(needle) >= 0;
      });
    }
    var online = [];
    var offline = [];
    rows.forEach(function (row) {
      (row.online ? online : offline).push(row);
    });
    // 能选中的只有协议对端：手机行点了也发不出去（见 clientRow 的注释）。
    // 从**全部**行里找（搜索把某台设备藏起来时，选中项不该跟着丢）。
    var selectable = allRows.filter(function (row) { return !row.isClient; });

    if (!store.selectedDeviceId && selectable.length) {
      var preferred = selectable.find(function (d) { return d.local; }) ||
        selectable.find(function (d) { return d.trusted; }) || selectable[0];
      store.selectedDeviceId = preferred.id;
    }
    if (store.selectedDeviceId && !selectable.some(function (d) { return d.id === store.selectedDeviceId; })) {
      store.selectedDeviceId = selectable.length ? selectable[0].id : null;
    }

    // 选中的设备落在折叠着的那一段时，自动把那一段展开 —— 否则选中了却看不见。
    // 只在"选中项换了段"的那一次动手：用户自己手动折叠仍然说了算。
    var selectedId = store.selectedDeviceId;
    var selectedOnline = online.some(function (d) { return d.id === selectedId; });
    var selectedOffline = offline.some(function (d) { return d.id === selectedId; });
    var revealKey = selectedId ? selectedId + ':' + (selectedOnline ? 'online' : 'offline') : '';
    if (revealKey && revealKey !== revealedFor) {
      revealedFor = revealKey;
      if (selectedOnline) store.devSections.online = true;
      if (selectedOffline) store.devSections.offline = true;
    }

    // 同名（两台不同的设备真的重名）时标题上补地址 —— 见 deviceRow。
    var nameCount = nameCounts(rows);

    var signature = allRows
      .map(function (d) {
        return [d.id, d.name, d.trusted, d.address, d.online, d.source, d.secondsAgo,
          d.clientKind, d.isClient].join('|');
      })
      .join(',')
      + '#' + selectedId
      + '#' + needle
      + '#' + (store.devSections.online ? 'o1' : 'o0')
      + '#' + (store.devSections.offline ? 'f1' : 'f0');

    // 「清空」入口的可用状态：这一段能清的那份名单空着就没得清 —— 禁用（标题也
    // 换成一句说明），而不是让人点一下什么都没发生。放在签名判断之前：设备数变了
    // 但签名恰好没变（比如同一个 id 只是换了个字段顺序）时它也必须是对的。
    // 只数协议对端：`/api/devices/forget-offline` 清的是"以前见过"的电脑/手机
    // 记录，配对过的手机（客户端记录）不在那份名单里，不能算进去。
    var clearable = offline.filter(function (row) { return !row.isClient; }).length;
    var clearButton = $('#btn-clear-offline');
    if (clearButton) {
      clearButton.disabled = clearable === 0;
      clearButton.title = clearable
        ? '清掉「以前见过」的 ' + clearable + ' 台离线设备（聊天记录、配对关系保留）'
        : '现在没有离线的设备';
    }
    if (!changed('devices', signature)) return;

    var list = $('#device-list');
    var listOffline = $('#device-list-offline');
    var empty = $('#device-empty');
    clear(list);
    clear(listOffline);
    empty.hidden = rows.length > 0;

    online.forEach(function (device) {
      list.appendChild(deviceRow(device, nameCount[deviceLabel(device)] > 1));
    });
    offline.forEach(function (device) {
      listOffline.appendChild(deviceRow(device, nameCount[deviceLabel(device)] > 1));
    });

    $('#device-count').textContent = rows.length ? rows.length + ' 台' : '';
    $('#dev-online-count').textContent = String(online.length);
    $('#dev-offline-count').textContent = String(offline.length);
    paintDeviceSection($('#dev-online-head'), list, $('#online-empty'), store.devSections.online, online.length);
    paintDeviceSection($('#dev-offline-head'), listOffline, $('#offline-empty'), store.devSections.offline, offline.length);
  }

  /** 「离线」那一段里**能清空**的那些设备：以前见过、现在不在线的协议对端
   *  （判定同 renderDevices，手机行不算 —— 清空接口不动配对关系）。 */
  function offlineDevices() {
    return deviceRows().filter(function (row) { return !row.online && !row.isClient; });
  }

  /** 清空的结果写在「离线」标题下面那一行（状态行）—— 折叠着也看得见。
   *  传空字符串就收起来；失败走 isError（红字），不静默。 */
  function setOfflineNote(text, isError) {
    var node = $('#offline-note');
    if (!node) return;
    node.textContent = text || '';
    node.hidden = !text;
    node.classList.toggle('is-error', !!isError);
  }

  /** 清空「以前见过」的离线设备名单（侧栏「离线」标题行上的入口）。
   *
   *  先问一句，再说清代价：`POST /api/devices/forget-offline` 只清电脑记住的
   *  那份「以前见过」名单 —— 在线设备、聊天记录、文件和配对关系都不动
   *  （docs/UI_LAYOUT.md 的「离线列表可以清空」）。有聊天记录的设备行不会跟着
   *  消失：那种行是打开那段会话的唯一入口，所以结果里如实告诉用户还剩几台。
   */
  function clearOfflineDevices() {
    var before = offlineDevices().length;
    // 这一段空着的时候长按标题也会走到这里（那个小按钮此时是禁用的）：
    // 说明一句就完了 —— 不弹确认框、也不发请求。
    if (before === 0) {
      setOfflineNote('现在没有离线的设备，没有可以清空的名单。');
      toast('现在没有离线的设备');
      return;
    }
    var ask = '清空「离线」里的设备？\n\n'
      + '只清掉「以前见过、现在不在线」的这份名单（现在有 ' + before + ' 台）：'
      + '在线设备、聊天记录、文件和配对关系都保留。\n\n'
      + '有聊天记录的设备行不会跟着消失 —— 要清掉它得删掉那段会话。';
    if (!window.confirm(ask)) return;
    api('/api/devices/forget-offline', { method: 'POST' })
      .then(function (data) {
        var forgotten = Number((data && data.forgotten) || 0);
        var note = '清空了 ' + forgotten + ' 台离线设备（聊天记录保留）';
        setOfflineNote(note);
        toast(note);
        // 刷新一次 state：那几台从列表里消失，在线的照旧。
        return refreshState().then(function () {
          // 这一段里剩下的行数（含配对过的手机）—— 与「离线」徽标同一个口径。
          var left = deviceRows().filter(function (row) { return !row.online; }).length;
          if (left > 0) {
            setOfflineNote(note + '；这一段还剩 ' + left + ' 台来自聊天记录或以前的配对，删掉那段会话才会消失。');
          }
        }, function () {
          // 清空本身已经成功了：刷新失败由连接状态（已断开）和轮询去说，不谎报失败。
        });
      })
      .catch(function (error) {
        var message = '清空离线设备失败：' + (error && error.message ? error.message : '未知错误');
        setOfflineNote(message, true);
        toast(message, 'error');
      });
  }

  /** 「离线」标题行上的入口：右边的小按钮，外加整行右键 / 长按（650ms）。
   *
   *  手机没有右键，长按和 bindDeviceMenu / bindFileActions 一个做法；两种形态
   *  （窄屏抽屉、宽屏常驻侧栏）用的是同一个元素，所以只需要绑一次。
   */
  function bindOfflineClear(head, button) {
    button.addEventListener('click', function () { clearOfflineDevices(); });
    var timer = null;
    var longPressed = false;
    // 名字不能叫 stop：bindDeviceMenu 里已经有一个（selftest 查重名函数）。
    function cancelLongPress() {
      if (timer) { clearTimeout(timer); timer = null; }
    }
    head.addEventListener('contextmenu', function (event) {
      event.preventDefault();
      clearOfflineDevices();
    });
    head.addEventListener('touchstart', function () {
      cancelLongPress();
      longPressed = false;
      timer = setTimeout(function () {
        timer = null;
        longPressed = true;
        clearOfflineDevices();
        // 兜底：万一浏览器没有补那次 click（比如被当成滚动），别让它一直吞点击。
        setTimeout(function () { longPressed = false; }, 1500);
      }, 650);
    }, { passive: true });
    ['touchend', 'touchmove', 'touchcancel'].forEach(function (name) {
      head.addEventListener(name, cancelLongPress, { passive: true });
    });
    // 长按之后手指抬起，浏览器还会补一次 click —— 那一次不算（否则这一段会
    // 跟着展开/折叠一下）。在捕获阶段拦住：标题自己的展开/折叠照旧。
    head.parentNode.addEventListener('click', function (event) {
      if (!longPressed) return;
      longPressed = false;
      event.preventDefault();
      event.stopPropagation();
    }, true);
  }

  /** 长按（手机）或右键（电脑）= 信任开关。
   *
   *  接口里和"管一台设备"有关的动作只有 /api/trust —— 协议没有"忘记这台设备"，
   *  所以这里不摆一个按不动的菜单项。
   */
  function bindDeviceMenu(button, device) {
    var timer = null;
    function stop() {
      if (timer) { clearTimeout(timer); timer = null; }
    }
    button.addEventListener('contextmenu', function (event) {
      event.preventDefault();
      toggleTrust(device);
    });
    button.addEventListener('touchstart', function () {
      stop();
      timer = setTimeout(function () { timer = null; toggleTrust(device); }, 650);
    }, { passive: true });
    ['touchend', 'touchmove', 'touchcancel'].forEach(function (name) {
      button.addEventListener(name, stop, { passive: true });
    });
  }

  function toggleTrust(device) {
    var trusted = !device.trusted;
    var ask = trusted
      ? '信任「' + deviceLabel(device) + '」？以后它发文件过来就直接收，不再问。'
      : '取消对「' + deviceLabel(device) + '」的信任？以后它发文件过来还要你点一次。';
    if (!window.confirm(ask)) return;
    api('/api/trust', { method: 'POST', json: { deviceId: device.id, trusted: trusted } })
      .then(function () {
        toast(trusted ? '已信任 ' + deviceLabel(device) : '已取消信任 ' + deviceLabel(device));
        return refreshState();
      })
      .catch(function (error) { toast(error.message, 'error'); });
  }

  // 待发文件的缩略图：同一个文件只建一次 object URL，取消/发送之后再回收 ——
  // 不然选几轮文件就把手机的内存吃光了。
  var tileCache = new Map();

  function tileEntry(file) {
    var entry = tileCache.get(file);
    if (entry) return entry;
    var url = '';
    try { url = URL.createObjectURL(file); } catch (error) { url = ''; }
    entry = { url: url, poster: '' };
    tileCache.set(file, entry);
    return entry;
  }

  function pruneTileCache() {
    tileCache.forEach(function (entry, file) {
      if (store.picked.indexOf(file) >= 0) return;
      try { URL.revokeObjectURL(entry.url); } catch (error) { /* 已经回收了 */ }
      tileCache.delete(file);
    });
  }

  /** 视频封面帧：电脑端用 FFmpeg 抽一帧，浏览器里就 <video> + canvas 抽。
   *  抽到了换上去，抽不到（编码不认识、太大）就还是那个 🎬 图标。 */
  function tileVideoPoster(file, thumb, entry) {
    var probe = document.createElement('video');
    probe.muted = true;
    probe.playsInline = true;
    probe.preload = 'auto';
    probe.src = entry.url;
    var settled = false;
    function finish(dataUrl) {
      if (settled) return;
      settled = true;
      try { probe.removeAttribute('src'); probe.load(); } catch (error) { /* 收尾 */ }
      if (!dataUrl || !thumb.isConnected) {
        return;
      }
      entry.poster = dataUrl;
      clear(thumb);
      thumb.appendChild(h('img', { alt: '', src: dataUrl }));
      thumb.classList.add('has-frame');
    }
    function grab() {
      try {
        var width = probe.videoWidth || 320;
        var height = probe.videoHeight || 180;
        var scale = Math.min(1, 360 / Math.max(width, height));
        var canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        canvas.getContext('2d').drawImage(probe, 0, 0, canvas.width, canvas.height);
        finish(canvas.toDataURL('image/jpeg', 0.72));
      } catch (error) {
        finish('');
      }
    }
    probe.addEventListener('loadedmetadata', function () {
      // 往后挪一点点，别把片头的黑场当封面。
      var target = Math.min(1, (probe.duration || 0) * 0.1);
      if (target > 0) {
        try { probe.currentTime = target; } catch (error) { /* 抽不到就抽不到 */ }
      }
    });
    probe.addEventListener('seeked', grab);
    probe.addEventListener('loadeddata', function () {
      if (probe.currentTime === 0) grab();
    });
    probe.addEventListener('error', function () { finish(''); });
    setTimeout(function () { finish(''); }, 4000);
  }

  /** 一个待发文件 = 一块方块：上面是缩略图（图片真缩略图 / 视频封面帧 / 类型
   *  图标），下面是文件名；右上角那个 ✕ 是"不发送这个"（规格 §3⑥）。 */
  function pickedTile(file) {
    var named = previewKindFromName(file.name);
    var kind = named || (String(file.type || '').split('/')[0] || 'file');
    var entry = tileEntry(file);
    var thumb = h('span', { class: 'tile-thumb' });
    if (entry.url && named === 'image') {
      thumb.appendChild(h('img', { alt: '', src: entry.url, loading: 'lazy' }));
    } else if (entry.url && named === 'video') {
      if (entry.poster) {
        thumb.appendChild(h('img', { alt: '', src: entry.poster }));
        thumb.classList.add('has-frame');
      } else {
        thumb.appendChild(h('span', { class: 'tile-icon', 'aria-hidden': 'true', text: '🎬' }));
        tileVideoPoster(file, thumb, entry);   // 抽到封面帧就换上
      }
    } else {
      thumb.appendChild(h('span', {
        class: 'tile-icon', 'aria-hidden': 'true', text: previewIcon(kind, file.name)
      }));
    }
    return h('li', { class: 'tile' },
      h('button', {
        class: 'tile-btn', type: 'button', title: file.name + '（点一下预览）',
        'aria-label': '预览 ' + file.name,
        onclick: function () { openPreview(previewSourceFromFile(file)); }
      },
        thumb,
        h('span', { class: 'tile-name', title: file.name, text: file.name })
      ),
      h('button', {
        class: 'tile-remove', type: 'button', title: '不发送这个',
        'aria-label': '不发送 ' + file.name, text: '✕',
        onclick: function () {
          var at = store.picked.indexOf(file);
          if (at >= 0) store.picked.splice(at, 1);
          renderPicked();
          updateSendButton();
        }
      })
    );
  }

  /** 输入框**上面**那一行：左边一排方块（放不下就横向滚），最右边「共 N 项 · X MB」。
   *  没有文件时这一行只剩一句拖放提示。 */
  function renderPicked() {
    var signature = store.picked
      .map(function (f) { return f.name + '|' + f.size + '|' + (f.lastModified || 0); }).join(',');
    if (!changed('picked', signature)) return;
    pruneTileCache();
    var list = $('#file-list');
    clear(list);
    var total = 0;
    store.picked.forEach(function (file) {
      total += Number(file.size) || 0;
      list.appendChild(pickedTile(file));
    });
    $('#composer-picked').classList.toggle('is-empty', store.picked.length === 0);
    $('#file-total').textContent = store.picked.length
      ? '共 ' + store.picked.length + ' 项 · ' + fmtBytes(total)
      : '';
  }

  function updateSendButton() {
    var button = $('#btn-send');
    var device = selectedDevice();
    button.disabled = !store.picked.length || !device || !!uploader.active;
    button.textContent = uploader.active
      ? '正在发送…'
      : (store.picked.length ? '发送 ' + store.picked.length + ' 个文件' : '发送');
    // 输入行最右边只有一个「发送」：有待发文件时它发文件，没有时它发输入框里那句话
    // （两个按钮都在 DOM 里，同一时刻只露一个）。
    button.hidden = store.picked.length === 0;
    var chatSend = $('#btn-chat-send');
    if (chatSend) chatSend.hidden = store.picked.length > 0;
    // 对方要 PIN 的时候（上一次传失败说到了 PIN），把输入框点出来。
    var pin = $('#pin');
    if (pin) pin.classList.toggle('is-needed', pinNeeded());
  }

  /** 上一次发给这台设备失败、而且失败原因提到 PIN/口令：那就得填。 */
  function pinNeeded() {
    var device = selectedDevice();
    if (!device) return false;
    return store.transfers.some(function (t) {
      var peer = t.peer || {};
      return t.status === 'failed' && peer.id === device.id && /pin|口令|密码/i.test(String(t.error || ''));
    });
  }

  /** 这条客户端记录，服务端是不是已经有一份了（表里会有一行，动作也在那一行）。
   *
   *  「上传说成功了」和「传输页里那一行已完成」是同一件事的两种写法，以前两处
   *  各写一遍，同一个文件就占了两行 —— 用户看到的就是"传一次、列表里出现两条"。
   *  能对上传输号（/api/upload 的返回里有）就按传输号认，认不出来才留着这条
   *  客户端记录（比如根本没到服务端的中断）。
   */
  function recordedUpload(item) {
    if (!item) return false;
    var id = item.transferId || '';
    if (!id) return false;
    return store.transfers.some(function (t) { return t.transferId === id; }) ||
      (store.recentUploads || []).some(function (u) { return u.transferId === id; });
  }

  /** 同一个文件只留一条：表里那一行（带「预览」等动作）留下，卡片里这条收起。 */
  function pendingUploads() {
    return uploader.done.filter(function (item) { return !recordedUpload(item); });
  }

  function renderUpload() {
    var card = $('#upload-card');
    var body = $('#upload-body');
    var job = uploader.active;
    // 签名里带上服务端的传输/记录：它们一到，卡片里那条就该收起来（只留表里那行）。
    var signature = (job ? job.file.name + '|' + job.file.size : '') + '#' +
      uploader.queue.length + '#' + uploader.done.length + '#' +
      store.transfers.length + '#' + (store.recentUploads || []).length;
    if (!changed('upload', signature)) return;
    clear(body);
    var left = pendingUploads();
    if (!job && !uploader.queue.length && !left.length) { card.hidden = true; return; }
    card.hidden = false;

    if (job) {
      var bar = h('i');
      body.appendChild(h('li', { class: 'upload-head' },
        h('span', { class: 'fname', text: job.file.name }),
        h('span', { id: 'upload-percent', text: '0%' })
      ));
      body.appendChild(h('li', {}, h('span', { class: 'bar' }, bar)));
      body.appendChild(h('li', { class: 'stats' },
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
      body.appendChild(h('li', { class: 'hint', text: '队列中还有 ' + uploader.queue.length + ' 个文件' }));
      body.appendChild(h('li', {}, queue));
    }

    if (uploader.done.length) {
      var done = h('ul', { class: 'queue' });
      // 只列服务端还没有记录的那些（已经落进表里那一行的，不在这里重一遍）。
      left.slice(-6).forEach(function (item) {
        done.appendChild(h('li', {},
          h('span', { text: item.name }),
          h('span', { class: item.ok ? 'state-ok' : 'state-err', text: item.ok ? '已发送' : (item.error || '失败') })
        ));
      });
      if (done.childElementCount) body.appendChild(h('li', {}, done));
    }
  }

  // ------------------------------------------------------- 传输表格
  //
  // docs/ENTERPRISE_CHARTER.md §3.2：传输页 = 一行工具条（[接收｜发送] 分段 +
  // [全部｜进行中｜已完成｜失败] 筛选药丸 + 搜索 + ⋯）下面一张表。
  // 「文件 / 来自 / 大小 / 时间 / 状态 / 操作」和「文件 / 发给 / 大小 / 进度 /
  // 速度 · 剩余 / 操作」两套列共用这一块区域，不跳页、不弹窗。
  //
  // 每一行都带两个 dataset，筛选与搜索全靠它们（不重建 DOM）：
  //   data-state  active | done | failed      —— 三颗药丸分别对应后三种
  //   data-search 小写的「文件名 + 对方名」    —— 搜索框按子串匹配
  // 行右键 / 长按 = 完整文件动作菜单（§4）。

  var FILTER_STATES = { active: 'active', done: 'done', failed: 'failed' };

  function setTransferFilter(name) {
    if (name !== 'all' && !FILTER_STATES[name]) name = 'all';
    store.filter = name;
    Array.prototype.forEach.call($('#filter-pills').querySelectorAll('button'), function (button) {
      var on = button.getAttribute('data-filter') === name;
      button.classList.toggle('is-active', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    applyRowFilter();
  }

  /** 按当前药丸 + 搜索框把行收起来（hidden，不是删行 —— 删行会让活动传输的
   *  进度节点对错行）。收起来的行不参与"空状态"计数。 */
  function applyRowFilter() {
    var want = FILTER_STATES[store.filter] || '';
    var needle = String(store.search || '').trim().toLowerCase();
    Array.prototype.forEach.call(document.querySelectorAll('#receive-transfers tr, #send-transfers tr'), function (row) {
      var state = row.dataset.state || '';
      var hay = row.dataset.search || '';
      var okState = !want || state === want;
      var okText = !needle || hay.indexOf(needle) >= 0;
      // landedRow 是"文件已经落地、只留历史那一条"的行，本来就不显示。
      var pinned = row.dataset.landed === '1';
      row.hidden = pinned || !(okState && okText);
    });
    updateTableEmpty();
  }

  // 失败原因（§5.6：传输失败要有原因码 + 中文原因）。引擎把异常的原文放在
  // `error` 里，可能是英文（"connection reset by peer"），直接摆到界面上就是
  // 中文软件里冒英文；这里翻成一句人话，并给一个可截图报障的原因码。
  var FAILURE_REASONS = [
    [/pin|密码/i, 'E_PIN', 'PIN 不对：对方要求输入接收 PIN'],
    [/reject|refus|declin|拒绝/i, 'E_REJECTED', '对方拒绝了这次传输'],
    [/timeout|timed out|超时/i, 'E_TIMEOUT', '连接超时：对方一直没有响应'],
    [/no space|enospc|disk|quota|空间|磁盘/i, 'E_DISK', '磁盘空间不足：接收方写不下了'],
    [/checksum|hash|verif|corrupt|truncat|校验/i, 'E_CHECKSUM', '校验失败：文件传坏了，请重试'],
    [/cancel|abort|取消/i, 'E_CANCEL', '传输被取消'],
    [/permission|denied|eacces|read-?only|权限/i, 'E_PERM', '没有权限写入接收目录'],
    [/not found|enoent|missing|找不到/i, 'E_NOTFOUND', '找不到文件：对方那边可能已经删了'],
    [/connect|connection|reset|broken pipe|unreachable|network|socket|econn|连接|中断|不在线/i,
      'E_LINK', '连接中断：对方可能已经下线'],
  ];

  /** 引擎给的错误串 → {code, text}，两个都是给用户看的（中文）。
   *
   *  引擎自己也会给中文（"对方拒绝或传输失败"），那就不用再包一层"传输失败"；
   *  真正的英文异常（"connection reset by peer"）翻成一句人话 + 原因码，原文进
   *  title —— 中文软件里不该冒英文，但排查问题的人还得拿得到原文。 */
  function failureReason(error) {
    var raw = String(error || '').trim();
    if (!raw) return { code: 'E_FAIL', text: '传输失败', raw: raw };
    for (var i = 0; i < FAILURE_REASONS.length; i++) {
      if (FAILURE_REASONS[i][0].test(raw)) {
        return { code: FAILURE_REASONS[i][1], text: FAILURE_REASONS[i][2], raw: raw };
      }
    }
    if (/[\u4e00-\u9fff]/.test(raw)) return { code: 'E_FAIL', text: raw, raw: raw };
    return { code: 'E_FAIL', text: '传输失败', raw: raw };
  }

  /** 失败行里那一小句红字：中文原因 + 原因码，鼠标悬停给原文。 */
  function failureNode(error) {
    var reason = failureReason(error);
    return h('span', {
      class: 'fail-reason',
      title: '原因码 ' + reason.code + (reason.raw ? '：' + reason.raw : ''),
      text: reason.text + '（' + reason.code + '）'
    });
  }

  /** 每一行的两个 dataset（筛选 + 搜索）与右键菜单。 */
  function markRow(row, state, search) {
    row.dataset.state = state || 'active';
    row.dataset.search = String(search || '').toLowerCase();
    return row;
  }

  /** 给一行挂上"完整文件动作菜单"（右键 / 长按 650ms，§4）。 */
  function bindRowActions(row, options) {
    if (!options || !options.url) return row;
    row.classList.add('has-menu');
    row.title = '右键（长按）有完整文件动作';
    return bindFileActions(row, function () { return fileActionItems(options); });
  }

  /** 操作格里的 ⋯：和右键同一套动作（手机上不一定想得起来长按）。 */
  function moreButton(options) {
    return h('button', {
      class: 'row-act is-more', type: 'button', text: '⋯',
      title: '更多文件动作', 'aria-label': '更多文件动作',
      onclick: function (event) {
        event.stopPropagation();
        var box = event.currentTarget.getBoundingClientRect();
        showFileMenu(fileActionItems(options), box.left, box.bottom + 4);
      }
    });
  }

  /** 下载接口：接收目录里的一个文件。 */
  function downloadUrl(name) {
    return '/api/download?path=' + encodeURIComponent(name);
  }

  /** 「文件」格：一个名字，或「第一个 等 N 个文件」。 */
  function fileCell(transfer) {
    var items = transfer.items || [];
    var text = '';
    if (items.length === 1) text = items[0].name;
    else if (items.length > 1) text = items[0].name + ' 等 ' + items.length + ' 个文件';
    else text = transfer.files ? transfer.files + ' 个文件' : '文件';
    return h('td', { class: 'col-file' }, h('span', { class: 'row-name', title: text, text: text }));
  }

  /** 设备行的副标题：一行说清「在哪 · 是什么 · 怎么发现的」（§3.1）。
   *
   *  以前这里排两行（一行地址、一行平台+版本+来源），同一个事实说两遍，
   *  行高被撑到 88px，288px 的侧栏里 6 行就顶到窗口外了。 */
  function deviceSubtitle(device) {
    // 副标题 = 地址 + 怎么发现的（桌面端裁定的口径：**不放端口**）。
    // 端口对用户没意义，同一台机器看到好几个端口更糊涂；平台、端口、id 都留给
    // 会话顶部的 PeerHeader 详情行（那里已经写着 127.0.0.1:52140）。
    // 依据：C9「同一事实不在两处重复」+ E1 三端措辞一致。
    var parts = [String((device && device.address) || '未知地址')];
    var source = sourceShort(device);
    if (source) parts.push(source);
    return parts.join(' · ');
  }

  /** 发现来源的短词：本机 / 网页 / 局域网 / 手动 / 扫描 / 以前见过。
   *  和电脑端设备列表同一个位置、同一套说法（长解释留给 PeerHeader 与设置页诊断）。 */
  function sourceShort(device) {
    if (!device) return '';
    if (device.isClient) return device.clientKind === 'app' ? '安卓 App' : '网页';
    if (device.local) return '本机';
    var source = String(device.source || '').toLowerCase();
    if (source === 'manual') return '手动';
    if (source === 'web' || source === 'app') return '网页';
    if (source === 'incoming') return '对方连过来';
    if (source === 'probe' || source === 'scan') return '扫描';
    if (source === 'history') return '以前见过';
    return '局域网';
  }

  function peerCell(peer) {
    var name = deviceLabel(peer);
    return h('td', { class: 'col-peer', title: name, text: name });
  }

  /** 一行传输的文件名（搜索用）。 */
  function transferNames(transfer) {
    return (transfer.items || []).map(function (item) { return item.name || ''; }).join(' ');
  }

  /** 「操作」格：进行中的能取消；传完 / 失败的那条给「预览」+「⋯」。
   *
   *  收完的那一条带**完整**动作（§4）：预览 / 打开 / 下载 / 另存为 / 复制 /
   *  重新发送 / 属性。网页版没有「打开所在文件夹」，那一项换成「下载」并写明。 */
  function actionCell(transfer, options) {
    var cell = h('td', { class: 'col-act' });
    if (transfer.status === 'active') {
      cell.appendChild(h('button', {
        class: 'row-act is-danger', type: 'button', text: '取消',
        onclick: function () { cancelTransfer(transfer.transferId); }
      }));
      return cell;
    }
    if (options && options.url) {
      cell.appendChild(previewButton(
        previewSourceFromUrl(options.name, options.size, options.url), 'row-act is-preview'
      ));
      cell.appendChild(moreButton(options));
    }
    return cell;
  }

  /** 进度条 + 百分比；进行中的行由 paint() 每 250ms 往前推。 */
  function progressNodes(transfer) {
    var bar = h('i');
    var percentNode = h('span', { class: 'cell-percent',
      text: (Number(transfer.percent) || 0).toFixed(0) + '%' });
    var bytesNode = h('span', { text: fmtBytes(transfer.bytes) + ' / ' + fmtBytes(transfer.total) });
    var speedNode = h('span', { text: '' });
    // 进度条是行内容的装饰，用 <span>（td 里不再套 div）。
    var box = h('span', { class: 'bar' + (transfer.status === 'failed' ? ' is-failed' : '') }, bar);
    if (transfer.status === 'active') {
      painted.set(transfer.transferId, {
        bar: bar,
        total: transfer.total,
        bytesNode: bytesNode,
        speedNode: speedNode,
        percentNode: percentNode
      });
      noteBytes(transfer.transferId, transfer.bytes);
    }
    return { box: box, bar: bar, percentNode: percentNode, bytesNode: bytesNode, speedNode: speedNode };
  }

  /** 传输开始了多久 —— 协议里没有"开始时间"，只有已经跑了多少秒。 */
  function startedClock(transfer) {
    var seconds = Number(transfer.elapsed) || 0;
    return formatClock(Date.now() / 1000 - seconds) || '—';
  }

  function receiveRow(transfer) {
    var nodes = progressNodes(transfer);
    var failed = transfer.status === 'failed';
    var items = transfer.items || [];
    var single = items.length === 1 ? items[0] : null;
    var options = single ? {
      name: single.name, size: single.size || single.done,
      url: downloadUrl(landedName(single) || single.name)
    } : null;
    var state = h('td', { class: 'col-state' },
      h('span', { class: 'cell-state' + (failed ? ' is-failed' : ''), text: failed ? '失败' : '接收中' }),
      failed ? failureNode(transfer.error) : h('span', { class: 'cell-progress' }, nodes.box, nodes.percentNode),
      h('span', { class: 'cell-sub' }, failed ? null : nodes.speedNode)
    );
    var row = h('tr', { class: failed ? 'is-failed' : '' },
      fileCell(transfer),
      peerCell(transfer.peer || {}),
      h('td', { class: 'col-size' }, failed ? h('span', { text: fmtBytes(transfer.total) }) : nodes.bytesNode),
      h('td', { class: 'col-time', text: startedClock(transfer) }),
      state,
      actionCell(transfer, options)
    );
    markRow(row, failed ? 'failed' : 'active', transferNames(transfer) + ' ' + deviceLabel(transfer.peer || {}));
    return bindRowActions(row, options);
  }

  function sendRow(transfer) {
    var nodes = progressNodes(transfer);
    var failed = transfer.status === 'failed';
    var single = (transfer.items || []).length === 1 ? transfer.items[0] : null;
    var options = single ? {
      name: single.name, size: single.size,
      url: single.path ? downloadUrl(single.path) : ''
    } : null;
    var row = h('tr', { class: failed ? 'is-failed' : '' },
      fileCell(transfer),
      peerCell(transfer.peer || {}),
      h('td', { class: 'col-size' }, nodes.bytesNode),
      h('td', { class: 'col-progress' },
        h('span', { class: 'cell-progress' }, nodes.box, nodes.percentNode),
        failed ? failureNode(transfer.error) : null
      ),
      h('td', { class: 'col-speed' }, nodes.speedNode),
      actionCell(transfer, failed ? null : options)
    );
    markRow(row, failed ? 'failed' : 'active', transferNames(transfer) + ' ' + deviceLabel(transfer.peer || {}));
    return bindRowActions(row, options);
  }

  /** 传完的那一条（服务端的上传记录）：发出去过什么，也要留在表里。 */
  function uploadRow(upload) {
    var failed = upload.status === 'failed';
    var state = failed ? '失败' : (upload.status === 'handed-off' ? '已交给手机页面' : '已完成');
    var url = upload.name ? downloadUrl(upload.name) : '';
    var options = !failed && url ? { name: upload.name, size: upload.size, url: url } : null;
    var row = h('tr', { class: failed ? 'is-failed' : '' },
      h('td', { class: 'col-file' },
        h('span', { class: 'row-name', title: upload.name || '', text: upload.name || '文件' })),
      h('td', { class: 'col-peer', title: upload.deviceName || '', text: upload.deviceName || '本机' }),
      h('td', { class: 'col-size' }, h('span', { text: fmtBytes(upload.size) })),
      h('td', { class: 'col-progress' },
        h('span', { class: failed ? 'cell-state is-failed' : 'cell-state is-done', text: state }),
        failed ? failureNode(upload.error) : null
      ),
      h('td', { class: 'col-speed' }, h('span', { text: failed ? '' : (formatClock(upload.finished) || '') })),
      actionCell({ status: failed ? 'failed' : 'done', direction: 'send', transferId: upload.id }, options)
    );
    markRow(row, failed ? 'failed' : 'done', (upload.name || '') + ' ' + (upload.deviceName || ''));
    return bindRowActions(row, options);
  }

  /** 「已完成」那一行的「来自」是谁。
   *
   *  这一段的来源是电脑的接收目录（`/api/files`），记录里通常不带发送方。
   *  手里有什么证据就用什么：正在/刚失败的传输（对端就是发送方）→ 电脑分享给
   *  这台手机的（分享方就是这台电脑）→ 这台手机上传的记录（发送方是这台手机）。
   *  都没有就写「本机」并在 title 里说明 —— **一列破折号等于这一列没有信息**，
   *  用户原话就是"信息不明，不详细"。 */
  function landedSource(file) {
    var name = String((file && file.name) || '');
    var transfer = (store.transfers || []).find(function (item) {
      return (item.items || []).some(function (one) { return landedName(one) === name; });
    });
    if (transfer && transfer.peer) {
      return { text: deviceLabel(transfer.peer), hint: '这次传输的发送方' };
    }
    var share = (store.shares || []).find(function (item) { return item.name === name; });
    if (share) return { text: '本机', hint: '电脑分享给这台手机的文件' };
    var upload = (store.recentUploads || []).find(function (item) { return item.name === name; });
    if (upload && upload.deviceName) {
      return { text: String(upload.deviceName), hint: '这台手机发过去的文件' };
    }
    return { text: '本机', hint: '这台电脑接收目录里的文件（发送方不在记录里）' };
  }

  /** 已经收完的文件（电脑接收目录里的，"电脑上的文件"）。
   *
   *  `/api/state` 不报接收历史，但这台电脑的接收目录就在这里列着 —— 一张表里
   *  把"正在传"和"已经收完"接在一起，用户才不会觉得「已完成」那颗药丸是死的。
   */
  function landedRow(file) {
    var url = downloadUrl(file.path || file.name);
    var options = { name: file.name, size: file.size, url: url, landed: true };
    var from = landedSource(file);
    var row = h('tr', {},
      h('td', { class: 'col-file' }, h('span', { class: 'row-name', title: file.path || file.name, text: file.name })),
      h('td', { class: 'col-peer', title: from.hint, text: from.text }),
      h('td', { class: 'col-size' }, h('span', { text: fmtBytes(file.size) })),
      h('td', { class: 'col-time', text: formatClock(file.mtime) || '—' }),
      h('td', { class: 'col-state' }, h('span', { class: 'cell-state is-done', text: '已完成' })),
      actionCell({ status: 'done', direction: 'receive', transferId: 'file:' + file.name }, options)
    );
    markRow(row, 'done', (file.name || '') + ' ' + (file.path || ''));
    return bindRowActions(row, options);
  }

  /** 收到的文件是不是已经落地了（「电脑上的文件」里已经有它那一条）。
   *
   *  电脑端同一套规则：文件一落地，「正在传」那一行就收起来，只留带完整动作的
   *  那一条 —— 一个文件占两行，用户看到的就是"收了一次、列表里两条"。
   */
  function landedTransfer(transfer) {
    if (!transfer || transfer.direction === 'send') return false;
    var items = transfer.items || [];
    if (!items.length) return false;
    return items.every(function (item) {
      return item && fileListHas(landedName(item));
    });
  }

  /** 这个文件落到电脑上的叫什么名字。
   *
   *  传输项里的 ``path`` 是接收端真实的落地路径（重名时引擎已经改成
   *  ``名字 (1).jpg`` 这种），拿它和「电脑上的文件」对齐才准；没有 path
   *  的时候退回原始文件名。
   */
  function landedName(item) {
    var path = String((item && item.path) || '');
    if (path) {
      var parts = path.split(/[\\/]/);
      var last = parts[parts.length - 1];
      if (last) return last;
    }
    return String((item && item.name) || '');
  }

  /** 「电脑上的文件」那一列里有没有这个名字。 */
  function fileListHas(name) {
    var wanted = String(name || '');
    if (!wanted) return false;
    return (store.files || []).some(function (file) {
      return file.name === wanted || file.path === wanted;
    });
  }

  /** 一张表里真正看得见的行数（收起来的行不算，空表提示才说得对）。 */
  function visibleRowCount(selector) {
    var box = $(selector);
    if (!box) return 0;
    return Array.prototype.filter.call(box.children, function (row) {
      return !row.hidden;
    }).length;
  }

  /** 空状态：一句人话 + 说明当前筛选（§3.2「空状态：居中图标 + 一句话」）。 */
  function emptyText(kind) {
    var filter = store.filter || 'all';
    var needle = String(store.search || '').trim();
    var who = kind === 'send' ? '发送过' : '收到';
    if (needle) return '没有匹配「' + needle + '」的传输记录';
    if (filter === 'active') return kind === 'send' ? '现在没有正在发送的文件' : '现在没有正在接收的文件';
    if (filter === 'done') return kind === 'send' ? '还没有发送完成的文件' : '还没有收到文件';
    if (filter === 'failed') return '没有失败的传输';
    return kind === 'send' ? '还没有发送过文件' : '还没有收到文件';
  }

  /** 空表要有一句人话，而不是一张空网格。 */
  function updateTableEmpty() {
    var received = visibleRowCount('#offer-list') + visibleRowCount('#receive-transfers');
    var receiveEmpty = $('#receive-empty');
    receiveEmpty.hidden = received > 0;
    $('#receive-empty-text').textContent = emptyText('receive');
    var sendEmpty = $('#send-empty');
    sendEmpty.hidden = visibleRowCount('#send-transfers') > 0;
    $('#send-empty-text').textContent = emptyText('send');
  }

  function renderTransfers() {
    var signature = store.transfers
      .map(function (t) {
        return [t.transferId, t.direction, t.status, t.error, t.files, (t.peer || {}).name, t.total].join('|');
      })
      .join(',')
      + '#' + (store.recentUploads || []).map(function (u) { return u.id + '|' + u.status + '|' + u.error; }).join(',')
      + '#' + (store.files || []).map(function (f) { return f.name + '|' + f.size + '|' + f.mtime; }).join(',');
    if (!changed('transfers', signature)) return;

    var sendBox = $('#send-transfers');
    var receiveBox = $('#receive-transfers');
    clear(sendBox);
    clear(receiveBox);
    painted.forEach(function (value, key) { if (key !== '__upload__') painted.delete(key); });

    var running = {};
    store.transfers.forEach(function (transfer) {
      var row = transfer.direction === 'send' ? sendRow(transfer) : receiveRow(transfer);
      // 文件已经落地：这条「正在传」收起来（hidden，不是从表里删掉）——
      // 同一个文件的那一条记录带着完整动作留在下面（已完成那一段）。
      // 删除会让它下面每一行的下标上移，活跃传输行还按位置持有进度节点。
      if (landedTransfer(transfer)) {
        row.dataset.landed = '1';
        row.hidden = true;
      }
      if (transfer.direction === 'send') sendBox.appendChild(row);
      else receiveBox.appendChild(row);
      if (transfer.status === 'active') running[transfer.transferId] = true;
    });
    // 上传记录里那些已经传完的，接在正在传的下面。
    (store.recentUploads || []).slice().reverse().forEach(function (upload) {
      if (upload.transferId && running[upload.transferId]) return;
      sendBox.appendChild(uploadRow(upload));
    });
    // 接收侧：电脑接收目录里已经落地的文件（「电脑上的文件」）就是已完成那一段。
    (store.files || []).forEach(function (file) {
      receiveBox.appendChild(landedRow(file));
    });
    applyRowFilter();
  }

  function renderOffers() {
    var signature = store.offers
      .map(function (o) { return [o.request_id, o.total, (o.peer || {}).name, (o.files || []).length].join('|'); })
      .join(',');
    // Guarded so a 3 s poll cannot reset the "信任此设备" checkbox while the
    // user is looking at it.
    if (!changed('offers', signature)) return;

    var box = $('#offer-list');
    clear(box);
    store.offers.forEach(function (offer) {
      var peer = offer.peer || {};
      var files = offer.files || [];
      var label = files.length
        ? (files.length > 1 ? files[0].name + ' 等 ' + files.length + ' 个文件' : files[0].name)
        : '对方要发文件过来';
      var detail = files.map(function (file) {
        return file.name + '（' + fmtBytes(file.size) + '）';
      }).join('\n');
      var trust = h('input', { type: 'checkbox' });

      box.appendChild(h('tr', {},
        h('td', { class: 'col-file' }, h('span', { class: 'row-name', title: detail || label, text: label })),
        h('td', { class: 'col-peer', title: deviceLabel(peer), text: deviceLabel(peer) }),
        h('td', { class: 'col-size', text: fmtBytes(offer.total) + (offer.resume_bytes ? '（可续传）' : '') }),
        h('td', { class: 'col-time', text: formatClock(offer.ts) || '—' }),
        h('td', { class: 'col-offer', colspan: '2' },
          h('span', { class: 'tag is-pin', text: '待确认' }),
          h('button', {
            class: 'row-act is-primary', type: 'button', text: '接受',
            onclick: function (event) { respondOffer(offer.request_id, true, trust.checked, event.target); }
          }),
          h('button', {
            class: 'row-act is-danger', type: 'button', text: '拒绝',
            onclick: function (event) { respondOffer(offer.request_id, false, false, event.target); }
          }),
          h('label', { class: 'trust-mini' }, trust, '信任此设备')
        )
      ));
    });
    $('#offer-count').textContent = store.offers.length
      ? store.offers.length + ' 个待确认 —— 接受之后才开始收'
      : '';
    updateTableEmpty();
  }

  /** 「电脑上的文件」那一行说明。
   *
   *  这些文件**在电脑上**（`/api/files` 列的是电脑接收目录），点「下载」才到手机里。
   *  以前这句话下面还挂着一张独立的文件卡片，现在那些文件就是接收表里「已完成」
   *  那一段（一个文件只占一行，C8）—— 所以这里只剩一句说明，列表在表里。
   */
  function renderFiles() {
    var signature = store.files.map(function (f) { return f.path + '|' + f.size + '|' + f.mtime; }).join(',');
    if (!changed('files', signature)) return;
    var note = $('#receive-dir');
    if (note) {
      note.textContent = store.files.length
        ? '电脑上的文件（' + store.files.length + ' 个）：这些文件在电脑上；点「下载」就能取到手机里。'
        : '电脑上的文件：电脑接收目录里还没有文件。';
    }
    renderTransfers();
  }

  function noteShareDownload(share) {
    // The browser handles the actual download (and writes it to the phone's
    // Downloads folder); all this can do is tell the user where it went.
    toast('开始下载：' + share.name + '（保存在手机的「下载」目录里）');
  }

  // ------------------------------------------------------------- 文件预览
  //
  // docs/UI_LAYOUT.md §6：点待发方块、气泡上的「预览」、接收表 / 文件列表里的
  // 「预览」，都在页内弹层里就地摊开（不开新标签页）。图片铺满、点一下切原始
  // 大小；视频/音频用浏览器内置播放器直接放；文本看开头 256 KB；其它二进制
  // 老实说"看不出内容"，只给下载。
  //
  // 类型判定和 desktop/preview.py 是同一套：**扩展名优先，再闻内容** —— 前
  // 4 KB 里有 NUL 字节、或者命中 zip/gzip/PNG/JPEG/PDF/ELF/SQLite/rar/7z 这些
  // 魔数，就按二进制处理；其余当文本（README、日志、配置这些没后缀的也能看）。

  //: 这几张表和 desktop/preview.py 逐项对齐：少一项就会出现"一端能看、另一端说看不了"。
  var IMAGE_EXTENSIONS = [
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.ico', '.tif', '.tiff', '.svg', '.avif'
  ];
  var VIDEO_EXTENSIONS = [
    '.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.flv', '.wmv', '.mpg', '.mpeg', '.ts', '.3gp'
  ];
  var AUDIO_EXTENSIONS = [
    '.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus', '.oga', '.wma', '.amr', '.aiff'
  ];
  var TEXT_EXTENSIONS = [
    '.txt', '.md', '.markdown', '.rst', '.log', '.json', '.yaml', '.yml', '.toml', '.ini',
    '.cfg', '.conf', '.csv', '.tsv', '.xml', '.html', '.htm', '.css', '.js', '.mjs', '.cjs',
    '.ts', '.tsx', '.jsx', '.py', '.rb', '.go', '.rs', '.java', '.kt', '.kts', '.c', '.h',
    '.cpp', '.hpp', '.cc', '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd', '.sql',
    '.dockerfile', '.gitignore', '.env', '.properties', '.gradle', '.lua', '.php', '.pl',
    '.swift', '.m', '.mm', '.cs', '.scala', '.clj', '.vim', '.tex', '.srt', '.vtt'
  ];
  var ARCHIVE_EXTENSIONS = ['.zip', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar'];

  //: 预览文本时最多读这么多（再长就不是"看一眼"，是编辑器的活）。
  var PREVIEW_TEXT_BYTES = 256 * 1024;
  //: 判断"是不是文本"时闻一下的字节数。
  var PREVIEW_SNIFF_BYTES = 4096;
  //: 几个"开头就写着我是二进制"的签名（NUL 之外的补充）。
  var PREVIEW_MAGIC = [
    [0x50, 0x4b, 0x03, 0x04],                     // zip / docx / apk
    [0x1f, 0x8b],                                 // gzip
    [0x89, 0x50, 0x4e, 0x47],                     // PNG
    [0xff, 0xd8, 0xff],                           // JPEG
    [0x47, 0x49, 0x46, 0x38],                     // GIF8
    [0x25, 0x50, 0x44, 0x46],                     // %PDF
    [0x7f, 0x45, 0x4c, 0x46],                     // ELF
    [0x53, 0x51, 0x4c, 0x69, 0x74, 0x65, 0x20,    // SQLite format 3
     0x66, 0x6f, 0x72, 0x6d, 0x61, 0x74, 0x20, 0x33],
    [0x52, 0x61, 0x72, 0x21, 0x1a],               // Rar!
    [0x37, 0x7a, 0xbc, 0xaf]                      // 7z
  ];

  var previewState = { source: null, zoom: 'fit' };

  function previewExtension(name) {
    var text = String(name || '');
    var dot = text.lastIndexOf('.');
    return dot > 0 ? text.slice(dot).toLowerCase() : '';
  }

  /** 光看后缀能定下来的类型；定不下来（没后缀、后缀不认识）返回 ''，交给内容说。 */
  function previewKindFromName(name) {
    var extension = previewExtension(name);
    if (IMAGE_EXTENSIONS.indexOf(extension) >= 0) return 'image';
    if (VIDEO_EXTENSIONS.indexOf(extension) >= 0) return 'video';
    if (AUDIO_EXTENSIONS.indexOf(extension) >= 0) return 'audio';
    if (TEXT_EXTENSIONS.indexOf(extension) >= 0) return 'text';
    if (extension === '.pdf') return 'binary';   // 浏览器里没有 PDF 渲染器，别装作能看
    return '';
  }

  function previewKindLabel(kind) {
    return {
      image: '图片', video: '视频', audio: '音频', text: '文本', binary: '二进制文件'
    }[kind] || '文件';
  }

  /** 没有缩略图时这个文件用哪个图标（和电脑端 tile_icon_name 一一对应）。 */
  function previewIcon(kind, name) {
    if (kind === 'image') return '🖼';
    if (kind === 'video') return '🎬';
    if (kind === 'audio') return '🎵';
    if (kind === 'text') return '📄';
    if (ARCHIVE_EXTENSIONS.indexOf(previewExtension(name)) >= 0) return '🗜';
    return '📄';
  }

  function previewLooksBinary(bytes) {
    if (!bytes || !bytes.length) return false;
    for (var i = 0; i < bytes.length; i++) {
      if (bytes[i] === 0) return true;          // NUL 字节：文本文件不会有
    }
    return PREVIEW_MAGIC.some(function (magic) {
      if (bytes.length < magic.length) return false;
      for (var k = 0; k < magic.length; k++) {
        if (bytes[k] !== magic[k]) return false;
      }
      return true;
    });
  }

  /** 预览的一个"来源"：url 要能直接喂给 <img>/<video>/<audio>/fetch。 */
  function previewSource(options) {
    var url = options.url || '';
    return {
      name: options.name || '文件',
      size: Number(options.size) || 0,
      url: url,
      download: options.download === undefined ? url : options.download,
      hint: options.hint || '',
      file: options.file || null,
      poster: options.poster || ''
    };
  }

  function previewSourceFromFile(file) {
    var entry = tileEntry(file);
    return previewSource({
      name: file.name || '文件',
      size: file.size,
      url: entry.url,
      download: entry.url,
      hint: previewKindFromName(file.name) || String(file.type || '').split('/')[0],
      file: file,
      poster: entry.poster
    });
  }

  /** 气泡里的附件：按消息 id 走已有的 /api/chat/media/<id>（不新增接口）。 */
  function previewSourceFromMessage(message) {
    var hinted = message.kind === 'voice' ? 'audio' : (message.kind === 'file' ? '' : message.kind);
    return previewSource({
      name: message.mediaName || '附件',
      size: message.mediaSize || 0,
      url: '/api/chat/media/' + encodeURIComponent(message.id),
      hint: hinted || ''
    });
  }

  /** 电脑上的文件 / 电脑发来的文件 / 接收表里那条：都走已有的下载接口。 */
  function previewSourceFromUrl(name, size, url) {
    return previewSource({ name: name, size: size, url: url });
  }

  /** 取开头 limit 字节：本地文件直接切片，远端走 Range 请求，顺带带回总大小。 */
  function readPreviewHead(source, limit) {
    if (source.file && source.file.slice) {
      return source.file.slice(0, limit).arrayBuffer().then(function (buffer) {
        return { bytes: new Uint8Array(buffer), total: source.file.size || 0 };
      });
    }
    return fetch(source.url, {
      cache: 'no-store',
      headers: { Range: 'bytes=0-' + Math.max(0, limit - 1) }
    }).then(function (response) {
      if (response.status !== 200 && response.status !== 206) {
        throw new Error('HTTP ' + response.status);
      }
      var total = previewTotalBytes(response, limit);
      return readPreviewCapped(response, limit).then(function (bytes) {
        return { bytes: bytes, total: Math.max(total, bytes.length) };
      });
    });
  }

  function previewTotalBytes(response, limit) {
    var range = response.headers.get('Content-Range') || '';
    var match = /\/(\d+)\s*$/.exec(range);
    if (match) return parseInt(match[1], 10);
    var length = parseInt(response.headers.get('Content-Length') || '', 10);
    return isFinite(length) && length > 0 ? length : limit;
  }

  /** 只读到这里为止：万一对面不理 Range，也不会把几个 G 的文件全拉进手机。 */
  function readPreviewCapped(response, limit) {
    if (!response.body || !response.body.getReader) {
      return response.arrayBuffer().then(function (buffer) {
        return new Uint8Array(buffer).slice(0, limit);
      });
    }
    var reader = response.body.getReader();
    var chunks = [];
    var size = 0;
    function step() {
      return reader.read().then(function (result) {
        if (result.done) return null;
        var chunk = result.value;
        if (size + chunk.length > limit) chunk = chunk.slice(0, limit - size);
        chunks.push(chunk);
        size += chunk.length;
        if (size >= limit) {
          try { reader.cancel(); } catch (error) { /* 读够了就停 */ }
          return null;
        }
        return step();
      });
    }
    return step().then(function () {
      var merged = new Uint8Array(size);
      var offset = 0;
      chunks.forEach(function (chunk) { merged.set(chunk, offset); offset += chunk.length; });
      return merged;
    });
  }

  /** 类型判定：扩展名优先，其次闻内容 —— 和电脑端同一套顺序。 */
  function resolvePreviewKind(source) {
    var named = previewKindFromName(source.name);
    if (named) {
      // 语音那条是 audio/webm：后缀会说成"视频"，但消息类型已经告诉我们它是语音，
      // 这时候听协议的（否则弹出来是个黑框在放声音）。
      if (source.hint === 'audio' && named === 'video') return Promise.resolve('audio');
      return Promise.resolve(named);
    }
    if (source.hint === 'image' || source.hint === 'video' || source.hint === 'audio') {
      return Promise.resolve(source.hint);
    }
    return readPreviewHead(source, PREVIEW_SNIFF_BYTES).then(function (head) {
      return previewLooksBinary(head.bytes) ? 'binary' : 'text';
    }).catch(function () { return 'binary'; });
  }

  /** 文本解码：先严格 UTF-8，不成就退回 GB18030（中文日志/配置常见）。
   *
   *  注意"读开头 256 KB"这句话里藏着一个坑：切点可能正好落在多字节字符中间，
   *  严格解码会因此整段失败、错退到 GB18030，中文就变成"锟斤拷"。所以末尾不完整
   *  时先少喂几个字节再试（UTF-8 一个字符最多 4 字节）。 */
  function decodePreviewBytes(bytes) {
    if (!window.TextDecoder) {
      var plain = '';
      for (var i = 0; i < bytes.length; i++) plain += String.fromCharCode(bytes[i]);
      return plain;
    }
    for (var trim = 0; trim < 4 && trim < bytes.length; trim++) {
      try {
        return new TextDecoder('utf-8', { fatal: true })
          .decode(bytes.subarray(0, bytes.length - trim));
      } catch (error) { /* 尾巴被切断，或者这段根本不是 UTF-8 */ }
    }
    try {
      return new TextDecoder('gb18030').decode(bytes);
    } catch (error) { /* 连 GB18030 都不认识（老浏览器） */ }
    return new TextDecoder('utf-8').decode(bytes);
  }

  function openPreview(source) {
    if (!source || !source.url) {
      toast('这个文件现在没有可预览的地址', 'error');
      return;
    }
    previewState.source = source;
    previewState.zoom = 'fit';
    var body = $('#preview-body');
    body.className = 'preview-body';
    clear(body);
    clear($('#preview-foot'));
    $('#preview-icon').textContent = previewIcon(previewKindFromName(source.name), source.name);
    $('#preview-title').textContent = source.name;
    $('#preview-facts').textContent = source.size ? fmtBytes(source.size) : '';
    body.appendChild(h('p', { class: 'preview-loading muted', text: '正在读取…' }));
    $('#preview').hidden = false;
    document.body.classList.add('preview-open');
    resolvePreviewKind(source).then(function (kind) {
      if (previewState.source !== source) return;   // 已经关掉、或者又点了别的
      paintPreview(kind, source);
    });
  }

  function paintPreview(kind, source) {
    var body = $('#preview-body');
    var foot = $('#preview-foot');
    body.className = 'preview-body';
    clear(body);
    clear(foot);
    $('#preview-icon').textContent = previewIcon(kind, source.name);
    $('#preview-facts').textContent = [
      previewKindLabel(kind), source.size ? fmtBytes(source.size) : ''
    ].filter(Boolean).join(' · ');

    if (kind === 'image') {
      body.appendChild(previewImageNode(source));
      foot.appendChild(h('span', { class: 'hint', text: '点一下图片，在「适应窗口 / 原始大小」之间切换。' }));
    } else if (kind === 'video' || kind === 'audio') {
      body.appendChild(previewMediaNode(kind, source));
      foot.appendChild(h('span', { class: 'hint', text: kind === 'video'
        ? '用浏览器内置播放器直接播放，不用别的程序。'
        : '用浏览器内置播放器直接听。' }));
    } else if (kind === 'text') {
      body.classList.add('is-text');
      previewTextInto(body, source);
      foot.appendChild(h('span', { class: 'hint',
        text: '只读、可选中；等宽字体显示开头 ' + fmtBytes(PREVIEW_TEXT_BYTES) + '。' }));
    } else {
      body.appendChild(previewBinaryNode(kind, source));
      foot.appendChild(h('span', { class: 'hint',
        text: '浏览器打不开手机上的别的程序：点「下载」存到手机里，再用对应的应用打开。' }));
    }
    if (source.download) {
      foot.appendChild(h('a', {
        class: 'ghost', href: source.download, download: source.name, text: '⬇ 下载'
      }));
    }
  }

  function previewImageNode(source) {
    var image = h('img', { class: 'preview-image is-fit', src: source.url, alt: source.name });
    image.title = '点一下看原始大小';
    image.addEventListener('error', function () {
      image.replaceWith(h('p', { class: 'preview-note',
        text: '这个图片打不开（文件可能坏了，或者浏览器不认识这个格式）。' }));
    });
    image.addEventListener('click', function () {
      previewState.zoom = previewState.zoom === 'fit' ? 'original' : 'fit';
      var fit = previewState.zoom === 'fit';
      image.classList.toggle('is-fit', fit);
      image.classList.toggle('is-original', !fit);
      $('#preview-body').classList.toggle('is-zoom', !fit);
      image.title = fit ? '点一下看原始大小' : '点一下回到适应窗口';
    });
    return image;
  }

  function previewMediaNode(kind, source) {
    var box = h('div', { class: 'preview-media' });
    if (kind === 'video') {
      var video = h('video', {
        class: 'preview-video', src: source.url, controls: 'controls',
        playsinline: true, preload: 'metadata'
      });
      if (source.poster) video.setAttribute('poster', source.poster);
      box.appendChild(video);
    } else {
      box.appendChild(h('audio', {
        class: 'preview-audio', src: source.url, controls: 'controls', preload: 'metadata'
      }));
    }
    return box;
  }

  function previewTextInto(body, source) {
    var pre = h('pre', { class: 'preview-text', text: '正在读取…' });
    body.appendChild(pre);
    readPreviewHead(source, PREVIEW_TEXT_BYTES).then(function (head) {
      if (previewState.source !== source) return;
      pre.textContent = decodePreviewBytes(head.bytes);
      if (head.total > PREVIEW_TEXT_BYTES) {
        body.appendChild(h('p', { class: 'preview-note',
          text: '…（文件比 ' + fmtBytes(PREVIEW_TEXT_BYTES) + ' 大，这里只显示开头一段）' }));
      }
    }).catch(function (error) {
      if (previewState.source !== source) return;
      pre.textContent = '';
      body.appendChild(h('p', { class: 'preview-note', text: '读不出来：' + error.message }));
    });
  }

  function previewBinaryNode(kind, source) {
    return h('div', { class: 'preview-empty' },
      h('span', { class: 'preview-big-icon', 'aria-hidden': 'true', text: previewIcon(kind, source.name) }),
      h('p', { class: 'empty-title', text: '这类文件没有可预览的内容' }),
      h('p', { class: 'empty-hint', text: source.name + '（' + previewKindLabel(kind) +
        (source.size ? ' · ' + fmtBytes(source.size) : '') + '）在界面里看不出内容。' })
    );
  }

  /** 关闭时把 body 清空 —— 正在播的视频/音频跟着元素一起没了，声音也就停了。 */
  function closePreview() {
    var overlay = $('#preview');
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    previewState.source = null;
    document.body.classList.remove('preview-open');
    clear($('#preview-body'));
    clear($('#preview-foot'));
  }

  function previewKeydown(event) {
    if (event.key === 'Escape' || event.key === 'Esc') closePreview();
  }

  /** 各处入口共用的那个「预览」按钮。 */
  function previewButton(source, className) {
    return h('button', {
      class: className || 'msg-save', type: 'button', text: '预览',
      title: '在这里看一眼内容，不用先下载',
      onclick: function () { openPreview(source); }
    });
  }

  // ------------------------------------------------------- 文件动作菜单
  //
  // 电脑端在文件卡片上右键出的是这四个动作（预览 / 打开 / 打开所在文件夹 /
  // 另存为 —— 见 desktop/widgets.py 的 file_actions_menu，会话气泡和传输表里
  // 那一行共用）。网页版用同一套说法，只有一条必须换：浏览器看不到手机的文件
  // 系统，没有"在文件夹里显示"这个动作，所以那一条是「下载」，并且写明存到哪儿。

  var fileMenuNode = null;

  /** 下拉菜单（扫描 ▾ / 传输页 ⋯）：同一时刻只开一个，点别处就收起。 */
  var openMenu = null;

  function closeMenus() {
    if (!openMenu) return;
    openMenu.menu.hidden = true;
    openMenu.button.setAttribute('aria-expanded', 'false');
    openMenu = null;
  }

  function toggleMenu(button, menu) {
    if (!button || !menu) return;
    var was = openMenu && openMenu.menu === menu;
    closeMenus();
    if (was) return;
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    openMenu = { button: button, menu: menu };
  }

  /** 收起菜单：点别处、按 Esc、滚动、换页都算。 */
  function closeFileMenu() {
    if (!fileMenuNode || fileMenuNode.hidden) return;
    fileMenuNode.hidden = true;
    clear(fileMenuNode);
  }

  /** 在 (x, y)（视口坐标）弹出动作菜单；一项 = {label, hint, run}。 */
  function showFileMenu(items, x, y) {
    if (!fileMenuNode) {
      fileMenuNode = h('div', { class: 'file-menu', id: 'file-menu', role: 'menu' });
      document.body.appendChild(fileMenuNode);
    }
    clear(fileMenuNode);
    items.forEach(function (item) {
      fileMenuNode.appendChild(h('button', {
        class: 'file-menu-item', type: 'button', role: 'menuitem',
        onclick: function () { closeFileMenu(); item.run(); }
      },
        h('span', { class: 'menu-label', text: item.label }),
        item.hint ? h('span', { class: 'menu-hint', text: item.hint }) : null
      ));
    });
    // 先摆出来再量尺寸：贴着手指/鼠标那一角，但不许越出屏幕（窄屏尤其明显）。
    fileMenuNode.style.left = '0px';
    fileMenuNode.style.top = '0px';
    fileMenuNode.hidden = false;
    var box = fileMenuNode.getBoundingClientRect();
    var left = Math.max(8, Math.min(x, window.innerWidth - box.width - 8));
    var top = Math.max(8, Math.min(y, window.innerHeight - box.height - 8));
    fileMenuNode.style.left = Math.round(left) + 'px';
    fileMenuNode.style.top = Math.round(top) + 'px';
  }

  /** 「打开」：新标签页交给浏览器（图片、视频、文本就地看；认不出类型的会下载）。 */
  function openFileExternally(url) {
    if (!url) return;
    var opened = window.open(url, '_blank', 'noopener');
    if (!opened) toast('浏览器挡住了新窗口：改用「下载」或「预览」', 'error');
  }

  /** 「下载」/「另存为」：一个带 download 的链接，和页面上那些下载按钮同一条路。 */
  function downloadFrom(url, name) {
    if (!url) return;
    var link = h('a', { href: url, download: name || '' });
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  /** 一个文件的四个动作。参数：{name, size, url, source}。
   *
   *  「另存为」在网页版就是同一条 download 链接（用户的说法）：能不能弹出"存到
   *  哪儿"的对话框由浏览器决定 —— 手机浏览器没有这个对话框，文件统一进「下载」。
   */
  function fileActionItems(options) {
    var name = options.name || '文件';
    var url = options.url || '';
    var source = options.source || previewSourceFromUrl(name, options.size, url);
    var kind = previewKindFromName(name);
    var items = [
      {
        label: '预览', hint: '在这里看一眼内容，不用先下载',
        run: function () { openPreview(source); }
      },
      {
        label: '打开', hint: '用浏览器打开；认不出类型的会直接下载',
        run: function () { openFileExternally(url); }
      },
      {
        // §4：网页版没有「打开所在文件夹」（浏览器打不开电脑的文件管理器），
        // 那一项换成「下载」，并在提示里写明 —— 不是漏做。
        label: '下载', hint: '存到手机的「下载」目录（网页版没有"打开所在文件夹"）',
        run: function () { downloadFrom(url, name); }
      },
      {
        label: '另存为…', hint: '和「下载」同一条路，存到哪儿由浏览器决定',
        run: function () { downloadFrom(url, name); }
      },
      {
        label: '复制文件', hint: (kind === 'image' || kind === 'text')
          ? '复制到剪贴板（图片、文本可以；其它类型复制不了）'
          : '这一类文件复制不了（只有文本和图片能进剪贴板）',
        run: function () { copyFile(url, name, kind); }
      },
      {
        label: '重新发送', hint: '再发给选中的设备（原文件不在浏览器里时做不到，请重新选文件）',
        run: function () { resendFile(options); }
      },
      {
        label: '属性', hint: '名字 / 大小 / 类型 / 时间（网页版没有路径与 SHA-256）',
        run: function () { showFacts(name, options.size, options.path || '', options.url || ''); }
      }
    ];
    return items.filter(function (item) { return item; });
  }

  /** 「复制文件」：文本读进来、图片走 ClipboardItem，其它类型老实地说明白。 */
  function copyFile(url, name, kind) {
    if (!url) { toast('这个文件不在浏览器能读的地方', 'error'); return; }
    if (!navigator.clipboard) { toast('这个浏览器不给剪贴板权限（需要 HTTPS 或 localhost）', 'error'); return; }
    var isImage = kind === 'image';
    var isText = kind === 'text' || kind === '';
    if (!isImage && !isText) { toast('这一类文件复制不了：只有文本和图片能进剪贴板', 'error'); return; }
    fetch(url).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return isImage ? response.blob() : response.text();
    }).then(function (payload) {
      if (isImage) {
        return navigator.clipboard.write([new ClipboardItem({ [payload.type || 'image/png']: payload })]);
      }
      return navigator.clipboard.writeText(payload);
    }).then(function () {
      toast('已复制「' + name + '」到剪贴板');
    }).catch(function (error) {
      toast('复制失败：' + error.message, 'error');
    });
  }

  /** 「重新发送」：把还在待发区里的同一个文件再发一次。
   *
   *  手机上从「下载」目录里翻回原文件是做不到的（浏览器没有文件系统访问权），
   *  所以能重发的只有"这个页面还拿着 File 对象"的那些 —— 其余情况说明白。 */
  function resendFile(options) {
    var wanted = String((options && options.name) || '');
    var file = (store.picked || []).find(function (item) { return item.name === wanted; });
    if (!file) {
      toast('原文件不在浏览器里了：点「传输文件」重新选一次「' + wanted + '」', 'error');
      return;
    }
    var device = selectedDevice();
    if (!device) { toast('先在左边选一台设备', 'error'); return; }
    toast('把「' + wanted + '」再加进待发区，点「发送」就发出去');
    renderPicked();
    switchView('session');
  }

  /** 「属性」：名字 / 大小 / 类型 / 时间；网页版没有路径，SHA-256 也不下发（写明）。 */
  function showFacts(name, size, path, url) {
    var body = $('#facts-body');
    var title = $('#facts-title');
    if (!body || !title) return;
    title.textContent = name || '文件';
    clear(body);
    var kind = previewKindLabel(previewKindFromName(name)) || '未知类型';
    var rows = [
      ['名字', name || '—'],
      ['大小', fmtBytes(size || 0)],
      ['类型', kind],
      ['路径', '网页版看不到（浏览器拿不到电脑上的路径）'],
      ['时间', path ? path : '以文件自身的修改时间为准'],
      ['SHA-256', '网页版不下发（在电脑端的「属性」里看）']
    ];
    rows.forEach(function (pair) {
      body.appendChild(h('dt', { text: pair[0] }));
      body.appendChild(h('dd', { text: pair[1] }));
    });
    $('#facts-modal').hidden = false;
  }

  /** 给一个文件卡片挂右键 / 长按（触屏没有右键，按 650ms 等价）。 */
  function bindFileActions(node, build) {
    var timer = null;
    var openedAt = 0;
    // 名字避开页面上已有的那些（selftest 会查重名；重名的最后一个悄悄生效）。
    function cancelPress() {
      if (timer) { clearTimeout(timer); timer = null; }
    }
    function openMenuAt(x, y) {
      openedAt = Date.now();
      showFileMenu(build(), x, y);
    }
    node.addEventListener('contextmenu', function (event) {
      event.preventDefault();
      cancelPress();
      openMenuAt(event.clientX, event.clientY);
    });
    node.addEventListener('touchstart', function (event) {
      cancelPress();
      var touch = event.touches && event.touches[0];
      if (!touch) return;
      var x = touch.clientX;
      var y = touch.clientY;
      timer = setTimeout(function () { timer = null; openMenuAt(x, y); }, 650);
    }, { passive: true });
    ['touchend', 'touchmove', 'touchcancel'].forEach(function (name) {
      node.addEventListener(name, cancelPress, { passive: true });
    });
    // 长按弹过菜单之后浏览器还会补一次 click：那一下不能再当"点开图片"。
    node.addEventListener('click', function (event) {
      if (Date.now() - openedAt < 700) {
        event.preventDefault();
        event.stopPropagation();
      }
    }, true);
    return node;
  }

  /** 一个可以右键的文件卡片：挂上菜单，顺便标出来"这里能右键"。 */
  function withFileActions(node, options) {
    node.classList.add('has-file-menu');
    node.title = (node.title ? node.title + ' · ' : '') + '右键（长按）有更多动作';
    return bindFileActions(node, function () { return fileActionItems(options); });
  }

  // ------------------------------------------------------------------ chat

  var emojiOpen = false;
  var recorder = { media: null, chunks: [], started: 0, timer: null, state: 'idle' };
  //: 会话里那些"进度长在气泡下面"的附件（和电脑端的 _media_bubbles 一个意思）。
  var mediaProgress = [];
  //: 本机正在发给电脑的那个附件（浏览器自己的上传进度）。
  var chatUpload = null;

  function chatUnread() {
    return (store.chat && store.chat.unread) || 0;
  }

  function openConversation(convId) {
    store.conversationId = convId || '';
    if (store.chatUnavailable) {
      store.messages = [];
      renderChat();
      return Promise.resolve();
    }
    return refreshChat();
  }

  /** 选中设备对应的那条会话。
   *
   *  - 本机（给你这个页面的电脑）：我和它的 1:1。还没建过也没关系 ——
   *    会话 id 两边算出来是一样的（"d:" + 排序后的两个成员 id）。
   *  - 别的电脑：只有已经存在的那条会话（聊天记录存在电脑上，它负责转发）。
   *  - 什么都没有：返回 null，意思是"这台设备只能收文件"。
   */
  function conversationForDevice(device) {
    if (!device) return '';
    var convs = (store.chat && store.chat.conversations) || [];
    var mine = (store.chat && store.chat.selfId) || '';
    var withMe = convs.filter(function (c) {
      var members = c.members || [];
      return members.indexOf(device.id) >= 0 && (!mine || members.indexOf(mine) >= 0);
    });
    if (withMe.length) return withMe[0].id;
    if (!device.local) return null;
    var own = convs.find(function (c) { return mine && (c.members || []).indexOf(mine) >= 0; });
    if (own) return own.id;
    if (mine) return 'd:' + [String(device.id), String(mine)].sort().join('|');
    return '';
  }

  function selectedDevice() {
    return store.devices.find(function (d) { return d.id === store.selectedDeviceId; }) || null;
  }

  function currentGroup() {
    if (!store.groupId) return null;
    var convs = (store.chat && store.chat.conversations) || [];
    return convs.find(function (c) { return c.id === store.groupId; }) || null;
  }

  /** 侧栏点一行设备：选中它，主区回到会话页，输入区跟着换目标。 */
  function selectDevice(deviceIdValue) {
    store.selectedDeviceId = deviceIdValue;
    store.groupId = '';
    var device = selectedDevice();
    var convId = conversationForDevice(device);
    store.chatUnavailable = convId === null;
    closeDrawer();
    // 会话先对上号再切页：否则 switchView 会拿上一条会话去拉消息。
    openConversation(convId === null ? '' : convId);
    switchView('session');
    renderDevices();
    renderConversations();
    renderHeader();
    updateSendButton();
  }

  /** 侧栏点一条群聊：主区显示群聊，而不是选中的设备。 */
  function openGroup(convId) {
    store.groupId = convId || '';
    store.chatUnavailable = false;
    closeDrawer();
    openConversation(convId);
    switchView('session');
    renderConversations();
    renderHeader();
  }

  /** 自动选中（第一台设备）或列表变化后，把选中的设备和会话对上号。 */
  function syncSelection() {
    var device = selectedDevice();
    if (!device) { store.chatUnavailable = false; return; }
    if (store.groupId) return;
    var convId = conversationForDevice(device);
    store.chatUnavailable = convId === null;
    var wanted = convId === null ? '' : convId;
    if (wanted === store.conversationId) return;
    store.conversationId = wanted;
    if (store.chatUnavailable) {
      store.messages = [];
      renderChat();
      return;
    }
    refreshChat();
  }

  function refreshChat() {
    if (store.chatUnavailable) return Promise.resolve();
    var query = '/api/chat' + (store.conversationId
      ? '?conv=' + encodeURIComponent(store.conversationId) : '');
    return api(query).then(function (data) {
      store.chat = {
        selfId: data.selfId || (store.chat && store.chat.selfId) || '',
        conversations: data.conversations || [],
        unread: (data.conversations || []).reduce(function (sum, c) { return sum + (c.unread || 0); }, 0)
      };
      // 服务端在没有指定会话时会给列表里的第一条。如果那一条根本不是"我和这台
      // 电脑"的会话（比如是别的电脑和它的），不要张冠李戴 —— 先当还没有会话。
      if (!store.groupId && !store.conversationId && data.conversationId) {
        var conv = (data.conversations || []).find(function (c) { return c.id === data.conversationId; });
        var mine = store.chat.selfId;
        if (conv && mine && (conv.members || []).indexOf(mine) < 0) {
          store.messages = [];
          renderChat();
          return;
        }
      }
      if (data.conversationId) store.conversationId = data.conversationId;
      store.messages = data.messages || [];
      renderChat();
      renderHeader();
    }).catch(function () { /* the connection badge already says it */ });
  }

  /** 成员 id → 已配对的那台手机（网页客户端的几种写法都认：web:x / android:x / x）。
   *
   *  手机的 id 有三副面孔：App 用 /api/hello 登记成 ``android:<deviceId>``，发消息
   *  时请求头里带的是 ``web:<deviceId>``，老记录还可能是"地址|UA摘要"式的键。
   *  会话里存的成员 id 是当时那一个，只按一种写法查就会查不到名字。
   *
   *  判据用 identityVariants（= 服务端 core.chat.client_aliases 的等价物）：记录上
   *  被归并掉的旧身份（``aliases``）也算，否则那段 289 条消息的老会话会掉成
   *  「未知设备」。
   */
  function clientForMember(member) {
    var wanted = bareIdentity(member);
    if (!wanted) return null;
    var known = (store.knownClients || []).find(function (client) {
      return identityVariants(client).indexOf(wanted) >= 0;
    });
    if (known) return known;
    return (store.devices || []).find(function (device) {
      return identityVariants(device).indexOf(wanted) >= 0;
    }) || null;
  }

  /** 成员 id → 设备自称的名字（V2408A）。查不到给空串。 */
  function memberName(member) {
    var found = clientForMember(member);
    if (!found) return '';
    var name = String(found.name || '').trim();
    return name === '未知设备' ? '' : name;
  }

  /** 这些是"客户端类型"，不是设备名字（历史消息里存的就是它们）。
   *
   *  服务端现在按设备自称的名字存（web/server.py 的 ``_self_client_name``），
   *  但以前的消息存的是类型名。手机自己叫 V2408A，页面上就不该写「安卓 App」——
   *  显示时拿设备列表里的名字兜底，老消息也跟着对了。
   */
  var GENERIC_SENDER_NAMES = ['安卓 App', '网页版', '手机', '未知客户端', '命令行/脚本'];

  /** 一条消息署谁的名：优先设备自己的名字，类型名/空值去设备和配对列表里找。 */
  function senderLabel(message) {
    var stored = String((message && message.senderName) || '').trim();
    if (stored && GENERIC_SENDER_NAMES.indexOf(stored) < 0) return stored;
    var name = memberName(message && message.sender);
    if (name) return name;
    if (message && message.direction === 'out') return '我';
    return stored || '对方';
  }

  /** 群聊的成员，用和设备列表同一套字段写出来。 */
  function memberLine(conv) {
    return ((conv && conv.members) || []).map(function (id) {
      if (id === (store.chat.selfId || '')) return '我';
      var found = clientForMember(id);
      // 配过对的手机（knownClients）带 clientKind；协议对端（电脑）带 platform。
      if (found && found.clientKind) {
        var parts = [found.clientKind === 'app' ? '安卓 App' : '网页版'];
        if (found.version) parts.push('韧传 ' + found.version);
        if (found.address) parts.push(found.address);
        // 名字优先用设备自称的那个（V2408A），label（"手机"）只是类型说明。
        return (found.name || found.label || '手机') + '（' + parts.join(' · ') + '）';
      }
      if (found) {
        var bits = [platformName(found.platform) || '电脑'];
        if (found.version) bits.push('韧传 ' + found.version);
        if (found.address) bits.push(found.address);
        return (found.name || '未知设备') + '（' + bits.join(' · ') + '）';
      }
      return id.slice(0, 8);
    }).join(' · ');
  }

  /** 侧栏「群聊」那一段：只有群聊进来，一对一挂在设备行上。 */
  function renderConversations() {
    var list = $('#conv-list');
    var groups = ((store.chat && store.chat.conversations) || []).filter(function (conv) {
      return conv.kind === 'group' || String(conv.id).indexOf('g:') === 0;
    });
    clear(list);
    groups.forEach(function (conv) {
      var active = conv.id === store.groupId;
      list.appendChild(h('li', {
        class: 'conv-item' + (active ? ' is-active' : ''),
        onclick: function () { openGroup(conv.id); }
      },
        h('span', { class: 'conv-title', text: conv.displayTitle || conv.title || '群聊' }),
        conv.unread ? h('span', { class: 'badge', text: String(conv.unread) }) : null,
        h('span', { class: 'conv-last muted', text: conv.lastText || '' })
      ));
    });
    $('#group-title').hidden = groups.length === 0;
    $('#group-sep').hidden = groups.length === 0;
  }

  /** ④ 会话顶部：对方设备的详细信息（名字、平台、id、地址、发现方式、状态）。 */
  function renderHeader() {
    var group = currentGroup();
    var device = group ? null : selectedDevice();
    var tags = $('#peer-tags');
    var pin = $('#pin');
    clear(tags);
    if (pin) pin.classList.toggle('is-needed', !group && pinNeeded());

    if (group) {
      $('#peer-avatar').textContent = '👥';
      $('#chat-title').textContent = group.displayTitle || group.title || '群聊';
      $('#peer-facts').textContent = '群聊 · ' + ((group.members || []).length) + ' 人 · 记录存在电脑上';
      $('#chat-members').textContent = memberLine(group);
      tags.appendChild(h('span', { class: 'tag', text: '群聊' }));
      $('#btn-chat-back').hidden = false;
      $('#mobile-title').textContent = group.displayTitle || group.title || '群聊';
      return;
    }

    $('#btn-chat-back').hidden = true;
    if (!device) {
      $('#peer-avatar').textContent = '💻';
      $('#chat-title').textContent = '从左边选一台设备';
      $('#peer-facts').textContent = '左边点一台设备，这里显示它的地址、系统和状态。';
      $('#chat-members').textContent = '';
      $('#mobile-title').textContent = '会话';
      return;
    }

    $('#peer-avatar').textContent = platformIcon(device.platform, device.kind);
    // 会话标题和设备列表用同一句话：两台设备真的重名时，标题上带地址，
    // 切过去一眼能看出换了一台（否则主区看上去完全没变）。
    var title = displayName(device, nameCounts(deviceRows())[deviceLabel(device)] > 1);
    $('#chat-title').textContent = title;
    var facts = [];
    facts.push(platformName(device.platform) || (device.kind === 'server' ? '电脑' : (device.kind || '设备')));
    if (device.version) facts.push('韧传 ' + device.version);
    facts.push('id ' + (device.id ? String(device.id).slice(0, 8) : '—'));
    facts.push(deviceAddress(device));
    facts.push(sourceName(device));
    $('#peer-facts').textContent = facts.join(' · ');
    $('#chat-members').textContent = device.local
      ? '这台电脑就是给你这个页面的那台'
      : (device.trusted ? '已信任：它发过来的文件直接收' : '还没信任：它发文件过来会先问你');
    if (device.local) tags.appendChild(h('span', { class: 'tag is-self', text: '本机' }));
    else tags.appendChild(statusChip(device.online !== false, device.secondsAgo, false));
    if (device.trusted && !device.local) tags.appendChild(h('span', { class: 'tag is-trusted', text: '已信任' }));
    if (device.webUrl && !device.local) tags.appendChild(h('span', { class: 'tag', text: '可网页打开' }));
    if (pinNeeded()) tags.appendChild(h('span', { class: 'tag is-pin', text: '需要 PIN' }));
    else if (pin && pin.value.trim()) tags.appendChild(h('span', { class: 'tag is-pin', text: 'PIN 已填' }));
    $('#mobile-title').textContent = title;
  }

  function renderChat() {
    // 未读徽标原来挂在主区顶部那个 [会话｜传输] 分段控件上；那个分段控件是
    // 「一个意图两个入口」里的第二个，已经删掉 —— 未读只在侧栏群聊那一行显示
    // （见 renderConversations），不再另起一个入口。
    renderConversations();

    var thread = $('#msg-list');
    var empty = $('#chat-empty');
    var box = $('#msg-list');
    var input = $('#chat-input');
    var sendButton = $('#btn-chat-send');
    clear(box);
    if (input) input.disabled = store.chatUnavailable;
    if (sendButton) sendButton.disabled = store.chatUnavailable;

    if (store.chatUnavailable) {
      thread.hidden = true;
      empty.hidden = false;
      $('#chat-empty-title').textContent = '这台设备只能传文件';
      $('#chat-empty-hint').textContent =
        '聊天是网页版这台电脑上的会话：在左边「设备列表」里点「本机」。' +
        '选中的其他设备，用下面的「传输文件」把文件传过去。';
      return;
    }

    empty.hidden = !!store.conversationId;
    thread.hidden = !store.conversationId;
    if (!store.conversationId) {
      $('#chat-empty-title').textContent = '还没有会话';
      $('#chat-empty-hint').textContent =
        '在左边「设备列表」里点一台设备就开始：文字、表情、图片、视频、文件、语音都在这里收发。' +
        '语音需要 HTTPS 地址（见「设置」），因为浏览器只在安全上下文里给麦克风。';
      return;
    }

    if (!store.messages.length && !chatUpload) {
      box.appendChild(h('li', { class: 'msg-system', text: '还没有消息。' }));
    }
    mediaProgress = [];
    store.messages.forEach(function (message) {
      box.appendChild(messageNode(message));
    });
    // 正在发的那个附件：会话里先占一行，进度就写在这一行上（发送成功之后它变成
    // 真正的气泡，从 /api/chat 里读回来）。
    if (chatUpload) box.appendChild(chatUploadNode());
    paintMediaProgress();
    paintChatUpload();
    box.scrollTop = box.scrollHeight;
  }

  /** 本机正在发的附件：一个"还没落地的气泡"，只有名字和进度。 */
  function chatUploadNode() {
    return h('li', { class: 'msg is-mine is-pending', id: 'chat-upload' },
      h('span', { class: 'msg-meta muted', text: '我 · 正在发送' }),
      h('span', { class: 'msg-media' },
        h('span', { class: 'msg-file' },
          h('span', { text: '📄 ' + (chatUpload.name || '附件') }),
          h('span', { class: 'muted', text: chatUpload.size ? ' ' + fmtBytes(chatUpload.size) : '' })
        ),
        h('span', { class: 'msg-progress', id: 'chat-upload-progress', text: '⬆ 发送中 0%' })
      )
    );
  }

  /** 本机上传的进度：XHR 拿得到已经出去多少字节，就照实写。 */
  function paintChatUpload() {
    var node = $('#chat-upload-progress');
    if (!node || !chatUpload) return;
    var percent = chatUpload.size
      ? Math.min(100, Math.round(100 * (chatUpload.loaded || 0) / chatUpload.size))
      : 0;
    node.textContent = '⬆ 发送中 ' + percent + '%'
      + (chatUpload.size ? '（' + fmtBytes(chatUpload.loaded || 0) + ' / ' + fmtBytes(chatUpload.size) + '）' : '');
  }

  /** 这个文件名正在被哪一次传输搬：进度就配到同名的气泡上。 */
  function transferForMedia(name) {
    var wanted = String(name || '');
    if (!wanted) return null;
    var found = null;
    (store.transfers || []).forEach(function (transfer) {
      if (transfer.status !== 'active') return;
      (transfer.items || []).forEach(function (item) {
        if (item && item.name === wanted) found = transfer;
      });
    });
    return found;
  }

  /** 把活动传输的进度写进对应的附件气泡（没有对应传输就收起来）。 */
  function paintMediaProgress() {
    mediaProgress.forEach(function (pair) {
      var found = transferForMedia(pair.message && pair.message.mediaName);
      if (!found) { pair.node.hidden = true; return; }
      var percent = Math.round(Number(found.percent) || 0);
      var speed = Number(found.speed) || 0;
      pair.node.hidden = false;
      pair.node.textContent = '⬆ 传输中 ' + percent + '%' + (speed > 0 ? ' · ' + fmtSpeed(speed) : '');
    });
  }

  // emoji 在面板里是 26px，发出去也得是同一个尺寸 —— 不然"挑的时候挺大、
  // 发出去变小"，看着像两个东西（电脑端/安卓同一个规则，见 docs/UI_LAYOUT.md §3⑥）。
  // 按码位切段：零宽连接符（👨‍👩‍👧）、变体选择符（✅）、肤色、国旗（区域指示符成对）
  // 都算一段，不会被拆成几个"半个表情"。
  var EMOJI_RUN = /[\u{1F1E6}-\u{1F1FF}\u{1F300}-\u{1F5FF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}\u{1F3FB}-\u{1F3FF}]+/gu;

  function emojiNodes(text) {
    var plain = String(text == null ? '' : text);
    if (!plain) return [];
    var nodes = [];
    var last = 0;
    var match;
    EMOJI_RUN.lastIndex = 0;
    while ((match = EMOJI_RUN.exec(plain)) !== null) {
      if (match.index > last) nodes.push(document.createTextNode(plain.slice(last, match.index)));
      nodes.push(h('span', { class: 'emoji', text: match[0] }));
      last = match.index + match[0].length;
    }
    if (!nodes.length) return [];
    if (last < plain.length) nodes.push(document.createTextNode(plain.slice(last)));
    return nodes;
  }

  /** 一个消息正文节点：有 emoji 就把 emoji 段单独放大，其余照旧。 */
  function textNode(text, className) {
    var emojis = emojiNodes(text);
    if (!emojis.length) return h('span', { class: className, text: text || '' });
    return appendNodes(h('span', { class: className }), emojis);
  }

  function messageNode(message) {
    var mine = message.direction === 'out';
    var body = [];
    if (message.kind === 'text' || message.kind === 'system') {
      body.push(textNode(message.text || '', 'msg-text'));
    } else {
      body.push(attachmentNode(message));
    }
    return h('li', { class: 'msg' + (mine ? ' is-mine' : '') + (message.kind === 'system' ? ' is-system' : '') },
      h('span', { class: 'msg-meta muted',
        // 署名是设备自称的名字（V2408A），不是「安卓 App」这种类型名。
        text: senderLabel(message) + ' · ' + formatClock(message.ts) }),
      appendNodes(h('span', { class: 'msg-body' }), body),
      message.state === 'failed' ? h('span', { class: 'state-err', text: '发送失败（对方不在线）' }) : null
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
    var source = previewSourceFromMessage(message);
    // 附件一律给一个下载链接：能看的东西也要能存下来（用户明确要的）。
    function downloadLink(label) {
      return h('a', { class: 'msg-save', href: url, download: name, text: '⬇ ' + label });
    }
    var card;
    if (message.kind === 'image') {
      card = h('span', { class: 'msg-media' },
        // 以前点图片开一个新标签页；现在跟电脑端一样在页内弹层里看（§6）。
        h('img', {
          class: 'msg-image', src: url, alt: name, loading: 'lazy',
          title: '点一下在这里看大图',
          onclick: function () { openPreview(source); }
        }),
        h('span', { class: 'msg-actions' }, previewButton(source), downloadLink('保存图片')),
      );
    } else if (message.kind === 'video') {
      card = h('span', { class: 'msg-media' },
        h('video', { class: 'msg-video', src: url, controls: 'controls', preload: 'metadata' }),
        h('span', { class: 'msg-actions' }, previewButton(source), downloadLink('保存视频')),
      );
    } else if (message.kind === 'voice') {
      var seconds = message.durationMs ? Math.round(message.durationMs / 1000) : 0;
      card = h('span', { class: 'msg-voice' },
        h('audio', { src: url, controls: 'controls', preload: 'metadata' }),
        h('span', { class: 'muted', text: '语音' + (seconds ? ' · ' + seconds + ' 秒' : '') }),
        h('span', { class: 'msg-actions' }, previewButton(source), downloadLink('保存语音'))
      );
    } else {
      card = h('span', { class: 'msg-media' },
        h('a', { class: 'msg-file', href: url, download: name },
          h('span', { text: '📄 ' + name }),
          h('span', { class: 'muted', text: ' ' + fmtBytes(message.mediaSize || 0) })
        ),
        h('span', { class: 'msg-actions' }, previewButton(source), downloadLink('保存文件'))
      );
    }
    // 进度长在这条消息下面：和电脑端同一个做法（按文件名把活动传输配到气泡上，
    // 见 desktop/chat_view.py 的 _refresh_media_progress）。没有对应的传输就收起来。
    var progress = h('span', { class: 'msg-progress', hidden: true });
    card.appendChild(progress);
    mediaProgress.push({ node: progress, message: message });
    // 右键（手机上长按）：预览 / 打开 / 下载 / 另存为 —— 和电脑端卡片同一套动作。
    return withFileActions(card, {
      name: name, size: message.mediaSize || 0, url: url, source: source
    });
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
    if (!text) return;
    if (store.chatUnavailable) {
      toast('选中的这台设备只能传文件；聊天请选中「本机」那台电脑。', 'error');
      return;
    }
    if (!store.conversationId) {
      toast('还没有会话：先在左边「设备列表」里点一台设备。', 'error');
      return;
    }
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

  /** 发一个附件（图片/视频/语音/文件）到当前会话。
   *
   *  以前用 fetch：拿不到上传进度，会话里也看不见"正在发的那个"。现在走 XHR，
   *  进度写在会话里那一行上（气泡是发完之后才有的 —— 服务端存下来才有消息 id）。
   */
  function uploadAttachment(file, kind, durationMs) {
    if (!store.conversationId) return Promise.resolve();
    var name = file.name || ('voice-' + Date.now() + '.webm');
    var query = '/api/chat/upload?name=' + encodeURIComponent(name) +
      '&conv=' + encodeURIComponent(store.conversationId) +
      '&kind=' + encodeURIComponent(kind || 'file') +
      '&duration=' + encodeURIComponent(durationMs || 0);
    chatUpload = { name: name, size: Number(file.size) || 0, loaded: 0 };
    if (store.page === 'session') renderChat();
    return new Promise(function (resolve, reject) {
      var request = new XMLHttpRequest();
      request.open('POST', query, true);
      request.setRequestHeader('X-EverSend-Token', TOKEN);
      request.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
      var headers = deviceHeaders();
      Object.keys(headers).forEach(function (key) {
        request.setRequestHeader(key, headers[key]);
      });
      request.upload.onprogress = function (event) {
        if (!event.lengthComputable) return;
        chatUpload.loaded = event.loaded;
        paintChatUpload();
      };
      request.onload = function () {
        var data = null;
        try { data = JSON.parse(request.responseText); } catch (err) { data = null; }
        chatUpload = null;
        if (request.status >= 200 && request.status < 300 && data && data.ok) {
          resolve(data);
          return;
        }
        reject(new Error((data && data.error) || ('HTTP ' + request.status)));
      };
      request.onerror = function () {
        chatUpload = null;
        reject(new Error('网络中断（熄屏？）'));
      };
      request.ontimeout = request.onerror;
      request.onabort = function () {
        chatUpload = null;
        reject(new Error('已取消'));
      };
      request.send(file);
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

  /** 只拉分享表（小响应），SSE 通知到了就调它 —— 不必等整个 /api/state。 */
  function refreshShares() {
    return api('/api/shares').then(function (data) {
      store.shares = data.shares || [];
      renderShares();
      renderNav();
    }).catch(function () { /* 下一次轮询会补上 */ });
  }

  function renderShares() {
    // Files the desktop handed over for this phone.  A browser cannot be
    // pushed to, so this list *is* the "receive" direction: the computer puts
    // the file here and the phone pulls it with one tap.
    var signature = store.shares.map(function (s) {
      return s.id + '|' + s.size + '|' + (s.downloaded ? 1 : 0) + '|' + (s.sentBytes || 0);
    }).join(',');
    if (!changed('shares', signature)) return;
    var card = $('#share-card');
    var list = $('#share-list');
    clear(list);
    card.hidden = store.shares.length === 0;
    if (!store.shares.length) return;
    store.shares.forEach(function (share) {
      var url = '/api/share/' + encodeURIComponent(share.id);
      list.appendChild(withFileActions(h('li', { class: 'file-item' },
        h('span', { class: 'fname', title: share.name, text: share.name }),
        h('span', { class: 'fsize', text: fmtBytes(share.size) }),
        // 先看一眼是什么，再决定要不要占手机的空间（同样是那条下载接口）。
        previewButton(previewSourceFromUrl(share.name, share.size, url), 'file-act'),
        // Say plainly whether this phone already took it.  The download itself
        // goes straight to the phone's Downloads folder, which a web page
        // cannot look at -- so this marker is the only "received" record the
        // page can honestly show.
        share.downloaded
          ? h('span', { class: 'state-ok', text: '✔ 已下载' })
          : (Number(share.sentBytes) > 0
            // 上次取到一半（网络断了、页面被切走）：那是服务端记下的**总体进度**，
            // 再点一次「下载」浏览器会重新发请求，服务端从已有进度接着算。
            ? h('span', { class: 'muted share-took', text: '上次取到 ' +
                Math.min(99, Math.round(100 * Number(share.sentBytes) / Math.max(1, Number(share.size) || 1))) + '%' })
            : null),
        h('a', {
          href: url,
          download: share.name,
          text: share.downloaded ? '再下载' : '下载',
          onclick: function () { noteShareDownload(share); }
        })
      ), { name: share.name, size: share.size, url: url }));
    });
  }

  function renderSelf() {
    var device = store.device || {};
    var signature = [device.id, device.name, device.platform, store.receiveDir, store.freeSpace,
      (device.urls || []).join(',')].join('|');
    if (!changed('self', signature)) return;
    // 侧栏品牌行第二行：本机名 + 地址，别人能照着念、照着输。
    var address = (device.addresses && device.addresses[0]) || '';
    $('#self-name').textContent = device.name || '本机';
    $('#self-addr').textContent = address
      ? (address + (device.webPort ? ':' + device.webPort : ''))
      : (store.receiveDir || '地址读取中…');
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
    syncSelection();
    renderHeader();
    renderConversations();
    renderNav();
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

  /** 侧栏底部两项：传输（进行中数量徽标）+ 设置。 */
  function renderNav() {
    // 窄屏顶栏那个标题也顺手写掉：它以前只写"会话/传输/设置"，现在带上未读数
    // —— 主区顶部那个带未读徽标的分段控件已经删了，未读得有地方看得见。
    var title = $('#mobile-title');
    if (title) {
      var unread = chatUnread();
      title.textContent = (VIEW_TITLE[store.page] || '会话') +
        (store.page === 'session' && unread ? '（' + unread + ' 条未读）' : '');
    }
    var active = store.transfers.filter(function (t) { return t.status === 'active'; }).length;
    var count = active + store.offers.length + (uploader.active ? 1 : 0);
    var badge = $('#offer-badge');
    badge.hidden = count === 0;
    badge.textContent = String(count);
    badge.title = store.offers.length
      ? (store.offers.length + ' 个待确认 + ' + active + ' 个进行中')
      : (count + ' 个进行中');
    $('#nav-transfers').classList.toggle('is-active', store.page === 'transfers');
    $('#nav-settings').classList.toggle('is-active', store.page === 'settings');
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
          nodes.speedNode.textContent = window.innerWidth <= 560
            ? fmtSpeed(rate) + ' · ' + fmtDuration(eta)
            : fmtSpeed(rate) + ' · 剩余 ' + fmtDuration(eta);
        } else {
          nodes.speedNode.textContent = '';
        }
      }
    });
    // 会话里那些附件的进度（和电脑端一样，每 250ms 跟着活动传输走）。
    paintMediaProgress();
    paintChatUpload();
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
      // 文件一落地，「正在传」那一行就该收起来（只留「电脑上的文件」里那一条）。
      // 不能只等 3 秒一次的 /api/state：那期间同一个文件会占两行。
      renderTransfers();
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
    // 窄屏时侧栏是抽屉，顶上那个小圆点就是"还连着吗"。
    var dot = $('#conn-dot');
    if (dot) {
      dot.classList.toggle('is-live', mode === 'live');
      dot.classList.toggle('is-poll', mode === 'poll');
      dot.classList.toggle('is-down', mode === 'down');
      dot.title = mode === 'live' ? '已连接' : mode === 'poll' ? '轮询中' : '已断开';
    }
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
    chat_message: 1, chat_sent: 1,
    // 电脑一发布文件就推过来，不用等下一次轮询（"发布即通知"）。
    share_added: 1, share_taken: 1
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
        // 侧栏「传输」上的徽标 + 一句提示就够了；正在打字的时候把页面抢走更烦人。
        toast('收到来自 ' + deviceLabel(event.peer || {}) + ' 的 ' + ((event.files || []).length) +
          ' 个文件，去「传输 → 接收」里点接受');
        if (store.page === 'transfers') switchTransferSeg('receive');
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
        // 同样按设备名署名（服务端改好了，这里兜住老消息）。
        toast('💬 ' + senderLabel(chat) + '：' +
          (chat.text || { image: '[图片]', video: '[视频]', voice: '[语音]', file: '[文件]' }[chat.kind] || '新消息'));
        break;
      case 'share_added':
        // 电脑刚把文件发布出来：手机**立刻**知道有几件事要办，不用等 3 秒一次的
        // 轮询。文案只报数，不抢页面（用户可能正在打字）。
        toast('电脑发来 ' + (Number(event.total) > 1 ? Number(event.total) + ' 个文件' :
          '「' + (event.name || '一个文件') + '」') + '，去「传输 → 接收」里点「下载」', 'ok');
        if (store.page === 'transfers') switchTransferSeg('receive');
        refreshShares();
        break;
      case 'share_taken':
        // 回执：整份文件都发出去了（服务端 mark_share_downloaded 才会发这一条）。
        refreshShares();
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

  /** 选好的文件先排进输入框上面那一行的方块，点「发送」才真的开始传。
   *
   *  以前一选文件就立刻上传 —— 选错了也没有反悔的余地，PIN 更是来不及填。
   */
  function stageFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    files.forEach(function (file) { store.picked.push(file); });
    renderPicked();
    updateSendButton();
  }

  /** 「发送」：把排好的文件交给上传队列，一个接一个传。 */
  function sendPicked() {
    if (!store.picked.length || uploader.active) return;
    var device = selectedDevice();
    if (!device) {
      toast('请先选择目标设备', 'error');
      return;
    }
    store.picked.forEach(function (file) { uploader.queue.push(file); });
    store.picked = [];
    renderPicked();
    renderUpload();
    updateSendButton();
    pump();
  }

  function pump() {
    if (uploader.active || !uploader.queue.length) {
      if (!uploader.active) updateSendButton();
      return;
    }
    var device = selectedDevice();
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
    request.setRequestHeader('X-EverSend-Name', encodeHeader(deviceName()));
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
        // 记下传输号：服务端（以及传输表里那一行）就是靠它认领这条记录的。
        transferId: (payload && payload.transferId) || '',
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

  // --------------------------------------------------------------- 页面切换
  //
  // 主区就一页三态：会话 / 传输 / 设置（docs/ENTERPRISE_CHARTER.md §3）。
  // **入口只有一处**：侧栏底部的「传输」「设置」+ 点一台设备进会话 ——
  // 主区顶部原来那套 [会话｜传输] 分段控件是同一个意图的第二个入口，已经删掉。

  var VIEW_ALIAS = { send: 'transfers', receive: 'transfers', chat: 'session', about: 'settings' };
  var VIEW_TITLE = { session: '会话', transfers: '传输', settings: '设置' };

  function switchView(view, kind) {
    view = VIEW_ALIAS[view] || view;
    if (view !== 'session' && view !== 'transfers' && view !== 'settings') view = 'session';
    store.page = view;
    if (kind) store.transferKind = kind === 'send' ? 'send' : 'receive';
    $('#main').dataset.page = view;
    $('#page-session').hidden = view !== 'session';
    $('#page-transfers').hidden = view !== 'transfers';
    $('#page-settings').hidden = view !== 'settings';
    if (view === 'transfers') switchTransferSeg(store.transferKind);
    if (view === 'session') refreshChat();
    closeMenus();
    renderNav();
    renderHeader();
    closeDrawer();
  }

  /** 传输页里 [接收｜发送]：两张表共用一块区域，就地切（不跳页、不弹窗）。 */
  function switchTransferSeg(kind) {
    store.transferKind = kind === 'send' ? 'send' : 'receive';
    // 页面上带 data-seg 的东西跟着这一格走：接收表 / 发送表 / 两边的卡片。
    // 用 class 而不是 hidden —— hidden 是元素**自己**的状态（比如「电脑发来的
    // 文件」在没有内容时本来就藏着），分段控件不能把它顶开。
    Array.prototype.forEach.call(document.querySelectorAll('[data-seg]'), function (node) {
      node.classList.toggle('is-seg-off', node.getAttribute('data-seg') !== store.transferKind);
    });
    Array.prototype.forEach.call($('#transfer-seg').querySelectorAll('button'), function (button) {
      var on = button.getAttribute('data-transfer') === store.transferKind;
      button.classList.toggle('is-active', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $('#receive-empty').hidden = store.transferKind !== 'receive';
    updateTableEmpty();
  }

  // ------------------------------------------------------------------ 抽屉

  function openDrawer() {
    $('#app').classList.add('drawer-open');
    $('#scrim').hidden = false;
  }

  function closeDrawer() {
    $('#app').classList.remove('drawer-open');
    $('#scrim').hidden = true;
  }

  /** 手动添一台设备：填 ip[:port]，走 /api/peer 登记成普通设备。 */
  function addManualDevice() {
    var answer = window.prompt('对方的 IP（端口不填就用默认的）\n例如：192.168.1.23 或 192.168.1.23:52119', '');
    if (answer === null) return;
    var text = answer.trim();
    if (!text) return;
    var port = 0;
    var match = /^(.*?):(\d+)$/.exec(text);
    var address = text;
    if (match && match[1].indexOf(']') < 0) {
      address = match[1];
      port = parseInt(match[2], 10) || 0;
    }
    api('/api/peer', { method: 'POST', json: { address: address, port: port } })
      .then(function (data) {
        var device = data.device || {};
        toast('已加入 ' + (device.name || address) + (device.address ? '（' + device.address + '）' : ''));
        return refreshState();
      })
      .then(function () {
        var added = store.devices.find(function (d) {
          return d.address === address || (port && d.tcpPort === port && d.address === address);
        });
        if (added) selectDevice(added.id);
      })
      .catch(function (error) { toast('加不上：' + error.message, 'error'); });
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

  /** 「连接手机」：一个二维码弹窗（§3.1 —— 侧栏那颗按钮点了直接出二维码）。
   *  这是二维码的**唯一**入口：设置页原来那张二维码卡片已经并到这里。 */
  function openQr() {
    closeMenus();
    $('#qr-modal').hidden = false;
    loadQr();
  }

  function closeQr() {
    var modal = $('#qr-modal');
    if (modal) modal.hidden = true;
  }

  /** 「群聊」：建一个群聊会话并进去（群聊列表接在设备列表下面）。 */
  function newGroupChat() {
    closeMenus();
    var members = [];
    var me = deviceId();
    var peer = selectedDevice();
    if (peer && peer.id) members.push(String(peer.id));
    else if (store.device && store.device.id) members.push(String(store.device.id));
    if (me && members.indexOf(me) < 0) members.push(me);
    var seed = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : String(Date.now()) + '-' + Math.random().toString(16).slice(2);
    var conv = 'g:' + seed;
    api('/api/chat/send', {
      method: 'POST',
      json: { conv: conv, members: members, title: '群聊', text: '群聊已创建' }
    })
      .then(function () { return refreshState(); })
      .then(function () {
        openGroup(conv);
        toast('已建好一个群聊');
      })
      .catch(function (error) { toast('建群失败：' + error.message, 'error'); });
  }

  // ----------------------------------------------------------------- wiring

  function bind() {
    // ---- 侧栏：底部两项 + 抽屉
    Array.prototype.forEach.call(document.querySelectorAll('.nav-item'), function (item) {
      item.addEventListener('click', function () { switchView(item.dataset.view); });
    });
    Array.prototype.forEach.call($('#transfer-seg').querySelectorAll('button'), function (item) {
      item.addEventListener('click', function () { switchTransferSeg(item.dataset.transfer); });
    });
    // 传输页的筛选药丸：[全部 | 进行中 | 已完成 | 失败]（§3.2）。
    Array.prototype.forEach.call($('#filter-pills').querySelectorAll('button'), function (item) {
      item.addEventListener('click', function () { setTransferFilter(item.dataset.filter); });
    });
    var search = $('#transfer-search');
    if (search) {
      search.addEventListener('input', function () {
        store.search = search.value || '';
        applyRowFilter();
      });
    }
    var deviceSearch = $('#device-search-input');
    if (deviceSearch) {
      deviceSearch.addEventListener('input', function () {
        store.deviceSearch = deviceSearch.value || '';
        renderDevices();
      });
    }
    $('#btn-menu').addEventListener('click', openDrawer);
    $('#scrim').addEventListener('click', closeDrawer);
    $('#btn-back-session').addEventListener('click', function () { switchView('session'); });

    // ---- 三个下拉菜单：扫描 ▾ / 传输页 ⋯ /（连接手机是弹窗，不走菜单）
    $('#btn-scan-more').addEventListener('click', function (event) {
      event.stopPropagation();
      toggleMenu($('#btn-scan-more'), $('#scan-menu'));
    });
    $('#btn-transfers-more').addEventListener('click', function (event) {
      event.stopPropagation();
      toggleMenu($('#btn-transfers-more'), $('#transfers-menu'));
    });
    document.addEventListener('click', function (event) {
      if (!openMenu) return;
      if (openMenu.menu.contains(event.target) || openMenu.button.contains(event.target)) return;
      closeMenus();
    });

    // ---- 文件动作菜单：点别处、按 Esc、滚动、换页都收起来（手机上没有"点别处"，
    //      手指一滑就该让它消失，不然它会一直挡着会话）。
    document.addEventListener('click', function (event) {
      if (!fileMenuNode || fileMenuNode.hidden) return;
      if (fileMenuNode.contains(event.target)) return;
      closeFileMenu();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape' && event.key !== 'Esc') return;
      closeFileMenu();
      closeMenus();
      closeQr();
      $('#facts-modal').hidden = true;
    });
    window.addEventListener('scroll', closeFileMenu, true);
    window.addEventListener('resize', closeFileMenu);

    // ---- 选文件：输入区的「📎 传输文件」（label[for=file-input]）和拖放，都进待发列表。
    //      侧栏那个「＋ 发送文件」按钮已经去掉：它和 📎 是同一件事。
    var input = $('#file-input');
    input.addEventListener('change', function () {
      stageFiles(input.files);
      input.value = '';
    });

    // 拖放的目标是主区（<main id="main">）：文件落到会话/传输/设置哪一页都算数。
    var dropzone = $('#main');
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
      if (event.dataTransfer && event.dataTransfer.files) stageFiles(event.dataTransfer.files);
    });
    // Dropping anywhere else must not make the browser navigate away.
    window.addEventListener('dragover', function (e) { e.preventDefault(); });
    window.addEventListener('drop', function (e) { e.preventDefault(); });

    $('#btn-send').addEventListener('click', sendPicked);
    // ---- 预览弹层：右上角 ✕、点黑底、Esc 都能关（§6）
    $('#preview-close').addEventListener('click', closePreview);
    $('#preview').addEventListener('click', function (event) {
      if (event.target === $('#preview')) closePreview();
    });
    document.addEventListener('keydown', previewKeydown);
    $('#btn-rescan').addEventListener('click', triggerScan);
    $('#btn-scan').addEventListener('click', triggerScan);
    $('#btn-manual').addEventListener('click', function () {
      closeMenus();
      addManualDevice();
    });
    // 「连接手机」是二维码的**唯一**入口（设置页那张二维码卡片已并进这里）。
    $('#btn-phone-link').addEventListener('click', openQr);
    $('#btn-group').addEventListener('click', newGroupChat);
    $('#qr-close').addEventListener('click', closeQr);
    $('#qr-modal').addEventListener('click', function (event) {
      if (event.target === $('#qr-modal')) closeQr();
    });
    $('#facts-close').addEventListener('click', function () { $('#facts-modal').hidden = true; });
    $('#facts-modal').addEventListener('click', function (event) {
      if (event.target === $('#facts-modal')) $('#facts-modal').hidden = true;
    });
    $('#btn-refresh-files').addEventListener('click', function () {
      closeMenus();
      refreshFiles();
      refreshState().catch(function () { /* the connection badge says it all */ });
    });

    // 设备列表的两段：点标题整段展开 / 折叠（在线默认展开、离线默认折叠）。
    $('#dev-online-head').addEventListener('click', function () { toggleDeviceSection('online'); });
    $('#dev-offline-head').addEventListener('click', function () { toggleDeviceSection('offline'); });
    // 离线段标题行上的「清空」入口（小按钮 + 长按/右键整行，手机上没有右键）。
    bindOfflineClear($('#dev-offline-head'), $('#btn-clear-offline'));

    // ---- chat
    $('#btn-chat-send').addEventListener('click', sendText);
    $('#chat-input').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); sendText(); }
    });
    $('#btn-emoji').addEventListener('click', toggleEmoji);
    $('#btn-chat-back').addEventListener('click', function () {
      store.groupId = '';
      selectDevice(store.selectedDeviceId);
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
      if (event.key === 'Enter') sendPicked();
    });
    $('#pin').addEventListener('input', function () { renderHeader(); });
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
    bindTheme();
    var disconnectButton = $('#btn-disconnect');
    if (disconnectButton) {
      disconnectButton.addEventListener('click', function () {
        if (disconnected) { reconnect(); disconnectButton.textContent = '断开与电脑的连接'; return; }
        disconnect();
        disconnectButton.textContent = '重新连接';
      });
    }
  }

  // ----------------------------------------------------------------- 主题
  //
  // 深色是默认（跟 harness 的 Web 界面同一套 token）。选了浅色就写进
  // localStorage，下次打开时 <head> 里的那几行会在样式生效前先定好，
  // 不会先闪一下浅色再变深。

  var THEME_KEY = 'eversend-theme';

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  function applyTheme(mode) {
    if (mode === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', mode === 'light' ? '#ffffff' : '#151517');
    var seg = $('#theme-seg');
    if (seg) {
      Array.prototype.forEach.call(seg.querySelectorAll('button'), function (item) {
        item.classList.toggle('is-active', item.getAttribute('data-theme-value') === mode);
      });
    }
  }

  function setTheme(mode, remember) {
    applyTheme(mode);
    if (remember === false) return;
    try { localStorage.setItem(THEME_KEY, mode); } catch (error) { /* 无痕模式 */ }
  }

  /** 记住的那个主题。assets/theme.js 会在样式生效前先读一遍，这里再兜一次：
   *  localStorage 里存的是用户的最后一次选择，属性只是它的投影。 */
  function storedTheme() {
    try {
      return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark';
    } catch (error) {
      return 'dark';
    }
  }

  function bindTheme() {
    applyTheme(storedTheme());
    var seg = $('#theme-seg');
    if (seg) {
      Array.prototype.forEach.call(seg.querySelectorAll('button'), function (item) {
        item.addEventListener('click', function () {
          setTheme(item.getAttribute('data-theme-value') === 'light' ? 'light' : 'dark');
        });
      });
    }
  }

  // ------------------------------------------------------------------ boot

  function boot() {
    bind();
    switchView('session');
    renderAll();
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
