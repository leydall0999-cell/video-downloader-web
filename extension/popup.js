/* 视频工坊 媒体嗅探 —— popup 逻辑。
 *
 * 发送通道复用桌面端 /api/sniffer/send（add_manual → picked 队列），
 * 桌面端 web/js/desktop-app.js 的 picked 轮询会自动建下载任务 —— 与
 * CDP 悬浮球的兜底回传同一条路，不新增下载端点。
 *
 * 回执通道（2026-09-27 用户实测补充）：
 *   /api/sniffer/send 只是**入队**，必然返回 200；真正建任务发生在桌面端进程里。
 *   桌面端未登录 / 链接不支持 / 超配额时，建任务会失败，但失败发生在桌面端——
 *   扩展原本无从得知，于是按钮显示「已发送 ✓」而用户什么也没等到（用户原话
 *   「扩展点击下载 app 没反应」）。现在 send 返回 send_id，扩展轮询
 *   /api/sniffer/result 拿到真实结果，失败时把原因直接显示在条目上。
 */
'use strict';

var $ = function (id) { return document.getElementById(id); };
var listEl = $('list');

var state = { endpoint: '', sentUrls: {}, carryCookie: true, quality: 'best' };
// 桌面端登录态：true=已登录 / false=未登录 / null=未知（App 未启动或界面已关闭）
var desktopLoggedIn = null;
// 面板只呈现**当前标签页当前页**的嗅探结果（背景按 tabId 分库，见 background.js）
var currentTabId = -1;

// ---- 清晰度（2026-10-01 用户反馈「目前没法选择分辨率」）----
// 扩展发来的条目在桌面端一律按「最佳画质（自动）」建任务，用户在扩展里无从选择。
// 现在 popup 顶部给一个下拉：选中的清晰度随每条发送上报（/api/sniffer/send 的
// quality 字段），桌面端解析类条目按它下载；选择落 chrome.storage.local 记住。
// 只对「视频页 / HLS 清单」上报 —— 直链本身就是单一流，带清晰度反而可能挑不到流。
var QUALITY_OPTIONS = ['best', '2160', '1440', '1080', '720', '480', '360', 'audio'];
var QUALITY_STORE_KEY = 'sendQuality';

/** 取下拉里该档的显示文案（用于回显，options 是 popup.html 里的静态项）。 */
function qualityLabel(k) {
  var sel = $('sendQuality');
  if (sel) {
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === k) return sel.options[i].textContent;
    }
  }
  return k || '最佳画质（自动）';
}

/** 该条目是否该带上清晰度（只对可解析出多档的条目有意义）。 */
function qualityForItem(it) {
  var k = (it && it.kind) || '';
  if (k !== 'page' && k !== 'playlist') return '';
  return state.quality || 'best';
}

/** 把 state.quality 同步到两处 UI：下拉选中值 + 空状态回显。
 *
 * 两处必须永远一致 —— 用户判断「选了有没有生效」只看这两处；曾经出现
 * 「下拉显示 2K 1440P、回显却是最佳画质（自动）」的自相矛盾（2026-10-02 用户截图）。
 */
function syncQualityUI() {
  var q = state.quality || 'best';
  var sel = $('sendQuality');
  if (sel && sel.value !== q) sel.value = q;
  var line = document.querySelector('.empty-q');
  if (line) line.textContent = '将以「' + qualityLabel(q) + '」下载';
}

function fmtTime(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  var p = function (n) { return (n < 10 ? '0' : '') + n; };
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

/** 时长（秒 → 12:34 / 1:02:03）；0/非法 → ''（调用方据此不显示）。2026-10-02 */
function fmtDur(sec) {
  var s = Math.round(Number(sec) || 0);
  if (!(s > 0)) return '';
  var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  var p = function (n) { return (n < 10 ? '0' : '') + n; };
  return h > 0 ? (h + ':' + p(m) + ':' + p(r)) : (m + ':' + p(r));
}

/** 大小（字节 → 128.4 MB）；0/非法 → ''（调用方据此不显示）。2026-10-02 */
function fmtSize(n) {
  var b = Number(n) || 0;
  if (!(b > 0)) return '';
  var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i === 0 ? b : (b >= 100 ? b.toFixed(0) : b.toFixed(1))) + ' ' + u[i];
}

function chipText(kind) {
  if (kind === 'page') return '页面';
  return kind === 'playlist' ? '清单' : (kind === 'segment' ? '分片' : '直链');
}

/** 条目下方的结果行（原来的失败是「静默无反应」，现在必须看得见）。 */
function setItemMsg(btn, text, isErr) {
  var item = btn && btn.closest ? btn.closest('.item') : null;
  var el = item ? item.querySelector('.item-msg') : null;
  if (!el) return;
  el.textContent = text || '';
  el.className = 'item-msg' + (isErr ? ' is-err' : '');
  el.hidden = !text;
}

/** 发送失败：按钮回到可重试状态，原因同时进 title 与条目文字。 */
function failSend(btn, msg) {
  btn.disabled = false;
  btn.textContent = '重试';
  btn.title = msg;
  setItemMsg(btn, msg, true);
}

function renderItem(it) {
  var div = document.createElement('div');
  div.className = 'item' + (state.sentUrls[it.url] ? ' sent' : '');

  var top = document.createElement('div');
  top.className = 'item-top';
  var chip = document.createElement('span');
  chip.className = 'chip ' + (it.kind || 'media');
  chip.textContent = chipText(it.kind);
  var url = document.createElement('span');
  url.className = 'item-url';
  // 原名优先（2026-11-13）：媒体 URL 常是纯数字 ID / 哈希（2044590383.mp3），
  // 用户第一眼要看到的是页面上那个真实歌名。完整地址仍保留在 title 悬停提示与
  // 「复制」按钮里，不丢任何信息。
  var realName = String(it.title || '').trim();
  url.textContent = realName || it.url;
  url.title = it.url;
  if (realName) url.className = 'item-url item-named';
  top.appendChild(chip);
  top.appendChild(url);

  if (it.kind !== 'segment') {
    var actions = document.createElement('span');
    actions.className = 'item-actions';
    var dl = document.createElement('button');
    dl.className = 'mini primary';
    dl.textContent = state.sentUrls[it.url] ? '已发送 ✓' : '下载';
    dl.addEventListener('click', function () { sendItem(it, dl); });
    var cp = document.createElement('button');
    cp.className = 'mini';
    cp.textContent = '复制';
    cp.addEventListener('click', function () {
      navigator.clipboard.writeText(it.url).then(function () {
        cp.textContent = '已复制';
        setTimeout(function () { cp.textContent = '复制'; }, 1200);
      });
    });
    actions.appendChild(dl);
    actions.appendChild(cp);
    top.appendChild(actions);
  }
  div.appendChild(top);

  if (it.pageTitle) {
    var t = document.createElement('div');
    t.className = 'item-title';
    t.textContent = it.pageTitle;
    div.appendChild(t);
  }
  var meta = [];
  // 时长 / 大小（2026-10-02 用户「把标题、时长、大小也加上」）：时长来自页面侧
  // <video>.duration，大小来自响应头 Content-Length；取不到就不显示（不写 0）。
  var du = fmtDur(it.duration);
  if (du) meta.push('时长 ' + du);
  var sz = fmtSize(it.size);
  if (sz) meta.push(sz);
  if (it.count > 1) meta.push('×' + it.count);
  // 用户实测反馈（2026-09-27）：裸写 `00:36:14` 会被读成「时长」，以为 UI 音效有 36 分钟。
  // 它其实是「第一次嗅到这个地址的时刻」（本地时钟 时:分:秒），加前缀消歧。
  if (it.firstSeen) meta.push('嗅于 ' + fmtTime(it.firstSeen));
  if (it.referer) { try { meta.push(new URL(it.referer).host); } catch (e) { /* 忽略 */ } }
  if (meta.length) {
    var m = document.createElement('div');
    m.className = 'item-meta';
    m.textContent = meta.join(' · ');
    div.appendChild(m);
  }
  var msg = document.createElement('div');
  msg.className = 'item-msg';
  msg.hidden = true;
  div.appendChild(msg);
  return div;
}

function render(st) {
  // ⚠️ 2026-10-02 用户截图实测：这里原本直接 `state = st`，而后台 getState 快照里
  //   **没有 quality 字段**（见 background.js 的 snap）→ 用户选的清晰度当场被抹成
  //   undefined。下拉 DOM 因为下面那句 `state.quality &&` 守卫没被重置，看起来还停在
  //   「2K 1440P」，实际发送的却是 best（截图现象：下拉 2K 1440P、回显「最佳画质（自动）」）。
  //   现在把用户选的档位显式接过来，只有快照真的带回合法值才覆盖。
  var keptQuality = (state && state.quality) || 'best';
  state = st || {};
  if (QUALITY_OPTIONS.indexOf(state.quality) < 0) state.quality = keptQuality;
  var epEl = $('endpointText');
  if (st.endpoint) {
    epEl.textContent = '已连接桌面端';
    epEl.className = 'ep-ok';
  } else {
    epEl.textContent = '未找到桌面端（请确认 App 已启动）';
    epEl.className = 'ep-none';
  }
  $('sniffToggle').checked = st.enabled;
  $('carryCookie').checked = st.carryCookie !== false;

  // 桌面端未登录时必须提前说清楚：否则用户点下载只会看到桌面端弹登录框
  //（浏览器弹窗焦点在前台，桌面端窗口在后台，用户以为「没反应」）。
  var warn = $('loginWarn');
  if (desktopLoggedIn === false) {
    warn.textContent = '桌面端未登录 —— 请先在「视频工坊」里登录，扩展发来的下载才会开始';
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }

  var items = st.items || [];
  var segs = st.segments || [];
  listEl.innerHTML = '';
  if (!items.length && !segs.length) {
    if (st.videoPage) {
      // 视频页但抓不到直链：YouTube/B站等用加密流（UMP/SABR）播放，媒体流抓不到、
      // 也不该抓 —— 正解是把页面链接交给桌面端用 yt-dlp 解析（已自动发生）。
      // 2026-10-01 用户反馈①：原来只写「没有嗅探到媒体流」，被读成功能坏了；
      // 反馈②「这里也要可以操作」→ 空状态直接给两个按钮（解析并下载 / 复制链接）。
      var box = document.createElement('div');
      box.className = 'empty';
      // 视频信息（2026-10-02 用户截图「这里把嗅探到的视频信息也加上，比如：标题、时长、大小」）：
      // 加密流站点抓不到直链，但**这一页放的是什么视频**是已知的，兜底视图不该只有一段解释。
      //   标题 = 标签页标题（background 的 getState 带出 videoTitle，取不到退回推送记账）；
      //   时长 = 页面侧 <video>.duration（pagewatch.js 上报 → 按 tab 记账 → videoDuration）；
      //   大小 = 加密流是分片的、没有整体 Content-Length，扩展结构上拿不到 → 如实说明，
      //         不写「0 B」充数（与列表条目同口径：取不到就不显示）。
      var vtitle = String(st.videoTitle || '').trim();
      if (vtitle) {
        var vt = document.createElement('div');
        vt.className = 'empty-title';
        vt.textContent = vtitle;
        vt.title = vtitle;
        box.appendChild(vt);
      }
      var vmeta = [];
      var vdu = fmtDur(st.videoDuration);
      if (vdu) vmeta.push('时长 ' + vdu);
      if (vmeta.length) {
        var vm = document.createElement('div');
        vm.className = 'empty-meta';
        vm.textContent = vmeta.join(' · ');
        box.appendChild(vm);
      }
      var tip = document.createElement('div');
      tip.className = 'empty-tip';
      tip.innerHTML =
        '这类站点用加密流播放，扩展抓不到直链和整体大小（正常现象）。<br>' +
        (st.pagePushAt
          ? '✓ 已于 ' + fmtTime(st.pagePushAt) + ' 把本页交给桌面端解析。<br>' +
            '点下面「解析并下载」，或到桌面端「媒体嗅探」列表操作。'
          : '正在把本页交给桌面端解析；若一直是这样，请确认「视频工坊」App 已启动。');
      box.appendChild(tip);
      // 当前清晰度回显（改上方「清晰度」下拉时同步，见 sendQuality 的 change 绑定）：
      // 用户在扩展里选的分辨率必须看得见，否则「选了没生效」无从判断。
      var qline = document.createElement('div');
      qline.className = 'empty-q';
      qline.textContent = '将以「' + qualityLabel(state.quality) + '」下载';
      box.appendChild(qline);
      var acts = document.createElement('div');
      acts.className = 'empty-actions';
      var dlBtn = document.createElement('button');
      dlBtn.type = 'button';
      dlBtn.className = 'mini primary';
      dlBtn.textContent = '解析并下载';
      dlBtn.title = '把本页地址交给桌面端用 yt-dlp 解析，并加入下载队列';
      dlBtn.addEventListener('click', function () { sendCurrentPage(dlBtn); });
      var cpBtn = document.createElement('button');
      cpBtn.type = 'button';
      cpBtn.className = 'mini';
      cpBtn.textContent = '复制链接';
      cpBtn.title = '复制本页地址';
      cpBtn.addEventListener('click', function () { copyPageUrl(cpBtn); });
      acts.appendChild(dlBtn);
      acts.appendChild(cpBtn);
      box.appendChild(acts);
      listEl.appendChild(box);
    } else {
      listEl.innerHTML = '<div class="empty">当前页没有嗅探到媒体流。<br>' +
        '播放页面里的视频或音频后这里会自动列出；已离开的页面不会保留。</div>';
    }
  } else {
    items.forEach(function (it) { listEl.appendChild(renderItem(it)); });
  }

  // 清晰度 UI 同步放在最后：空状态刚重建完（上一轮那个已被 innerHTML='' 丢掉），
  // 这样下拉与回显都指向当前这一份 DOM。
  syncQualityUI();

  var note = $('segNote');
  if (segs.length) {
    var total = 0;
    segs.forEach(function (s) { total += s.count || 0; });
    note.textContent = '另有 ' + segs.length + ' 路分片流（共 ' + total +
      ' 片）——正在播放的 HLS 流，清单出现后可整段下载';
    note.hidden = false;
  } else {
    note.hidden = true;
  }

  // 桌面端地址可能在本面板打开期间才探到（首帧是空串）→ 登录态区的按钮要跟着解禁，
  // 否则用户看到「存到我自己的 App」一直灰着，会以为功能坏了。
  ckUpdateBtns();
}

function refresh() {
  // 先取当前标签页：背景按 tabId 分库，不传就会落到「最近激活的标签页」，
  // 多窗口场景下可能给错那一页的数据
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    var tab = tabs && tabs[0];
    if (tab && typeof tab.id === 'number') currentTabId = tab.id;
    // v1.0.40：打开面板这一下，就顺手把视频页交给桌面端（用户显式打开面板 = 明确意图，
    // 忽略 5 分钟冷却）。必须在取 state 之前，否则渲染时 pagePushAt 还是旧值。
    chrome.runtime.sendMessage({
      type: 'pushPageNow',
      tabId: currentTabId,
      url: (tab && tab.url) || '',
      title: (tab && tab.title) || ''
    }, function () {
      void chrome.runtime.lastError;
      loadState();
    });
  });
}

function loadState() {
  chrome.runtime.sendMessage({ type: 'getState', tabId: currentTabId }, function (st) {
    if (chrome.runtime.lastError) return;
    render(st);
  });
}

// ---- 发送到桌面端 ----
function postSend(base, it, carry) {
  var payload = {
    url: it.url,
    mime: it.mime || '',
    referer: it.referer || '',
    page_url: it.pageUrl || it.pageTitle || '',
    page_title: it.pageTitle || '',
    // 媒体原名（2026-11-13）：走 /api/sniffer/send → add_manual → 桌面端建任务，
    // 服务端据此做列表标题与落盘文件名，避免 URL basename 把任务名变成一串数字。
    title: it.title || '',
    cookie: carry ? (it.cookie || '') : '',
    // 条目类型：必须随包上报。页面 URL（YouTube 等）没有媒体后缀，服务端判不出类型，
    // 少这一项就会被当成「直链」，进而丢弃上面的 quality（2026-10-02 真机实测发现）。
    kind: it.kind || '',
    // 清晰度：只对「视频页 / HLS 清单」上报（见 qualityForItem）。空串=让桌面端用它的默认值。
    quality: qualityForItem(it),
    // 标题/时长/大小（2026-10-02 用户「把标题、时长、大小也加上」）：
    //   大小 = 响应头 Content-Length（item.size，直链/清单才有）；
    //   时长 = 页面侧 <video>.duration —— 「页面」条目本身没有，取 state.videoDuration
    //         （后台按 tab 记在 pagePushed 里随 getState 带回）。缺省 0 = 未知，服务端据此不展示。
    size: it.size || 0,
    duration: it.duration || ((it.kind === 'page') ? (state.videoDuration || 0) : 0),
    source: 'extension'
  };
  return fetch(base + '/api/sniffer/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

function fetchResult(base, sid) {
  return fetch(base + '/api/sniffer/result?send_id=' + encodeURIComponent(sid))
    .then(function (r) { return r.json(); });
}

/** 轮询回执直到有结论：ok → 已加入下载；error → 显示桌面端给的原因。 */
function pollResult(it, btn, sid, okLabel, left) {
  fetchResult(state.endpoint, sid).then(function (j) {
    var st = (j && j.state) || 'unknown';
    if (st === 'ok') {
      chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () {});
      btn.disabled = false;
      btn.textContent = '已加入下载 ✓';
      btn.title = '';
      setItemMsg(btn, '', false);
      refresh();
      return;
    }
    if (st === 'error') {
      failSend(btn, (j && j.message) || '桌面端建任务失败');
      return;
    }
    if (left > 0) {
      setTimeout(function () { pollResult(it, btn, sid, okLabel, left - 1); }, 900);
      return;
    }
    // 超时（桌面端界面没开 / 卡住）：不谎报成功，如实说「已投递但未确认」
    chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () {});
    btn.disabled = false;
    btn.textContent = okLabel;
    setItemMsg(btn, '已投递，但未收到桌面端确认（请到桌面端任务列表查看）', false);
  }).catch(function () {
    btn.disabled = false;
    btn.textContent = okLabel;
  });
}

/** 网络级失败自愈（2026-10-26 用户实测「发送失败: Failed to fetch」）：
 * 后台 getEndpoint 命中缓存后**永不重探**，而 App 重启可能换端口（8321 被旧实例
 * 短暂占住 ⇒ 新实例落到 8322）⇒ 扩展拿着死端口发请求必然 Failed to fetch，
 * 标题栏「已连接桌面端」还照常亮（它只判断缓存非空，不验证对方活着）。
 * 自愈：网络级错误 ⇒ 走 detectEndpoint 清缓存重探端口表 ⇒ 拿到新端点重试一次。
 * HTTP 层错误（服务器有回应）不算，照常上报，不做无谓重试。 */
function postSendHealed(it, carry) {
  return postSend(state.endpoint, it, carry).catch(function (e) {
    var msg = String((e && e.message) || '');
    var netDead = (e instanceof TypeError) ||
      /failed to fetch|networkerror|load failed/i.test(msg);
    if (!netDead) throw e;
    return new Promise(function (resolve, reject) {
      chrome.runtime.sendMessage({ type: 'detectEndpoint' }, function (r) {
        if (chrome.runtime.lastError || !r || !r.endpoint) { reject(e); return; }
        state.endpoint = r.endpoint;
        postSend(r.endpoint, it, carry).then(resolve, reject);
      });
    });
  });
}

function sendItem(it, btn, okLabel) {
  okLabel = okLabel || '已发送 ✓';
  if (!state.endpoint) {
    chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
      if (r && r.endpoint) { state.endpoint = r.endpoint; sendItem(it, btn, okLabel); }
      else { failSend(btn, '未找到桌面端：请先启动「视频工坊」App'); }
    });
    return;
  }
  if (desktopLoggedIn === false) {
    // 可能是「刚在桌面端登录完」的旧信号：再核一次，确认仍未登录才拒绝，
    // 免得用户明明登录了却点不动（桌面端每 3s 轮询会刷新这个信号）
    refreshDesktopAuth(function () {
      if (desktopLoggedIn === false) {
        failSend(btn, '桌面端未登录：请先在「视频工坊」里登录，再点下载');
      } else {
        sendItem(it, btn, okLabel);
      }
    });
    return;
  }
  btn.disabled = true;
  btn.textContent = '发送中…';
  postSendHealed(it, state.carryCookie !== false)
    .then(function (resp) {
      var sid = resp && resp.send_id;
      if (!sid) {
        chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () {});
        btn.disabled = false;
        btn.textContent = okLabel;
        return;
      }
      pollResult(it, btn, sid, okLabel, 12);
    })
    .catch(function (e) {
      failSend(btn, '发送失败：' + ((e && e.message) || '桌面端未响应'));
    });
}

/** 发送当前标签页的页面地址（2026-09-27）。
 *  作用：YouTube 等站点用 MSE 播放，扩展嗅不到可下载的媒体直链（能嗅到的往往
 *  只是站点自己的 UI 音效），把页面地址交给桌面端走 yt-dlp 解析才是正解。 */
function sendCurrentPage(btn) {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    if (chrome.runtime.lastError) return;
    var tab = tabs && tabs[0];
    var url = (tab && tab.url) || '';
    if (!/^https?:/i.test(url)) {
      failSend(btn, '当前标签页不是网页地址，无法发送');
      return;
    }
    var it = {
      url: url, mime: '', kind: 'page', referer: url,
      pageUrl: url, pageTitle: (tab && tab.title) || '', cookie: ''
    };
    // 页面项不进嗅探列表，结果写在工具栏下方的提示行里
    sendItemWithNote(it, btn, '页面已发送 ✓');
  });
}

/** 复制当前标签页地址（视频页空状态用）。扩展 popup 是安全上下文 + 用户手势，
 *  navigator.clipboard 可用；失败时如实提示，绝不静默。 */
function copyPageUrl(btn) {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    var url = (tabs && tabs[0] && tabs[0].url) || '';
    if (!url) { note(btn, '取不到当前页地址，请手动复制地址栏链接', true); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () {
        var old = btn.textContent;
        btn.textContent = '已复制';
        setTimeout(function () { btn.textContent = old; }, 1200);
        note(btn, '已复制本页链接', false);
      }, function () { note(btn, '复制失败，请手动复制地址栏链接', true); });
    } else {
      note(btn, '复制失败，请手动复制地址栏链接', true);
    }
  });
}

/** 与 sendItem 同流程，但结果写到 #sendNote（没有对应条目 DOM 可写）。 */
function sendItemWithNote(it, btn, okLabel) {
  if (!state.endpoint) {
    chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
      if (r && r.endpoint) { state.endpoint = r.endpoint; sendItemWithNote(it, btn, okLabel); }
      else { note(btn, '未找到桌面端：请先启动「视频工坊」App', true); }
    });
    return;
  }
  if (desktopLoggedIn === false) {
    // 同 sendItem：先核实一次再拒绝，避免旧信号误伤刚登录的用户
    refreshDesktopAuth(function () {
      if (desktopLoggedIn === false) {
        note(btn, '桌面端未登录：请先在「视频工坊」里登录，再发送', true);
      } else {
        sendItemWithNote(it, btn, okLabel);
      }
    });
    return;
  }
  btn.disabled = true;
  btn.textContent = '发送中…';
  note(btn, '正在把页面地址交给桌面端解析…', false);
  postSendHealed(it, state.carryCookie !== false)
    .then(function (resp) {
      var sid = resp && resp.send_id;
      if (!sid) { donePage(btn, okLabel, '已投递（未返回回执）'); return; }
      pollPageResult(btn, sid, okLabel, 20);
    })
    .catch(function (e) {
      note(btn, '发送失败：' + ((e && e.message) || '桌面端未响应'), true);
      btn.disabled = false;
      btn.textContent = '发送当前页';
    });
}

function pollPageResult(btn, sid, okLabel, left) {
  fetchResult(state.endpoint, sid).then(function (j) {
    var st = (j && j.state) || 'unknown';
    if (st === 'ok') { donePage(btn, okLabel, '桌面端已开始解析并加入下载队列'); return; }
    if (st === 'error') {
      note(btn, '失败：' + ((j && j.message) || '桌面端建任务失败'), true);
      btn.disabled = false;
      btn.textContent = '发送当前页';
      return;
    }
    if (left > 0) { setTimeout(function () { pollPageResult(btn, sid, okLabel, left - 1); }, 900); return; }
    donePage(btn, okLabel, '已投递，但未收到桌面端确认（请到桌面端任务列表查看）');
  }).catch(function () {
    donePage(btn, okLabel, '已投递（未收到回执）');
  });
}

function donePage(btn, okLabel, text) {
  btn.disabled = false;
  btn.textContent = okLabel;
  note(btn, text, false);
}

/** 结果提示行（工具栏下方）。第一个参数保留为触发按钮，便于将来按来源分流。 */
function note(_btn, text, isErr) {
  var el = $('sendNote');
  el.textContent = text || '';
  el.className = 'send-note' + (isErr ? ' is-err' : '');
  el.hidden = !text;
}

// ---- 事件绑定 ----
$('sniffToggle').addEventListener('change', function (e) {
  chrome.runtime.sendMessage({ type: 'setEnabled', value: e.target.checked }, refresh);
});
$('carryCookie').addEventListener('change', function (e) {
  chrome.runtime.sendMessage({ type: 'setCarryCookie', value: e.target.checked });
});
// 清晰度：改选立即生效（后续每条发送都带上），并记住这次选择
$('sendQuality').addEventListener('change', function (e) {
  var v = e.target.value;
  state.quality = QUALITY_OPTIONS.indexOf(v) >= 0 ? v : 'best';
  try { chrome.storage.local.set({ sendQuality: state.quality }); } catch (err) { /* 静默 */ }
  syncQualityUI();
});
$('clearAll').addEventListener('click', function () {
  chrome.runtime.sendMessage({ type: 'clear' }, refresh);
});
$('sendPage').addEventListener('click', function (e) {
  sendCurrentPage(e.currentTarget);
});
$('reDetect').addEventListener('click', function () {
  var ep = $('endpointText');
  ep.textContent = '探测中…';
  ep.className = 'ep-unknown';
  chrome.runtime.sendMessage({ type: 'detectEndpoint' }, function (r) {
    if (chrome.runtime.lastError) return;
    state.endpoint = (r && r.endpoint) || '';
    refresh();
    refreshDesktopAuth();
  });
});
$('sendAll').addEventListener('click', function () {
  var btn = $('sendAll');
  var targets = (state.items || []).filter(function (it) { return !state.sentUrls[it.url]; });
  if (!targets.length) return;
  if (!state.endpoint) { $('reDetect').click(); return; }
  // 这里不做「未登录就不发」的前置拦截：批量发送靠逐条回执给结论（见 pollBatchResults），
  // 免得桌面端刚登录、扩展还握着旧信号时把用户挡在门外；顶部红条已提前提醒。
  btn.disabled = true;
  btn.textContent = '发送中…';
  var sids = [];
  var done = 0;
  var step = function () {
    if (done >= targets.length) {
      btn.disabled = false;
      btn.textContent = '全部发送';
      if (sids.length) pollBatchResults(sids, 10);
      else refresh();
      return;
    }
    var it = targets[done++];
    postSend(state.endpoint, it, state.carryCookie !== false)
      .then(function (resp) {
        if (resp && resp.send_id) sids.push(resp.send_id);
        chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () { step(); });
      })
      .catch(function () { step(); });
  };
  step();
});

/** 批量回执：每轮并发查一次，未出结论的下一轮再查，全部有结论或超时后给一行汇总
 *  （绝不谎报成功）。⚠️ 下一轮必须等本轮 fetch 全部回填后再排——否则 pending 还是空的。 */
function pollBatchResults(sids, left) {
  if (!sids.length) return;
  var pending = [];
  var failed = [];
  var doneCount = 0;
  var settle = function () {
    doneCount++;
    if (doneCount < sids.length) return;
    if (failed.length) {
      note($('sendAll'), failed.length + ' 条未成功，例如：' + failed[0], true);
    } else if (pending.length) {
      if (left > 0) {
        setTimeout(function () { pollBatchResults(pending, left - 1); }, 1200);
        return;
      }
      note($('sendAll'),
        '有 ' + pending.length + ' 条已投递但未收到桌面端确认（请到桌面端任务列表查看）', false);
    } else {
      note($('sendAll'), '', false);
    }
    refresh();
  };
  sids.forEach(function (sid) {
    fetchResult(state.endpoint, sid).then(function (j) {
      var st = (j && j.state) || 'unknown';
      if (st === 'error') failed.push((j && j.message) || '未知原因');
      else if (st !== 'ok') pending.push(sid);
      settle();
    }).catch(function () { pending.push(sid); settle(); });
  });
}

/** 取桌面端登录态（借 /api/sniffer/status 的 desktop_logged_in 字段）。cb 用于「先核实再拒绝」。 */
function refreshDesktopAuth(cb) {
  cb = cb || function () {};
  if (!state.endpoint) { desktopLoggedIn = null; refresh(); cb(); return; }
  fetch(state.endpoint + '/api/sniffer/status')
    .then(function (r) { return r.json(); })
    .then(function (j) {
      desktopLoggedIn = (j && typeof j.desktop_logged_in === 'boolean')
        ? j.desktop_logged_in : null;
      refresh();
      cb();
    })
    .catch(function () { desktopLoggedIn = null; refresh(); cb(); });
}

// 初始化：先取背景状态，再取桌面端登录态
// 清晰度记忆值：读不到就用默认 best；storage 异常也不该阻断面板
try {
  chrome.storage.local.get([QUALITY_STORE_KEY], function (st) {
    var v = st && st[QUALITY_STORE_KEY];
    if (typeof v === 'string' && QUALITY_OPTIONS.indexOf(v) >= 0) state.quality = v;
    // 读数可能晚于第一次 render（异步），这里必须再同步一次 UI —— 否则下拉是记忆值、
    // 回显却还是默认档（2026-10-02 那个「选了没生效」的现象就是这么来的）。
    syncQualityUI();
  });
} catch (e) { /* 静默 */ }

// 列表只依赖后台本地 store，不该等桌面端端口探测：缓存失效时 probeEndpoint 要
// 逐端口试（每个 900ms × 2），几十秒里弹窗一片空白，用户以为「没嗅探到」。
// 先渲染首帧，连接态等 getEndpoint 回来再补刷按钮。
refresh();
chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
  if (chrome.runtime.lastError) return;
  if (r && r.endpoint) {
    state.endpoint = r.endpoint;
    ckUpdateBtns();
  }
  refreshDesktopAuth();
});


// ---- 登录态同步（2026-10-18）----
// 为什么要有这块：桌面 App 自己解密浏览器 Cookie 库只认 Chrome/Edge/Brave/Chromium
// 的默认 profile（macOS 上还撞钥匙串授权），网页版更是受同源策略彻底读不到别站 Cookie。
// 于是让用户一键把「浏览器里已经登录的站」交给本机 App（私密，写本机缓存），
// 或显式同意后贡献到云端公共池（共享，网页版靠它拿到登录态）。
// 采集与推送逻辑全在 cookie-harvest.js（可 node 直测）；本段只做 UI 与串行推送，
// 绝不自动触发外发 —— 每一个出网动作都必须来自用户点按钮。
var ckItems = [];      // 最近一次扫描结果（VDLCookie.harvest 的返回）
var ckBusy = false;    // 扫描 / 推送进行中：防连点，也用于禁按钮
var ckScanned = false;

function ckMsg(text, kind) {
  var el = $('ckMsg');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'ckz-msg' + (kind ? ' is-' + kind : '');
  el.hidden = !text;
}

/** 按钮可用性：本机推送要「有桌面端地址 + 至少一项已登录」；云端还要求勾选同意。 */
function ckUpdateBtns() {
  var local = $('ckPushLocal');
  var cloud = $('ckPushCloud');
  if (!local || !cloud) return;
  var ready = VDLCookie.pickReady(ckItems);
  var readyCloud = VDLCookie.pickReady(ckItems, { cloud: true });
  var agree = !!($('ckAgree') && $('ckAgree').checked);
  local.disabled = ckBusy || !ready.length || !state.endpoint;
  cloud.disabled = ckBusy || !readyCloud.length || !agree;
  // 灰着的原因要能看见：否则用户以为「扫到了却点不动」是坏了
  local.title = !state.endpoint ? '未连接桌面端：请先启动「视频工坊」App'
    : (!ready.length ? '没有已登录的平台' : '');
  cloud.title = !readyCloud.length ? '没有可贡献的平台（微博/爱奇艺只能同步到本机）'
    : (!agree ? '请先勾选上方同意项' : '');
}

function ckRender() {
  var list = $('ckList');
  if (!list) return;
  list.innerHTML = '';
  var onCount = 0;
  var noPerm = false;
  var timedOut = 0;
  // 已登录的排在前面：用户扫完只想看「哪几个能同步」
  var rows = (ckItems || []).slice().sort(function (a, b) {
    return (b.loggedIn ? 1 : 0) - (a.loggedIn ? 1 : 0);
  });
  rows.forEach(function (it) {
    if (it.error === 'no-permission') noPerm = true;
    if (it.error === 'timeout') timedOut++;
    if (it.loggedIn) onCount++;
    var row = document.createElement('div');
    row.className = 'ckz-row' + (it.loggedIn ? ' on' : ' off');
    var nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = it.name;
    row.appendChild(nm);
    if (!it.cloud) {
      var tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = '仅本机';
      tag.title = '该站不在云端公共池白名单内，只能同步给自己的 App';
      row.appendChild(tag);
    }
    var st = document.createElement('span');
    st.className = 'st';
    if (it.error === 'no-permission') st.textContent = '缺少 Cookie 权限';
    else if (it.error === 'timeout') st.textContent = '读取超时';
    else if (it.error) st.textContent = '读取失败';
    else if (it.loggedIn) st.textContent = '已登录 · ' + it.count + ' 项';
    else st.textContent = '未登录';
    row.appendChild(st);
    list.appendChild(row);
  });

  var sum = $('ckSummary');
  if (sum) {
    if (noPerm) {
      // 最可能的真实原因：manifest 新增了 cookies 权限 → Chrome 停用了扩展，
      // 用户必须到扩展管理页点一次「重新启用」，否则 getAll 永远报权限错。
      sum.textContent = '缺少 Cookie 权限，请在扩展管理页重新启用';
      sum.className = 'ckz-sub is-warn';
    } else if (!ckScanned) {
      sum.textContent = '展开后点「扫描」';
      sum.className = 'ckz-sub';
    } else if (onCount) {
      sum.textContent = onCount + ' 个平台已登录' + (timedOut ? '（' + timedOut + ' 个超时）' : '');
      sum.className = 'ckz-sub is-ok';
    } else if (timedOut) {
      // 全部超时（常见于浏览器网络服务异常）：别写成「没有已登录的平台」——
      // 那会让用户去挨个重新登录，而真正的解法是重试或重启浏览器。
      sum.textContent = timedOut + ' 个平台读取超时，点「重新扫描」再试';
      sum.className = 'ckz-sub is-warn';
    } else {
      sum.textContent = '没有检测到已登录的平台';
      sum.className = 'ckz-sub';
    }
  }
  ckUpdateBtns();
}

function ckScan(done) {
  if (ckBusy) { if (done) done(); return; }
  if (typeof VDLCookie === 'undefined' || !VDLCookie.harvest) {
    ckMsg('登录态模块未加载：请到扩展管理页重新加载一次扩展', 'err');
    if (done) done();
    return;
  }
  ckBusy = true;
  var btn = $('ckScan');
  if (btn) { btn.disabled = true; btn.textContent = '扫描中…'; }
  ckUpdateBtns();
  VDLCookie.harvest(chrome, function (res) {
    // 「默认共享到云端」是产品策略（与桌面 App 每 30 分钟自动推一致）：
    // 首次扫到已登录站点就直接推一次，不需要用户再点按钮；取消勾选才不推。
    // 30 分钟节流 —— popup 每打开都是新实例，不节流会每次开面板都重推，
    // 撞云端每域 30 秒的限流。
    var first = !ckScanned;
    ckItems = res || [];
    ckScanned = true;
    ckBusy = false;
    if (btn) { btn.disabled = false; btn.textContent = '重新扫描'; }
    ckRender();
    if (done) done();
    // 勾选框是「当下这一刻」的意愿，优先于 storage：用户刚点了取消（set 还没落地）
    // 就展开面板，也必须不推。storage 里只负责跨会话记住这个选择。
    if (first && !($('ckAgree') && $('ckAgree').checked === false)) {
      // 打开面板补推一次。与 background 的 alarm / startup 共用 autoPush 内部
      // 的节流戳（storage.local），所以不会双推；用户取消勾选后 autoPush 直接跳过。
      VDLCookie.autoPush(chrome, null, function (r) {
        if (r && r.pushed) ckMsg('已自动共享 ' + r.pushed + ' 个平台到云端池', 'ok');
      });
    }
  });
}

/** 串行推送：逐站发、逐站回报，失败要把原因落到界面上（别只显示「失败」）。 */
function ckPush(kind) {
  if (ckBusy) return;
  var isCloud = kind === 'cloud';
  var targets = VDLCookie.pickReady(ckItems, { cloud: isCloud });
  if (!targets.length) return;
  if (!isCloud && !state.endpoint) {
    ckMsg('未连接桌面端：请先启动「视频工坊」App，再回来点一次', 'err');
    return;
  }
  if (isCloud && !($('ckAgree') && $('ckAgree').checked)) {
    ckMsg('请先勾选同意，才会把登录态贡献到云端池', 'err');
    return;
  }

  var btn = isCloud ? $('ckPushCloud') : $('ckPushLocal');
  var label = btn.textContent;
  ckBusy = true;
  ckUpdateBtns();
  ckMsg('', '');
  var okN = 0;
  var fail = [];
  var i = 0;
  function step() {
    if (i >= targets.length) {
      ckBusy = false;
      btn.textContent = label;
      ckUpdateBtns();
      if (!fail.length) {
        ckMsg('已同步 ' + okN + ' 个平台' +
          (isCloud ? '到云端池：网页版现在可以用这几个站的登录态了' : '到本机 App'), 'ok');
      } else if (okN) {
        ckMsg('成功 ' + okN + ' 个，失败 ' + fail.length + ' 个：' + fail[0], 'err');
      } else {
        ckMsg('同步失败：' + fail[0], 'err');
      }
      return;
    }
    var p = targets[i++];
    btn.textContent = '同步中 ' + i + '/' + targets.length + '…';
    var onDone = function (r) {
      if (r && r.ok) okN++;
      else fail.push(p.name + '（' + ((r && r.error) || '失败') + '）');
      step();
    };
    if (isCloud) VDLCookie.pushCloud(p, p.header, onDone);
    else VDLCookie.pushLocal(state.endpoint, p, p.header, onDone);
  }
  step();
}

(function bindCookieZone() {
  var zone = $('ckZone');
  if (!zone) return;
  // 首次展开才读 Cookie（不展开完全不碰，避免无谓的权限调用与用户疑虑）
  zone.addEventListener('toggle', function () {
    if (zone.open && !ckScanned && !ckBusy) ckScan();
  });
  if ($('ckScan')) $('ckScan').addEventListener('click', function () { ckScan(); });
  // 勾选状态持久化到 storage.local（background 的自动推送读同一个键）。
  // HTML 默认 checked；只有用户显式取消过才落 false —— 别把「读不到」当成关闭。
  if ($('ckAgree')) {
    try {
      var shareKey = VDLCookie.AUTO_KEY;
      chrome.storage.local.get([shareKey], function (d) {
        if (d && d[shareKey] === false) {
          $('ckAgree').checked = false;
          ckUpdateBtns();
        }
      });
    } catch (e) { /* 读不到就按 HTML 默认（共享） */ }
    $('ckAgree').addEventListener('change', function () {
      var on = !!$('ckAgree').checked;
      try {
        var patch = {};
        patch[VDLCookie.AUTO_KEY] = on;
        chrome.storage.local.set(patch);
      } catch (e) { /* 存不了不影响本次会话 */ }
      ckUpdateBtns();
      if (on) ckMsg('', '');
    });
  }
  if ($('ckPushLocal')) {
    $('ckPushLocal').addEventListener('click', function () { ckPush('local'); });
  }
  if ($('ckPushCloud')) {
    $('ckPushCloud').addEventListener('click', function () { ckPush('cloud'); });
  }
})();
