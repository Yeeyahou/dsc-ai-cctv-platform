"use strict";
const el = id => document.getElementById(id);
let config, accessToken = "", loginUntil = 0, live = null, peer = null, resource = null;
let expiryTimer, loginTimer, monitor, starting = false, generation = 0;
const status = text => { el("status").textContent = text; };
const overlay = text => { el("overlay").textContent = text; el("overlay").hidden = !text; };
const base64url = bytes => btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
async function request(url, options = {}) {
  const response = await fetch(url, { ...options, signal: AbortSignal.timeout(15000), cache: "no-store", redirect: "error" });
  if (!response.ok) throw new Error(`요청 실패 (${response.status}). 권한과 현장 연결을 확인하세요.`);
  return response;
}
async function api(path, body) {
  if (!accessToken || Date.now() >= loginUntil) throw new Error("로그인이 만료되었습니다. 다시 로그인하세요.");
  const headers = { Authorization: `Bearer ${accessToken}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return (await request(`${config.apiUrl}${path}`, { method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body) })).json();
}
async function login() {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  sessionStorage.setItem("dsc.live.pkce", JSON.stringify({ verifier, state, created: Date.now() }));
  const url = new URL("/oauth2/authorize", config.oauthOrigin);
  url.search = new URLSearchParams({ client_id: config.clientId, redirect_uri: config.redirectUri, response_type: "code", scope: "openid dsc/live.read", state, code_challenge: challenge, code_challenge_method: "S256" });
  location.assign(url);
}
async function finishLogin() {
  const params = new URLSearchParams(location.search);
  if (!params.has("code") && !params.has("error")) return;
  const saved = JSON.parse(sessionStorage.getItem("dsc.live.pkce") || "null");
  sessionStorage.removeItem("dsc.live.pkce");
  history.replaceState({}, "", "/");
  if (!saved || saved.state !== params.get("state") || Date.now() - saved.created > 600000 || params.has("error")) throw new Error("로그인 확인에 실패했습니다. 다시 로그인하세요.");
  const response = await request(`${config.oauthOrigin}/oauth2/token`, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ grant_type: "authorization_code", client_id: config.clientId, code: params.get("code"), code_verifier: saved.verifier, redirect_uri: config.redirectUri }) });
  const data = await response.json();
  if (data.token_type?.toLowerCase() !== "bearer" || !data.access_token || !Number.isFinite(data.expires_in) || data.expires_in <= 0) throw new Error("로그인 응답이 올바르지 않습니다.");
  accessToken = data.access_token;
  loginUntil = Date.now() + data.expires_in * 1000;
  loginTimer = setTimeout(async () => { await stop(); accessToken = ""; el("login").hidden = false; el("logout").hidden = true; el("start").disabled = true; status("로그인이 만료되었습니다. 다시 로그인하세요."); }, data.expires_in * 1000);
  el("login").hidden = true; el("logout").hidden = false;
  const { items } = await api("/live/cameras");
  el("camera").replaceChildren(...items.map(camera => new Option(`${camera.displayName} · ${camera.siteId}`, camera.assetId)));
  el("camera").disabled = !items.length; el("start").disabled = !items.length;
  status(items.length ? "카메라를 선택하고 실시간 보기를 누르세요." : "시청 권한이 있는 카메라가 없습니다.");
}
async function stop() {
  generation += 1;
  clearTimeout(expiryTimer); clearInterval(monitor);
  const prior = live, priorResource = resource;
  live = null; resource = null;
  if (peer) { peer.onconnectionstatechange = null; peer.close(); peer = null; }
  el("video").srcObject = null; overlay("시청 중인 영상이 없습니다");
  el("stop").disabled = true; el("start").disabled = !accessToken || starting || Date.now() >= loginUntil;
  await Promise.allSettled([
    priorResource && request(priorResource, { method: "DELETE", headers: { Authorization: `Bearer ${prior.sessionToken}` } }),
    prior && api(`/live/sessions/${prior.sessionId}/stop`, {})
  ]);
}
async function start() {
  if (starting) return;
  starting = true; el("start").disabled = true;
  await stop();
  const ownGeneration = generation;
  try {
    status("현장 카메라에 연결하고 있습니다…"); overlay("연결 중…");
    live = await api(`/live/cameras/${encodeURIComponent(el("camera").value)}/sessions`, {});
    const endpoint = new URL(live.whepUrl);
    if (!config.gatewayOrigins.includes(endpoint.origin) || !/^\/media\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\/whep$/.test(endpoint.pathname) || endpoint.search || endpoint.hash || endpoint.username || endpoint.password) throw new Error("등록되지 않은 영상 주소입니다.");
    const connection = new RTCPeerConnection({ iceServers: [] }); peer = connection;
    connection.addTransceiver("video", { direction: "recvonly" });
    connection.ontrack = event => { el("video").srcObject = event.streams[0] || new MediaStream([event.track]); };
    connection.onconnectionstatechange = () => {
      if (peer !== connection) return;
      if (connection.connectionState === "connected") { overlay(""); status("실시간 시청 중 · 영상이 멈추면 다시 연결하세요."); }
      if (["failed", "disconnected", "closed"].includes(connection.connectionState)) { overlay("현장 연결이 끊겼습니다"); status("연결이 끊겼습니다. 실시간 보기를 눌러 다시 연결하세요."); }
    };
    await connection.setLocalDescription(await connection.createOffer());
    await new Promise((resolve, reject) => {
      if (connection.iceGatheringState === "complete") return resolve();
      const timeout = setTimeout(() => reject(new Error("영상 연결 준비 시간이 초과되었습니다.")), 10000);
      connection.onicegatheringstatechange = () => { if (connection.iceGatheringState === "complete") { clearTimeout(timeout); resolve(); } };
    });
    const response = await request(endpoint.href, { method: "POST", headers: { Authorization: `Bearer ${live.sessionToken}`, "Content-Type": "application/sdp" }, body: connection.localDescription.sdp });
    const locationHeader = response.headers.get("Location");
    if (!locationHeader) throw new Error("영상 세션 주소가 없습니다.");
    const locationUrl = new URL(locationHeader, endpoint);
    if (locationUrl.origin !== endpoint.origin || !locationUrl.pathname.startsWith(`${endpoint.pathname}/`) || locationUrl.search || locationUrl.hash) throw new Error("영상 세션 주소가 올바르지 않습니다.");
    resource = locationUrl.href;
    await connection.setRemoteDescription({ type: "answer", sdp: await response.text() });
    if (generation !== ownGeneration) throw new Error("시청 요청이 취소되었습니다.");
    expiryTimer = setTimeout(() => { stop().then(() => status("시청 연결이 만료되었습니다. 다시 보기를 누르세요.")); }, Math.max(0, Date.parse(live.expiresAt) - Date.now()));
    let lastFrames = -1, stalled = 0;
    monitor = setInterval(async () => {
      if (peer !== connection) return;
      let stats;
      try { stats = await connection.getStats(); } catch { return; }
      if (peer !== connection) return;
      for (const report of stats.values()) if (report.type === "inbound-rtp" && report.kind === "video") {
        stalled = report.framesDecoded === lastFrames ? stalled + 1 : 0; lastFrames = report.framesDecoded;
        if (stalled >= 2) { overlay("영상 수신이 멈췄습니다"); status("영상 수신이 멈췄습니다. 현장 연결을 확인하고 다시 연결하세요."); }
        else if (connection.connectionState === "connected") overlay("");
      }
    }, 3000);
    el("stop").disabled = false;
  } catch (error) { await stop(); status(error.message); }
  finally { starting = false; el("start").disabled = !accessToken || Date.now() >= loginUntil; }
}
el("login").onclick = () => login().catch(() => status("로그인을 시작할 수 없습니다."));
el("start").onclick = start;
el("stop").onclick = () => stop().then(() => status("시청을 종료했습니다."));
el("logout").onclick = async () => { await stop(); clearTimeout(loginTimer); accessToken = ""; const url = new URL("/logout", config.oauthOrigin); url.search = new URLSearchParams({ client_id: config.clientId, logout_uri: config.redirectUri }); location.assign(url); };
window.addEventListener("pagehide", () => { if (peer) peer.close(); });
(async () => { try { config = await (await request("/config.json")).json(); await finishLogin(); } catch (error) { status(error.message); } })();
