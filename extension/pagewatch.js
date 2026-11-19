/* 页面侧哨兵（v1.0.40）—— 为什么必须加它：
 *
 * MV3 的后台 service worker 空闲 ~30s 就被 Chrome 挂起，而用户常常在装/更新扩展前
 * 就把 YouTube 页面开着了；YouTube 从首页点进某个视频又是 SPA 换页（不发主文档请求）。
 * 于是后台那条「视频页直推」一次都不触发 —— 2026-10-01 用户实测：弹窗显示
 * 「当前页没有嗅探到媒体流」、桌面端条目里零 YouTube 记录（腾讯视频等整页导航的站正常）。
 *
 * 内容脚本活在页面里，不受 SW 挂起影响，负责在三个时机通知后台：
 *   ① 加载完成；② 地址变化（SPA，低频轮询兜底）；③ 播放器就绪（video 出现且有时长）。
 * 判定规则只有一份：这里只做「是不是视频页」的**初筛**，用的是同一个 sniff-core.js
 * （manifest 的 content_scripts 里先加载它），后台再做一次权威判定与去重推送。
 *
 * 只对视频页发消息：非视频页 isVideoPage 返回 ''，一个字节都不发。
 */
(function () {
  'use strict';

  var CORE = (typeof self !== 'undefined' && self.VDLSniffCore) || null;
  if (!CORE || typeof CORE.isVideoPage !== 'function') return;

  var lastKey = '';      // 已上报过的归一化视频页 URL
  var lastWhy = '';      // 上一次上报的原因（'player' 用于「同一页也要补报一次播放器就绪」）
  var ticks = 0;
  var timer = null;

  /** 当前页 <video> 的时长（秒；拿不到 → 0）。
   *  2026-10-02 用户「把嗅探到的视频信息也加上标题、时长、大小」——
   *  时长只有**页面侧**读得到（YouTube 等 SABR 站的后台 webRequest 抓不到媒体流，
   *  也没有 Content-Duration 头），所以必须由这里随哨兵消息带出去。 */
  function pageDuration() {
    try {
      var v = document.querySelector('video');
      var d = v && v.duration;
      return (typeof d === 'number' && isFinite(d) && d > 0) ? d : 0;
    } catch (e) { return 0; }
  }

  function report(why) {
    var vp = '';
    try { vp = CORE.isVideoPage(location.href); } catch (e) { return; }
    if (!vp) return;
    // 同一页只报一次；只有「播放器就绪」允许在同一页补报一次
    if (vp === lastKey && (why !== 'player' || lastWhy === 'player')) return;
    lastKey = vp;
    lastWhy = why;
    try {
      chrome.runtime.sendMessage({
        type: 'pageSeen',
        url: vp,
        title: document.title || '',
        why: why,
        duration: pageDuration()
      }, function () { void chrome.runtime.lastError; });
    } catch (e) { /* 扩展被重载/停用：忽略 */ }
  }

  /** 播放器就绪 = 页面上已有 video 且拿到了时长（用户确实在播这一页）。 */
  function checkPlayer() {
    var v = null;
    try { v = document.querySelector('video'); } catch (e) { return; }
    if (!v) return;
    if (!(v.duration > 0) && !(v.readyState > 0)) return;
    report('player');
  }

  function tick() {
    report('poll');
    checkPlayer();
    ticks++;
    // 前 1 分钟 2s 一次（覆盖 SPA 换页与播放器起播），之后降到 10s 长期守候
    if (ticks === 30 && timer) {
      clearInterval(timer);
      timer = setInterval(tick, 10000);
    }
  }

  var start = function () {
    report('load');
    checkPlayer();
    timer = setInterval(tick, 2000);
  };

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    start();
  } else {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  }
})();

/* 媒体原名采集（v1.0.57，2026-11-13）—— 为什么必须单独做：
 *
 * 嗅探只拿得到媒体 URL，而很多站点的 URL 是纯数字 ID / 哈希
 * （实测 flac.music.hi.cn：2044590383.mp3、M5000039SjyS2Oznrv.mp3）。
 * 直链响应头的 Content-Disposition 同样是哈希名，靠标准字段拿原名是死路；
 * 真正的原名只存在于**页面 DOM**（下载弹窗里的「名称：友谊之光 - Maria Cordero」）。
 * 浏览器标签页标题又是 SPA 恒定站点名，也不是原名。
 *
 * 于是页面侧负责把「用户看到的那个名字」报给后台，由后台按 URL 精确关联到
 * 嗅探条目。三条采集路径，覆盖绝大多数站点：
 *   ① <a download="歌名.mp3">  —— HTML 标准下载文件名（最可靠）
 *   ② 点击下载链接时，从同一容器里读「名称：/文件名：/标题：xxx」
 *      —— 本站音频下载就是 <a href=直链 target="_blank">，没有 download 属性
 *   ③ MutationObserver 盯着动态弹出的下载弹窗 / 动态插入的 <a download>
 * 报给后台时**同时带上下载链接 href**，让它能精确关联（新标签页打开也认得）。
 */
(function () {
  'use strict';
  if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.sendMessage) return;

  var NOISE_WORDS = ['下载', '点击下载', '复制名称', '无损音乐下载', '播放', '试听',
    '保存', '文件', '名称', '未知', 'undefined', 'null', '确定', '取消', '关闭'];
  var LABEL_RE = /^(?:名称|文件名|歌曲名|歌曲|标题|片名|文件|资源名)\s*[:：]\s*(.+)$/;
  var EXT_RE = /\.(mp3|mp4|m4a|flac|wav|aac|ogg|mkv|webm|mov|m3u8|lrc|zip|avi|ts)$/i;

  function isNoise(s) {
    if (!s || s.length < 2 || s.length > 120) return true;
    if (/^\d+$/.test(s)) return true;              // 纯数字 ID：2044590383
    if (/^[0-9a-f]{16,}$/i.test(s)) return true;  // 纯哈希：M5000039... 这类
    if (/^(https?:\/\/|blob:|data:)/i.test(s)) return true;
    return NOISE_WORDS.indexOf(s) >= 0;
  }

  /** 清洗成「可直接当文件名」的原名：去标签前缀、去扩展名、压空白、剔噪。 */
  function cleanName(raw) {
    var s = String(raw || '').replace(/\s+/g, ' ').trim();
    var m = LABEL_RE.exec(s);
    if (m) s = m[1].trim();
    s = s.replace(EXT_RE, '').trim();
    return isNoise(s) ? '' : s;
  }

  /** 在容器里找「名称：xxx」这一行（本站弹窗是 <span class="truncate">名称：xxx</span>）。 */
  function labelName(root) {
    if (!root || !root.querySelectorAll) return '';
    var nodes = root.querySelectorAll('span,div,p,td,li,h1,h2,h3,label,strong');
    for (var i = 0; i < nodes.length; i++) {
      var t = (nodes[i].textContent || '').replace(/\s+/g, ' ').trim();
      if (!t || t.length > 160) continue;
      var m = LABEL_RE.exec(t);
      if (m) {
        var c = cleanName(m[1]);
        if (c) return c;
      }
    }
    return '';
  }

  function anchorOf(node) {
    var n = node;
    for (var i = 0; n && i < 6; i++, n = n.parentNode) {
      if (n.nodeType === 1 && n.tagName === 'A') return n;
    }
    return null;
  }

  var lastSent = { name: '', href: '', at: 0 };
  function submit(name, href) {
    var now = Date.now();
    if (name === lastSent.name && href === lastSent.href && now - lastSent.at < 3000) return;
    lastSent = { name: name, href: href, at: now };
    try {
      chrome.runtime.sendMessage(
        { type: 'mediaName', name: name, href: href || '', ts: now },
        function () { void chrome.runtime.lastError; });
    } catch (e) { /* 扩展被重载/停用：忽略 */ }
  }

  /** 点击：优先 <a download>，其次同容器标签文本，最后属性兜底。 */
  function onClick(ev) {
    var a = anchorOf(ev.target);
    var name = '';
    var href = a ? (a.href || '') : '';
    if (a) {
      var dl = a.getAttribute('download');
      if (dl) name = cleanName(dl);
    }
    if (!name) {
      var n = a || ev.target;
      for (var i = 0; n && i < 6 && !name; i++, n = n.parentNode) {
        if (!n || n.nodeType !== 1 || n === document.body || n === document.documentElement) continue;
        name = labelName(n);
        if (name) break;
        if (n.getAttribute) {
          var at = n.getAttribute('aria-label') || n.getAttribute('data-title') ||
                   n.getAttribute('data-name') || n.getAttribute('title');
          var c = cleanName(at || '');
          if (c) { name = c; break; }
        }
      }
    }
    if (name) submit(name, href);
  }

  /** 弹窗先渲染、用户后点：MutationObserver 提前把名字记下（点击时也能再报一次，
   *  后台按 URL 关联，重复上报幂等）。只盯有下载语义的变化，避免全页高频噪声。 */
  function scanAdded(node) {
    if (!node || node.nodeType !== 1) return;
    var selfDl = node.tagName === 'A' ? node.getAttribute('download') : '';
    if (selfDl) {
      var c0 = cleanName(selfDl);
      if (c0) submit(c0, node.href || '');
    }
    if (node.querySelectorAll) {
      var as = node.querySelectorAll('a[download]');
      for (var i = 0; i < as.length; i++) {
        var c1 = cleanName(as[i].getAttribute('download'));
        if (c1) submit(c1, as[i].href || '');
      }
    }
    var txt = (node.textContent || '');
    if (txt.length < 2000 && /名称\s*[:：]|文件名\s*[:：]|歌曲名\s*[:：]/.test(txt)) {
      var c2 = labelName(node);
      if (c2) submit(c2, '');
    }
  }

  document.addEventListener('click', onClick, true);   // 捕获阶段：站点 preventDefault 也拦不住
  if (typeof MutationObserver !== 'undefined') {
    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var added = muts[i].addedNodes || [];
        for (var j = 0; j < added.length; j++) {
          try { scanAdded(added[j]); } catch (e) { /* 单个节点解析失败不影响其余 */ }
        }
      }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }
})();
