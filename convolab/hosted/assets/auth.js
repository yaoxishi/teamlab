// TeamLab browser authentication component v1. The pinned adapter loads only
// the approved official Turnstile script when a new visitor explicitly connects.
// Only three authentication fields enter localStorage. Model keys never enter
// this component. The server verifies identity independently on every redraw.
const MESSAGES = Object.freeze({
  needs_session: "Connect a private workspace to continue.",
  ready: "Private workspace connected. Keep this browser's data to return.",
  storage_blocked: "Browser storage is blocked. Allow site storage or use another browser.",
  browser_unsupported: "This browser cannot safely coordinate tabs. Use a current Chrome, Edge, Firefox or Safari browser.",
  session_expired: "This browser workspace expired. Reconnect to create a new workspace; old results cannot be recovered here.",
  unavailable: "The workspace service is unavailable. Retry when your connection returns.",
  invalid_response: "The workspace service returned an invalid response. Retry later.",
  signup_unavailable: "New visitor workspaces will be enabled after hosting and bot protection are configured.",
  captcha_required: "Complete the website's bot check before connecting.",
  rate_limited: "The workspace service is busy. Wait before trying again.",
});

export function createAuthController(config, env) {
  if (!config || !/^https:\/\/[a-z0-9]{20}\.supabase\.co$/.test(config.url)
      || typeof config.publishable_key !== "string" || config.component_version !== 1) {
    throw new Error("Invalid public authentication configuration");
  }
  const project = new URL(config.url).hostname.split(".")[0];
  const storageKey = `convolab.auth.v1.${project}`;
  const lockName = `${storageKey}.lock`;
  let closed = false, lastSignature = null;
  const now = () => Math.floor(env.now() / 1000);

  function emit(status, session = null) {
    if (closed) return;
    const message = {status, access_token: session?.access_token ?? null};
    const signature = JSON.stringify(message);
    if (signature === lastSignature) return;
    lastSignature = signature;
    env.emit(message, MESSAGES[status] ?? MESSAGES.unavailable);
  }

  function checkStorage() {
    const probe = `${storageKey}.probe`;
    try {
      env.storage.setItem(probe, "1");
      if (env.storage.getItem(probe) !== "1") throw new Error();
      env.storage.removeItem(probe);
    } catch (_) { throw {status: "storage_blocked"}; }
  }

  function normalized(body) {
    const expiry = body?.expires_at ?? (now() + body?.expires_in);
    if (!body || typeof body.access_token !== "string" || !body.access_token.trim()
        || body.access_token.length > 16384 || /\s/.test(body.access_token)
        || typeof body.refresh_token !== "string" || !body.refresh_token.trim()
        || body.refresh_token.length > 16384 || /\s/.test(body.refresh_token)
        || !Number.isSafeInteger(expiry) || expiry <= 0) {
      throw {status: "invalid_response"};
    }
    return {access_token: body.access_token, refresh_token: body.refresh_token, expires_at: expiry};
  }

  function read() {
    let raw;
    try { raw = env.storage.getItem(storageKey); }
    catch (_) { throw {status: "storage_blocked"}; }
    if (raw === null) return null;
    try {
      const body = JSON.parse(raw);
      if (Object.keys(body).sort().join(",") !== "access_token,expires_at,refresh_token") throw new Error();
      return normalized(body);
    } catch (_) {
      // Never authorize from corrupt/stale storage or silently adopt a user ID.
      env.storage.removeItem(storageKey);
      throw {status: "session_expired"};
    }
  }

  async function locked(fn) {
    checkStorage();
    if (!env.locks || typeof env.locks.request !== "function") throw {status: "browser_unsupported"};
    const abort = new env.AbortController();
    const timer = env.setTimeout(() => abort.abort(), 15000);
    try { return await env.locks.request(lockName, {mode: "exclusive", signal: abort.signal}, fn); }
    catch (error) { if (error?.name === "AbortError") throw {status: "unavailable"}; throw error; }
    finally { env.clearTimeout(timer); }
  }

  async function request(path, body) {
    const abort = new env.AbortController();
    const timer = env.setTimeout(() => abort.abort(), 15000);
    let response;
    try {
      response = await env.fetch(config.url + "/auth/v1" + path, {
        method: "POST", headers: {apikey: config.publishable_key, "Content-Type": "application/json"},
        body: JSON.stringify(body), signal: abort.signal, redirect: "error",
        credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
      });
    } catch (_) { throw {status: "unavailable"}; }
    finally { env.clearTimeout(timer); }
    if (response.status === 429) throw {status: "rate_limited"};
    if ([400, 401, 403, 422].includes(response.status)) {
      throw {status: path === "/signup" ? "captcha_required" : "session_expired"};
    }
    if (response.status !== 200) throw {status: "unavailable"};
    try { return normalized(await response.json()); }
    catch (_) { throw {status: "invalid_response"}; }
  }

  async function act(fn) {
    try { await locked(fn); }
    catch (error) { emit(error?.status ?? "unavailable"); }
  }

  async function restore(forceRefresh = false) {
    await act(async () => {
      let session = read();
      if (!session) { emit("needs_session"); return; }
      if (forceRefresh || session.expires_at <= now() + 60) {
        try {
          session = await request("/token?grant_type=refresh_token", {refresh_token: session.refresh_token});
          env.storage.setItem(storageKey, JSON.stringify(session));
        } catch (error) {
          if (error?.status === "session_expired") env.storage.removeItem(storageKey);
          throw error;
        }
      }
      emit("ready", session);
    });
  }

  async function connect(captchaToken) {
    await act(async () => {
      // Re-read inside the shared tab lock; a second click/tab cannot create a
      // second identity or overwrite a refresh token created by the first.
      const existing = read();
      if (existing) { emit("ready", existing); return; }
      if (config.signup_enabled !== true) throw {status: "signup_unavailable"};
      if (typeof captchaToken !== "string" || !captchaToken.trim()
          || captchaToken.length > 2048) throw {status: "captcha_required"};
      const session = await request("/signup", {data: {}, gotrue_meta_security: {captcha_token: captchaToken}});
      env.storage.setItem(storageKey, JSON.stringify(session));
      emit("ready", session);
    });
    // Handles an existing near-expiry session without recursively taking a lock.
    if (lastSignature && JSON.parse(lastSignature).status === "ready") await restore();
  }

  async function forget() {
    await act(async () => { env.storage.removeItem(storageKey); emit("needs_session"); });
  }

  return {restore, connect, forget, storageKey, close: () => { closed = true; }};
}

// A token stays in this callback's memory: never Streamlit state, storage,
// URLs or logs. Auth validates it at signup; a browser callback is not proof.
export function createBotConnector(config, env) {
  let closed = false, generation = 0, widget = null, pending = false;
  function clear() {
    generation++; pending = false;
    if (widget !== null) { try { env.remove(widget); } catch (_) {} widget = null; }
    env.busy(false);
  }
  async function start() {
    if (closed || pending) return;
    if (config.signup_enabled !== true) { env.status(MESSAGES.signup_unavailable); return; }
    if (typeof config.turnstile_site_key !== 'string'
        || !/^0x[A-Za-z0-9_-]{14,62}$/.test(config.turnstile_site_key)) {
      env.status('The website bot check is not configured. No workspace was created.'); return;
    }
    clear(); pending = true; env.busy(true);
    const current = generation;
    env.status('Complete the bot check to connect your private workspace.');
    const fail = () => {
      if (closed || generation !== current) return;
      clear(); env.status('The bot check could not complete. Click Connect private workspace to retry.');
    };
    try {
      await env.load();
      if (closed || generation !== current) return;
      const id = env.render({sitekey: config.turnstile_site_key, theme: 'auto', size: 'flexible',
        'response-field': false, retry: 'never', 'refresh-expired': 'manual',
        callback: async token => {
          if (closed || generation !== current) return;
          if (typeof token !== 'string' || !token.trim() || token.length > 2048) { fail(); return; }
          clear(); pending = true; env.busy(true);
          try { await env.connect(token); }
          finally { pending = false; if (!closed) env.busy(false); }
        }, 'error-callback': fail, 'expired-callback': fail, 'timeout-callback': fail});
      if (closed || generation !== current) env.remove(id); else widget = id;
    } catch (_) { fail(); }
  }
  return {start, clear, close: () => { closed = true; clear(); }};
}

let turnstileLoading = null;
export function loadTurnstile(env) {
  if (env.turnstile?.render) return Promise.resolve();
  if (turnstileLoading) return turnstileLoading;
  turnstileLoading = new Promise((resolve, reject) => {
    const script = env.document.createElement('script');
    let settled = false;
    // Cloudflare requires this official live URL; it must not be self-hosted.
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    script.async = true; script.referrerPolicy = 'no-referrer';
    const timer = env.setTimeout(() => finish(false), 15000);
    function finish(ok) {
      if (settled) return;
      settled = true;
      env.clearTimeout(timer); script.onload = script.onerror = null;
      if (ok && env.turnstile?.render) resolve();
      else { script.remove(); turnstileLoading = null; reject(new Error('Bot check unavailable')); }
    }
    script.onload = () => finish(true); script.onerror = () => finish(false);
    env.document.head.appendChild(script);
  });
  return turnstileLoading;
}

export default function(component) {
  const {data, parentElement, setStateValue} = component;
  const status = parentElement.querySelector('[data-auth-status]');
  const connect = parentElement.querySelector('[data-auth-connect]');
  const retry = parentElement.querySelector('[data-auth-retry]');
  const botElement = parentElement.querySelector('[data-auth-bot]');
  let bot = null, ready = false;
  const controller = createAuthController(data, {
    storage: {
      getItem: (key) => globalThis.localStorage.getItem(key),
      setItem: (key, value) => globalThis.localStorage.setItem(key, value),
      removeItem: (key) => globalThis.localStorage.removeItem(key),
    }, locks: globalThis.navigator?.locks,
    fetch: globalThis.fetch.bind(globalThis), now: Date.now, AbortController: globalThis.AbortController,
    // Browser timers require their Window receiver when called through env.
    setTimeout: globalThis.setTimeout.bind(globalThis), clearTimeout: globalThis.clearTimeout.bind(globalThis),
    emit: (message, label) => {
      status.textContent = label;
      ready = message.status === "ready";
      if (ready) bot?.clear();
      connect.disabled = ready;
      setStateValue("auth", message);
    },
  });
  bot = createBotConnector(data, {
    load: () => loadTurnstile(globalThis),
    render: options => globalThis.turnstile.render(botElement, options),
    remove: id => globalThis.turnstile.remove(id),
    connect: token => controller.connect(token),
    status: text => { status.textContent = text; },
    busy: value => { connect.disabled = ready || value; },
  });
  connect.onclick = () => { if (!ready) bot.start(); };
  retry.onclick = () => controller.restore(true);
  const storageChanged = (event) => { if (event.key === controller.storageKey || event.key === null) controller.restore(); };
  const visible = () => { if (document.visibilityState === "visible") controller.restore(); };
  globalThis.addEventListener("storage", storageChanged);
  document.addEventListener("visibilitychange", visible);
  const timer = globalThis.setInterval(() => controller.restore(), 30000);
  controller.restore();
  return () => {
    bot.close(); controller.close(); globalThis.clearInterval(timer);
    globalThis.removeEventListener("storage", storageChanged);
    document.removeEventListener("visibilitychange", visible);
    connect.onclick = retry.onclick = null;
  };
}
