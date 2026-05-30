"""
Cardnews Studio Web Server
==========================
스튜디오 HTML을 서빙하고, 봇이 만든 세션을 자동 로드/저장합니다.
또한 브라우저 → Anthropic API 의 프록시 역할도 합니다 (CORS 회피 + API 키 보호).

기능:
- GET  /                          스튜디오 HTML (URL ?session=<id>로 자동 로드)
- GET  /auto                      데일리 자동화 페이지 (데일리싱크 + 툴팁 + 사관학교 + 발행큐)
- GET  /api/sessions              세션 목록
- GET  /api/sessions/<id>         세션 로드
- PUT  /api/sessions/<id>         세션 저장 (스튜디오 편집 자동 동기화)
- POST /api/claude                Anthropic API 프록시 (브라우저용)
- GET  /api/dailysync/clusters    데일리싱크 클러스터 목록
- GET  /api/dailysync/cluster/<id> 클러스터 상세 + 원기사
- POST /api/auto/generate         자동 카드뉴스 생성 (소스: cluster/topic/template)
- POST /api/uploads/<session_id>  스튜디오에서 렌더링한 PNG들 업로드
- GET  /uploads/<session_id>/<n>.png  업로드된 PNG 정적 서빙
- GET  /api/instagram/status      IG 토큰 상태
- POST /api/instagram/publish/<id> IG 발행 (캐러셀)

실행:
    python server.py
    # 또는 포트 변경
    PORT=8080 python server.py
"""

import os
import json
import re
import sqlite3
import time
from pathlib import Path
from datetime import datetime, date
from dotenv import load_dotenv

import anthropic
from flask import Flask, jsonify, abort, request, Response, send_from_directory

load_dotenv()

ROOT = Path(__file__).parent
SESSIONS_DIR = ROOT / "sessions"
UPLOADS_DIR = ROOT / "uploads"
STUDIO_HTML = ROOT / "cardnews_studio.html"
AUTO_HTML = ROOT / "auto_studio.html"
SESSIONS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = os.getenv("MODEL", "claude-sonnet-4-20250514")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:5050").rstrip("/")
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# 데일리싱크 SQLite DB 경로 (기본: 사용자 환경)
DAILYSYNC_DB_PATH = os.getenv(
    "DAILYSYNC_DB_PATH",
    r"F:\ai-news-digest\ai-news-digest\data\app.db",
)

# Instagram Graph API (없으면 발행 기능 비활성)
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.getenv("IG_USER_ID", "").strip()
IG_DEFAULT_CAPTION = os.getenv("IG_DEFAULT_CAPTION", "").strip()

DEFAULT_BRAND = {
    "name": "인공지능사관학교 서포터즈",
    "handle": "@dailysync_ai",
    "footer": "인공지능사관학교 서포터즈",
}
DEFAULT_TONE = "친근한 정보 전달"

app = Flask(__name__)


@app.after_request
def add_cors_for_uploads(resp):
    """/uploads/* 응답에 CORS 헤더 — html2canvas useCORS:true 가 cloudflare 도메인
    이미지를 fetch 할 때 Access-Control-Allow-Origin 없으면 tainted canvas 가 되어
    배경이 검정으로 캡처됨.
    """
    p = request.path or ""
    if p.startswith("/uploads/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return resp

# ============================================================
# 봇 연동용 스크립트 (HTML 인젝션)
# 스튜디오 HTML을 수정하지 않고, 서버에서 동적으로 추가합니다.
# ============================================================
BOT_INTEGRATION_SCRIPT = r"""
<script>
// === Bot Integration (server.py가 인젝션) ===
(function() {
  const params = new URLSearchParams(window.location.search);
  const sessionId = params.get("session");

  // 스튜디오(멀티 프로젝트)가 init() 까지 끝났는지
  function studioReady() {
    return window.__studioReady === true
      && typeof STATE !== 'undefined'
      && typeof WORKSPACE !== 'undefined'
      && typeof blankProjectData === 'function'
      && typeof loadProjectToState === 'function';
  }

  // [편집] 세션을 "새 스튜디오 탭"으로 연다 (기존 탭/작업 보존)
  //   sid 인자가 없으면 URL 의 sessionId 사용. 다른 탭(오토 페이지)에서 채널로 호출될 때는 sid 전달.
  function loadSessionAsTab(sid) {
    sid = sid || sessionId;
    if (!sid) return;
    if (!studioReady()) { setTimeout(() => loadSessionAsTab(sid), 80); return; }
    // 같은 세션 탭이 이미 열려 있으면 새로 만들지 않고 그 탭으로 전환
    const existing = WORKSPACE.projects.find(p => p.meta && p.meta.sessionId === sid);
    if (existing) {
      if (WORKSPACE.activeId !== existing.id && typeof activateProject === 'function') {
        activateProject(existing.id);
      } else if (typeof renderTabs === 'function') {
        renderTabs();
      }
      if (typeof toast === 'function') toast(`이미 열린 탭으로 전환: ${sid}`, "success");
      return;
    }
    fetch(`/api/sessions/${sid}`)
      .then(r => { if (!r.ok) throw new Error("세션 없음"); return r.json(); })
      .then(p => {
        if (typeof syncStateToProject === 'function') syncStateToProject(); // 현재 탭 보존
        const proj = blankProjectData((p.meta && p.meta.title) || p.name || ("세션 " + sid));
        proj.theme = p.theme || "navy";
        proj.brand = p.brand || proj.brand;
        proj.cards = p.cards || [];
        proj.activeId = p.activeId || (proj.cards[0] && proj.cards[0].id) || null;
        proj.lastSourceText = p.lastSourceText || "";
        proj.meta = p.meta || {};
        proj.meta.sessionId = sid; // 서버 세션과 바인딩(자동저장 동기화용)
        WORKSPACE.projects.push(proj);
        WORKSPACE.activeId = proj.id;
        loadProjectToState(proj);
        if (typeof setTheme === 'function') setTheme(STATE.theme);
        if (typeof renderTabs === 'function') renderTabs();
        if (typeof renderAll === 'function') renderAll();
        if (typeof toast === 'function') toast(`✓ 새 탭으로 세션 로드: ${sid}`, "success");
      })
      .catch(err => {
        if (typeof toast === 'function') toast("세션 로드 실패: " + err.message, "error");
      });
  }

  // 다른 탭(오토 페이지)에서 "이 세션을 여기 새 탭으로 열어줘" 요청을 수신
  function setupStudioChannel() {
    if (typeof BroadcastChannel === 'undefined' || window.__studioChannel) return;
    let ch;
    try { ch = new BroadcastChannel('cardnews-studio'); } catch (e) { return; }
    window.__studioChannel = ch;
    ch.onmessage = (e) => {
      const m = e.data || {};
      if (m.type === 'open-session' && m.sessionId) {
        loadSessionAsTab(m.sessionId);
        // 살아있다는 응답(ack) — 오토 페이지가 새 브라우저 탭을 열지 않도록
        try { ch.postMessage({ type: 'session-opened', sessionId: m.sessionId }); } catch (e2) {}
        try { window.focus(); } catch (e3) {} // 최선 노력(브라우저가 막을 수 있음)
      } else if (m.type === 'ping') {
        try { ch.postMessage({ type: 'pong' }); } catch (e2) {}
      }
    };
  }

  // [발행 publish=1] 워크스페이스/로컬스토리지를 건드리지 않고 STATE 에만 로드(렌더 전용, 창은 자동 닫힘)
  function loadSessionForPublish() {
    if (typeof STATE === 'undefined') { setTimeout(loadSessionForPublish, 80); return; }
    fetch(`/api/sessions/${sessionId}`)
      .then(r => { if (!r.ok) throw new Error("세션 없음"); return r.json(); })
      .then(p => {
        STATE.theme = p.theme || "navy";
        STATE.brand = p.brand || STATE.brand;
        STATE.cards = p.cards || [];
        STATE.activeId = p.activeId || (STATE.cards[0] && STATE.cards[0].id) || null;
        STATE.lastSourceText = p.lastSourceText || "";
        STATE.meta = p.meta || {};
        // setTheme 은 autoSave(로컬스토리지 저장)를 호출하므로 발행 모드에선 테마를 직접 적용
        if (typeof applyTheme === 'function') applyTheme(STATE.theme);
        const pv = document.getElementById("preview-scaler");
        if (pv) pv.className = `preview-scaler theme-${STATE.theme}`;
        const ra = document.getElementById("render-area");
        if (ra) ra.className = `render-area theme-${STATE.theme}`;
        if (typeof renderAll === 'function') renderAll();
      })
      .catch(err => {
        if (typeof toast === 'function') toast("세션 로드 실패: " + err.message, "error");
      });
  }

  // autoSave 후킹: 세션과 연결된 탭을 편집하면 해당 서버 세션에도 저장(탭 인식형)
  function hookAutoSave() {
    if (typeof autoSave === 'undefined' || typeof activeProject !== 'function') {
      setTimeout(hookAutoSave, 100); return;
    }
    if (window.__botAutoSaveHooked) return;
    window.__botAutoSaveHooked = true;
    const orig = autoSave;
    window.autoSave = function() {
      orig(); // syncStateToProject + 로컬스토리지 저장
      try {
        const p = activeProject();
        const sid = p && p.meta && p.meta.sessionId;
        if (!sid) return; // 세션과 연결된 탭일 때만 서버에 저장
        fetch(`/api/sessions/${sid}`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            theme: p.theme, brand: p.brand, cards: p.cards,
            activeId: p.activeId, lastSourceText: p.lastSourceText,
            meta: p.meta, // 메타(cluster_id, sessionId 등) 보존
          }),
        }).catch(() => {});
      } catch (e) {}
    };
  }

  // ?publish=1 일 때 자동으로 PNG 렌더+업로드 후 탭 닫기
  async function autoPublishUpload() {
    if (params.get("publish") !== "1") return;
    // 카드 렌더가 끝날 때까지 대기
    let tries = 0;
    while ((typeof STATE === 'undefined' || !STATE.cards || !STATE.cards.length || typeof renderCard !== 'function') && tries < 100) {
      await new Promise(r => setTimeout(r, 100));
      tries++;
    }
    if (typeof renderCard !== 'function' || !STATE.cards.length) return;

    const renderArea = document.getElementById("render-area");
    if (!renderArea) { alert("렌더 영역을 찾지 못했어요. 스튜디오 UI가 바뀌었는지 확인 필요."); return; }
    renderArea.className = `render-area theme-${STATE.theme}`;

    const formData = new FormData();
    if (typeof toast === 'function') toast(`📷 ${STATE.cards.length}장 렌더링 시작...`, "success");

    for (let i = 0; i < STATE.cards.length; i++) {
      renderArea.innerHTML = renderCard(STATE.cards[i], i + 1, STATE.cards.length);
      await new Promise(r => setTimeout(r, 150));
      // 큰 숫자 자동 피팅
      if (typeof window.autofitStat === "function") window.autofitStat(renderArea);
      // 배경 이미지 휘도 분석 (밝은 배경 자동 감지)
      if (typeof window.applyBgLuminance === "function") await window.applyBgLuminance(renderArea);
      const el = renderArea.querySelector(".card");
      if (!el) continue;
      const canvas = await html2canvas(el, { width:1080, height:1350, scale:1, useCORS:true, backgroundColor:null });
      const blob = await new Promise(res => canvas.toBlob(res, "image/png"));
      formData.append("files", blob, `${String(i).padStart(2,"0")}.png`);
    }

    try {
      const r = await fetch(`/api/uploads/${sessionId}`, { method: "POST", body: formData });
      const j = await r.json();
      if (r.ok && j.ok) {
        if (typeof toast === 'function') toast(`✓ ${j.files.length}장 인스타용으로 업로드됨. 곧 닫힙니다.`, "success");
        setTimeout(() => { try { window.close(); } catch {} }, 1500);
      } else {
        alert("업로드 실패: " + (j.error || r.status));
      }
    } catch (e) {
      alert("업로드 오류: " + e.message);
    }
  }

  // === 스튜디오 헤더의 "발행" 버튼 → 한 번 클릭으로 렌더+업로드+모달 ===
  async function publishFromStudio() {
    if (typeof STATE === 'undefined' || !STATE.cards || !STATE.cards.length) {
      alert("카드가 없습니다. 먼저 카드뉴스를 만드세요.");
      return;
    }
    // 세션 ID — '활성 탭'에 연결된 세션을 사용(멀티탭). 미연결이면 새 임시 세션 생성.
    // URL 의 sessionId 로 폴백하지 않는다 — 다른 탭 내용으로 잘못 발행되는 버그 방지.
    let ap = null;
    try { ap = (typeof activeProject === 'function') ? activeProject() : null; } catch (e) {}
    let sid = (ap && ap.meta && ap.meta.sessionId) ? ap.meta.sessionId : null;
    const isNewSid = !sid;
    if (!sid) sid = "studio_" + Date.now().toString(36);
    // 활성 탭의 '현재' 내용을 항상 해당 세션에 먼저 저장 → 캡션이 이 탭 기준으로 생성됨
    try {
      const payload = {
        theme: STATE.theme, brand: STATE.brand, cards: STATE.cards,
        activeId: STATE.activeId, lastSourceText: STATE.lastSourceText || "",
        meta: Object.assign({}, STATE.meta || {}, { sessionId: sid }),
      };
      const r = await fetch(`/api/sessions/${sid}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(await r.text());
    } catch (e) {
      alert("세션 저장 실패: " + e);
      return;
    }
    // 새로 만든 임시 세션이면 활성 탭에 연결(이후 편집도 이 세션에 동기화)
    if (isNewSid) {
      if (typeof STATE !== 'undefined') { STATE.meta = STATE.meta || {}; STATE.meta.sessionId = sid; }
      if (typeof autoSave === 'function') autoSave();
    }

    const btn = document.getElementById("btn-publish");
    const orig = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "📷 렌더링 중…"; }

    // PNG 렌더 + 업로드 (autoPublishUpload 와 동일 흐름)
    const renderArea = document.getElementById("render-area");
    if (!renderArea) { alert("렌더 영역 없음"); return; }
    renderArea.className = `render-area theme-${STATE.theme}`;
    const formData = new FormData();
    try {
      for (let i = 0; i < STATE.cards.length; i++) {
        if (btn) btn.textContent = `📷 렌더 ${i+1}/${STATE.cards.length}`;
        renderArea.innerHTML = renderCard(STATE.cards[i], i + 1, STATE.cards.length);
        await new Promise(r => setTimeout(r, 150));
        if (typeof window.autofitStat === "function") window.autofitStat(renderArea);
        if (typeof window.applyBgLuminance === "function") await window.applyBgLuminance(renderArea);
        const el = renderArea.querySelector(".card");
        if (!el) continue;
        const canvas = await html2canvas(el, { width:1080, height:1350, scale:1, useCORS:true, backgroundColor:null });
        const blob = await new Promise(res => canvas.toBlob(res, "image/png"));
        formData.append("files", blob, `${String(i).padStart(2,"0")}.png`);
      }
      if (btn) btn.textContent = "📤 업로드 중…";
      const r = await fetch(`/api/uploads/${sid}`, { method: "POST", body: formData });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      if (typeof toast === 'function') toast(`✓ ${j.files.length}장 준비 완료`, "success");
    } catch (e) {
      alert("렌더·업로드 실패: " + e.message);
      if (btn) { btn.disabled = false; btn.textContent = orig; }
      return;
    } finally {
      renderArea.innerHTML = "";
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }

    // 발행 모달 표시
    await openPublishModal(sid);
  }

  // === 자체 발행 모달 (cardnews_studio.html 에 modal 함수 없으므로 인라인) ===
  async function openPublishModal(sid) {
    const status = await fetch("/api/publish/status").then(r => r.json()).catch(() => null);
    if (!status) { alert("서버 응답 없음"); return; }
    const P = status.platforms || {};
    const httpsOk = status.server_url_https;
    const anyCfg = Object.values(P).some(p => p.configured);

    // 프리필은 비워두고, 모달 열린 후 서버에서 Claude 바이럴 캡션 비동기 fetch
    let prefill = "⏳ Claude 가 바이럴 캡션 생성 중…";

    const back = document.createElement("div");
    back.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px";
    const platRow = (id, label, emoji, cfg) =>
      `<label style="display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid #444;border-radius:8px;cursor:${cfg?'pointer':'not-allowed'};opacity:${cfg?1:0.4};margin-bottom:6px">
        <input type="checkbox" id="pub-${id}" ${cfg?'checked':'disabled'} style="width:16px;height:16px">
        <span style="font-size:18px">${emoji}</span>
        <div style="flex:1">
          <div style="font-weight:600">${label}</div>
          <div style="font-size:11px;color:#999">${cfg?'✓ 토큰 설정됨':'토큰 미설정'}</div>
        </div>
      </label>`;

    back.innerHTML = `
      <div style="background:#1a1a1a;color:#fff;max-width:560px;width:100%;max-height:90vh;overflow:auto;border-radius:12px;padding:24px;font-family:'Pretendard Variable',Pretendard,sans-serif">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h2 style="margin:0;font-size:18px">📲 발행 — Instagram · Threads · TikTok</h2>
          <button id="pm-close" style="background:none;border:none;color:#aaa;font-size:24px;cursor:pointer">×</button>
        </div>
        ${!anyCfg ? `
          <div style="padding:12px;background:rgba(255,184,107,0.1);border-radius:6px;font-size:13px">
            ⚠️ 어떤 플랫폼 토큰도 설정 안 됨. docs/POSTING_SETUP.md 참고해서 .env 에 토큰 추가 후 서버 재시작.
          </div>` : `
          <div>
            ${platRow("instagram", "Instagram", "📷", P.instagram?.configured)}
            ${platRow("threads", "Threads", "🧵", P.threads?.configured)}
            ${platRow("tiktok", "TikTok", "🎵", P.tiktok?.configured)}
          </div>
          <div style="margin-top:8px;padding:8px 12px;background:rgba(110,231,255,0.06);border:1px dashed rgba(110,231,255,0.2);border-radius:6px;font-size:11.5px;color:#aaa">
            💡 페이스북 페이지는 인스타 앱에서 자동 공유 설정하면 미러링됩니다.
          </div>
          <details style="margin-top:8px">
            <summary style="cursor:pointer;font-size:11.5px;color:#888">⚙ 고급 — Facebook API 직접 발행</summary>
            <div style="margin-top:6px">${platRow("facebook", "Facebook 페이지 (API)", "📘", P.facebook?.configured)}</div>
          </details>
          ${!httpsOk ? `<div style="margin-top:10px;padding:10px;background:rgba(255,184,107,0.1);border:1px solid #ffb86b;border-radius:6px;font-size:12px;color:#ffb86b">⚠️ SERVER_URL 이 http. ngrok 으로 https 노출 필요.</div>` : ''}
          <div style="margin-top:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;margin-bottom:4px">
              <label>캡션 · 해시태그 (수정 가능)</label>
              <button id="pm-regen" type="button" style="background:#2a2a2a;color:#aaa;border:1px solid #444;border-radius:4px;padding:3px 8px;font-size:10.5px;cursor:pointer">🔄 재생성</button>
            </div>
            <textarea id="pm-caption" style="width:100%;min-height:220px;background:#0c0d12;color:#fff;border:1px solid #444;border-radius:6px;padding:10px;font-family:inherit;font-size:13px;line-height:1.5;resize:vertical;box-sizing:border-box"></textarea>
            <div style="display:flex;gap:14px;margin-top:6px;font-size:11px;color:#888">
              <span id="pm-cap-ig">📷 0 / 2200</span>
              <span id="pm-cap-th">🧵 0 / 500</span>
              <span id="pm-cap-src" style="margin-left:auto;color:#666"></span>
            </div>
          </div>
        `}
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">
          <button id="pm-cancel" style="padding:8px 16px;background:#333;color:#fff;border:none;border-radius:6px;cursor:pointer">취소</button>
          ${anyCfg ? `<button id="pm-go" style="padding:8px 18px;background:linear-gradient(135deg,#e1306c,#833ab4);color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600">📲 발행</button>` : ''}
        </div>
      </div>`;
    document.body.appendChild(back);
    const close = () => back.remove();
    back.querySelector("#pm-close").onclick = close;
    back.querySelector("#pm-cancel").onclick = close;
    back.addEventListener("click", e => { if (e.target === back) close(); });

    // 프리필 + 카운터
    const ta = back.querySelector("#pm-caption");
    const srcLbl = back.querySelector("#pm-cap-src");
    const regenBtn = back.querySelector("#pm-regen");
    if (ta) {
      ta.value = prefill;
      const ig = back.querySelector("#pm-cap-ig");
      const th = back.querySelector("#pm-cap-th");
      const upd = () => {
        const n = ta.value.length;
        ig.textContent = `📷 ${n} / 2200`;
        th.textContent = `🧵 ${n} / 500`;
        ig.style.color = n > 2200 ? "#ff6b86" : (n > 2000 ? "#ffd166" : "#888");
        th.style.color = n > 500  ? "#ff6b86" : (n > 450  ? "#ffd166" : "#888");
      };
      ta.addEventListener("input", upd);
      upd();

      // 서버에서 Claude 바이럴 캡션 가져오기 (캐시 있으면 즉시, 없으면 5-10초 걸림)
      const loadCaption = async (regenerate) => {
        try {
          const r = await fetch(`/api/sessions/${sid}/caption-preview`, {
            method: regenerate ? "POST" : "GET",
          });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const j = await r.json();
          ta.value = j.caption || "";
          if (srcLbl) srcLbl.textContent = j.source === "viral" ? "✨ Claude 최적화" : "⚠ 기본 조합 (Claude 실패)";
          upd();
        } catch (e) {
          ta.value = "";
          if (srcLbl) srcLbl.textContent = "✕ 로드 실패: " + e.message;
        }
      };
      loadCaption(false);
      if (regenBtn) regenBtn.onclick = () => {
        ta.value = "⏳ 재생성 중…";
        if (srcLbl) srcLbl.textContent = "";
        loadCaption(true);
      };
    }

    const goBtn = back.querySelector("#pm-go");
    if (goBtn) goBtn.onclick = async () => {
      const platforms = ["instagram", "facebook", "threads", "tiktok"]
        .filter(p => back.querySelector(`#pub-${p}`)?.checked);
      if (!platforms.length) { alert("플랫폼을 1개 이상 선택"); return; }
      const caption = ta.value;
      close();
      if (!window.PublishProgress) {
        alert("publish_progress.js 로드 실패 — 새로고침 후 다시 시도");
        return;
      }
      window.PublishProgress.start(sid, platforms, caption);
    };
  }

  // 버튼 바인딩 (DOM ready)
  function bindPublishBtn() {
    const b = document.getElementById("btn-publish");
    if (!b) { setTimeout(bindPublishBtn, 200); return; }
    b.onclick = publishFromStudio;
  }

  function initAll() {
    // 발행 전용(publish=1)이 아니면 항상 채널 수신 + autoSave 후킹을 설치한다.
    // 그래야 이미 열려 있는 스튜디오가 오토 페이지의 "편집" 요청을 받아 새 탭으로 추가할 수 있다.
    const isPublish = params.get("publish") === "1";
    if (!isPublish) {
      setupStudioChannel();
      hookAutoSave();
      // 스튜디오의 '발행큐에서 불러오기' 등에서 호출할 수 있게 노출
      window.loadSessionAsTab = loadSessionAsTab;
    }
    if (sessionId) {
      if (isPublish) {
        // 발행 전용 창: 새 탭/로컬스토리지 만들지 않고 렌더 후 업로드·자동 닫힘
        loadSessionForPublish();
        autoPublishUpload();
      } else {
        // URL 로 직접 열렸을 때(열린 스튜디오가 없어 폴백으로 새 탭이 떴거나, 직접 접속)
        loadSessionAsTab();
      }
    }
    bindPublishBtn();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
</script>
"""

# 보안: session_id 화이트리스트 검증
SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def safe_session_path(session_id: str) -> Path:
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")
    return SESSIONS_DIR / f"{session_id}.json"


# ============================================================
# 카드뉴스 생성 — bot.py 의 system prompt 와 동일
# ============================================================
def build_system_prompt(tone: str) -> str:
    return f"""당신은 인스타그램 카드뉴스 전문 에디터입니다. 100만 팔로워 계정 운영 경험으로, 사용자 입력을 카드뉴스 5-7장으로 재구성합니다.

## 입력 소스
- 텍스트만: 텍스트 분석
- 이미지만: 이미지 안의 모든 텍스트(OCR), 표, 차트, 도식, 손글씨까지 추출 후 분석
- 텍스트 + 이미지: 둘 다 종합

## 카드 타입과 레이아웃
cover: bold, number, question, split, minimal
topic: centered, index, qa, definition, compare
list: numbered, check, desc, icon, timeline
highlight: quote, stat, emphasis, pull, tip
closing: next, save, ask, multi, thanks

## 각 레이아웃의 데이터 필드
{{
  "cover/bold": ["eyebrow","title","subtitle","badge"],
  "cover/number": ["number","eyebrow","title","subtitle"],
  "cover/question": ["title","subtitle"],
  "cover/split": ["eyebrow","badge","title","subtitle"],
  "cover/minimal": ["eyebrow","title","badge"],
  "topic/centered": ["label","title","body"],
  "topic/index": ["number","label","title","body"],
  "topic/qa": ["question","answer"],
  "topic/definition": ["term","pron","body"],
  "topic/compare": ["compareTitle","labelA","titleA","bodyA","labelB","titleB","bodyB"],
  "list/numbered": ["title","sub","items"],
  "list/check": ["title","sub","items"],
  "list/desc": ["title","descItems"],
  "list/icon": ["title","sub","items"],
  "list/timeline": ["title","timelineItems"],
  "highlight/quote": ["quote","attribution"],
  "highlight/stat": ["statPre","stat","statUnit","statLabel","statNote"],
  "highlight/emphasis": ["emphPre","emphasis","emphPost"],
  "highlight/pull": ["quote","attribution"],
  "highlight/tip": ["tipLabel","tipBody"],
  "closing/next": ["eyebrow","title","account","hashtag"],
  "closing/save": ["title","saveReason","account","hashtag"],
  "closing/ask": ["eyebrow","askQuestion","askCta","account","hashtag"],
  "closing/multi": ["eyebrow","title","account","hashtag"],
  "closing/thanks": ["title","subtitle","account","hashtag"]
}}

## 필드 의미
- eyebrow: 짧은 영문 라벨 (대문자, 20자 이내)
- title/subtitle: 한국어. 14자 근처에서 \\n 줄바꿈
- body: 1-2문장 (80자 이내)
- items[]: 3-5개, 각 25자 이내
- descItems[]: {{title, desc}} 3-4개
- timelineItems[]: {{label, content}} 3-5개
- stat: 원문/이미지에 실제 숫자 있을 때만
- account: "@핸들"
- hashtag: "#태그1 #태그2" 7-10개 — 모든 closing 레이아웃에 반드시 포함 (캡션 자동 생성에 사용. 카드 표시는 'next' 레이아웃 외에는 안 됨)

## 규칙
1. 첫=cover, 마지막=closing
2. 5-7장
3. 한 카드 한 핵심
4. 원문/이미지에 숫자 없으면 stat 금지
5. 톤: {tone}
6. 테마 추천 (navy, mono, mint, sand, berry, ocean, sunny, tech, paper, lav)
7. **레이아웃별 필드 엄수** — 위 "각 레이아웃의 데이터 필드" 에 정의된 필드만 data 에 포함. 다른 필드는 절대 추가 금지.
   예: highlight/quote 이면 data 에는 quote, attribution 두 키만. stat/statPre/emphasis 등 다른 키 추가 금지.
8. **레이아웃 선택** — 카드의 핵심이 큰 숫자 1개(통계·예측) 면 반드시 highlight/stat 를 골라라.
   "포춘 500대 기업당 15만개 운영 예상" 같이 숫자가 주인공이면 highlight/quote 가 아닌 highlight/stat.
9. **길이 제한 엄수** — 긴 단어가 들어가면 카드 밖으로 밀려나므로 다음을 지켜라:
   - quote/emphasis: 한 줄 12자 이하씩, 전체 35자 이하. 긴 문장은 \\n 으로 줄바꿈.
   - title/subtitle: 한 줄 14자 이하, \\n 으로 끊어쓰기. 한 줄 안에 긴 영문/숫자/혼합 토큰(예: "15만개+", "AI에이전트") 두 개 이상 금지.
   - stat: 8자 이하 (예: "15만개+", "13%").
   - body: 한 줄당 25자, 총 80자 이하.

## 출력
오직 JSON. 마크다운 금지:
{{
  "theme": "navy",
  "cards": [{{ "type": "cover", "layout": "bold", "data": {{...}} }}, ...]
}}"""


def extract_json(text: str) -> dict:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("응답에서 JSON 객체를 찾지 못함")
    return json.loads(cleaned[start:end + 1])


def _extract_card_corpus(session_data: dict) -> str:
    """카드 전체에서 캡션 생성용 텍스트 코퍼스 추출."""
    cards = session_data.get("cards") or []
    blocks: list[str] = []
    for c in cards:
        d = c.get("data") or {}
        bits: list[str] = []
        for k in ("eyebrow", "title", "subtitle", "body", "quote", "attribution",
                  "emphasis", "emphPre", "emphPost", "stat", "statLabel", "statNote",
                  "tipLabel", "tipBody", "question", "answer", "term",
                  "compareTitle", "labelA", "titleA", "bodyA", "labelB", "titleB", "bodyB",
                  "askQuestion", "askCta", "saveReason"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                bits.append(v.replace("\n", " ").strip())
        for arr_key in ("items", "descItems", "timelineItems"):
            arr = d.get(arr_key)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, str) and item.strip():
                        bits.append(item.strip())
                    elif isinstance(item, dict):
                        for v in item.values():
                            if isinstance(v, str) and v.strip():
                                bits.append(v.strip())
        if bits:
            label = f"{c.get('type','?')}/{c.get('layout','?')}"
            blocks.append(f"[{label}] {' / '.join(bits)}")
    return "\n".join(blocks)


def generate_viral_caption(session_data: dict, max_len: int = 2100) -> str:
    """Claude 로 인스타 바이럴 최적화 캡션 한 번에 생성.

    구성: 후크 → 본문 이모지 불릿 → CTA → 점3개 구분선 → 해시태그 25-30개(대중/중간/니치 혼합).
    실패 시 빈 문자열 (호출부가 build_auto_caption fallback).
    """
    if not claude:
        return ""
    corpus = _extract_card_corpus(session_data)
    if not corpus:
        return ""

    brand = session_data.get("brand") or DEFAULT_BRAND
    handle = brand.get("handle") or "@aiacademy"

    user_prompt = f"""당신은 100만 팔로워 인스타 카드뉴스 계정 운영자입니다. 아래 카드뉴스로 **바이럴 최적화된 캡션**을 작성하세요.

[카드 원문]
{corpus[:2200]}

[목표]
- 도달·저장·공유 극대화
- 첫 125자 안에 스크롤을 멈추게 만드는 후크

[구조 — 정확히 이 순서]
1) **후크 1줄** (~50자) — 호기심/놀라움/숫자/질문 중 하나. 시작에 이모지 1개. 인삿말 절대 금지.
2) 빈 줄
3) **본문 3-5줄** — 카드 핵심 포인트. 각 줄 앞에 ✓ / 🔥 / 💡 / 📊 / ⚡ 같은 이모지(같은 거 반복 금지). 한 줄 30자 이내.
4) 빈 줄
5) **CTA 2줄** — 첫 줄: 저장/공유 유도("저장해두고 다시 보세요 🔖" 풍). 둘째 줄: "{handle} 팔로우하면 매일 AI 인사이트" 풍.
6) 빈 줄, `·`, `·`, `·` (점 한 줄에 하나씩 3줄)
7) 빈 줄
8) **해시태그 25-30개** 한 줄에 공백 구분. 다음 비율로 섞기:
   - 대중적 (#AI #테크 #인공지능 같이 검색량 많음) 30%
   - 중간 (#스타트업 #챗GPT #생성형AI 같이 명확한 카테고리) 40%
   - 니치 (카드 핵심 키워드 기반 구체 태그) 30%
   각 태그는 공백 없이 한 토큰. 한글 위주, 영문 키워드 일부 OK.

[금지]
- 마크다운/코드블록/번호목록
- "안녕하세요" "여러분" 같은 인삿말
- 해시태그 30개 초과
- 전체 {max_len}자 초과

캡션 본문만 출력. 다른 설명·서두 금지."""

    try:
        resp = claude.messages.create(
            model=MODEL,
            max_tokens=1800,
            messages=[{"role": "user", "content": user_prompt}],
        )
        out = (resp.content[0].text or "").strip()
        # 코드블록 래퍼가 들어오면 벗기기
        if out.startswith("```"):
            out = out.strip("`").lstrip("\n")
            if out.lower().startswith("text\n"):
                out = out[5:]
        if len(out) > max_len:
            out = out[:max_len].rstrip()
        return out
    except Exception:
        return ""


def ensure_viral_caption(session_id: str, session_data: dict) -> str:
    """세션 캡션 보장 — meta.viral_caption 캐시 → Claude 생성 후 저장."""
    meta = session_data.get("meta") or {}
    cached = (meta.get("viral_caption") or "").strip()
    if cached:
        return cached
    generated = generate_viral_caption(session_data)
    if not generated:
        return ""
    meta = dict(meta)
    meta["viral_caption"] = generated
    session_data["meta"] = meta
    try:
        path = SESSIONS_DIR / f"{session_id}.json"
        path.write_text(json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return generated


def _extract_session_hashtag(session_data: dict) -> str:
    """카드 안 hashtag 필드 또는 meta.auto_hashtag 캐시 추출 (build_auto_caption fallback 용)."""
    cards = session_data.get("cards") or []
    for c in cards:
        d = c.get("data") or {}
        if d.get("hashtag"):
            return d["hashtag"].strip()
    meta = session_data.get("meta") or {}
    return (meta.get("auto_hashtag") or "").strip()


def build_auto_caption(session_data: dict, max_len: int = 2100,
                       fallback_hashtag: str = "") -> str:
    """단순 fallback 캡션 빌더 — Claude 미가용 시. 형식: cover 제목/부제 + 본문요약 + 해시태그."""
    cards = session_data.get("cards") or []
    parts: list[str] = []

    cover = next((c for c in cards if c.get("type") == "cover"), None)
    if cover:
        d = cover.get("data") or {}
        title = (d.get("title") or "").replace("\n", " ").strip()
        subtitle = (d.get("subtitle") or "").replace("\n", " ").strip()
        if title:
            parts.append(title)
        if subtitle:
            parts.append(subtitle)

    body_lines: list[str] = []
    for c in cards:
        if c.get("type") not in ("topic", "list", "highlight"):
            continue
        d = c.get("data") or {}
        for k in ("title", "body", "quote", "emphasis", "tipBody", "question", "answer", "term"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                body_lines.append(v.replace("\n", " ").strip())
                break
    if body_lines:
        parts.append("")
        parts.extend(body_lines[:6])

    hashtag = _extract_session_hashtag(session_data) or (fallback_hashtag or "").strip()
    caption = "\n".join(parts).strip()
    if hashtag:
        budget = max_len - len(hashtag) - 2
        if len(caption) > budget:
            caption = caption[:budget].rstrip() + "…"
        caption = f"{caption}\n\n{hashtag}" if caption else hashtag
    elif len(caption) > max_len:
        caption = caption[:max_len].rstrip() + "…"
    return caption


def generate_cards(text: str, tone: str = DEFAULT_TONE, brand: dict = None) -> dict:
    if not claude:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    brand = brand or DEFAULT_BRAND
    if not text or len(text.strip()) < 10:
        raise ValueError("입력 텍스트가 너무 짧음")

    user_text = (
        f"다음 내용을 카드뉴스로 만들어주세요:\n\n{text}\n\n"
        f"계정 핸들: {brand['handle']}\n브랜드명: {brand['name']}"
    )
    response = claude.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=build_system_prompt(tone),
        messages=[{"role": "user", "content": user_text}],
    )
    return extract_json(response.content[0].text)


def save_generated_session(session_id: str, result: dict, source_text: str,
                           brand: dict, meta: dict = None) -> Path:
    cards = result.get("cards", [])
    for i, c in enumerate(cards):
        c["id"] = c.get("id") or f"{int(time.time() * 1000)}_{i}"
    studio_data = {
        "theme": result.get("theme", "navy"),
        "brand": brand,
        "cards": cards,
        "activeId": cards[0]["id"] if cards else None,
        "lastSourceText": source_text,
        "createdAt": time.time(),
        "meta": meta or {},
    }
    path = SESSIONS_DIR / f"{session_id}.json"
    path.write_text(json.dumps(studio_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ============================================================
# 데일리싱크 DB 접근 (read-only)
# ============================================================
def dailysync_conn():
    if not Path(DAILYSYNC_DB_PATH).exists():
        abort(503, f"데일리싱크 DB 없음: {DAILYSYNC_DB_PATH}")
    # read-only URI
    uri = f"file:{DAILYSYNC_DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _parse_json_field(v, default):
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


# ============================================================
# 라우트 — 스튜디오
# ============================================================
@app.route("/")
def index():
    if not STUDIO_HTML.exists():
        return f"cardnews_studio.html 파일이 없어요. {STUDIO_HTML} 위치에 두세요.", 500
    html = STUDIO_HTML.read_text(encoding="utf-8")
    html = html.replace(
        "</body>",
        '<script src="/static/publish_progress.js"></script>\n'
        + BOT_INTEGRATION_SCRIPT + "\n</body>"
    )
    return Response(html, mimetype="text/html")


@app.route("/auto")
def auto_index():
    if not AUTO_HTML.exists():
        return f"auto_studio.html 파일이 없어요. {AUTO_HTML} 위치에 두세요.", 500
    return Response(AUTO_HTML.read_text(encoding="utf-8"), mimetype="text/html")


# ============================================================
# 라우트 — 세션
# ============================================================
@app.route("/api/sessions")
def api_list():
    files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    items = []
    for f in files[:50]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            cover = next((c for c in data.get("cards", []) if c.get("type") == "cover"), None)
            title = (cover["data"].get("title") or "").split("\n")[0][:50] if cover else f.stem
            items.append({
                "id": f.stem,
                "title": title,
                "mtime": f.stat().st_mtime,
                "cardCount": len(data.get("cards", [])),
                "theme": data.get("theme", "navy"),
                "meta": data.get("meta", {}),
            })
        except Exception:
            items.append({"id": f.stem, "title": f.stem, "mtime": f.stat().st_mtime})
    return jsonify(items)


@app.route("/api/sessions/<session_id>")
def api_get(session_id):
    path = safe_session_path(session_id)
    if not path.exists():
        abort(404, "세션 없음")
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


@app.route("/api/sessions/<session_id>", methods=["PUT"])
def api_save(session_id):
    path = safe_session_path(session_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, "잘못된 JSON")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete(session_id):
    path = safe_session_path(session_id)
    if path.exists():
        path.unlink()
    # 관련 업로드도 같이 정리
    up_dir = UPLOADS_DIR / session_id
    if up_dir.exists():
        for f in up_dir.glob("*"):
            try:
                f.unlink()
            except Exception:
                pass
        try:
            up_dir.rmdir()
        except Exception:
            pass
    return jsonify({"ok": True})


# ============================================================
# 라우트 — Claude 프록시
# ============================================================
@app.route("/api/claude", methods=["POST"])
def api_claude():
    if not claude:
        return jsonify({"error": "ANTHROPIC_API_KEY 미설정 (.env 확인)"}), 500
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "잘못된 JSON"}), 400
    try:
        response = claude.messages.create(
            model=payload.get("model", MODEL),
            max_tokens=payload.get("max_tokens", 1000),
            system=payload.get("system", ""),
            messages=payload.get("messages", []),
        )
        return jsonify({
            "content": [{"type": b.type, "text": getattr(b, "text", "")} for b in response.content],
            "model": response.model,
            "usage": {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens},
        })
    except anthropic.APIStatusError as e:
        return jsonify({"error": f"Anthropic API 오류: {e.message}"}), e.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# 라우트 — 데일리싱크 연동
# ============================================================
@app.route("/api/dailysync/clusters")
def api_dailysync_clusters():
    """클러스터 목록.
    쿼리:
      date=YYYY-MM-DD     해당 first_shown_date
      since=YYYY-MM-DD    이후
      min_importance=N    최소 중요도 (1-5)
      category=...        카테고리 한글 (categories JSON 에 contains)
      saved=1             saved_at NOT NULL 만 (북마크)
      include_hidden=1    hidden_at NOT NULL 포함 (기본 제외)
      limit=N             기본 50, 최대 200
      offset=N            페이지네이션 (기본 0)
      sort=score|recent|importance  데일리싱크와 동일 기본값=score (중요도+매체수)
    응답:
      { "items": [...], "total": N, "limit": N, "offset": N, "has_more": bool }
    """
    args = request.args
    date_str = args.get("date")
    since = args.get("since")
    min_importance = args.get("min_importance", type=int)
    category = args.get("category")
    saved_only = args.get("saved") == "1"
    include_hidden = args.get("include_hidden") == "1"
    limit = min(int(args.get("limit", 50)), 200)
    offset = max(0, int(args.get("offset", 0)))
    sort_mode = args.get("sort", "score")

    base_sql = [
        "FROM clusters WHERE summary_ko IS NOT NULL",
    ]
    params: list = []
    if date_str:
        base_sql.append("AND first_shown_date = ?")
        params.append(date_str)
    if since:
        base_sql.append("AND (first_shown_date >= ? OR first_shown_date IS NULL)")
        params.append(since)
    if min_importance is not None:
        base_sql.append("AND importance >= ?")
        params.append(min_importance)
    if saved_only:
        base_sql.append("AND saved_at IS NOT NULL")
    if not include_hidden:
        base_sql.append("AND hidden_at IS NULL")

    # 정렬 — 데일리싱크 메인과 동일한 score 기본값 (importance DESC + 매체수 DESC + 최근)
    if sort_mode == "recent":
        order = "ORDER BY COALESCE(saved_at, created_at) DESC, id DESC"
    elif sort_mode == "importance":
        order = "ORDER BY importance DESC, id DESC"
    else:
        # score — 매체수 sub-query 로 계산 (단순화: importance 가중 + n_articles 가중)
        order = (
            "ORDER BY "
            "(importance * 10 + "
            " (SELECT COUNT(DISTINCT source_id) FROM articles WHERE cluster_id=clusters.id) * 5"
            ") DESC, "
            "COALESCE(saved_at, created_at) DESC, id DESC"
        )

    con = dailysync_conn()
    try:
        # 전체 count
        total = con.execute(
            "SELECT COUNT(*) " + " ".join(base_sql), params
        ).fetchone()[0]

        sql = (
            "SELECT id, topic, summary_ko, agreed_facts, divergences, categories, "
            "importance, hidden_at, saved_at, first_shown_date, created_at "
            + " ".join(base_sql)
            + " " + order
            + f" LIMIT {limit} OFFSET {offset}"
        )
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    items = []
    for r in rows:
        cats = _parse_json_field(r["categories"], [])
        if category and category not in cats:
            continue
        items.append({
            "id": r["id"],
            "topic": r["topic"],
            "summary": (r["summary_ko"] or "")[:300],
            "categories": cats,
            "importance": r["importance"],
            "saved": r["saved_at"] is not None,
            "hidden": r["hidden_at"] is not None,
            "date": r["first_shown_date"],
        })
    return jsonify({
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(items)) < total,
    })


@app.route("/api/dailysync/cluster/<int:cluster_id>")
def api_dailysync_cluster(cluster_id: int):
    """클러스터 상세 + 원기사 목록."""
    con = dailysync_conn()
    try:
        c = con.execute(
            "SELECT id, topic, summary_ko, agreed_facts, divergences, categories, importance, "
            "       hidden_at, saved_at, first_shown_date, created_at "
            "FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        if not c:
            abort(404, "클러스터 없음")
        arts = con.execute(
            "SELECT a.id, a.title, a.description, a.url, a.published_at, s.name AS source_name "
            "FROM articles a LEFT JOIN sources s ON a.source_id = s.id "
            "WHERE a.cluster_id = ? ORDER BY a.published_at DESC LIMIT 20",
            (cluster_id,),
        ).fetchall()
    finally:
        con.close()

    return jsonify({
        "id": c["id"],
        "topic": c["topic"],
        "summary": c["summary_ko"],
        "agreed_facts": _parse_json_field(c["agreed_facts"], []),
        "divergences": _parse_json_field(c["divergences"], []),
        "categories": _parse_json_field(c["categories"], []),
        "importance": c["importance"],
        "saved": c["saved_at"] is not None,
        "hidden": c["hidden_at"] is not None,
        "date": c["first_shown_date"],
        "articles": [
            {
                "id": a["id"],
                "title": a["title"],
                "description": a["description"],
                "url": a["url"],
                "published_at": a["published_at"],
                "source": a["source_name"],
            }
            for a in arts
        ],
    })


@app.route("/api/dailysync/categories")
def api_dailysync_categories():
    """최근 1000개 클러스터에서 카테고리 빈도 집계."""
    con = dailysync_conn()
    try:
        rows = con.execute(
            "SELECT categories FROM clusters WHERE summary_ko IS NOT NULL "
            "AND hidden_at IS NULL ORDER BY id DESC LIMIT 1000"
        ).fetchall()
    finally:
        con.close()
    counts = {}
    for r in rows:
        for cat in _parse_json_field(r["categories"], []):
            counts[cat] = counts.get(cat, 0) + 1
    items = [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]
    return jsonify(items)


# ============================================================
# 라우트 — 자동 생성
# ============================================================
def _compose_source_text(source_type: str, body: dict) -> tuple[str, dict]:
    """source_type 별로 Claude 에 줄 입력 텍스트 생성. (text, meta) 반환."""
    if source_type == "cluster":
        cluster_id = body.get("cluster_id")
        if not cluster_id:
            raise ValueError("cluster_id 필요")
        con = dailysync_conn()
        try:
            c = con.execute(
                "SELECT topic, summary_ko, agreed_facts, divergences, categories FROM clusters WHERE id = ?",
                (int(cluster_id),),
            ).fetchone()
            if not c:
                raise ValueError("클러스터를 찾을 수 없음")
            arts = con.execute(
                "SELECT title, description, url FROM articles WHERE cluster_id = ? "
                "ORDER BY published_at DESC LIMIT 5",
                (int(cluster_id),),
            ).fetchall()
        finally:
            con.close()

        facts = _parse_json_field(c["agreed_facts"], [])
        divs = _parse_json_field(c["divergences"], [])
        cats = _parse_json_field(c["categories"], [])

        parts = [
            f"[오늘의 AI 뉴스]",
            f"주제: {c['topic']}",
            f"카테고리: {', '.join(cats) if cats else 'AI'}",
            "",
            f"요약:\n{c['summary_ko']}",
        ]
        if facts:
            parts.append("\n핵심 사실:")
            parts.extend(f"- {f}" for f in facts[:6])
        if divs:
            parts.append("\n관점 차이:")
            parts.extend(f"- {d}" for d in divs[:4])
        if arts:
            parts.append("\n참고 기사 제목:")
            parts.extend(f"- {a['title']}" for a in arts[:5])

        text = "\n".join(parts)
        meta = {
            "source": "dailysync",
            "cluster_id": int(cluster_id),
            "topic": c["topic"],
            "kind": "daily_news",
        }
        return text, meta

    if source_type == "tooltip":
        tool = (body.get("tool") or "").strip()
        topic = (body.get("topic") or "").strip()
        note = (body.get("note") or "").strip()
        if not tool and not topic:
            raise ValueError("tool 또는 topic 필요")
        parts = [
            "[AI 툴 활용 팁 카드뉴스]",
            f"툴 이름: {tool}" if tool else "",
            f"다룰 주제: {topic}" if topic else "",
            "",
            "이 툴/주제에 대해 실무에서 바로 쓸 수 있는 팁 3-5가지를 카드뉴스로 만들어주세요.",
            "구체적인 사용법, 프롬프트 예시, 활용 시나리오를 포함하세요.",
        ]
        if note:
            parts.append(f"\n추가 요청사항:\n{note}")
        meta = {"source": "tooltip", "tool": tool, "topic": topic, "kind": "tool_tip"}
        return "\n".join(p for p in parts if p), meta

    if source_type == "academy":
        template = body.get("template") or "intro"
        note = (body.get("note") or "").strip()
        TEMPLATES = {
            "intro": (
                "[인공지능 사관학교 교육과정 소개]\n"
                "인공지능 사관학교는 AI 실무 인재 양성 프로그램입니다. "
                "Python 기초부터 머신러닝, 딥러닝, LLM 활용까지 단계별로 학습합니다. "
                "팀 프로젝트 중심 커리큘럼이며, 멘토링과 코드리뷰 문화가 활발합니다. "
                "수료 후에는 포트폴리오와 함께 AI 분야 취업으로 연결됩니다.\n"
                "이 내용을 처음 보는 사람도 흥미를 느낄 수 있게 카드뉴스로 정리해주세요."
            ),
            "curriculum": (
                "[인공지능 사관학교 커리큘럼]\n"
                "1주차: Python 기초·자료구조 / 2-3주차: 데이터 분석 (Pandas, 시각화) / "
                "4-5주차: 머신러닝 (sklearn) / 6-7주차: 딥러닝 (PyTorch) / "
                "8-9주차: NLP·CV 응용 / 10-12주차: 팀 프로젝트 (LLM 활용 서비스 구축)\n"
                "각 주차의 핵심을 한 카드씩 다루는 카드뉴스로 만들어주세요."
            ),
            "review": (
                "[인공지능 사관학교 수강 후기]\n"
                "선배 수강생들의 후기를 종합해 보면 — 코딩 처음이라도 차근차근 따라갈 수 있는 난이도, "
                "팀원과 함께 문제를 푸는 경험이 가장 가치 있었다는 평가, "
                "현직자 멘토링을 통한 실무 감각, 수료 후 포트폴리오의 강력함이 자주 언급됩니다.\n"
                "이런 후기를 신뢰감 있게 카드뉴스로 재구성해주세요."
            ),
            "apply": (
                "[인공지능 사관학교 모집 안내]\n"
                "지원 자격: 만 34세 이하 청년, 코딩 비전공자도 가능. "
                "교육비 무료(국비지원), 훈련수당 지급. "
                "선발 절차: 서류 → 코딩테스트(기초) → 면접. "
                "수료 후 취업 연계 프로그램 제공.\n"
                "지원 망설이는 사람의 마음을 움직일 수 있게 카드뉴스로 만들어주세요."
            ),
        }
        text = TEMPLATES.get(template, TEMPLATES["intro"])
        if note:
            text += f"\n\n추가 요청사항:\n{note}"
        meta = {"source": "academy", "template": template, "kind": "academy"}
        return text, meta

    if source_type == "freeform":
        text = (body.get("text") or "").strip()
        if len(text) < 20:
            raise ValueError("text 가 너무 짧음 (20자 이상)")
        meta = {"source": "freeform", "kind": body.get("kind", "freeform")}
        return text, meta

    raise ValueError(f"알 수 없는 source_type: {source_type}")


@app.route("/api/auto/generate", methods=["POST"])
def api_auto_generate():
    """자동 카드뉴스 생성.
    body: {
      source_type: "cluster" | "tooltip" | "academy" | "freeform",
      ... (각 타입별 필드),
      tone: "친근한 정보 전달",
      brand: { name, handle, footer }
    }
    """
    if not claude:
        return jsonify({"error": "ANTHROPIC_API_KEY 미설정 (.env 확인)"}), 500
    body = request.get_json(silent=True) or {}
    source_type = body.get("source_type") or "freeform"
    tone = body.get("tone") or DEFAULT_TONE
    brand = body.get("brand") or DEFAULT_BRAND

    try:
        source_text, meta = _compose_source_text(source_type, body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        result = generate_cards(text=source_text, tone=tone, brand=brand)
    except anthropic.APIStatusError as e:
        return jsonify({"error": f"Anthropic API 오류: {e.message}"}), e.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    session_id = f"auto_{int(time.time())}"
    meta["tone"] = tone
    meta["generated_at"] = datetime.utcnow().isoformat()
    save_generated_session(session_id, result, source_text, brand, meta=meta)

    cards = result.get("cards", [])
    cover = next((c for c in cards if c.get("type") == "cover"), None)
    title = (cover.get("data", {}).get("title") or "").split("\n")[0] if cover else ""

    return jsonify({
        "ok": True,
        "session_id": session_id,
        "preview_url": f"{SERVER_URL}/?session={session_id}",
        "card_count": len(cards),
        "theme": result.get("theme", "navy"),
        "title": title,
    })


def _extract_json_array(text: str):
    """텍스트에서 JSON 배열만 추출 (코드블럭·인용주석 제거)."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON 배열을 찾지 못함")
    return json.loads(cleaned[start:end + 1])


@app.route("/api/auto/tooltip-suggest", methods=["POST"])
def api_tooltip_suggest():
    """웹 검색으로 '지금 화제인 AI 툴 활용 꿀팁' 주제를 자동 추천.
    body(선택): { count: int=8, focus: "..." }  focus 로 분야 좁히기 가능.
    반환: { suggestions: [{tool, topic, hook, source}] }
    """
    if not claude:
        return jsonify({"error": "ANTHROPIC_API_KEY 미설정 (.env 확인)"}), 500
    body = request.get_json(silent=True) or {}
    try:
        count = int(body.get("count") or 8)
    except (TypeError, ValueError):
        count = 8
    count = max(4, min(count, 12))
    focus = (body.get("focus") or "").strip()

    system = (
        "당신은 AI 툴 트렌드 큐레이터입니다. 한국어 인스타그램/스레드 카드뉴스 'AI 툴 꿀팁' 시리즈 "
        "제작을 위해, 지금 이 시점에 실제로 화제가 되는 AI 툴과 '실무에서 바로 따라 할 수 있는 구체적 "
        "활용 팁' 주제를 추천합니다. 반드시 웹 검색으로 최근 1-2개월 내 최신 정보를 확인하세요. "
        "인스타그램·스레드·유튜브·레딧·X(트위터)·구글에서 실제로 회자되는 것 위주로, 한국 사용자 관심사를 "
        "반영하고, 새로 나온 툴이나 신규 기능도 포함하세요. 너무 일반적인 주제(예: 'ChatGPT 사용법')는 피하고 "
        "구체적이고 따라 하기 쉬운 활용 팁으로 좁히세요."
    )
    focus_line = f"\n특히 이 분야에 집중: {focus}" if focus else ""
    user = (
        f"지금 화제인 AI 툴 활용 꿀팁 {count}개를 추천해줘.{focus_line}\n\n"
        "각 항목 필드:\n"
        "- tool: 툴 이름 (예: ChatGPT, Claude, Midjourney, Gemini, Notion AI, Veo, Suno 등)\n"
        "- topic: 카드뉴스로 만들 구체적 주제 (12~22자, 따라 하기 쉬운 활용법)\n"
        "- hook: 왜 지금 화제인지/끌리는 한 줄 (25자 이내)\n"
        "- source: 어디서 화제인지 대략 (예: 유튜브, 스레드, 레딧, X)\n\n"
        "서로 다른 툴/주제로 다양하게. 오직 JSON 배열만 출력(마크다운·설명 금지):\n"
        '[{"tool":"","topic":"","hook":"","source":""}]'
    )

    try:
        response = claude.messages.create(
            model=MODEL,
            max_tokens=2500,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        )
    except anthropic.APIStatusError as e:
        return jsonify({"error": f"Anthropic API 오류: {e.message}"}), e.status_code
    except Exception as e:
        return jsonify({"error": f"웹 검색 추천 실패: {e}"}), 500

    text = "".join(getattr(b, "text", "") for b in response.content if b.type == "text")
    try:
        suggestions = _extract_json_array(text)
    except Exception:
        return jsonify({"error": "추천 결과 파싱 실패", "raw": text[:500]}), 502

    clean = []
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        tool = (s.get("tool") or "").strip()
        topic = (s.get("topic") or "").strip()
        if not tool and not topic:
            continue
        clean.append({
            "tool": tool,
            "topic": topic,
            "hook": (s.get("hook") or "").strip(),
            "source": (s.get("source") or "").strip(),
        })
    return jsonify({"suggestions": clean[:count]})


# ============================================================
# 라우트 — 기사 이미지 추출 + 프록시 (카드 배경 이미지 기능)
# ============================================================
@app.route("/api/article-images")
def api_article_images():
    """클러스터의 기사들에서 이미지 후보 추출 (og:image + inline img + 옵션 Unsplash).

    쿼리: cluster_id (필수), keyword (옵션, Unsplash 검색용).
    응답: { "images": [{url, source, alt, from, ...}, ...] }
    """
    cluster_id = request.args.get("cluster_id", type=int)
    if not cluster_id:
        return jsonify({"error": "cluster_id required"}), 400
    keyword = request.args.get("keyword", "").strip()
    from article_images import get_cluster_images
    images = get_cluster_images(cluster_id, keyword=keyword)
    # 외부 URL → 우리 프록시 경유 URL 로 변환 (CORS + html2canvas 호환)
    from urllib.parse import quote
    for img in images:
        img["proxy_url"] = f"/img-proxy?url={quote(img['url'], safe='')}"
    return jsonify({"images": images, "count": len(images)})


@app.route("/img-proxy")
def img_proxy():
    """외부 이미지를 우리 도메인을 통해 서빙 (CORS 우회 + 로컬 캐시)."""
    url = request.args.get("url", "")
    if not url or not url.startswith(("http://", "https://")):
        abort(400, "url required")
    from article_images import fetch_image_to_cache
    path = fetch_image_to_cache(url)
    if not path or not path.exists():
        abort(502, "image fetch failed")
    # 확장자 → MIME
    ext = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    resp = send_from_directory(path.parent, path.name, mimetype=mime)
    # html2canvas 가 useCORS=true 로 fetch 할 수 있게
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "public, max-age=604800"  # 7일
    return resp


# ============================================================
# 라우트 — 업로드 (스튜디오에서 렌더링한 PNG 받기)
# ============================================================
@app.route("/api/uploads/<session_id>", methods=["POST"])
def api_upload(session_id):
    """스튜디오가 html2canvas 로 만든 PNG 들을 업로드.
    formdata: files[] (image/png), 인덱스는 파일명 순서대로 0,1,2...
    """
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "파일 없음"}), 400

    out_dir = UPLOADS_DIR / session_id
    out_dir.mkdir(exist_ok=True)
    # 기존 파일 정리
    for f in out_dir.glob("*.png"):
        try:
            f.unlink()
        except Exception:
            pass

    saved = []
    for i, f in enumerate(files):
        path = out_dir / f"{i:02d}.png"
        f.save(path)
        saved.append({
            "index": i,
            "filename": path.name,
            "url": f"{SERVER_URL}/uploads/{session_id}/{path.name}",
        })
    return jsonify({"ok": True, "session_id": session_id, "files": saved})


@app.route("/uploads/<session_id>/<filename>")
def serve_upload(session_id, filename):
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")
    if not re.match(r"^[a-zA-Z0-9_\-.]{1,64}$", filename):
        abort(400, "잘못된 파일명")
    return send_from_directory(UPLOADS_DIR / session_id, filename)


@app.route("/api/uploads/bg/<session_id>", methods=["POST"])
def api_upload_bg(session_id):
    """배경 이미지 직접 업로드. uploads/{sid}/bg/{ts}.{ext} 로 저장.
    카드 PNG glob 와 섞이지 않게 bg/ 서브폴더 사용.
    """
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "파일 없음"}), 400

    fname = (f.filename or "image").rsplit(".", 1)
    ext = (fname[1].lower() if len(fname) > 1 else "png")
    if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
        return jsonify({"error": f"지원하지 않는 확장자: {ext}"}), 400

    bg_dir = UPLOADS_DIR / session_id / "bg"
    bg_dir.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000)}.{ext}"
    path = bg_dir / name
    f.save(path)
    return jsonify({
        "ok": True,
        "url": f"{SERVER_URL}/uploads/{session_id}/bg/{name}",
        "filename": name,
    })


@app.route("/uploads/<session_id>/bg/<filename>")
def serve_upload_bg(session_id, filename):
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")
    if not re.match(r"^[a-zA-Z0-9_\-.]{1,64}$", filename):
        abort(400, "잘못된 파일명")
    return send_from_directory(UPLOADS_DIR / session_id / "bg", filename)


# ============================================================
# 라우트 — Instagram Graph API 발행
# ============================================================
@app.route("/api/instagram/status")
def api_ig_status():
    has_token = bool(IG_ACCESS_TOKEN and IG_USER_ID)
    return jsonify({
        "configured": has_token,
        "user_id_set": bool(IG_USER_ID),
        "token_set": bool(IG_ACCESS_TOKEN),
        "setup_guide": "/auto/instagram-setup",
    })


@app.route("/auto/instagram-setup")
def ig_setup_guide():
    guide_path = ROOT / "INSTAGRAM_SETUP.md"
    if not guide_path.exists():
        return "INSTAGRAM_SETUP.md 없음", 404
    md = guide_path.read_text(encoding="utf-8")
    # 간단 HTML 래핑
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Instagram 셋업</title>
<style>body{{font-family:sans-serif;max-width:780px;margin:40px auto;padding:0 20px;line-height:1.6;color:#222}}
pre{{background:#f4f4f4;padding:12px;border-radius:6px;overflow-x:auto}}
code{{background:#f4f4f4;padding:2px 6px;border-radius:3px}}
h1,h2,h3{{margin-top:1.8em}}</style></head>
<body><pre style="background:none;padding:0;white-space:pre-wrap">{md.replace('<','&lt;')}</pre></body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/api/instagram/publish/<session_id>", methods=["POST"])
def api_ig_publish(session_id):
    """Instagram 캐러셀 발행.
    사전조건:
      - .env 에 IG_ACCESS_TOKEN, IG_USER_ID 설정
      - SERVER_URL 이 외부에서 접근 가능한 https URL (ngrok / cloudflare tunnel)
      - /api/uploads/<session_id> 로 PNG 들이 이미 업로드되어 있어야 함
    body: { caption?: string }
    """
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")
    if not (IG_ACCESS_TOKEN and IG_USER_ID):
        return jsonify({
            "error": "Instagram 토큰이 설정되지 않음. /auto/instagram-setup 참고",
            "setup_guide": f"{SERVER_URL}/auto/instagram-setup",
        }), 503

    up_dir = UPLOADS_DIR / session_id
    pngs = sorted(up_dir.glob("*.png"))
    if not pngs:
        return jsonify({"error": "업로드된 PNG 없음. 먼저 스튜디오에서 '인스타 업로드' 실행"}), 400
    if len(pngs) > 10:
        pngs = pngs[:10]  # IG 캐러셀 최대 10장

    if not SERVER_URL.startswith("https://"):
        return jsonify({
            "error": "SERVER_URL 이 https 가 아님. Instagram Graph API 는 공개 https URL 필요. ngrok/cloudflare tunnel 사용",
        }), 400

    # 캡션 — 명시값 없으면 Claude 바이럴 최적화 캡션, 실패 시 단순 조합 fallback
    body = request.get_json(silent=True) or {}
    caption = (body.get("caption") or "").strip()
    if not caption:
        try:
            session_data = json.loads((SESSIONS_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
            caption = ensure_viral_caption(session_id, session_data)
            if not caption:
                caption = build_auto_caption(session_data)
        except Exception:
            pass
    if not caption:
        caption = IG_DEFAULT_CAPTION

    # Instagram Graph API 호출
    try:
        import requests
    except ImportError:
        return jsonify({"error": "requests 패키지 미설치. pip install requests"}), 500

    GRAPH = "https://graph.facebook.com/v21.0"

    # 1) 각 이미지에 대한 미디어 컨테이너 생성 (is_carousel_item=true)
    children_ids = []
    for png in pngs:
        image_url = f"{SERVER_URL}/uploads/{session_id}/{png.name}"
        r = requests.post(
            f"{GRAPH}/{IG_USER_ID}/media",
            data={
                "image_url": image_url,
                "is_carousel_item": "true",
                "access_token": IG_ACCESS_TOKEN,
            },
            timeout=30,
        )
        if r.status_code >= 400:
            return jsonify({"error": f"이미지 컨테이너 생성 실패: {r.text}"}), 500
        children_ids.append(r.json()["id"])

    # 2) 캐러셀 컨테이너 생성
    r = requests.post(
        f"{GRAPH}/{IG_USER_ID}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(children_ids),
            "caption": caption,
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=30,
    )
    if r.status_code >= 400:
        return jsonify({"error": f"캐러셀 컨테이너 생성 실패: {r.text}"}), 500
    creation_id = r.json()["id"]

    # 3) 발행
    r = requests.post(
        f"{GRAPH}/{IG_USER_ID}/media_publish",
        data={
            "creation_id": creation_id,
            "access_token": IG_ACCESS_TOKEN,
        },
        timeout=30,
    )
    if r.status_code >= 400:
        return jsonify({"error": f"발행 실패: {r.text}"}), 500

    return jsonify({
        "ok": True,
        "media_id": r.json().get("id"),
        "card_count": len(pngs),
    })


# ============================================================
# 라우트 — 캡션 미리보기 (Claude 바이럴 최적화)
# ============================================================
@app.route("/api/sessions/<session_id>/caption-preview", methods=["GET", "POST"])
def api_caption_preview(session_id):
    """발행 모달에서 캡션 프리필용 — Claude 바이럴 최적화 캡션 반환.

    GET  : 캐시 있으면 캐시, 없으면 생성 후 캐시
    POST : 강제 재생성 (캐시 무시)
    실패 시 단순 fallback 캡션.
    """
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return jsonify({"error": "세션 없음"}), 404
    try:
        session_data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"세션 로드 실패: {e}"}), 500

    if request.method == "POST":
        meta = dict(session_data.get("meta") or {})
        meta.pop("viral_caption", None)
        session_data["meta"] = meta
        try:
            path.write_text(json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    caption = ensure_viral_caption(session_id, session_data)
    source = "viral"
    if not caption:
        caption = build_auto_caption(session_data)
        source = "fallback"
    return jsonify({"caption": caption, "source": source, "length": len(caption)})


# ============================================================
# 라우트 — 통합 멀티 플랫폼 발행 (인스타·페북·스레드)
# ============================================================
@app.route("/api/publish/status")
def api_publish_status():
    """각 플랫폼 토큰 설정 여부 + SERVER_URL https 여부."""
    from services import instagram as ig_svc, facebook as fb_svc, threads as th_svc, tiktok as tk_svc
    return jsonify({
        "server_url_https": SERVER_URL.startswith("https://"),
        "server_url": SERVER_URL,
        "platforms": {
            "instagram": {"configured": ig_svc.is_configured()},
            "facebook": {"configured": fb_svc.is_configured()},
            "threads": {"configured": th_svc.is_configured()},
            "tiktok": {"configured": tk_svc.is_configured()},
        },
    })


@app.route("/api/publish/<session_id>", methods=["POST"])
def api_publish_unified(session_id):
    """선택한 플랫폼들에 동시 발행 (부분 실패 허용).

    body: {
      "platforms": ["instagram", "facebook", "threads"],   # 활성화할 것만
      "caption": "...",                                     # 비우면 hashtag 카드에서 추출
    }
    응답: {
      "ok": bool,
      "results": {
        "instagram": {"ok": True, "media_id": "...", "permalink": "..."} | {"ok": False, "error": "..."} | {"ok": False, "skipped": True, "reason": "..."},
        "facebook":  {...},
        "threads":   {...}
      }
    }
    """
    if not SAFE_ID_RE.match(session_id):
        abort(400, "잘못된 세션 ID")

    body = request.get_json(silent=True) or {}
    requested = body.get("platforms") or ["instagram"]
    if isinstance(requested, str):
        requested = [requested]
    requested = [p.lower() for p in requested if p in ("instagram", "facebook", "threads", "tiktok")]
    if not requested:
        return jsonify({"ok": False, "error": "platforms 비어있음"}), 400

    # 업로드된 PNG
    up_dir = UPLOADS_DIR / session_id
    pngs = sorted(up_dir.glob("*.png"))
    if not pngs:
        return jsonify({"ok": False, "error": "PNG 미업로드. 스튜디오에서 'PNG 준비' 먼저"}), 400
    if len(pngs) > 10:
        pngs = pngs[:10]
    image_urls = [f"{SERVER_URL}/uploads/{session_id}/{p.name}" for p in pngs]

    # https 체크 (모든 플랫폼 공통 요구)
    if not SERVER_URL.startswith("https://"):
        return jsonify({
            "ok": False,
            "error": "SERVER_URL 이 https 가 아님. ngrok/cloudflared 로 외부 노출 필요",
        }), 400

    # 캡션 — 명시값 없으면 Claude 바이럴 최적화 캡션, 실패 시 단순 조합 fallback
    caption = (body.get("caption") or "").strip()
    if not caption:
        try:
            session_data = json.loads((SESSIONS_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
            caption = ensure_viral_caption(session_id, session_data)
            if not caption:
                caption = build_auto_caption(session_data)
        except Exception:
            pass

    # 백그라운드 잡 시작 — 즉시 job_id 반환, 폴링은 /api/publish/jobs/<id>
    from services.publish_jobs import start_publish_job
    job_id = start_publish_job(
        session_id=session_id,
        platforms=requested,
        image_urls=image_urls,
        caption=caption,
        card_count=len(pngs),
    )
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "card_count": len(pngs),
        "caption": caption,
        "platforms": requested,
    }), 202


@app.route("/api/publish/jobs/<job_id>")
def api_publish_job_status(job_id: str):
    """발행 잡 진행 상황 폴링."""
    if not re.match(r"^[a-f0-9]{6,32}$", job_id):
        return jsonify({"error": "invalid job_id"}), 400
    from services.publish_jobs import get_job
    j = get_job(job_id)
    if not j:
        return jsonify({"error": "not_found"}), 404
    j.pop("created_at_ts", None)
    return jsonify(j)


# ============================================================
# 라우트 — 자동 스케줄러 상태/제어
# ============================================================
@app.route("/api/auto/scheduler/status")
def api_scheduler_status():
    """스케줄러 가동 여부 + 등록된 잡 다음 실행 시각."""
    from auto_scheduler import get_scheduler
    sched = get_scheduler()
    if sched is None:
        return jsonify({"active": False, "jobs": []})
    jobs = []
    for j in sched.get_jobs():
        jobs.append({
            "id": j.id,
            "name": j.name,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
        })
    return jsonify({"active": True, "jobs": jobs})


@app.route("/api/auto/scheduler/trigger/<job_id>", methods=["POST"])
def api_scheduler_trigger(job_id: str):
    """수동 트리거 (관리자용). job_id: generate_daily | publish_due | refresh_tokens."""
    from auto_scheduler import trigger_job_now
    ok = trigger_job_now(job_id)
    if not ok:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, "job_id": job_id, "triggered": True})


# ============================================================
# 기타
# ============================================================
@app.route("/health")
def health():
    from auto_scheduler import get_scheduler
    sched = get_scheduler()
    return {
        "ok": True,
        "sessions": len(list(SESSIONS_DIR.glob("*.json"))),
        "claude": bool(claude),
        "dailysync_db_exists": Path(DAILYSYNC_DB_PATH).exists(),
        "instagram_configured": bool(IG_ACCESS_TOKEN and IG_USER_ID),
        "scheduler_active": sched is not None,
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"🌐 Cardnews Studio Server")
    print(f"   스튜디오:   http://localhost:{port}")
    print(f"   자동화:     http://localhost:{port}/auto")
    print(f"   세션 폴더:  {SESSIONS_DIR}")
    print(f"   업로드 폴더: {UPLOADS_DIR}")
    print(f"   데일리싱크 DB: {DAILYSYNC_DB_PATH} {'✓' if Path(DAILYSYNC_DB_PATH).exists() else '✗ (없음)'}")
    if claude:
        print(f"   ✓ Claude API 활성")
    else:
        print(f"   ⚠️  ANTHROPIC_API_KEY 미설정 - AI 기능 작동 안 함")
    if IG_ACCESS_TOKEN and IG_USER_ID:
        print(f"   ✓ Instagram 발행 활성")
    else:
        print(f"   ℹ️  Instagram 토큰 미설정 - 발행 비활성 (/auto/instagram-setup)")

    # 자동 스케줄러 가동 (AUTO_SCHEDULER=false 로 끌 수 있음)
    try:
        from auto_scheduler import init_scheduler
        sched = init_scheduler()
        if sched:
            print(f"   ✓ 자동 스케줄러 가동 ({len(sched.get_jobs())}개 잡)")
        else:
            print(f"   ℹ️  자동 스케줄러 미가동 (AUTO_SCHEDULER=false)")
    except Exception as e:
        print(f"   ⚠️  스케줄러 시작 실패: {e}")

    app.run(host=host, port=port, debug=False)
