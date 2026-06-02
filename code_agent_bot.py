"""텔레그램 코딩봇 — 채팅으로 코드 수정·명령 실행·git 동기화.

이 Claude Code 를 구동하는 것과 같은 **Claude Agent SDK** 로, 텔레그램에서 보낸
메시지를 레포에 접근 권한이 있는 에이전트에 전달해 파일 편집/명령/커밋·푸시를
수행하고 결과를 답장한다. 멀티턴(대화 맥락 유지).

⚠️ 보안: 이 봇은 본질적으로 '텔레그램 → 내 PC 원격 코드 실행' 이다.
  - CODE_BOT_ALLOWED_IDS 에 등록된 사용자만 사용 가능 (필수).
  - 치명적 명령(rm -rf /, force push, 파이프 셸 등)은 _DANGER 패턴으로 차단.
  - 봇 토큰이 유출되면 PC 가 장악될 수 있으니 토큰·허용ID를 철저히 관리할 것.
  - PC(또는 서버)에서 이 프로세스가 떠 있어야 동작한다.

사전 준비:
  - pip install claude-agent-sdk python-telegram-bot
  - Claude Code CLI 설치 + 인증 (이미 사용 중이면 OK), ANTHROPIC_API_KEY (.env)
  - @BotFather 로 새 봇 생성 → 토큰

ENV (.env):
  CODE_BOT_TOKEN        @BotFather 토큰 (필수, bot.py 의 TELEGRAM_BOT_TOKEN 과 별도)
  CODE_BOT_ALLOWED_IDS  허용 텔레그램 사용자 ID, 쉼표구분 (필수). /whoami 로 확인
  CODE_BOT_CWD          작업 레포 경로 (비우면 이 파일이 있는 폴더)

실행: python code_agent_bot.py
"""
import os
import re
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
)
from claude_agent_sdk.types import (
    TextBlock,
    ToolUseBlock,
    PermissionResultAllow,
    PermissionResultDeny,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("code_bot")

TOKEN = os.getenv("CODE_BOT_TOKEN", "").strip()
ALLOWED_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("CODE_BOT_ALLOWED_IDS", "").strip()) if x.isdigit()
}
CWD = (os.getenv("CODE_BOT_CWD", "").strip() or str(Path(__file__).resolve().parent))
TG_MAX = 3900  # 텔레그램 메시지 길이 한도(4096) 여유


def _parse_projects() -> dict:
    """전환 가능한 프로젝트 목록. CODE_BOT_PROJECTS 환경변수로 덮어쓰기 가능.
    형식: 'name=path;name=path' (또는 줄바꿈 구분)."""
    raw = os.getenv("CODE_BOT_PROJECTS", "").strip()
    projs = {}
    if raw:
        for part in re.split(r"[;\n]+", raw):
            if "=" in part:
                name, path = part.split("=", 1)
                if name.strip() and path.strip():
                    projs[name.strip()] = path.strip()
    if not projs:
        projs = {
            "cardnews": r"f:\cardnews_bot\cardnews_bot",
            "dailysync": r"f:\ai-news-digest\ai-news-digest",
            "studiohub": r"f:\studio_app",
        }
    return projs


PROJECTS = _parse_projects()
DEFAULT_CWD = CWD  # 채팅별 미설정 시 기본 작업 폴더

# 치명적 bash 명령 차단 (안전망 — 완전하진 않음)
_DANGER = [
    r"rm\s+-rf?\s+(/|~|\$HOME|\*)",
    r":\(\)\s*\{",                       # fork bomb
    r"\bmkfs\b", r"\bdd\s+if=", r">\s*/dev/sd",
    r"git\s+push\b.*--force\b", r"git\s+push\b.*\s-f\b",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"chmod\s+-R\s+777\s+/",
    r"curl\b[^\n|]*\|\s*(sh|bash)", r"wget\b[^\n|]*\|\s*(sh|bash)",
    r"\bgit\s+reset\s+--hard\b.*origin",
]
_DANGER_RE = [re.compile(p) for p in _DANGER]

# 친근한 도구 라벨 (텔레그램 진행 표시용)
def _tool_label(name: str, inp: dict) -> str:
    if name == "Bash":
        return f"⚙ {(inp.get('command') or '')[:160]}"
    if name in ("Edit", "Write", "NotebookEdit"):
        return f"✏️ {name}: {inp.get('file_path', '')}"
    if name == "Read":
        return f"📖 {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"🔎 {name}: {inp.get('pattern', '')}"
    return f"🔧 {name}"


async def _permission(tool_name: str, inp: dict, context):
    """위험 명령만 차단, 나머지는 허용 (헤드리스 자동 실행)."""
    if tool_name == "Bash":
        cmd = inp.get("command", "") or ""
        for rx in _DANGER_RE:
            if rx.search(cmd):
                return PermissionResultDeny(message=f"차단된 위험 명령 패턴: {rx.pattern}")
    return PermissionResultAllow(updated_input=inp)


def _make_options(cwd: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="acceptEdits",   # 파일 편집 자동 승인, 나머지는 can_use_tool 로 게이트
        can_use_tool=_permission,
        system_prompt=(
            "당신은 텔레그램으로 지시받아 이 저장소에서 코딩 작업을 수행하는 개발 에이전트입니다. "
            "한국어로 간결히 보고하세요. 파일을 직접 수정하고, 필요한 셸 명령을 실행하며, "
            "사용자가 요청하면 git 커밋·푸시까지 합니다. 위험하거나 비가역적인 작업(강제 푸시, 대량 삭제 등)은 "
            "실행 전에 한 줄로 확인 요청하세요. 현재 작업 폴더 밖의 다른 프로젝트는 건드리지 마세요."
        ),
    )


# 채팅별 살아있는 세션 + 작업폴더 + 직렬화 락
_clients: dict[int, ClaudeSDKClient] = {}
_locks: dict[int, asyncio.Lock] = {}
_sessions: dict[int, str] = {}     # chat_id -> 마지막 session_id (참고용)
_chat_cwds: dict[int, str] = {}    # chat_id -> 현재 작업 프로젝트 폴더


def _allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_IDS)


def _cwd_for(chat_id: int) -> str:
    return _chat_cwds.get(chat_id, DEFAULT_CWD)


def _project_name(path: str) -> str:
    norm = os.path.normcase(os.path.abspath(path))
    for name, p in PROJECTS.items():
        if os.path.normcase(os.path.abspath(p)) == norm:
            return name
    return path


async def _drop_client(chat_id: int):
    c = _clients.pop(chat_id, None)
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass
    _sessions.pop(chat_id, None)


async def _get_client(chat_id: int) -> ClaudeSDKClient:
    c = _clients.get(chat_id)
    if c is None:
        cwd = _cwd_for(chat_id)
        c = ClaudeSDKClient(options=_make_options(cwd))
        await c.connect()
        _clients[chat_id] = c
        log.info("new agent session for chat %s (cwd=%s)", chat_id, cwd)
    return c


def _lock(chat_id: int) -> asyncio.Lock:
    return _locks.setdefault(chat_id, asyncio.Lock())


async def _send_chunked(update: Update, text: str):
    text = text.strip()
    if not text:
        return
    for i in range(0, len(text), TG_MAX):
        await update.effective_message.reply_text(text[i:i + TG_MAX])


# ---------------- 명령 ----------------
WELCOME = (
    "🤖 코딩 에이전트 봇\n\n"
    "메시지를 보내면 선택한 프로젝트 폴더에서 코드를 읽고·수정하고·명령을 실행하고 git 동기화까지 합니다.\n\n"
    "명령:\n"
    "/projects — 프로젝트 목록 + 현재 작업 프로젝트\n"
    "/project <이름> — 작업 프로젝트 전환 (예: /project dailysync)\n"
    "/whoami — 내 사용자 ID 확인(허용목록 등록용)\n"
    "/reset — 대화 맥락 초기화(새 세션)\n"
    "/cwd — 현재 작업 폴더 표시\n\n"
    "예) '서버 라우트에 헬스체크 추가하고 커밋해줘', 'git status 보여줘', '방금 변경 푸시해줘'"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text(
            f"⛔ 허용되지 않은 사용자입니다.\n당신의 ID: {update.effective_user.id}\n"
            "이 ID를 .env 의 CODE_BOT_ALLOWED_IDS 에 추가하세요.")
        return
    await update.message.reply_text(WELCOME)


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"사용자 ID: {u.id}\n이름: {u.full_name}\n"
        f"허용됨: {'예' if u.id in ALLOWED_IDS else '아니오'}")


async def cmd_cwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    cid = update.effective_chat.id
    cwd = _cwd_for(cid)
    sid = _sessions.get(cid)
    await update.message.reply_text(
        f"현재 프로젝트: {_project_name(cwd)}\n작업 폴더: {cwd}\n세션: {sid or '(없음)'}")


async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    cur = _cwd_for(update.effective_chat.id)
    curn = os.path.normcase(os.path.abspath(cur))
    lines = ["📂 프로젝트 (/project <이름> 으로 전환):"]
    for name, path in PROJECTS.items():
        active = "✅" if os.path.normcase(os.path.abspath(path)) == curn else "▫️"
        missing = "" if os.path.isdir(path) else "  ⚠️폴더없음"
        lines.append(f"{active} {name} — {path}{missing}")
    await update.message.reply_text("\n".join(lines))


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    if not ctx.args:
        await cmd_projects(update, ctx)
        return
    name = ctx.args[0].strip()
    if name not in PROJECTS:
        await update.message.reply_text(f"'{name}' 프로젝트가 없습니다. /projects 로 목록을 확인하세요.")
        return
    path = PROJECTS[name]
    if not os.path.isdir(path):
        await update.message.reply_text(f"⚠️ 폴더가 존재하지 않습니다: {path}")
        return
    cid = update.effective_chat.id
    await _drop_client(cid)            # 기존 세션 종료 (새 폴더로 새 세션)
    _chat_cwds[cid] = path
    await update.message.reply_text(
        f"📂 작업 프로젝트를 '{name}' 로 전환했어요.\n{path}\n(새 대화 세션으로 시작합니다)")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    await _drop_client(update.effective_chat.id)
    await update.message.reply_text("🔄 대화 맥락을 초기화했어요. 새 세션으로 시작합니다.")


# ---------------- 메인 메시지 처리 ----------------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        await update.message.reply_text(
            f"⛔ 허용되지 않은 사용자입니다. /whoami 로 ID 확인 후 CODE_BOT_ALLOWED_IDS 에 등록하세요.")
        return

    cid = update.effective_chat.id
    prompt = (update.message.text or "").strip()
    if not prompt:
        return

    lock = _lock(cid)
    if lock.locked():
        await update.message.reply_text("⏳ 직전 작업을 처리 중입니다. 끝나면 보내주세요.")
        return

    async with lock:
        status = await update.message.reply_text("🤖 작업 중…")
        await ctx.bot.send_chat_action(cid, ChatAction.TYPING)
        steps: list[str] = []
        texts: list[str] = []
        try:
            client = await _get_client(cid)
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            if block.text.strip():
                                texts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            steps.append(_tool_label(block.name, block.input))
                            # 진행 상황을 상태 메시지에 갱신(너무 잦지 않게 최근 8개만)
                            try:
                                await status.edit_text("🤖 작업 중…\n" + "\n".join(steps[-8:])[:TG_MAX])
                            except Exception:
                                pass
                elif isinstance(msg, ResultMessage):
                    _sessions[cid] = msg.session_id
                    cost = f" · ${msg.total_cost_usd:.3f}" if msg.total_cost_usd else ""
                    foot = f"\n\n— {'❌ 오류' if msg.is_error else '✓ 완료'} ({msg.num_turns}턴{cost})"
                    final = ("\n".join(texts)).strip() or "(텍스트 응답 없음)"
                    try:
                        await status.delete()
                    except Exception:
                        pass
                    await _send_chunked(update, final + foot)
        except Exception as e:
            log.exception("agent turn failed")
            try:
                await status.edit_text(f"❌ 실패: {e}")
            except Exception:
                await update.message.reply_text(f"❌ 실패: {e}")


def main():
    if not TOKEN:
        raise SystemExit("CODE_BOT_TOKEN 미설정 (.env). @BotFather 에서 봇 생성 후 토큰을 넣으세요.")
    if not ALLOWED_IDS:
        # 허용ID가 없으면 코딩은 전부 차단되지만, /whoami 로 본인 ID를 확인할 수 있게 봇은 띄운다.
        log.warning("CODE_BOT_ALLOWED_IDS 미설정 — 모든 코딩요청 차단됨. 봇에 /whoami 보내 ID 확인 후 "
                    ".env 의 CODE_BOT_ALLOWED_IDS 에 등록하고 재실행하세요.")
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY 미설정 — Claude Code CLI 인증이 없으면 동작하지 않을 수 있습니다.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("코딩봇 시작 — 기본cwd=%s, 프로젝트=%s, 허용ID=%s",
             DEFAULT_CWD, list(PROJECTS.keys()), sorted(ALLOWED_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
