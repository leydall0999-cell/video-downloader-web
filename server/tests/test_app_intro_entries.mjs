// 网页版「更多功能」页（#appIntroView）回归守卫（2026-09-30）
//
// 这一页是网页版唯一的「介绍桌面客户端能力」入口，历史问题是两份漂移：
//   ① 页面只列了 12 条，App 已有的种子下载 / 压缩 / 超分 / 二维码 / 网页生成 / 视频去水印 /
//      抠图 / 媒体库 / 保险箱 / 归档网盘 / 抽帧铃声 等一概没写，用户以为客户端只有下载；
//   ② 「网页可用 / 仅桌面端」标注与真实能力不符（甚至出现过 App 里并不存在的独立「配音工作室」）。
// 所以这里钉三件事：能力项必须齐全、每项必须标清在哪端可用、网页可用的必须真有入口。
//
// 2026-09-30 修订：删掉「账号」组（用户拍板「web 不要」）—— 这张卡讲的是网页自己的账号会员，
// 不属于「介绍 App 功能」，留着只会和页面主旨打架。卡片 25 → 24、分组 6 → 5、网页可用 9 → 8。
// 2026-10-12 修订：页面新增「证件照制作」卡（`data-cap="idphoto"`，标 `tag-desktop` 仅桌面端，
// 网页版确实没有该入口）而本守卫未同步 ⇒ 套件长期红灯（red 的是过期断言，不是页面）。
// 本次把卡片 24 → 25、仅桌面端 16 → 17；网页可用仍 8。
//
// 说明：不做 DOM 运行时（该区块依赖整页初始化），用「源码切片 + 结构断言」，锚点被挪走立即红。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const indexPath = join(repoRoot, 'web', 'index.html');
const indexHtml = readFileSync(indexPath, 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

// ---- 切出 #appIntroView 整段 ----
const start = indexHtml.indexOf('id="appIntroView"');
assert.ok(start > 0, '#appIntroView 必须存在（网页版「更多功能」页）');
const end = indexHtml.indexOf('<!-- 媒体库视图', start);
assert.ok(end > start, '#appIntroView 之后应紧跟媒体库视图注释（切片右边界）');
const intro = indexHtml.slice(start, end);

// ---- 期望：24 项能力，8 项网页可用 + 16 项仅桌面端 ----
// 网页可用 → 必须能在网页版找到入口锚点（见下方 WEB_ENTRY 表）
const WEB_CAPS = {
  download: 'tabDownload',
  subtitle: 'tabSubtitle',
  uploadconvert: 'tabUploadConvert',
  concat: 'ucSubMerge',
  musicconvert: 'tabMusicConvert',
  imageconvert: 'tabImageConvert',
  dw: 'tabDw',
  dwpdf: 'dwPdfBtn',
};
// 仅桌面端 → 网页版不得声称可用
const DESK_CAPS = [
  'subscribe', 'torrent', 'extension', 'commentary', 'compress', 'sr', 'share', 'page',
  'bridge', 'dwvideo', 'matting', 'idphoto', 'ffmpeg_tools', 'library', 'retention', 'crypto',
  // 2026-10-25：archive（一键归档网盘）卡片已随「网盘只做下载」的定位下线并删除，不再计入
];

// 卡片格式：<div class="app-intro-card[ app-intro-card--desk]" data-cap="xxx"> … <span class="app-intro-tag tag-web|tag-desktop">
// 2026-11-05 修订：`f554574` 重设计后，仅桌面端的卡片多了一个修饰类
// `app-intro-card--desk`（网页可用的仍是精确 `app-intro-card`）。旧正则只认精确类名，
// 于是只匹配到 8 张网页卡就断言失败 —— 红的是过期断言，不是页面回退（页面仍有 24 张）。
// 这里放宽到「主类 + 可选修饰类」，能力清单与端标注的实质校验保持不变。
const cardRe = /<div class="app-intro-card(?: [^"]*)?" data-cap="([^"]+)">([\s\S]*?)\n {10}<\/div>/g;
const cards = [];
let m;
while ((m = cardRe.exec(intro)) !== null) cards.push({ cap: m[1], body: m[2] });

assert.equal(cards.length, 24, `「更多功能」页应有 24 张能力卡片，实际 ${cards.length}`);

const caps = cards.map((c) => c.cap);
assert.equal(new Set(caps).size, caps.length, 'data-cap 不得重复：' + caps.filter((c, i) => caps.indexOf(c) !== i).join(','));

const expectedAll = [...Object.keys(WEB_CAPS), ...DESK_CAPS].sort();
assert.deepEqual([...caps].sort(), expectedAll,
  '能力项与期望清单不一致（缺：' + expectedAll.filter((c) => !caps.includes(c)).join(',') +
  '；多：' + caps.filter((c) => !expectedAll.includes(c)).join(',') + '）');

// 每张卡片恰有一个可用性标签，且与期望清单一致
for (const { cap, body } of cards) {
  const isWeb = /<span class="app-intro-tag tag-web">网页可用<\/span>/.test(body);
  const isDesk = /<span class="app-intro-tag tag-desktop">仅桌面端<\/span>/.test(body);
  assert.equal(isWeb + isDesk, 1, `「${cap}」卡片必须且只能有一个可用性标签（网页可用 xor 仅桌面端）`);
  if (cap in WEB_CAPS) assert.ok(isWeb, `「${cap}」网页版已支持，必须标「网页可用」`);
  else assert.ok(isDesk, `「${cap}」网页版没有，必须标「仅桌面端」`);
  assert.ok(/<p>[^<]{10,}<\/p>/.test(body), `「${cap}」卡片应有实质说明文案`);
}

// 「网页可用」必须真有入口 —— 否则等于虚假宣传
for (const [cap, anchor] of Object.entries(WEB_CAPS)) {
  assert.ok(indexHtml.includes(`id="${anchor}"`),
    `「${cap}」标了网页可用，但网页版找不到入口锚点 id="${anchor}"`);
}

// ---- 分组：与 App 侧栏同款 5 组 ----
const groupTitles = [...intro.matchAll(/<h3 class="app-intro-group-title"><span aria-hidden="true">[^<]*<\/span>([^<]+)<\/h3>/g)]
  .map((x) => x[1]);
assert.deepEqual(groupTitles, ['下载', '创作', '转换', '工具', '媒体库'],
  '分组标题应为 下载/创作/转换/工具/媒体库，实际：' + groupTitles.join('/'));

// ---- 已删的「账号」组不得复活（2026-09-30 用户拍板 web 端不要）----
assert.ok(!intro.includes('data-cap="profile"'),
  '「账号与会员」卡已删除，不该再出现（它的卖点属于网页自身，不是对 App 能力的介绍）');
assert.ok(!/app-intro-group-title[^>]*>[^<]*<\/span>账号</.test(intro),
  '「账号」分组已删除，不该再出现');

// ---- 静态占位数字必须与卡片实算一致（首屏不闪错数字；app.js 只是兜底纠正）----
// 2026-10-25：web-dev 的「更多功能」页已不再展示「网页可用 N / 仅桌面端 M」统计，
// 故改为**存在才强校验** —— app-dev 若仍保留统计，照旧逐字校验；两分支都不误报。
const webCount = Object.keys(WEB_CAPS).length;
const deskCount = DESK_CAPS.length;
const hasStats = /id="appIntroWebCount"/.test(intro);
if (hasStats) {
  // 2026-11-05 修订：`f554574` 重设计把统计数字的容器从 <b> 换成
  // <span class="app-intro-stat-num">。这里只钉「数字本身必须等于实算值」，
  // 不再钉标签名 —— 否则改个语义标签就误报（数字错位才是真问题）。
  assert.ok(new RegExp(`id="appIntroWebCount">${webCount}<`).test(intro),
    `index.html 静态占位应为「网页可用 ${webCount}」，否则首屏会闪错数字`);
  assert.ok(new RegExp(`id="appIntroDesktopCount">${deskCount}<`).test(intro),
    `index.html 静态占位应为「仅桌面端 ${deskCount}」`);
}

// ---- app.js 计数接线：卡片增删后数字自动跟上（仅页面保留统计元素时才需要）----
if (hasStats) {
  const statsStart = appJs.indexOf("// ---- 「更多功能」页能力计数");
  assert.ok(statsStart > 0, 'app.js 缺少「更多功能」页能力计数接线');
  const stats = appJs.slice(statsStart, statsStart + 900);
  assert.ok(/querySelectorAll\('#appIntroGroups \.app-intro-card'\)/.test(stats), '计数必须遍历真实的卡片容器');
  assert.ok(/card\.querySelector\('\.tag-web'\)/.test(stats) && /card\.querySelector\('\.tag-desktop'\)/.test(stats),
    '计数必须按可用性标签区分两类');
  assert.ok(/getElementById\('appIntroWebCount'\)/.test(stats) && /getElementById\('appIntroDesktopCount'\)/.test(stats),
    '计数结果必须写回两个统计元素');
}

// ---- 样式：分组视觉 ----
// 2026-11-05 修订：`f554574` 重设计后，分组标题的图标由 `::before` 伪元素
// 改为标题内的真实 <span aria-hidden="true">⬇️</span>（胶囊底色也换了）。
// 只要求「分组容器 / 标题 / 网格 / CTA」这些结构类存在；图标改用等价判定，
// 避免把「换了图标实现方式」误判成「视觉退化」。
for (const sel of ['.app-intro-groups {', '.app-intro-group-title {',
  '.app-intro-group .app-intro-grid {', '.app-intro-cta-actions {']) {
  assert.ok(stylesCss.includes(sel), `styles.css 缺少 ${sel}`);
}
assert.ok(stylesCss.includes('.app-intro-group-title::before {')
  || /<h3 class="app-intro-group-title"><span aria-hidden="true">/.test(intro),
  '分组标题必须有图标（::before 伪元素或标题内 span 二者其一）');
// 已废弃样式不得复活（页面已无对应元素）
for (const dead of ['.app-intro-soon {', '.app-intro-soon-plain {', '.app-intro-cta-card {']) {
  assert.ok(!stylesCss.includes(dead), `${dead} 是已删元素的死样式，不该再出现`);
}

// ---- CTA：必须有真实的客户端下载入口 ----
assert.ok(/<a class="btn btn-primary" href="\/download\/">查看客户端下载方式<\/a>/.test(intro),
  '页面底部必须有指向 /download/ 的客户端下载入口');

// ---- 页脚空指纹不得留下孤立圆点 ----
// 网页版没有任何代码给 #buildTag 填内容（桌面端是构建脚本写指纹），而 .build-tag::before
// 的「●」是无条件渲染的 ⇒ 线上页脚常年挂着一个孤立蓝点（用户 2026-09-30 反馈）。
assert.ok(/<span id="buildTag" class="build-tag"><\/span>/.test(indexHtml),
  '页脚构建指纹标签应保持空标签（网页版不填内容）');
assert.ok(/\.build-tag:empty \{ display: none; \}/.test(stylesCss),
  '空 .build-tag 必须隐藏，否则会在页脚留下一个孤立圆点');

console.log(`✅ 「更多功能」页回归守卫通过：${cards.length} 项能力 / 5 组 / 标签齐备 / 网页可用项入口存在 / 计数与样式完好`);
