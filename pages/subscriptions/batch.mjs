// No DOM or framework dependency: the existing single-add API remains authoritative.
export const MAX_BATCH_SIZE = 100;

export function parseBatch(text, existing = []) {
  const known = new Set(existing.map((name) => name.toLowerCase()));
  const seen = new Set();
  const entries = String(text).split(/[\s,，;；]+/u).filter(Boolean).map((input) => {
    const username = input.replace(/^@/, "");
    const key = username.toLowerCase();
    let status = "pending";
    if (!/^[A-Za-z0-9_]{1,15}$/.test(username)) status = "invalid";
    else if (seen.has(key)) status = "duplicate";
    else {
      seen.add(key);
      if (known.has(key)) status = "existing";
    }
    return { input, username, status, error: "" };
  });
  return { entries, overLimit: seen.size > MAX_BATCH_SIZE, uniqueCount: seen.size };
}

export class BatchQueue {
  constructor({ entries, umo, r18, media_only, send, onChange = () => {} }) {
    this.entries = entries.map((entry) => ({ ...entry }));
    this.target = Object.freeze({ umo, r18, media_only });
    this.send = send;
    this.onChange = onChange;
    this.running = false;
    this.stopRequested = false;
  }

  stop() {
    this.stopRequested = true;
    this.onChange(this);
  }

  async run(existing = []) {
    if (this.running) return false;
    this.running = true;
    this.stopRequested = false;
    const known = new Set(existing.map((name) => name.toLowerCase()));
    try {
      // Reconcile failed/unsent entries with a fresh overview before each run.
      for (const entry of this.entries) {
        if (!["pending", "failed"].includes(entry.status)) continue;
        entry.status = known.has(entry.username.toLowerCase()) ? "existing" : "pending";
        entry.error = "";
      }
      this.onChange(this);
      for (const entry of this.entries) {
        if (this.stopRequested) break;
        if (entry.status !== "pending") continue;
        entry.status = "adding";
        this.onChange(this);
        try {
          const result = await this.send({ ...this.target, username: entry.username });
          if (!result?.saved) throw new Error("接口未确认保存，请刷新后核对");
          entry.status = "added";
        } catch (error) {
          // A rejected/timeout request may have committed on the server.
          entry.status = "failed";
          entry.error = String(error?.message || "请求结果未确认，请刷新后核对");
        }
        this.onChange(this);
      }
    } finally {
      this.running = false;
      this.onChange(this);
    }
    return true;
  }
}
