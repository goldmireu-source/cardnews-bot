/* 발행 진행 패널 — 양쪽 페이지(스튜디오 / auto)에서 공통 사용.
   사용: window.PublishProgress.start(sessionId, platforms, caption, opts) */
(function () {
  if (window.PublishProgress) return;

  const PLATFORM_META = {
    instagram: { label: "Instagram", emoji: "📷" },
    facebook:  { label: "Facebook",  emoji: "📘" },
    threads:   { label: "Threads",   emoji: "🧵" },
    tiktok:    { label: "TikTok",    emoji: "🎵" },
  };

  const STATUS_ICON = {
    pending: "⌛", uploading: "📤", finalizing: "🧩",
    publishing: "🚀", done: "✓", error: "✕", skipped: "⊘",
  };

  const PHASE_BASE = {
    pending: 0, uploading: 10, finalizing: 80,
    publishing: 92, done: 100, skipped: 100, error: 100,
  };

  function platformPct(info) {
    const s = info.status || "pending";
    const base = PHASE_BASE[s] || 0;
    if (s === "uploading" && info.total) {
      return base + Math.floor(60 * (info.current || 0) / info.total);
    }
    return base;
  }

  function createOverlay() {
    return Object.assign(document.createElement("div"), {
      style: "position:fixed;inset:0;background:rgba(0,0,0,0.78);z-index:10000;display:flex;align-items:center;justify-content:center;padding:20px;font-family:'Pretendard Variable',Pretendard,sans-serif",
    });
  }

  async function start(sid, platforms, caption, opts) {
    opts = opts || {};

    // 1) 잡 시작
    let job_id;
    try {
      const r = await fetch(`/api/publish/${sid}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platforms, caption }),
      });
      const j = await r.json();
      if (!r.ok && r.status !== 202) throw new Error(j.error || `HTTP ${r.status}`);
      job_id = j.job_id;
    } catch (e) {
      alert("발행 시작 실패: " + e.message);
      return;
    }

    // 2) 오버레이 + 진행 패널
    const back = createOverlay();
    const panel = document.createElement("div");
    panel.style.cssText = "background:#1a1a1a;color:#fff;max-width:520px;width:100%;max-height:90vh;overflow:auto;border-radius:12px;padding:24px;box-shadow:0 20px 60px rgba(0,0,0,0.5)";
    panel.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <h2 id="pp-title" style="margin:0;font-size:17px">📤 발행 진행 중…</h2>
        <span id="pp-overall" style="font-size:14px;color:#aaa;font-variant-numeric:tabular-nums">0%</span>
      </div>
      <div style="height:8px;background:#2a2a2a;border-radius:4px;overflow:hidden;margin-bottom:18px">
        <div id="pp-overall-fill" style="height:100%;width:0%;background:linear-gradient(90deg,#e1306c,#833ab4);transition:width .4s"></div>
      </div>
      <ul id="pp-list" style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px"></ul>
      <div id="pp-final" hidden style="margin-top:18px;padding-top:14px;border-top:1px solid #333"></div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">
        <button id="pp-bg" style="padding:8px 14px;background:#333;color:#ccc;border:1px solid #444;border-radius:6px;cursor:pointer;font-size:12px">⊠ 백그라운드로</button>
        <button id="pp-close" style="padding:8px 16px;background:#444;color:#fff;border:none;border-radius:6px;cursor:not-allowed;opacity:0.5" disabled>닫기</button>
      </div>
    `;
    back.appendChild(panel);
    document.body.appendChild(back);

    const $ = sel => panel.querySelector(sel);
    const listEl = $("#pp-list");
    const overallEl = $("#pp-overall");
    const overallFill = $("#pp-overall-fill");
    const titleEl = $("#pp-title");
    const finalEl = $("#pp-final");
    const closeBtn = $("#pp-close");
    const bgBtn = $("#pp-bg");

    const rows = {};
    for (const p of platforms) {
      const meta = PLATFORM_META[p] || { label: p, emoji: "•" };
      const li = document.createElement("li");
      li.style.cssText = "border:1px solid #2a2a2a;border-radius:8px;padding:10px 12px;background:#111";
      li.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;margin-bottom:6px">
          <span style="font-weight:600">${meta.emoji} ${meta.label}</span>
          <span class="pp-status" style="color:#aaa;font-size:12px;font-variant-numeric:tabular-nums">⌛ 대기</span>
        </div>
        <div style="height:5px;background:#222;border-radius:3px;overflow:hidden">
          <div class="pp-fill" style="height:100%;width:0%;background:#666;transition:width .4s,background .4s"></div>
        </div>
        <div class="pp-err" hidden style="margin-top:6px;font-size:11px;color:#ff6b86;word-break:break-all;background:rgba(255,107,134,0.08);padding:6px 8px;border-radius:4px"></div>
        <a class="pp-link" hidden target="_blank" rel="noopener" style="display:inline-block;margin-top:6px;font-size:11.5px;color:#6ee7ff;text-decoration:none">↗ 게시물 보기</a>
      `;
      listEl.appendChild(li);
      rows[p] = li;
    }

    let stopped = false;

    function applySnapshot(j) {
      const pct = j.overall_percent || 0;
      overallEl.textContent = `${pct}%`;
      overallFill.style.width = `${pct}%`;

      for (const [p, info] of Object.entries(j.platforms || {})) {
        const row = rows[p]; if (!row) continue;
        const status = info.status || "pending";
        const statusEl = row.querySelector(".pp-status");
        const fillEl = row.querySelector(".pp-fill");
        const errEl = row.querySelector(".pp-err");
        const linkEl = row.querySelector(".pp-link");

        const icon = STATUS_ICON[status] || "?";
        let label = info.step_label || status;
        if (status === "uploading" && info.total) {
          label = `이미지 ${info.current || 0}/${info.total} 업로드`;
        }
        statusEl.textContent = `${icon} ${label}`;

        fillEl.style.width = `${platformPct(info)}%`;
        if (status === "done") {
          fillEl.style.background = "#3fc17a";
          statusEl.style.color = "#3fc17a";
        } else if (status === "error") {
          fillEl.style.background = "#ff6b86";
          statusEl.style.color = "#ff6b86";
        } else if (status === "skipped") {
          fillEl.style.background = "#666";
          statusEl.style.color = "#999";
        } else {
          fillEl.style.background = "linear-gradient(90deg,#e1306c,#833ab4)";
        }

        if (info.error) {
          errEl.hidden = false;
          errEl.textContent = info.error;
        } else {
          errEl.hidden = true;
        }
        if (info.permalink) {
          linkEl.hidden = false;
          linkEl.href = info.permalink;
        }
      }
    }

    function finalize(j) {
      const all = Object.values(j.platforms || {});
      const ok = all.filter(p => p.status === "done").length;
      const fail = all.filter(p => p.status === "error").length;
      const skip = all.filter(p => p.status === "skipped").length;
      const color = fail ? (ok ? "#ffb86b" : "#ff6b86") : "#3fc17a";
      const head = fail ? (ok ? "⚠ 일부 실패" : "✕ 모두 실패") : "✓ 전체 완료";
      titleEl.textContent = head;
      let line = `완료 ${ok}`;
      if (fail) line += ` · 실패 ${fail}`;
      if (skip) line += ` · 스킵 ${skip}`;
      finalEl.hidden = false;
      finalEl.innerHTML = `<div style="font-size:14px;font-weight:600;color:${color}">${head} (${line})</div>`;
      closeBtn.disabled = false;
      closeBtn.style.cursor = "pointer";
      closeBtn.style.opacity = "1";
      closeBtn.style.background = fail ? "#5a3a3a" : "#2a5a3a";
      bgBtn.style.display = "none";
      if (typeof opts.onDone === "function") opts.onDone(j);
    }

    async function tick() {
      if (stopped) return;
      try {
        const r = await fetch(`/api/publish/jobs/${job_id}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        applySnapshot(j);
        if (j.status === "done") {
          stopped = true;
          finalize(j);
          return;
        }
        setTimeout(tick, 1000);
      } catch {
        setTimeout(tick, 2000);
      }
    }

    function close() { stopped = true; back.remove(); }

    function background() {
      back.remove();
      if (typeof window.toast === "function") {
        window.toast("📤 발행이 백그라운드에서 계속됩니다", "success", 4000);
      }
      const bgTick = async () => {
        try {
          const r = await fetch(`/api/publish/jobs/${job_id}`);
          if (!r.ok) return;
          const j = await r.json();
          if (j.status === "done") {
            const all = Object.values(j.platforms || {});
            const ok = all.filter(p => p.status === "done").length;
            const fail = all.filter(p => p.status === "error").length;
            const msg = fail
              ? `✕ 발행 일부 실패 (${ok}/${all.length} 성공)`
              : `✓ 발행 완료 (${ok}개 플랫폼)`;
            if (typeof window.toast === "function") {
              window.toast(msg, fail ? "error" : "success", 8000);
            } else {
              alert(msg);
            }
            if (typeof opts.onDone === "function") opts.onDone(j);
            return;
          }
          setTimeout(bgTick, 2000);
        } catch { setTimeout(bgTick, 3000); }
      };
      setTimeout(bgTick, 2000);
    }

    closeBtn.onclick = close;
    bgBtn.onclick = background;

    tick();
  }

  window.PublishProgress = { start };
})();
