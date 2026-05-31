// WebSocket helper for streaming a job's stdout into a <pre> console element.
class TerminalStream {
  constructor(outputEl, onClose) {
    this.outputEl = outputEl;
    this.onClose = onClose;
    this.ws = null;
    this.jobId = null;
  }

  connect(jobId) {
    this.close();
    this.jobId = jobId;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/terminal/${encodeURIComponent(jobId)}`;
    this.ws = new WebSocket(url);

    this.ws.onmessage = (ev) => this.append(ev.data);
    this.ws.onclose = () => {
      this.append("\n[disconnected]");
      if (this.onClose) this.onClose(jobId);
    };
    this.ws.onerror = () => this.append("\n[websocket error]");
  }

  append(text) {
    this.outputEl.textContent += text + "\n";
    this.outputEl.scrollTop = this.outputEl.scrollHeight;
  }

  clear() { this.outputEl.textContent = ""; }

  close() {
    if (this.ws) {
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }
  }
}
