/* 视频工坊 媒体嗅探 —— MV3 service worker。
 *
 * 职责：webRequest 观察监听（不改写请求）→ sniff-core 判定 → 去重存储 →
 * 徽标计数；popup 通过 runtime 消息取列表 / 发送到桌面端。
 *
 * 设计要点（2026-09-27）：
 * - Referer/Cookie 在 Chrome 72+ 属于隐藏头，必须带 extraHeaders 才能读到；
 *   嗅探直链的 host 是 CDN，按 host 生成 Referer 必 403（桌面端 CDP 嗅探同坑），
 *   所以把页面真实 Referer / Cookie 一并捕获、随 /api/sniffer/send 回传。
 * - 过滤三类自噪音：本机回环（桌面端自己的 /api/media/proxy 中继流）、
 *   chrome-extension://（扩展自身请求）、非 http(s)。
 * - 只观察不阻塞：不用 onBeforeRequest 的 blocking 模式，MV3 无需
 *   declarativeNetRequest 权限，也不影响页面加载。
 * - 持久化用 chrome.storage.session（浏览器会话内有效，SW 被杀可恢复）。
 * - **分页隔离（2026-09-29）**：原实现是全局单库，逛多个站点后条目堆在一起，
 *   用户反馈「保存的信息太多了，保存当前页的就行」。现在按 tabId 分库
 *   （core.TabStores），并把「主文档请求」当作换页信号——换页即清掉该标签页
 *   上一页的条目；面板只呈现当前标签页的结果。
 */
'use strict';

importScripts('sniff-core.js');
// 登录态采集/自动共享（cookie-harvest.js 挂 globalThis.VDLCookie）。
// 只在 alarms 触发时用到；放这里是为了 SW 每次被唤醒都能拿到它。
importScripts('cookie-harvest.js');

var CORE = globalThis.VDLSniffCore;
var STORE_CAP = 200;        // 单个标签页的可下载项上限
var TAB_CAP = 30;           // 最多同时保留多少个标签页的分库
var PERSIST_DEBOUNCE_MS = 500;

var store = new CORE.TabStores(TAB_CAP, STORE_CAP);
var tabPage = {};            // storage.session['tabPage']，tabId -> 主文档 URL（换页判定基准）
var activeTabId = -1;        // 最近一次激活的标签页（徽标计数用）
var enabled = true;          // storage.local['enabled']，默认开
var carryCookie = true;      // storage.local['carryCookie']，发送时是否携带 Cookie
var endpoint = '';           // storage.local['endpoint']，探测到的桌面端基址
var sentUrls = {};           // storage.session['sentUrls']，已发送标记（popup 显示「已发送」）
var headerMeta = new Map();  // requestId -> {referer, cookie}
var persistTimer = null;

// 媒体原名线索（v1.0.57）：页面侧 pagewatch.js 采集到的「用户可见原名」。
// 直链 URL 常是纯数字 ID / 哈希，响应头 Content-Disposition 也是哈希，原名只在
// 页面 DOM 里 —— 必须单独按 URL 关联到嗅探条目，否则面板和落盘都是一串数字。
// 保留最近 60s / 40 条；服务端 _register 对 title 采用「后到即覆盖」，补推幂等。
var nameHints = [];            // {name, href, tabId, ts(ms)}，新→旧倒序遍历
var NAME_HINT_TTL = 60000;
var NAME_HINT_MAX = 40;

// ---- 视频页直推的记账（2026-10-01 v1.0.40）----
// 为什么必须落 storage.local：MV3 的 SW 空闲就被 Chrome 挂起，内存里的 pushTries 一重启
// 就归零。用户「YouTube 嗅探不到」时，面板上 pushed=0 既可能是「根本没推」，也可能只是
// SW 刚重启的假象 —— 判据不可信。现在跨重启累加，且记录最后一次成败原因。
var pushStat = { tries: 0, ok: 0, err: 0, pageTries: 0, lastErr: '', lastAt: 0 };
var pushStatTimer = null;
var PUSH_STAT_FLUSH_MS = 3000;    // 记账落盘节流
var pagePushed = {};              // tabId -> {key, at}：同一标签页同一视频页只自动推一次
var PAGE_PUSH_COOLDOWN = 300;     // 秒：同一页 5 分钟内不重复自动推（手动「发送当前页」忽略冷却）

var LOCAL_HOST_RE = /^(127\.0\.0\.1|localhost|::1|\[::1\])$/;

/** 读回跨重启的推送记账（SW 每次唤醒都要先恢复，否则又把历史抹成 0）。 */
function loadPushStat() {
  try {
    chrome.storage.local.get(['pushStat'], function (st) {
      var s = st && st.pushStat;
      if (!s || typeof s !== 'object') return;
      pushStat.tries = Number(s.tries) || 0;
      pushStat.ok = Number(s.ok) || 0;
      pushStat.err = Number(s.err) || 0;
      pushStat.pageTries = Number(s.pageTries) || 0;
      pushStat.lastErr = String(s.lastErr || '').slice(0, 120);
      pushStat.lastAt = Number(s.lastAt) || 0;
    });
  } catch (e) { /* storage 不可用：内存态照常工作 */ }
}

function savePushStat() {
  if (pushStatTimer) return;
  pushStatTimer = setTimeout(function () {
    pushStatTimer = null;
    try { chrome.storage.local.set({ pushStat: pushStat }); } catch (e) { /* 静默 */ }
  }, PUSH_STAT_FLUSH_MS);
}

/** 记一次推送结果：ok=true 表示桌面端（HTTP 2xx）确认收到。 */
function notePush(ok, err) {
  if (ok) {
    pushStat.ok++;
  } else {
    pushStat.err++;
    pushStat.lastErr = String(err || '未知').slice(0, 120);
  }
  pushStat.lastAt = Date.now() / 1000;
  savePushStat();
}

/** 换页判定基准（实现与测试都在 sniff-core.pageKeyOf）：忽略 hash ——
 *  页内锚点跳转不算换页，否则会把当前页刚嗅到的条目误清。 */
var pageKeyOf = CORE.pageKeyOf;

function isLocalUrl(u) {
  try { return LOCAL_HOST_RE.test(new URL(u).hostname); } catch (e) { return false; }
}

function shouldSkip(url, initiator) {
  if (!/^https?:/i.test(url)) return true;
  if (isLocalUrl(url)) return true;
  if (initiator && /^chrome-extension:/i.test(initiator)) return true;
  if (initiator && isLocalUrl(initiator)) return true;
  return false;
}

function rememberMeta(requestId, requestHeaders) {
  var picked = CORE.pickHeaders(null, requestHeaders);
  if (!picked.referer && !picked.cookie) return;
  headerMeta.set(requestId, picked);
  if (headerMeta.size > 1000) {
    // Map 保持插入序，弹最旧的 200 条防泄漏（正常请求都会走 onCompleted 清理）
    var drop = headerMeta.keys();
    for (var i = 0; i < 200; i++) {
      var k = drop.next();
      if (k.done) break;
      headerMeta.delete(k.value);
    }
  }
}

function persist() {
  if (persistTimer) return;
  persistTimer = setTimeout(function () {
    persistTimer = null;
    try {
      chrome.storage.session.set({
        vdlSniff: store.toJSON(),
        tabPage: tabPage,
        sentUrls: sentUrls,
        // pagePushed 也落会话存储（2026-10-02）：SW 空闲被挂起后重启，内存里的
        // 时长/标题记账就没了 —— popup 打开时的强推不带 duration，无从重建，
        // 弹窗兜底视图的「时长」会凭空消失。storage.session 寿命与浏览器会话一致，
        // 正好匹配「同一标签页里同一个视频页」的语义（关掉标签页/浏览器即失效）。
        pagePushed: pagePushed
      });
    } catch (e) { /* 会话存储不可用时静默（内存态仍在） */ }
  }, PERSIST_DEBOUNCE_MS);
}

// ---- 徽标 = 「本页到底提取到没有」的一眼可见提示（v1.0.49）----
// 2026-10-02 用户反馈：「让用户知道有提取到，不用点进去才知道有没有提取到」。
// 旧口径只数 store（本地直链/清单条目），而 YouTube 这类 SABR 站点的收成**不进 store**
// —— 它们是「整页交给桌面端解析」（pushPageToServer）。只数 store 的话，用户人在 YouTube
// 上角标永远空着，只能点开面板才知道到底提没提取到；若桌面端没启动，更是看不出任何异常。
// 现在四态（并同步写进 action.setTitle 悬停提示，不点开也能看明白）：
//   数字  本地可下载条目数（蓝）—— 直链/HLS，点开就能下
//   ✓     整页已提取且桌面端**确认收到**（绿）—— 去桌面端「媒体嗅探」看解析结果
//   !     提交了但桌面端没响应（琥珀）—— 提醒确认「视频工坊」是否在跑
//   …     刚提交、回执未到（琥珀）
var BADGE_COLORS = { count: '#2563eb', ok: '#16a34a', warn: '#d97706', off: '#9ca3af' };
var BADGE_NAME = '视频工坊 媒体嗅探：';

/** chrome.action 的各 setter 在旧 Chrome / 测试桩里可能缺 → 统一守卫调用，
 *  否则一个缺失的 API 会把后面的设置整段吞掉（外面那层 try 只报错不继续）。 */
function setAction(name, arg) {
  try {
    if (chrome.action && typeof chrome.action[name] === 'function') chrome.action[name](arg);
  } catch (e) { /* 无 action 场景忽略 */ }
}

/** 算出当前标签页该显示的角标（纯函数式：只读 store / pagePushed，便于测试钉住）。 */
function badgeState(tid, tab) {
  if (!enabled) {
    return { text: '', color: BADGE_COLORS.off, title: BADGE_NAME + '已暂停（点开可开启）' };
  }
  var n = (typeof tid === 'number' && tid >= 0) ? store.count(tid) : 0;
  var url = (tab && tab.url) || '';
  var vp = url ? CORE.isVideoPage(url) : '';
  var rec = null;
  if (vp && typeof tid === 'number' && tid > 0) {
    var r = pagePushed[tid];
    if (r && r.key === pageKeyOf(vp)) rec = r;
  }
  if (n > 0) {
    return {
      text: n > 99 ? '99+' : String(n),
      color: BADGE_COLORS.count,
      title: BADGE_NAME + '本页已提取 ' + n + ' 个可下载资源（点开查看）'
    };
  }
  if (rec && rec.ok === true) {
    return {
      text: '✓', color: BADGE_COLORS.ok,
      title: BADGE_NAME + '本页已提取，已交给桌面端解析（点开本节，或到桌面端「媒体嗅探」查看）'
    };
  }
  if (rec && rec.ok === false) {
    return {
      text: '!', color: BADGE_COLORS.warn,
      title: BADGE_NAME + '本页已尝试提取，但桌面端未响应'
        + (rec.err ? '（' + rec.err + '）' : '') + ' —— 请确认「视频工坊」已启动'
    };
  }
  if (rec) {
    return { text: '…', color: BADGE_COLORS.warn, title: BADGE_NAME + '正在提取本页…' };
  }
  if (vp) {
    return {
      text: '', color: BADGE_COLORS.off,
      title: BADGE_NAME + '本页是视频页，尚未提取到内容（点开可重新提交）'
    };
  }
  return { text: '', color: BADGE_COLORS.off, title: BADGE_NAME + '本页未提取到可下载内容' };
}

/** 徽标 = **当前标签页**的提取状态（列表已按页隔离，徽标口径必须跟着走）。 */
function updateBadge() {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    var tab = tabs && tabs[0];
    if (tab && typeof tab.id === 'number') activeTabId = tab.id;
    var st = badgeState(activeTabId, tab);
    setAction('setBadgeText', { text: st.text });
    setAction('setBadgeBackgroundColor', { color: st.color });
    setAction('setBadgeTextColor', { color: '#ffffff' });
    setAction('setTitle', { title: st.title });
  });
}

function handleCompleted(details, early) {
  var url = details.url || '';
  var initiator = details.initiator || details.originUrl || '';
  if (shouldSkip(url, initiator)) {
    if (!early) headerMeta.delete(details.requestId);
    return;
  }
  var tabId = (typeof details.tabId === 'number') ? details.tabId : -1;
  // HLS 清单常由播放器用**跨域 fetch** 拉取（Sec-Fetch-Site: cross-site），Chrome 对这类
  // 请求可能给不出 tabId（-1）。照原样塞进「无标签页」分库，弹窗按当前标签页取列表
  // 就永远是空 —— 表现为「视频在播，扩展却提示没嗅探到」，而桌面端其实已收到条目
  // （2026-11-08 nmwchxx.com 实测：page_url 退化成 initiator、page_title 为空即此分支）。
  // 参考猫抓「抓到的资源一律不丢」：归属不明时归到当前激活标签页，既修好显示，
  // 又保留用户要的「只保存当前页」分库语义。
  if (tabId <= 0 && activeTabId > 0) tabId = activeTabId;

  // 主文档请求 = 这个标签页换页了 → 丢掉上一页嗅到的条目（「只保存当前页」的闸门）。
  // 用 pageKeyOf 忽略 hash：页内锚点跳转不算换页。
  if (details.type === 'main_frame') {
    var pk = String(tabId);
    var prev = tabPage[pk] || '';
    if (prev && prev !== pageKeyOf(url)) {
      store.clearTab(tabId);
      updateBadge();
    }
    tabPage[pk] = pageKeyOf(url);
    persist();
    // 视频**页面**直推（2026-10-01）：YouTube 等站点走 UMP/SABR，媒体流抓不到、
    // 抓到也不能直接下；推页面链接让桌面端用 yt-dlp 解析（含 web_safari 回退）最稳。
    var vp = CORE.isVideoPage(url);
    if (vp && tabId > 0) pushPageToServer(tabId, vp);
    else if (vp) pushPageToServer(0, vp);
  }

  var picked = CORE.pickHeaders(details.responseHeaders, null);
  var meta = headerMeta.get(details.requestId) || {};
  // 早捕（early）时不能删：onCompleted 还要用同一份 Cookie / Referer 补推一次
  if (!early) headerMeta.delete(details.requestId);

  // mime 权威原则与桌面端一致：响应头 text/html 等错报一律过滤
  var res = store.add(tabId, {
    url: url,
    mime: picked.mime,
    referer: meta.referer || initiator || '',
    pageUrl: meta.referer || initiator || details.url || '',
    pageTitle: '',
    // 媒体原名（页面 DOM 采集，按 URL / 时间窗口关联）——URL 是纯数字 ID 时，
    // 这才是用户要看到、要用来存盘的名字；缺省空串时后端回落到 basename，行为不变。
    title: nameForUrl(url, tabId),
    cookie: meta.cookie || '',
    // 大小（2026-10-02）：onHeadersReceived 已从响应头解析出 contentLength → 落进条目
    contentLength: picked.contentLength,
    ts: Date.now() / 1000
  });
  if (res && res.isNew) updateBadge();
  if (res && tabId > 0) {
    // 补页面标题（异步，失败不影响条目）
    try {
      chrome.tabs.get(tabId, function (tab) {
        if (chrome.runtime.lastError || !tab) return;
        res.item.pageTitle = tab.title || '';
        res.item.pageUrl = tab.url || res.item.pageUrl;
        persist();
        pushToServer(res.item);   // 拿到标题再推，面板「来源」更完整
      });
    } catch (e) {
      pushToServer(res.item);
      /* tab 已关闭等 */
    }
  } else if (res) {
    pushToServer(res.item);
  }
  persist();
}

/** 自动嗅探直推（2026-10-01）：扩展每嗅到新条目就发给桌面端，进同一个
 *  items 库——没有这条链路，桌面面板列表永远是空的（用户以为没嗅到）。
 *  服务端按 URL 去重，重复推无副作用；失败静默（下次同 URL 再推）。 */
var pushTries = 0;            // 本次 SW 生命周期内推送尝试数（遥测：定位「没捕获」vs「推送失败」）
var pageSeenCount = 0;        // 内容脚本哨兵报来的视频页数（遥测：判断页面侧通道是否活着，v1.0.40）
var seenEvents = 0;           // onCompleted 观察到的响应总数（遥测）
var mediaHits = 0;            // 其中被判成媒体/播放列表/分片的条数（遥测）
var lastMime = '';            // 最近一条响应的 Content-Type（遥测：判定矩阵是否认这个站）

/** 视频页面链接直推（2026-10-01）：UMP/SABR 站点的媒体流抓不到，推页面让桌面端解析。
 *  v1.0.40 加两件事：①per-tab 去重（同一页 5 分钟内不重复打扰桌面端）；
 *  ②推送成败记账 → chrome.storage.local（见文件头 pushStat 说明）。 */
function pushPageToServer(tabId, pageUrl, title, force, duration) {
  if (!pageUrl) return;
  // 嗅探开关关掉 = 什么都不推（含 popup 的强推）——开关是用户的唯一总闸，不能有旁路
  if (!enabled) return;
  var key = pageKeyOf(pageUrl);
  var tid = (typeof tabId === 'number' && tabId > 0) ? tabId : -1;
  if (tid > 0 && force !== true) {
    var prev = pagePushed[tid];
    if (prev && prev.key === key && (Date.now() / 1000 - prev.at) < PAGE_PUSH_COOLDOWN) {
      return;
    }
  }
  // title 一并记账：标题后到的补推（refreshPageTitle）靠「同页同标题」去重，
  // 不记就没法判断这次的标题是不是已经推过了。duration 同样记账（2026-10-02）：
  // 视频页的时长要等播放器就绪才准，是**后到**的，去重必须连它一起比，
  // 否则「标题同、时长刚拿到」的这条会被当成重复推而丢掉。
  if (tid > 0) {
    // duration 是**后到值**：只有页面侧 pagewatch.js 读得到。而 popup 一打开就发
    // pushPageNow（只有 url/title，不带 duration）—— 若这里直接写 0，会把刚拿到的
    // 时长当场抹掉，弹窗兜底视图的「时长」就永远空着（2026-10-02 用户截图实测：
    // 提示块里始终没有时长，就是这一句造成的）。同一视频页（key 一致）保留已知时长；
    // 换页则重置为 0（新页的时长要等它自己的播放器就绪后重新读）。
    var prevRec = pagePushed[tid];
    var samePage = !!(prevRec && prevRec.key === key);
    var keepDur = samePage ? (prevRec.duration || 0) : 0;
    pagePushed[tid] = {
      key: key, at: Date.now() / 1000,
      title: title || '', duration: duration || keepDur,
      // 桌面端回执（v1.0.49 角标提示用）：true=确认收到 / false=没响应 / undefined=回执未到。
      // 同一视频页保留上一次回执 —— 标题/时长的补推（refreshPageTitle）会重进这里，
      // 若每次都清成 undefined，角标会在 ✓ 与 … 之间来回跳。
      ok: samePage ? prevRec.ok : undefined,
      err: samePage ? (prevRec.err || '') : ''
    };
    persist();   // 记账落 storage.session：SW 被挂起重启后仍能答出「这一页的时长/标题」
    updateBadge();   // 角标立刻反映「正在提取 / 已提取」
  }
  pushTries++;                 // 兼容旧遥测口径（内存值）
  pushStat.tries++;
  pushStat.pageTries++;
  savePushStat();

  // 桌面端回执 → 写回该标签页的记账并刷角标（✓ / !），这是「不用点进去就知道提取到没有」的依据
  function markResult(okFlag, errText) {
    if (tid > 0 && pagePushed[tid] && pagePushed[tid].key === key) {
      pagePushed[tid].ok = !!okFlag;
      pagePushed[tid].err = okFlag ? '' : String(errText || '').slice(0, 60);
      persist();
    }
    updateBadge();
  }

  function send(t) {
    getEndpoint(function (base) {
      if (!base) { notePush(false, '未找到桌面端'); markResult(false, '未找到桌面端'); return; }
      try {
        fetch(base + '/api/sniffer/ext-push', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            items: [{
              url: pageUrl,
              mime: 'text/html',
              kind_hint: 'page',
              referer: '',
              cookie: '',
              page_url: pageUrl,
              page_title: t || '',
              // 时长（秒，0=未知）：只有页面侧 read <video>.duration 拿得到，见 pagewatch.js
              duration: duration || 0
            }]
          })
        }).then(function (r) {
          if (r && r.ok) { notePush(true); markResult(true, ''); }
          else {
            var m = 'HTTP ' + ((r && r.status) || '?');
            notePush(false, m); markResult(false, m);
          }
        }).catch(function (e) {
          var m = (e && e.message) || '网络错误';
          notePush(false, m); markResult(false, m);
        });
      } catch (e) { notePush(false, '发送异常'); markResult(false, '发送异常'); }
    });
  }

  if (title) { send(title); return; }
  if (tid > 0) {
    try {
      chrome.tabs.get(tid, function (tab) {
        var t = '';
        try { t = (tab && !chrome.runtime.lastError && tab.title) || ''; } catch (e) { /* 关页 */ }
        // 记账里补上真实取到的标题，否则后续「标题补推」会把它当成没推过而重复推一次
        if (t && pagePushed[tid]) pagePushed[tid].title = t;
        send(t);
      });
      return;
    } catch (e) { /* 落到同步分支 */ }
  }
  send('');
}

/** 「这个标签页当前是不是视频页」→ 是就直推（v1.0.40 多入口触发）。
 *
 *  为什么必须补入口：原来只在 webRequest 的 main_frame 上判。但 YouTube 从首页点进
 *  某个视频是 **SPA 换页（不发主文档请求）**，而且用户往往在装/更新扩展前就把页面开着了
 *  —— 主文档请求早发生过，事件永远不会再来。2026-10-01 实测：面板 pushed=0、
 *  桌面端条目里零 YouTube 记录，而同一时刻腾讯视频能正常嗅到（那些站是整页导航）。
 *  现在四条不依赖主文档请求的入口：内容脚本哨兵 / 切标签页 / 标签页地址变化 / 心跳兜底。 */
function checkPagePush(tabId, url, title, force, duration) {
  if (!enabled) return false;
  if (typeof url !== 'string' || !url) return false;
  var vp = CORE.isVideoPage(url);
  if (!vp) return false;
  pushPageToServer(tabId, vp, title || '', force === true, duration || 0);
  return true;
}

/** 标题修正推送（v1.0.46）：SPA 换页时**地址先到、标题后到**。
 *
 *  YouTube 点开新视频的那一刻 changeInfo.url 已经变了，但 tab.title 还是**上一页**的
 *  （新页标题要等数据加载完才改）。首推因此必然带旧标题，而「同一页 5 分钟冷却」
 *  又把重推挡住 → 桌面端列表里最新那条**永远显示上一个视频的标题**，用户看到的就是
 *  「嗅探到的比视频慢一步、最新嗅探到的是上一个视频」（2026-10-02 用户实测）。
 *  标题 settle 后再补推一次，桌面端据此把标题覆盖成正确的（服务端见
 *  cdp_sniffer._register 的 page 条目覆盖规则）。
 *
 *  去重口径是「同页 + 同标题 + 同时长」：标题来回抖动只推一次，换页（key 变）必推；
 *  时长是播放器就绪后才拿到的「后到值」（pagewatch.js 的 player 补报），
 *  所以标题相同、时长刚拿到时也必须补推一次 —— 否则列表里时长永远空着（2026-10-02）。 */
function refreshPageTitle(tabId, pageUrl, title, duration) {
  if (!enabled) return false;
  var du = duration || 0;
  if (!title && !du) return false;          // 标题与时长都没有 → 没什么可补的
  var vp = CORE.isVideoPage(pageUrl);
  if (!vp) return false;
  var tid = (typeof tabId === 'number' && tabId > 0) ? tabId : -1;
  var rec = tid > 0 ? pagePushed[tid] : null;
  if (rec && rec.key === pageKeyOf(vp) && rec.title === title
      && (rec.duration || 0) === du) return false;
  pushPageToServer(tid, vp, title, true, du);   // force：标题/时长修正必须送达，不被冷却挡下
  return true;
}

/** 按 tabId 取当前 URL/标题后再判（切标签页用）。 */
function checkPagePushFromTab(tabId, force) {
  try {
    chrome.tabs.get(tabId, function (tab) {
      if (chrome.runtime.lastError) return;
      if (tab) checkPagePush(tabId, tab.url, tab.title, force);
    });
  } catch (e) { /* 标签页已关闭 */ }
}

/** 当前窗口激活的标签页（心跳兜底：浏览器挂着不动也能在 ≤60s 内交出去）。 */
function checkActiveTabPush(force) {
  try {
    chrome.tabs.query({ active: true, lastFocusedWindow: true }, function (tabs) {
      if (chrome.runtime.lastError) return;
      var t = tabs && tabs[0];
      if (t && typeof t.id === 'number') checkPagePush(t.id, t.url, t.title, force);
    });
  } catch (e) { /* 无窗口场景忽略 */ }
}

/** 两条 URL 是否指向同一份媒体：完整相等，或同 origin + pathname
 *  （下载直链常带 time/sign 之类一次性查询串，忽略它才能对上）。 */
function sameMediaUrl(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  try {
    var pa = new URL(a), pb = new URL(b);
    return pa.origin === pb.origin && pa.pathname === pb.pathname;
  } catch (e) { return false; }
}

/** 新建嗅探条目时，从页面线索里「认领」原名：
 *  ① 下载链接 URL 精确匹配（最强，`target="_blank"` 新开标签也认得）；
 *  ② 20s 内、同标签页优先、且**没有 href** 的线索（页面用 window.open / Blob
 *     下发时拿不到链接，只能靠时间窗口兜底）。
 *  🔴 带 href 却没匹配上的名称**绝不参与**时间窗口兜底：它明确属于另一份媒体，
 *     挂到新条目上就是「上一首歌的名字串到这一首」（测试 test_media_name.js 钉住）。
 *  超过 20s 的旧名称一律不认。 */
function nameForUrl(url, tabId) {
  var now = Date.now();
  var i, h;
  for (i = nameHints.length - 1; i >= 0; i--) {
    h = nameHints[i];
    if (now - h.ts > NAME_HINT_TTL) continue;
    if (h.href && sameMediaUrl(h.href, url)) return h.name;
  }
  for (i = nameHints.length - 1; i >= 0; i--) {
    h = nameHints[i];
    if (now - h.ts > 20000 || h.href) continue;
    if (tabId > 0 && h.tabId === tabId) return h.name;
  }
  for (i = nameHints.length - 1; i >= 0; i--) {
    h = nameHints[i];
    if (now - h.ts > 20000 || h.href) continue;
    return h.name;
  }
  return '';
}

/** 页面侧上报原名时：先记住，再给「已嗅到但当时还没名字」的条目补写并重推。
 *  只在 URL 能匹配上时补写 —— 名称晚到却对不上 URL 时宁可不补，绝不误伤同页
 *  其它音频（试听流与下载流常是两个不同地址）。服务端按 URL 去重，补推无副作用。 */
function noteMediaName(name, href, tabId) {
  if (!name) return;
  var now = Date.now();
  nameHints.push({ name: name, href: href || '', tabId: tabId, ts: now });
  while (nameHints.length > NAME_HINT_MAX) nameHints.shift();
  while (nameHints.length && now - nameHints[0].ts > NAME_HINT_TTL) nameHints.shift();
  if (!href) return;
  var hit = store.setTitleByUrl(href, name);
  if (hit) {
    persist();
    pushToServer(hit);
  }
}

function pushToServer(item) {
  if (!item || !item.url) return;
  pushTries++;
  getEndpoint(function (base) {
    if (!base) return;
    try {
      fetch(base + '/api/sniffer/ext-push', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: [{
            url: item.url,
            mime: item.mime || '',
            kind_hint: item.kind || '',
            referer: item.referer || '',
            cookie: item.cookie || '',
            page_url: item.pageUrl || '',
            page_title: item.pageTitle || '',
            // 媒体原名（2026-11-13）：页面 DOM 采集的用户可见名称，服务端/桌面端
            // 优先用它做列表标题与保存文件名；空串则回落到原有的 basename 行为。
            title: item.title || '',
            // 大小（字节，0=未知）：来自响应头 Content-Length，见 sniff-core pickHeaders
            size: item.size || 0
          }]
        })
      }).catch(function () {});
    } catch (e) { /* 静默 */ }
  });
}

try {
  chrome.webRequest.onSendHeaders.addListener(
    function (details) {
      if (!enabled) return;
      if (shouldSkip(details.url || '', details.initiator || '')) return;
      rememberMeta(details.requestId, details.requestHeaders);
    },
    { urls: ['http://*/*', 'https://*/*'] },
    ['requestHeaders', 'extraHeaders']
  );

  // 早捕（2026-11-15）：MP3 / MP4 直链是边下边播的流式响应，onCompleted 要等整段
  // 传完才触发（实测同一条 7.9MB 直链：响应头 3.1s 到达、请求完成 52.1s），
  // 这几十秒里弹窗一直显示「没有嗅探到媒体流」，用户以为没抓到。
  // 这里在响应头一到达就入库：store.add 按 URL 幂等，onCompleted 再走一遍不会
  // 重复推送；headerMeta 保留，让 onCompleted 仍能用同一份 Cookie / Referer。
  // 独立守卫：某些环境没有这个事件时，绝不能连累下面 onCompleted 的注册。
  if (chrome.webRequest.onResponseStarted) {
    try {
      chrome.webRequest.onResponseStarted.addListener(
        function (details) {
          if (!enabled) return;
          try {
            var _m = CORE.pickHeaders(details.responseHeaders, null).mime || '';
            if (!CORE.classifyMedia(details.url || '', _m)) return;   // 非媒体请求一律不碰
            handleCompleted(details, true);
          } catch (e) { /* 早捕失败不影响原有 onCompleted 路径 */ }
        },
        { urls: ['http://*/*', 'https://*/*'] },
        ['responseHeaders', 'extraHeaders']
      );
    } catch (e) { /* 无早捕能力时静默退回原 onCompleted 路径 */ }
  }

  chrome.webRequest.onCompleted.addListener(
    function (details) {
      if (!enabled) return;
      // 遥测（1.0.39）：统计所有响应数 / 被判定为媒体的数量 / 最近一次 Content-Type
      // ——「嗅探不到」时一眼看出是「没流量」还是「站点的 mime 判定矩阵不认」。
      try {
        seenEvents++;
        var _m = CORE.pickHeaders(details.responseHeaders, null).mime || '';
        if (_m) lastMime = String(_m).slice(0, 60);
        if (CORE.classifyMedia(details.url || '', _m)) mediaHits++;
      } catch (e) { /* 遥测失败不影响主流程 */ }
      handleCompleted(details);
    },
    { urls: ['http://*/*', 'https://*/*'] },
    ['responseHeaders', 'extraHeaders']
  );

  chrome.webRequest.onErrorOccurred.addListener(
    function (details) { headerMeta.delete(details.requestId); },
    { urls: ['http://*/*', 'https://*/*'] }
  );
} catch (e) {
  console.error('webRequest 监听注册失败', e);
}

// ---- 标签页生命周期：关页丢库、切页刷徽标（均不需要额外权限） ----
try {
  if (chrome.tabs && chrome.tabs.onRemoved) {
    chrome.tabs.onRemoved.addListener(function (tabId) {
      store.dropTab(tabId);
      delete tabPage[String(tabId)];
      persist();
      updateBadge();
    });
  }
  if (chrome.tabs && chrome.tabs.onActivated) {
    chrome.tabs.onActivated.addListener(function (info) {
      activeTabId = info && typeof info.tabId === 'number' ? info.tabId : activeTabId;
      updateBadge();
      // v1.0.40：切到该标签页就判一次视频页 —— 「早就开着的 YouTube 页」靠这条兜住
      if (info && typeof info.tabId === 'number') checkPagePushFromTab(info.tabId);
    });
  }
  // v1.0.40：标签页地址变化（SPA 换页，无主文档请求）/ 整页加载完成
  // v1.0.46：再加一条「标题变化」。地址先到、标题后到是 SPA 常态（见 refreshPageTitle
  // 顶部注释）——只认地址变化的话，条目会带着上一页的标题钉死 5 分钟冷却期。
  if (chrome.tabs && chrome.tabs.onUpdated) {
    chrome.tabs.onUpdated.addListener(function (tabId, changeInfo, tab) {
      if (!changeInfo) return;
      var tu = (tab && tab.url) || '';
      if (changeInfo.url) {
        // 换页先刷角标：新页的 ✓/数字不该沿用上一页的，等 push 回执再更新（v1.0.49）
        updateBadge();
        checkPagePush(tabId, changeInfo.url, (tab && tab.title) || '');
      }
      // 标题 settle 后补推一次（同页同标题去重，标题抖动不重复打扰）
      if (changeInfo.title && tu) refreshPageTitle(tabId, tu, changeInfo.title);
      if (!changeInfo.url && !changeInfo.title && changeInfo.status === 'complete' && tu) {
        checkPagePush(tabId, tu, tab.title || '');
      }
    });
  }
} catch (e) {
  console.error('标签页监听注册失败', e);
}

// ---- 端点探测：desktop_launcher 从 8321 起顺延找空闲端口，这里扫同一张表 ----
function fetchAbort(url, ms) {
  var ctrl = new AbortController();
  var t = setTimeout(function () { ctrl.abort(); }, ms);
  return fetch(url, { signal: ctrl.signal }).finally(function () { clearTimeout(t); });
}

async function probeEndpoint(cb) {
  var bases = CORE.probeBases();
  for (var i = 0; i < bases.length; i++) {
    var base = bases[i];
    try {
      var r = await fetchAbort(base + '/api/sniffer/status', 900);
      if (r.ok) {
        try {
          var j = await r.json();
          if (j && typeof j.state === 'string') { cb(base); return; }
        } catch (e) { /* JSON 不合预期 → 落到公开端点验证 */ }
      }
      // status 不合预期（如 VDL_API_TOKEN 开启后 401）→ 验证免 token 的 /api/nodes
      try {
        var r2 = await fetchAbort(base + '/api/nodes', 900);
        if (r2.ok) { cb(base); return; }
      } catch (e) { /* 下一个端口 */ }
    } catch (e) { /* 端口不通，下一个 */ }
  }
  cb('');
}

function getEndpoint(cb) {
  if (endpoint) { cb(endpoint); return; }
  chrome.storage.local.get(['endpoint'], function (st) {
    if (st.endpoint) { endpoint = st.endpoint; cb(endpoint); return; }
    probeEndpoint(function (base) {
      if (base) {
        endpoint = base;
        chrome.storage.local.set({ endpoint: base });
      }
      cb(base);
    });
  });
}

// ---- 心跳（2026-10-01）：桌面端面板显示「扩展已连接 ✓」的依据 ----
// MV3 SW 空闲 ~30s 被 Chrome 挂起；每次唤醒（webRequest 事件/消息/alarms）
// 都会重跑顶层代码 → 顺手 ping 一次。alarms 每分钟兜底（浏览器挂着不动也在线）。

// ---- 零点击自动更新：心跳响应带回「磁盘上已有新版」→ 扩展自己重载自己 ----
// （2026-10-02 用户问「扩展程序更新怎么办」）
//
// 为什么必须这么绕：解压版（Load unpacked）扩展 Chrome **不会自动更新** —— 它直接以
// 本地文件夹为源，改了文件也必须到 chrome://extensions 点一次 ↻（官方行为；实测同样：
// 覆盖成 1.0.43 后心跳仍自报 1.0.42，直到用户点 ↻）。但官方文档明确「解压版被
// reload 视为一次 update」，`chrome.runtime.reload()` 同样有效 → 于是拆成两半：
//   ① 桌面端把新版文件写进用户的扩展加载目录（server/routers/extension.py::maybe_sync
//      + extension_sync.py，带 manifest.name 校验，只增不删）
//   ② 心跳响应里回一句 reload_to → 这里自己重载
// 两条接上，用户就再也不用覆盖目录 / 点 ↻。**不需要任何新权限**（这是刻意的：
// 一旦 manifest 增权限，静默更新就会变成需要用户重新授权）。
//
// 防重启死循环：chrome.storage.local['autoReloadTried'] 记下「已为哪个目标版本试过重载」，
// 同一目标只试一次。若重载后版本仍未变（磁盘其实没写成功），就不再反复重启，
// 由桌面端面板提示用户手动点 ↻ 兜底。
function _verTuple(v) {
  var s = String(v || '').trim();
  if (!s) return null;
  var parts = s.split('.'), out = [];
  for (var i = 0; i < parts.length; i++) {
    if (!/^\d+$/.test(parts[i])) return null;
    out.push(parseInt(parts[i], 10));
  }
  return out;
}

function _verNewer(a, b) {
  var ta = _verTuple(a), tb = _verTuple(b);
  if (!ta || !tb) return false;             // 任一侧非法一律「不比新」
  for (var i = 0; i < Math.max(ta.length, tb.length); i++) {
    var x = ta[i] || 0, y = tb[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}

function maybeAutoReload(resp) {
  try {
    if (!resp || resp.ok !== true) return;
    var target = resp.reload_to || '';
    if (!target) return;
    var own = '';
    try { own = (chrome.runtime.getManifest() || {}).version || ''; } catch (e) {}
    if (!_verNewer(target, own)) return;    // 只有确实更新才重载（绝不自降级）
    chrome.storage.local.get(['autoReloadTried'], function (st) {
      var tried = (st && st.autoReloadTried) || {};
      if (tried[target]) return;            // 同一目标只试一次
      tried[target] = Date.now();
      chrome.storage.local.set({ autoReloadTried: tried }, function () {
        try { chrome.runtime.reload(); } catch (e) {}
      });
    });
  } catch (e) {}
}

function heartbeat() {
  // v1.0.40：浏览器挂着不动时的兜底 —— 每分钟看一眼激活标签页是不是视频页
  checkActiveTabPush(false);
  getEndpoint(function (base) {
    if (!base) return;
    try {
      // 1.0.37 起心跳带自报版本：桌面端与包内扩展比对 → 旧版提示一键更新。
      // 1.0.38 起再带捕获/推送遥测：captured=本地库条数、pushed=推送尝试数
      // ——用户「嗅探不到」时，一眼分清断在捕获层还是推送层。
      // 1.0.40 起补 push_ok/push_err/push_page（落盘的跨重启记账）+ 内容脚本哨兵遥测。
      // 1.0.44 起**读响应**：reload_to 非空即自动重载（见 maybeAutoReload）。
      var ver = '', captured = 0;
      try { ver = (chrome.runtime.getManifest() || {}).version || ''; } catch (e) {}
      try { captured = (typeof store.totalCount === 'function') ? store.totalCount() : 0; } catch (e) {}
      fetch(base + '/api/sniffer/ext-ping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          version: ver, captured: captured, pushed: pushTries,
          seen: seenEvents, media: mediaHits, last_mime: lastMime,
          push_ok: pushStat.ok, push_err: pushStat.err, push_page: pushStat.pageTries,
          push_last_err: pushStat.lastErr,
          page_saw: pageSeenCount
        }),
      }).then(function (r) { return r.json(); })
        .then(maybeAutoReload)
        .catch(function () {});
    } catch (e) {}
  });
}
heartbeat();
try {
  chrome.alarms.create('vdlHeartbeat', { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener(function (a) {
    if (a && a.name === 'vdlHeartbeat') heartbeat();
  });
} catch (e) { console.error('alarms 注册失败', e); }

// ---- 默认自动共享登录态到云端公共池（产品策略，2026-10-18）----
// 与桌面 App 的「启动推一次 + 每 30 分钟」同节奏。三个触发点共用 autoPush
// 内部的节流戳（storage.local.vdlCookieLastPush），所以不会因为多重触发而连推：
//   · alarm 每 30 分钟（浏览器挂着不动也照跑，不依赖用户点扩展图标）
//   · onInstalled / onStartup（装完、开机各补一次）
// 用户在 popup 取消勾选 → storage.local.vdlCookieShare = false → 这里直接跳过。
function cookieAutoPush(why) {
  try {
    if (!globalThis.VDLCookie || typeof VDLCookie.autoPush !== 'function') return;
    VDLCookie.autoPush(chrome, null, function (r) {
      if (r && r.pushed) {
        console.log('[cookie-sync] ' + why + ' pushed=' + r.pushed + '/' + (r.total || 0));
      }
    });
  } catch (e) { console.error('[cookie-sync] 失败', e); }
}
try {
  chrome.alarms.create('vdlCookieSync', { periodInMinutes: 30 });
  chrome.alarms.onAlarm.addListener(function (a) {
    if (a && a.name === 'vdlCookieSync') cookieAutoPush('alarm');
  });
  chrome.runtime.onInstalled.addListener(function () { cookieAutoPush('installed'); });
  chrome.runtime.onStartup.addListener(function () { cookieAutoPush('startup'); });
} catch (e) { console.error('cookie-sync 注册失败', e); }

// ---- 启动恢复 ----
(function restore() {
  loadPushStat();          // v1.0.40：先恢复跨重启的推送记账，否则心跳一直报 0
  chrome.storage.local.get(['enabled', 'carryCookie', 'endpoint'], function (st) {
    if (st.enabled === false) enabled = false;
    if (st.carryCookie === false) carryCookie = false;
    if (st.endpoint) endpoint = st.endpoint;
  });
  chrome.storage.session.get(['vdlSniff', 'tabPage', 'sentUrls', 'pagePushed'], function (st) {
    if (st.vdlSniff) store.loadFrom(st.vdlSniff);
    if (st.tabPage) tabPage = st.tabPage;
    if (st.sentUrls) sentUrls = st.sentUrls;
    if (st.pagePushed && typeof st.pagePushed === 'object') pagePushed = st.pagePushed;
    // 已关闭标签页的残留分库清掉（只删不存在的，不动现存标签页的数据）
    try {
      chrome.tabs.query({}, function (tabs) {
        if (chrome.runtime.lastError) return;
        var ids = (tabs || []).map(function (t) { return t.id; });
        store.dropTabsExcept(ids);
        // pagePushed 同理：只保留还开着的标签页，免得越攒越多
        Object.keys(pagePushed).forEach(function (k) {
          if (ids.indexOf(Number(k)) < 0) delete pagePushed[k];
        });
        updateBadge();
        persist();
      });
    } catch (e) {
      updateBadge();
    }
  });
})();

// ---- popup 消息通道 ----
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  msg = msg || {};
  // popup 传它自己的标签页 id；没传（旧版 popup）则回落到最近激活的标签页
  function scopeTabId() {
    return (typeof msg.tabId === 'number' && msg.tabId >= 0) ? msg.tabId : activeTabId;
  }
  switch (msg.type) {
    case 'getState':
      var tid = scopeTabId();
      var hostOf = function (u) {
        try { return new URL(u).hostname.toLowerCase(); } catch (e) { return ''; }
      };
      var scoped = store.list(tid);
      // 兼容存量：修复前「归属不明」的请求都进了 -1 分库，升级后不再产生，但当前页
      // 已经抓到的流要能立刻看见（不必刷新重播）。只并入同站条目，避免串进别页内容。
      if (!scoped.length && tid > 0) {
        var curHost = hostOf(tabPage[String(tid)] || '');
        if (curHost) {
          var orphans = store.list(-1) || [];
          for (var oi = 0; oi < orphans.length; oi++) {
            var it = orphans[oi];
            if (hostOf(it.referer || it.pageUrl || '') === curHost) scoped.push(it);
          }
        }
      }
      var snap = {
        enabled: enabled,
        carryCookie: carryCookie,
        endpoint: endpoint,
        tabId: tid,
        items: scoped,
        segments: store.segments(tid),
        sentUrls: sentUrls,
        // v1.0.40：把推送记账带给 popup —— 「没嗅到」时用户能自己看出是不是推送失败
        pushOk: pushStat.ok,
        pushErr: pushStat.err,
        pushPage: pushStat.pageTries,
        pushLastErr: pushStat.lastErr,
        videoPage: '',
        // 当前页视频标题（popup 兜底视图用，2026-10-02）：加密流站点抓不到直链，
        // 但「这一页是什么视频」是已知的 —— 标题就是标签页标题（兜底用推送记账里的）。
        videoTitle: '',
        pagePushAt: 0,
        // 当前页视频时长（秒，0=未知）：复用 pushPageToServer 的 per-tab 记账
        // （popup 没有 scripting 权限、也不该为此加权限，见 manifest 注释）→ 2026-10-02
        videoDuration: 0
      };
      if (typeof tid !== 'number' || tid < 0) { sendResponse(snap); return false; }
      (function () {
        var replied = false;
        var reply = function (vp, at) {
          if (replied) return;
          replied = true;
          snap.videoPage = vp || '';
          snap.pagePushAt = at || 0;
          sendResponse(snap);
        };
        try {
          chrome.tabs.get(tid, function (tab) {
            var u = '';
            var tt = '';
            try {
              u = (tab && !chrome.runtime.lastError && tab.url) || '';
              tt = (tab && !chrome.runtime.lastError && tab.title) || '';
            } catch (e) { /* 关页 */ }
            var vp = u ? CORE.isVideoPage(u) : '';
            var rec = (vp && pagePushed[tid]) ? pagePushed[tid] : null;
            var ok = !!(rec && vp && rec.key === pageKeyOf(vp));
            snap.videoDuration = ok ? (rec.duration || 0) : 0;   // 时长：popup 展示用（2026-10-02）
            // 标题优先用标签页**实时**标题（面板打开这一刻它就是当前视频的），
            // 取不到再退回推送记账里的（那是交给桌面端解析时送的标题）。
            snap.videoTitle = tt || (ok ? (rec.title || '') : '');
            reply(vp, ok ? rec.at : 0);
          });
        } catch (e) { reply('', 0); }
        // 兜底：tabs.get 意外不回调时别让面板干等
        setTimeout(function () { reply('', 0); }, 800);
      })();
      return true;   // 异步
    case 'mediaName': {
      // 页面侧原名采集（v1.0.57）：sender.tab 就是用户点下载的那一页。
      // 内容脚本发消息必然唤醒被挂起的 SW，所以这条通道在 SPA 站点也活着。
      var mnTab = sender && sender.tab ? sender.tab : null;
      var mnTid = (mnTab && typeof mnTab.id === 'number') ? mnTab.id : -1;
      noteMediaName(String(msg.name || '').slice(0, 160), String(msg.href || ''), mnTid);
      sendResponse({ ok: true });
      return false;
    }
    case 'setEnabled':
      enabled = !!msg.value;
      chrome.storage.local.set({ enabled: enabled });
      if (!enabled) {
        store.clearAll();
        tabPage = {};
        persist();
      }
      updateBadge();   // 关掉后角标要立刻清空/变成「已暂停」说明（v1.0.49）
      sendResponse({ ok: true, enabled: enabled });
      return false;
    case 'setCarryCookie':
      carryCookie = !!msg.value;
      chrome.storage.local.set({ carryCookie: carryCookie });
      sendResponse({ ok: true, carryCookie: carryCookie });
      return false;
    case 'clear':
      // 「清空」= 清当前页（面板里能看到的就是这一页的）；
      // 已发送标记只跟着这一页的条目走，别把别页的标记一起抹了
      var ctid = scopeTabId();
      store.list(ctid).forEach(function (it) { delete sentUrls[it.url]; });
      store.clearTab(ctid);
      persist();
      updateBadge();
      sendResponse({ ok: true, tabId: ctid });
      return false;
    case 'pageSeen': {
      // 内容脚本哨兵（v1.0.40）：页面侧的「加载完成 / SPA 换页 / 播放器出现」。
      // SW 被 Chrome 挂起时页面照样能唤醒它（runtime 消息必然唤醒），这是 YouTube
      // 这类 SPA 站点唯一可靠的入口 —— 它们不发新的主文档请求。
      pageSeenCount++;
      var sTab = sender && sender.tab ? sender.tab : null;
      var sTid = (sTab && typeof sTab.id === 'number') ? sTab.id : -1;
      var ttl = msg.title || (sTab && sTab.title) || '';
      // why==='player' = 播放器已就绪，此刻 document.title 已是**本视频**的标题 →
      // 走标题修正通道。不能走 checkPagePush：同页 5 分钟冷却会把这条补报直接挡掉，
      // 标题就永远修不回来（v1.0.46 修「嗅探比视频慢一步」）。
      var hit = false;
      if (msg.why === 'player') {
        var vpSeen = CORE.isVideoPage(msg.url || '');
        if (vpSeen) { refreshPageTitle(sTid, vpSeen, ttl, msg.duration); hit = true; }
      } else {
        hit = checkPagePush(sTid, msg.url || '', ttl, msg.force === true, msg.duration);
      }
      sendResponse({ ok: hit });
      return false;
    }
    case 'pushPageNow': {
      // popup 打开即强制推一次（用户显式打开面板 = 明确意图 → 忽略 5 分钟冷却）
      var ptid = scopeTabId();
      var pvp = CORE.isVideoPage(msg.url || '');
      if (pvp) {
        pushPageToServer(ptid, pvp, msg.title || '', true);
        sendResponse({ ok: true, url: pvp });
      } else {
        checkPagePushFromTab(ptid, true);
        sendResponse({ ok: true, url: '' });
      }
      return false;
    }
    case 'markSent':
      sentUrls[msg.url] = Date.now();
      persist();
      sendResponse({ ok: true });
      return false;
    case 'getEndpoint':
      getEndpoint(function (base) { sendResponse({ endpoint: base }); });
      return true;   // 异步
    case 'detectEndpoint':
      endpoint = '';
      chrome.storage.local.remove('endpoint');
      probeEndpoint(function (base) {
        if (base) {
          endpoint = base;
          chrome.storage.local.set({ endpoint: base });
        }
        sendResponse({ endpoint: base });
      });
      return true;   // 异步
  }
  return false;
});
