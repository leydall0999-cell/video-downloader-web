/* 视频工坊 —— 登录态（Cookie）采集与推送，供 popup 使用（2026-10-18）。
 *
 * 为什么需要它
 * ------------
 * 1) 桌面 App 自己会解密本机浏览器 Cookie 库（downloader.get_browser_cookie_header），
 *    但那条路有两个天花板：① 只认 Chrome / Edge / Brave / Chromium 的默认 profile；
 *    ② macOS 上其它 profile 或系统钥匙串授权的差异会让解密静默失败。
 * 2) 网页版（hanyuxz.top）跑在浏览器沙箱里，受同源策略限制**读不到**别站的 Cookie，
 *    只能用户手动粘贴，或者吃云端公共池里别人贡献的那份。
 * 于是「由扩展替用户把登录态取出来」是唯一既准确又省事的通道：扩展有 host_permissions
 * （manifest 里已经是 http/https 全站），再申请 "cookies" 权限就能按站全量读取。
 *
 * 与 background.js 里「截流请求头 Cookie」的区别（别混淆）
 * ------------------------------------------------------
 * background.js 用 webRequest + extraHeaders 抓的是**某一条请求**携带的 Cookie 头，
 * 只有页面正在播放/加载媒体时才存在；本文件是**按站点主动拉全量**，不依赖页面活动。
 * 前者继续保留（直链下载要用它那条请求的原样 Cookie），两者互补。
 *
 * 安全边界（改动前请先读）
 * ------------------------
 * - 本文件只负责「取 + 交给用户点的那条通道」，绝不后台自动外发：
 *   推本机走 /api/cookie/browser-push，推云端走 /api/cookie/contribute，
 *   都必须由用户在 popup 里点按钮触发；云端贡献还要求显式勾选同意（共享语义）。
 * - 云端接口有白名单（server/cookie_pool.py::is_allowed）：平台表里 cloud=false 的站
 *   只允许推本机，UI 据此禁用/跳过，避免每次点都吃一个 400。
 * - 采集结果不落 storage（Cookie 是凭据，留在内存里用完即弃）；只有用户点推送才出内存。
 *
 * 可测试性：本文件不直接摸 chrome.*，由调用方把 chrome 命名空间传进来（见 harvest），
 * 因此能在 node 里用桩跑（extension/tests/test_cookie_harvest.js）。
 */
'use strict';

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  try { root.VDLCookie = api; } catch (e) { /* 非浏览器环境 */ }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {

  /* 平台表
   * - domain：chrome.cookies.getAll 的检索域（会连带子域，如 www. / api. / passport.）
   * - keys：登录态判据。**只放「登录后才存在」的字段**，别放 cna / _ga 这类游客也有的，
   *   否则会把「没登录」误报成「已登录」，用户点同步后验真被拒，体验更糟。
   * - cloud：该站是否在公共池白名单内（cookie_pool._BASE_DOMAINS + _COOKIE_HARDENED_DOMAINS
   *   + _POOL_EXTRA_DOMAINS）。weibo / iqiyi 不在白名单 ⇒ 只给本机用。
   */
  var PLATFORMS = [
    { id: 'bilibili', name: '哔哩哔哩', domain: 'bilibili.com',
      url: 'https://www.bilibili.com/', keys: ['SESSDATA', 'DedeUserID'], cloud: true },
    { id: 'youtube', name: 'YouTube', domain: 'youtube.com',
      url: 'https://www.youtube.com/', keys: ['SAPISID', '__Secure-3PSID', '__Secure-1PSID', 'SID'], cloud: true },
    { id: 'douyin', name: '抖音', domain: 'douyin.com',
      url: 'https://www.douyin.com/', keys: ['sessionid', 'sid_tt', 'passport_csrf_token'], cloud: true },
    { id: 'kuaishou', name: '快手', domain: 'kuaishou.com',
      url: 'https://www.kuaishou.com/', keys: ['passToken', 'userId'], cloud: true },
    { id: 'xiaohongshu', name: '小红书', domain: 'xiaohongshu.com',
      url: 'https://www.xiaohongshu.com/', keys: ['web_session'], cloud: true },
    { id: 'youku', name: '优酷', domain: 'youku.com',
      url: 'https://www.youku.com/', keys: ['P_pck_rm', '__ysuid'], cloud: true },
    { id: 'vqq', name: '腾讯视频', domain: 'v.qq.com',
      url: 'https://v.qq.com/', keys: ['vqq_vusession', 'vqq_access_token', 'uin'], cloud: true },
    { id: 'weixin', name: '微信视频号', domain: 'weixin.qq.com',
      url: 'https://channels.weixin.qq.com/', keys: ['wap_sid2', 'wxuin'], cloud: true },
    { id: 'tiktok', name: 'TikTok', domain: 'tiktok.com',
      url: 'https://www.tiktok.com/', keys: ['sessionid', 'sid_tt'], cloud: true },
    { id: 'instagram', name: 'Instagram', domain: 'instagram.com',
      url: 'https://www.instagram.com/', keys: ['sessionid', 'ds_user_id'], cloud: true },
    { id: 'weibo', name: '微博', domain: 'weibo.com',
      url: 'https://weibo.com/', keys: ['SUB'], cloud: false },
    { id: 'iqiyi', name: '爱奇艺', domain: 'iqiyi.com',
      url: 'https://www.iqiyi.com/', keys: ['P00001'], cloud: false },
    // 百度网盘（2026-10-23 接入）：domain 用根域 baidu.com，BDUSS 挂在根域下，
    // chrome.cookies.getAll 会连带 pan.baidu.com 等子域。
    // **cloud 必须为 false**：BDUSS 等于完整网盘权限（比视频站 cookie 敏感得多），
    // 只推本机、绝不进云端公共池。
    { id: 'baidupan', name: '百度网盘', domain: 'baidu.com',
      url: 'https://pan.baidu.com/', keys: ['BDUSS'], cloud: false }
  ];

  var CLOUD_BASE = 'https://hanyuxz.top';

  /** 登录判据的最小值长度：4 太松（"1" / "null" 都过），但不同站值长差异大，取 6。 */
  var MIN_KEY_LEN = 6;

  /* 单个平台的读取超时。真机实测（2026-10-18）：chrome.cookies 的回调在某些机器状态下
   * 永不回来（Chrome 的 network service 崩溃、或系统钥匙串拒绝授权时）——没有超时兜底，
   * 面板就会永远停在「扫描中…」。 */
  var DEFAULT_READ_TIMEOUT_MS = 5000;

  /* 自动共享的存储键与节奏（与桌面 App 的 30 分钟一致）。
   * AUTO_KEY 缺省视为开启，只有显式 false 才停 —— 见 autoPush 的注释。 */
  var AUTO_KEY = 'vdlCookieShare';
  var AUTO_TS_KEY = 'vdlCookieLastPush';
  var AUTO_MIN_GAP_MS = 30 * 60 * 1000;

  /* 同名 Cookie 去重：浏览器可能同时存在 path=/ 与 path=/xxx、domain 与 .domain 两条，
   * 直接全拼进请求头会出现「同名不同值」，服务端取哪条不确定。评分规则：
   * path 越短越优先（'/' 通吃），domain 越长越具体越优先。 */
  function _score(c) {
    var path = c && c.path ? String(c.path) : '/';
    var dom = c && c.domain ? String(c.domain) : '';
    return (path === '/' ? 100 : 100 - path.length) + Math.min(dom.length, 40) / 100;
  }

  /**
   * 把 chrome.cookies.getAll 的结果拼成 `name=value; name=value` 请求头。
   * @param {Array} cookies  chrome.cookies.Cookie[]
   * @param {number} [nowMs] 当前时间（毫秒），默认 Date.now()，测试可注入
   * @returns {{header:string, count:number, names:string[]}}
   */
  function buildCookieHeader(cookies, nowMs) {
    var now = typeof nowMs === 'number' ? nowMs : Date.now();
    var best = Object.create(null);
    var order = [];
    (cookies || []).forEach(function (c) {
      if (!c || !c.name || typeof c.value !== 'string' || c.value === '') return;
      // 已过期：Chrome 一般不会返回，但 storage 里的快照可能带旧值，稳妥过滤
      if (c.expirationDate && c.expirationDate * 1000 <= now) return;
      var cur = best[c.name];
      if (!cur) { best[c.name] = c; order.push(c.name); return; }
      if (_score(c) > _score(cur)) best[c.name] = c;
    });
    var pairs = [];
    var names = [];
    for (var i = 0; i < order.length; i++) {
      var n = order[i];
      pairs.push(n + '=' + best[n].value);
      names.push(n);
    }
    return { header: pairs.join('; '), count: pairs.length, names: names };
  }

  /**
   * 该 Cookie 集合是否代表「已登录」。
   * 只认 PLATFORMS[].keys 里出现且长度足够的字段 —— 游客态也会有 cna / _ga / buvid3，
   * 拿它们当判据会让用户以为已登录、点了却被服务端验真拒掉。
   */
  function isLoggedIn(platform, cookies) {
    if (!platform || !platform.keys) return false;
    var map = Object.create(null);
    (cookies || []).forEach(function (c) {
      if (c && c.name) map[c.name] = c.value || '';
    });
    for (var i = 0; i < platform.keys.length; i++) {
      var v = map[platform.keys[i]];
      if (typeof v === 'string' && v.length >= MIN_KEY_LEN) return true;
    }
    return false;
  }

  /**
   * 逐个平台读取登录态。串行执行：并行 getAll 在旧版 Chrome 上会让 popup 卡顿，
   * 12 个站的量级串行也就几十毫秒。
   * @param {object} chromeApi   真实的 chrome（或测试桩）
   * @param {Function} cb        cb(results) —— results[i] = {id,name,domain,cloud,header,loggedIn,count,names,error}
   * @param {Array} [platforms]  默认 PLATFORMS，测试可裁剪
   */
  function harvest(chromeApi, cb, platforms, opts) {
    var list = platforms || PLATFORMS;
    var timeoutMs = (opts && opts.timeoutMs) || DEFAULT_READ_TIMEOUT_MS;
    var out = new Array(list.length);
    var left = list.length;
    var finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      if (typeof cb === 'function') cb(out);
    }

    var hasApi = !!(chromeApi && chromeApi.cookies && typeof chromeApi.cookies.getAll === 'function');
    if (!hasApi) {
      // 没权限 / 浏览器不支持：显式报错，别让 UI 显示「全部未登录」误导用户
      for (var k = 0; k < list.length; k++) out[k] = _empty(list[k], 'no-permission');
      finish();
      return;
    }
    if (!list.length) { finish(); return; }

    // 并发发起：每项独立计时，一个站挂住不影响其它站出结果。
    list.forEach(function (p, i) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        out[i] = _empty(p, 'timeout');
        if (--left === 0) finish();
      }, timeoutMs);

      function done(cks, err) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (err) {
          out[i] = _empty(p, err);
        } else {
          var built = buildCookieHeader(cks);
          out[i] = {
            id: p.id, name: p.name, domain: p.domain, cloud: !!p.cloud,
            header: built.header, loggedIn: isLoggedIn(p, cks) && !!built.header,
            count: built.count, names: built.names, error: ''
          };
        }
        if (--left === 0) finish();
      }

      try {
        chromeApi.cookies.getAll({ domain: p.domain }, function (cks) {
          var err = '';
          try {
            if (chromeApi.runtime && chromeApi.runtime.lastError) {
              err = String((chromeApi.runtime.lastError || {}).message || 'read-error');
            }
          } catch (e) { /* 桩里没有 runtime，忽略 */ }
          done(cks || [], err);
        });
      } catch (e) {
        done([], 'read-error');
      }
    });
  }

  function _empty(p, err) {
    return {
      id: p.id, name: p.name, domain: p.domain, cloud: !!p.cloud,
      header: '', loggedIn: false, count: 0, names: [], error: err || ''
    };
  }

  /** 只挑出真正可推送的（已登录且拿到 header）。 */
  function pickReady(results, opts) {
    var onlyCloud = !!(opts && opts.cloud);
    return (results || []).filter(function (r) {
      if (!r || !r.loggedIn || !r.header) return false;
      if (onlyCloud && !r.cloud) return false;
      return true;
    });
  }

  /**
   * 推给本机桌面 App → 写进 ~/.videodownloader/cookies/，之后解析/下载自动带上。
   * 后端只接受本机直连 + 扩展来源（见 app.py::cookie_browser_push）。
   */
  function pushLocal(endpoint, platform, header, cb) {
    var base = String(endpoint || '').replace(/\/+$/, '');
    if (!base) { if (cb) cb({ ok: false, error: 'no-endpoint' }); return; }
    _postJSON(base + '/api/cookie/browser-push', {
      domain: platform.domain, cookie: header, platform: platform.id
    }).then(function (r) { if (cb) cb(r); }, function (e) {
      if (cb) cb({ ok: false, error: String((e && e.message) || e || 'network') });
    });
  }

  /**
   * 贡献到云端公共池（网页版靠它拿到登录态）。
   * ⚠️ 语义是**共享**：进池后其他用户解析同站也可能用到这份登录态。
   *    产品策略＝默认共享（与桌面 App 每 30 分钟自动推一致）：
   *    popup 的勾选框默认选中，取消才停；自动调度走下面的 autoPush。
   */
  function pushCloud(platform, header, cb, base) {
    var root = String(base || CLOUD_BASE).replace(/\/+$/, '');
    _postJSON(root + '/api/cookie/contribute', {
      url: platform.url || ('https://' + platform.domain + '/'), cookie: header
    }).then(function (r) { if (cb) cb(r); }, function (e) {
      if (cb) cb({ ok: false, error: String((e && e.message) || e || 'network') });
    });
  }

  /**
   * 默认自动共享（产品策略，2026-10-18 定）：按 30 分钟节流，把「已登录 + 在云端
   * 白名单内」的站推到公共池。调用方三处共用同一节流戳，不会重复推：
   *   · background 的 vdlCookieSync alarm（每 30 分钟，浏览器挂着就一直在跑）
   *   · background 的 onStartup / onInstalled（装完/开机立刻来一次）
   *   · popup 展开时补推（保证用户点开面板那一刻看到的池子是新数据）
   *
   * 开关存 storage.local[AUTO_KEY]：**只有显式 false 才停**（undefined 视为开启），
   * 这样老用户升级上来不会因为读不到新键就静默停止共享。
   *
   * cb({ok, pushed, failed, total, skipped})
   *   skipped: 'off' 用户关了 / 'throttled' 30 分钟内推过 / 'empty' 没有可推的站
   */
  function autoPush(chromeApi, opts, cb) {
    opts = opts || {};
    var st = (chromeApi && chromeApi.storage && chromeApi.storage.local) || null;
    if (!st || typeof st.get !== 'function') {
      if (cb) cb({ ok: false, skipped: 'no-storage', pushed: 0 });
      return;
    }
    st.get([AUTO_KEY, AUTO_TS_KEY], function (d) {
      d = d || {};
      if (d[AUTO_KEY] === false) {
        if (cb) cb({ ok: true, skipped: 'off', pushed: 0 });
        return;
      }
      var last = parseInt(d[AUTO_TS_KEY] || 0, 10) || 0;
      if (!opts.force && Date.now() - last < AUTO_MIN_GAP_MS) {
        if (cb) cb({ ok: true, skipped: 'throttled', pushed: 0 });
        return;
      }
      harvest(chromeApi, function (rows) {
        var targets = pickReady(rows, { cloud: true });
        if (!targets.length) {
          // 没有任何「已登录且在云端白名单」的站：不写节流戳。
          // 写了会让用户刚登录完的 30 分钟内推不出去。
          if (cb) cb({ ok: true, skipped: 'empty', pushed: 0, total: 0 });
          return;
        }
        var okN = 0;
        var fail = [];
        var i = 0;
        function step() {
          if (i >= targets.length) {
            var patch = {};
            patch[AUTO_TS_KEY] = Date.now();
            try { st.set(patch); } catch (e) { /* 存不了就退化成每次都试 */ }
            if (cb) cb({ ok: okN > 0, pushed: okN, failed: fail.length, total: targets.length, errors: fail });
            return;
          }
          var p = targets[i++];
          pushCloud(p, p.header, function (r) {
            if (r && r.ok) okN++;
            else fail.push(p.id + ':' + ((r && r.error) || 'fail'));
            step();
          });
        }
        step();
      });
    });
  }

  function _postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (resp) {
      return resp.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
        if (resp.ok) return { ok: true, added: !!(data && data.added), verified: !!(data && data.verified) };
        var detail = (data && (data.detail || data.error)) || ('HTTP ' + resp.status);
        return { ok: false, status: resp.status, error: String(detail).slice(0, 160) };
      });
    });
  }

  return {
    PLATFORMS: PLATFORMS,
    CLOUD_BASE: CLOUD_BASE,
    MIN_KEY_LEN: MIN_KEY_LEN,
    DEFAULT_READ_TIMEOUT_MS: DEFAULT_READ_TIMEOUT_MS,
    buildCookieHeader: buildCookieHeader,
    isLoggedIn: isLoggedIn,
    harvest: harvest,
    pickReady: pickReady,
    pushLocal: pushLocal,
    pushCloud: pushCloud,
    autoPush: autoPush,
    AUTO_KEY: AUTO_KEY,
    AUTO_TS_KEY: AUTO_TS_KEY,
    AUTO_MIN_GAP_MS: AUTO_MIN_GAP_MS
  };
});
