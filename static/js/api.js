// Thin fetch wrappers around the CryFi JSON API.
const API = {
  async _req(method, path, body, isForm = false) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      if (isForm) {
        opts.body = body; // FormData; let the browser set Content-Type
      } else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
    }
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  },

  get(p) { return this._req("GET", p); },
  post(p, b) { return this._req("POST", p, b); },
  del(p) { return this._req("DELETE", p); },

  // Domain helpers
  health() { return this.get("/api/health"); },
  me() { return this.get("/api/me"); },
  logout() { return this.post("/api/logout"); },
  changePassword(current_password, new_password) { return this.post("/api/change-password", { current_password, new_password }); },
  interfaces() { return this.get("/api/interfaces"); },
  monitorStart(iface) { return this.post(`/api/interfaces/${encodeURIComponent(iface)}/monitor/start`); },
  monitorStop(iface) { return this.post(`/api/interfaces/${encodeURIComponent(iface)}/monitor/stop`); },
  regulatory() { return this.get("/api/regulatory"); },

  scanStart(payload) { return this.post("/api/scan/start", payload); },
  scanStop() { return this.post("/api/scan/stop"); },
  scanResults() { return this.get("/api/scan/results"); },

  capture(payload) { return this.post("/api/capture/start", payload); },
  captureJobStatus(id) { return this.get(`/api/capture/${encodeURIComponent(id)}/status`); },
  deauth(payload) { return this.post("/api/aireplay/deauth", payload); },
  jobStop(id) { return this.post(`/api/jobs/${encodeURIComponent(id)}/stop`); },
  jobGet(id) { return this.get(`/api/jobs/${encodeURIComponent(id)}`); },

  handshakes() { return this.get("/api/handshakes"); },
  revealHandshakeSsid(cap) { return this.post(`/api/handshakes/${encodeURIComponent(cap)}/reveal`); },
  deleteHandshake(cap) { return this.del(`/api/handshakes/${encodeURIComponent(cap)}`); },

  captures() { return this.get("/api/captures"); },
  deleteCapture(name) { return this.del(`/api/captures/${encodeURIComponent(name)}`); },
  deleteCaptures(names) { return this.post("/api/captures/delete", { names }); },
  analyzeCaptures() { return this.post("/api/captures/analyze"); },
  cleanCaptures() { return this.post("/api/captures/clean"); },
  wordlists() { return this.get("/api/wordlists"); },
  previewWordlist(name, n) { return this.get(`/api/wordlists/${encodeURIComponent(name)}/preview?lines=${n || 10}`); },
  deleteWordlist(name) { return this.del(`/api/wordlists/${encodeURIComponent(name)}`); },
  uploadWordlist(file) {
    const fd = new FormData();
    fd.append("file", file);
    return this._req("POST", "/api/wordlists/upload", fd, true);
  },

  crack(payload) { return this.post("/api/crack/start", payload); },

  wordgenEstimate(payload) { return this.post("/api/wordgen/estimate", payload); },
  wordgenPreview(payload) { return this.post("/api/wordgen/preview", payload); },
  wordgenGenerate(payload) { return this.post("/api/wordgen/generate", payload); },
  wordgenJob(id) { return this.get(`/api/wordgen/jobs/${encodeURIComponent(id)}`); },
  wordgenStop(id) { return this.post(`/api/wordgen/jobs/${encodeURIComponent(id)}/stop`); },
};
