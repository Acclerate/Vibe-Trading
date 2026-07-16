#!/usr/bin/env node

import { readFileSync, writeFileSync } from 'node:fs';

// CDP: connect directly to Chrome's native remote debugging port (default 9222),
// NOT the web-access proxy (3456). This removes the extra proxy dependency —
// you only need Chrome launched with --remote-debugging-port=9222.
// Override via env CDP_PORT or --cdp-port.
const CDP_PORT = process.env.CDP_PORT || '9222';
const CDP_HTTP = `http://localhost:${CDP_PORT}`;
const XUEQIU_BASE = 'https://xueqiu.com';
const DEFAULT_LOOKBACK_HOURS = 24;
const DEFAULT_MAX_POSTS = 5;

// Fallback path: when Chrome CDP is unavailable, fetch via Xueqiu's status API
// using the XUEQIU_TOKEN env var. Less robust (WAF may block, token expires)
// but lets the digest run without a local Chrome.
const XUEQIU_TOKEN = process.env.XUEQIU_TOKEN || process.env.XUEQIUTOKEN || '';
const FETCH_MODE_CDP = 'cdp';
const FETCH_MODE_API = 'api';
let fetchMode = FETCH_MODE_CDP; // determined at startup

// --- CLI arg parsing ---
const args = process.argv.slice(2);
function getArg(name) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : null;
}

const configPath = getArg('config') || null;
const statePath = getArg('state') || null;
const lookbackHours = parseInt(getArg('lookback') || String(DEFAULT_LOOKBACK_HOURS), 10);
const maxPosts = parseInt(getArg('max') || String(DEFAULT_MAX_POSTS), 10);

// --- CDP helpers (direct Chrome DevTools Protocol over WebSocket) ---
// Each tab has its own webSocketDebuggerUrl; we open a WS, send commands,
// and await the matching response by id.

let _cdpMsgId = 0;

/** Open a CDP WebSocket session to a target and return a sender/await pair. */
async function cdpSession(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message || 'CDP error'));
      else resolve(msg.result);
    }
  });
  async function send(method, params = {}) {
    const id = ++_cdpMsgId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
  }
  return { send, close: () => ws.close() };
}

async function cdpNew(url) {
  // PUT /json/new?<url> creates a tab and returns its target descriptor.
  const res = await fetch(`${CDP_HTTP}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' });
  if (!res.ok) throw new Error(`CDP new tab failed: HTTP ${res.status}`);
  const tab = await res.json();
  return { targetId: tab.id, wsUrl: tab.webSocketDebuggerUrl };
}

async function cdpEval(target, js) {
  // target is { targetId, wsUrl } from cdpNew.
  const session = await cdpSession(target.wsUrl);
  try {
    // Runtime.evaluate with awaitPromise so async expressions resolve.
    const { result, exceptionDetails } = await session.send('Runtime.evaluate', {
      expression: js,
      awaitPromise: true,
      returnByValue: true,
    });
    if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || 'eval exception');
    return result.value;
  } finally {
    session.close();
  }
}

async function cdpScroll(target, y = 3000) {
  // Use Runtime.evaluate to scroll (avoids needing Page.enable).
  const session = await cdpSession(target.wsUrl);
  try {
    await session.send('Runtime.evaluate', {
      expression: `window.scrollBy(0, ${y})`,
    });
  } finally {
    session.close();
  }
}

async function cdpClose(targetId) {
  await fetch(`${CDP_HTTP}/json/close/${targetId}`);
}

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// --- Time parsing ---
function parseRelativeTime(timeStr) {
  // "5小时前", "昨天 14:31", "04-17 09:36", "03-15 10:02"
  const now = new Date();
  const s = timeStr.trim();

  const hoursMatch = s.match(/^(\d+)\s*小时前/);
  if (hoursMatch) {
    return new Date(now.getTime() - parseInt(hoursMatch[1], 10) * 3600_000);
  }

  const minsMatch = s.match(/^(\d+)\s*分钟前/);
  if (minsMatch) {
    return new Date(now.getTime() - parseInt(minsMatch[1], 10) * 60_000);
  }

  const yesterdayMatch = s.match(/^昨天\s+(\d{1,2}):(\d{2})/);
  if (yesterdayMatch) {
    const d = new Date(now);
    d.setDate(d.getDate() - 1);
    d.setHours(parseInt(yesterdayMatch[1], 10), parseInt(yesterdayMatch[2], 10), 0, 0);
    return d;
  }

  // MM-DD HH:mm format
  const dateMatch = s.match(/^(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})/);
  if (dateMatch) {
    return new Date(
      now.getFullYear(),
      parseInt(dateMatch[1], 10) - 1,
      parseInt(dateMatch[2], 10),
      parseInt(dateMatch[3], 10),
      parseInt(dateMatch[4], 10),
    );
  }

  // YYYY-MM-DD HH:mm format
  const fullDateMatch = s.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})/);
  if (fullDateMatch) {
    return new Date(
      parseInt(fullDateMatch[1], 10),
      parseInt(fullDateMatch[2], 10) - 1,
      parseInt(fullDateMatch[3], 10),
      parseInt(fullDateMatch[4], 10),
      parseInt(fullDateMatch[5], 10),
    );
  }

  return null;
}

// --- Parse interaction numbers ---
function parseInteractionNum(text) {
  if (!text) return 0;
  const num = text.match(/(\d+)/);
  return num ? parseInt(num[1], 10) : 0;
}

// --- CDP availability probe ---
async function probeCdp() {
  // Probe Chrome's native CDP endpoint at /json/version.
  try {
    const res = await fetch(`${CDP_HTTP}/json/version`, { signal: AbortSignal.timeout(1500) });
    if (!res.ok) return false;
    const info = await res.json();
    return Boolean(info && info.webSocketDebuggerUrl);
  } catch {
    return false;
  }
}

// --- API fallback: fetch a user's recent statuses via Xueqiu's status API ---
// Resolves slug (字母别名 or 数字ID) to a numeric user id, then calls
// /v4/statuses/user_timeline.json. Returns posts in the same shape as the
// CDP path so downstream code is unchanged.
async function fetchAuthorPostsApi(author, seenIds) {
  const posts = [];
  const errors = [];

  if (!XUEQIU_TOKEN) {
    errors.push(`${author.name}: API fallback unavailable — XUEQIU_TOKEN not set`);
    return { posts, errors };
  }

  const headers = {
    'Cookie': `xq_a_token=${XUEQIU_TOKEN}`,
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',
  };

  try {
    // Resolve slug → numeric uid (slug may already be numeric).
    let uid = author.slug;
    if (!/^\d+$/.test(uid)) {
      const profRes = await fetch(`${XUEQIU_BASE}/user/show.json?id=${author.slug}`, { headers, signal: AbortSignal.timeout(10000) });
      const profJson = await profRes.json();
      if (profJson.error_code) {
        errors.push(`${author.name}: token 无效或过期 (${profJson.error_description})`);
        return { posts, errors };
      }
      uid = String(profJson.data?.id || profJson.data?.user_id || author.slug);
    }

    const tlRes = await fetch(
      `${XUEQIU_BASE}/v4/statuses/user_timeline.json?user_id=${uid}&page=1&count=40`,
      { headers, signal: AbortSignal.timeout(10000) },
    );
    const tlJson = await tlRes.json();
    if (tlJson.error_code) {
      errors.push(`${author.name}: ${tlJson.error_description || 'timeline 请求失败'}`);
      return { posts, errors };
    }

    const rawPosts = tlJson.statuses || tlJson.list || [];
    const cutoff = new Date(Date.now() - lookbackHours * 3600_000);

    for (const p of rawPosts) {
      if (posts.length >= maxPosts) break;
      const id = String(p.id || p.id_str || '');
      if (!id || seenIds.has(id)) continue;
      const createdAt = p.created_at ? new Date(Number(p.created_at)) : null;
      if (!createdAt || createdAt < cutoff) continue;
      posts.push({
        id,
        url: p.id ? `${XUEQIU_BASE}/${uid}/${id}` : null,
        createdAt: createdAt.toISOString(),
        text: p.description || p.text || p.title || '',
        forwardText: p.retweeted_status?.description || p.retweeted_status?.text || '',
        forwardAuthor: p.retweeted_status?.user?.screen_name || '',
        forwardUrl: p.retweeted_status?.id ? `${XUEQIU_BASE}/${p.retweeted_status.user?.id || ''}/${p.retweeted_status.id}` : '',
        likes: p.like_count || p.likeCount || 0,
        comments: p.reply_count || p.replyCount || 0,
        reposts: p.retweet_count || p.retweetCount || 0,
      });
    }
  } catch (err) {
    errors.push(`${author.name}: ${err.message}`);
  }
  return { posts, errors };
}

// --- Fetch posts for one author (dispatches by mode) ---
async function fetchAuthorPosts(author, seenIds) {
  if (fetchMode === FETCH_MODE_API) {
    return fetchAuthorPostsApi(author, seenIds);
  }
  return fetchAuthorPostsCdp(author, seenIds);
}

// --- Fetch posts for one author via CDP ---
async function fetchAuthorPostsCdp(author, seenIds) {
  const url = `${XUEQIU_BASE}/${author.slug}`;
  let target; // { targetId, wsUrl } from cdpNew
  const posts = [];
  const errors = [];

  try {
    target = await cdpNew(url);
    await sleep(3000);

    // Verify page loaded correctly (not 404, not login wall)
    const title = await cdpEval(target, 'document.title');
    if (title.includes('404') || title.includes('登录')) {
      errors.push(`${author.name}: 页面加载异常 - ${title}`);
      return { posts, errors };
    }

    // Scroll to load more content
    await cdpScroll(target, 5000);
    await sleep(2000);

    // Extract posts via DOM
    const rawPosts = await cdpEval(target, `
      (function() {
        var items = document.querySelectorAll('article.timeline__item');
        var results = [];
        for (var i = 0; i < items.length; i++) {
          var item = items[i];
          var dateAnchor = item.querySelector('a.date-and-source');
          var forwardBlock = item.querySelector('blockquote.timeline__item__forward');

          // Post ID and URL
          var postId = dateAnchor ? dateAnchor.getAttribute('data-id') : null;
          var postUrl = dateAnchor ? dateAnchor.getAttribute('href') : null;
          var timeText = dateAnchor ? dateAnchor.firstChild.textContent.trim() : null;

          // Main content (author's own text)
          var contentEl = item.querySelector('div.timeline__item__content div.content--description > div:last-child');
          var mainText = contentEl ? contentEl.innerText.trim() : '';

          // Forward content (what was forwarded)
          var forwardText = '';
          var forwardAuthor = '';
          var forwardUrl = '';
          if (forwardBlock) {
            var fwdContent = forwardBlock.querySelector('div.timeline__item__forward__content');
            forwardText = fwdContent ? fwdContent.innerText.trim() : '';
            var fwdAuthorEl = forwardBlock.querySelector('span.user-name');
            forwardAuthor = fwdAuthorEl ? fwdAuthorEl.innerText.trim().replace(/^@/, '') : '';
            var fwdAnchor = forwardBlock.querySelector('a.fake-anchor');
            forwardUrl = fwdAnchor ? fwdAnchor.getAttribute('href') : '';
          }

          // Interaction numbers
          var ft = item.querySelector('div.timeline__item__ft');
          var likes = 0, comments = 0, reposts = 0, bookmarks = 0;
          if (ft) {
            var controls = ft.querySelectorAll('span');
            controls.forEach(function(span) {
              var t = span.textContent.trim();
              if (t.indexOf('转发') !== -1) reposts = parseInt(t.match(/\\d+/)) || 0;
              if (t.indexOf('收藏') !== -1) bookmarks = parseInt(t.match(/\\d+/)) || 0;
            });
            var replayEl = ft.querySelector('a.replay-count');
            if (replayEl) comments = parseInt(replayEl.textContent.match(/\\d+/)) || 0;
            // likes: the icon before "讨论" usually has the count
            var likeSpans = ft.querySelectorAll('span.like-count');
            // count spans in footer for like count
            var allSpans = ft.querySelectorAll('span');
            // likes are typically the 3rd number
            var nums = [];
            ft.querySelectorAll('span').forEach(function(s) {
              var n = parseInt(s.textContent.trim());
              if (!isNaN(n) && n > 0) nums.push({text: s.textContent.trim(), val: n});
            });
            // Usually order: likes, comments indicator, reposts, bookmarks
            if (nums.length >= 2) likes = nums[0].val;
          }

          results.push({
            id: postId,
            url: postUrl ? 'https://xueqiu.com' + postUrl : null,
            timeText: timeText,
            mainText: mainText,
            forwardText: forwardText,
            forwardAuthor: forwardAuthor,
            forwardUrl: forwardUrl ? 'https://xueqiu.com' + forwardUrl : '',
            likes: likes,
            comments: comments,
            reposts: reposts,
            bookmarks: bookmarks
          });
        }
        return results;
      })()
    `);

    if (!Array.isArray(rawPosts)) {
      errors.push(`${author.name}: DOM 提取返回非数组 - ${typeof rawPosts}`);
      return { posts, errors };
    }

    const cutoff = new Date(Date.now() - lookbackHours * 3600_000);

    for (const p of rawPosts) {
      if (posts.length >= maxPosts) break;
      if (!p.id || seenIds.has(p.id)) continue;

      const createdAt = parseRelativeTime(p.timeText);
      if (!createdAt || createdAt < cutoff) continue;

      posts.push({
        id: p.id,
        url: p.url,
        createdAt: createdAt.toISOString(),
        text: p.mainText,
        forwardText: p.forwardText,
        forwardAuthor: p.forwardAuthor,
        forwardUrl: p.forwardUrl,
        likes: p.likes,
        comments: p.comments,
        reposts: p.reposts,
      });
    }
  } catch (err) {
    errors.push(`${author.name}: ${err.message}`);
  } finally {
    if (target) {
      try { await cdpClose(target.targetId); } catch {}
    }
  }

  return { posts, errors };
}

// --- Main ---
async function main() {
  // Determine fetch mode: prefer CDP, fall back to API if CDP unavailable.
  const cdpOk = await probeCdp();
  if (cdpOk) {
    fetchMode = FETCH_MODE_CDP;
    process.stderr.write('Fetch mode: CDP (local Chrome)\n');
  } else {
    fetchMode = FETCH_MODE_API;
    if (XUEQIU_TOKEN) {
      process.stderr.write('Fetch mode: API fallback (CDP unavailable, using XUEQIU_TOKEN)\n');
    } else {
      process.stderr.write('WARNING: CDP unavailable AND XUEQIU_TOKEN not set — digest will be empty\n');
    }
  }

  // Load config
  let config;
  if (configPath) {
    config = JSON.parse(readFileSync(configPath, 'utf-8'));
  } else {
    const skillDir = new URL('..', import.meta.url).pathname;
    config = JSON.parse(readFileSync(`${skillDir}/config/default-sources.json`, 'utf-8'));
  }

  // Load state
  let state = { seen: {}, lastRun: null };
  if (statePath) {
    try {
      state = JSON.parse(readFileSync(statePath, 'utf-8'));
    } catch {}
  }
  const seenIds = new Set(Object.keys(state.seen || {}));

  const authors = config.sources || config.authors || [];
  const allErrors = [];
  const result = [];

  for (const author of authors) {
    process.stderr.write(`Fetching: ${author.name}...\n`);
    const { posts, errors } = await fetchAuthorPosts(author, seenIds);
    allErrors.push(...errors);

    if (posts.length > 0) {
      result.push({
        name: author.name,
        tag: author.tag || '',
        slug: author.slug,
        profileUrl: `${XUEQIU_BASE}/${author.slug}`,
        posts,
      });

      // Update seen state
      for (const p of posts) {
        state.seen[p.id] = new Date().toISOString();
      }
    }

    // Delay between authors to avoid anti-crawl
    if (authors.indexOf(author) < authors.length - 1) {
      await sleep(3000);
    }
  }

  // Prune old seen entries (keep 7 days)
  const pruneBefore = new Date(Date.now() - 7 * 24 * 3600_000).toISOString();
  for (const [id, ts] of Object.entries(state.seen)) {
    if (ts < pruneBefore) delete state.seen[id];
  }
  state.lastRun = new Date().toISOString();

  // Save state
  if (statePath) {
    writeFileSync(statePath, JSON.stringify(state, null, 2), 'utf-8');
  }

  const totalPosts = result.reduce((sum, a) => sum + a.posts.length, 0);

  const output = {
    status: totalPosts > 0 ? 'ok' : 'empty',
    generatedAt: new Date().toISOString(),
    authors: result,
    stats: {
      totalAuthors: authors.length,
      authorsWithPosts: result.length,
      totalPosts,
    },
    errors: allErrors,
  };

  process.stdout.write(JSON.stringify(output, null, 2));
}

main().catch(err => {
  process.stderr.write(`Fatal error: ${err.message}\n`);
  process.exit(1);
});
