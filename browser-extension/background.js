const APP_ORIGINS = new Set(['http://localhost:3166', 'http://127.0.0.1:3166']);

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  let origin;
  try { origin = new URL(sender.url).origin; } catch { return; }
  if (!APP_ORIGINS.has(origin) || sender.frameId !== 0 || !sender.tab || message?.type !== 'VI_YOUTUBE_SESSION_READ') return;
  (async () => {
    // Only YouTube cookies from this browser profile. Never read Google or other sites.
    const cookies = await chrome.cookies.getAll({domain: 'youtube.com'});
    const current = cookies.filter(c => !c.partitionKey &&
      (c.domain.replace(/^\./, '') === 'youtube.com' || c.domain.endsWith('.youtube.com')) &&
      (!c.expirationDate || c.expirationDate > Date.now() / 1000) &&
      ![c.domain,c.path,c.name,c.value].some(value => /[\t\r\n\0]/.test(value)));
    if (!current.some(c => /^(SID|SAPISID|APISID|LOGIN_INFO|__Secure-(1P|3P)(SID|APISID))$/.test(c.name))) {
      throw new Error('Sign in to YouTube in this browser profile, then import the link again.');
    }
    const rows = current.map(c => [
      (c.httpOnly ? '#HttpOnly_' : '') + c.domain,
      c.domain.startsWith('.') ? 'TRUE' : 'FALSE', c.path, c.secure ? 'TRUE' : 'FALSE',
      String(Math.floor(c.expirationDate || 0)), c.name, c.value
    ].join('\t'));
    reply({ok: true, text: '# Netscape HTTP Cookie File\n' + rows.join('\n') + '\n'});
  })().catch(() => reply({ok: false, error: 'YouTube session unavailable. Sign in to YouTube in this browser profile and try again.'}));
  return true;
});
