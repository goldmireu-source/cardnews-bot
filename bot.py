"""
Cardnews Telegram Bot
=====================
텔레그램에 텍스트/이미지/둘 다 보내면 Claude API로 카드뉴스 JSON을 생성합니다.
이미지는 OCR + 차트·표·도식 분석까지 자동.

실행:
    python bot.py

환경변수 (.env):
    TELEGRAM_BOT_TOKEN   필수
    ANTHROPIC_API_KEY    필수
    SERVER_URL           선택 (기본 http://localhost:5050)
    ALLOWED_USERS        선택 (콤마구분 user_id)
    MODEL                선택 (기본 claude-sonnet-4-20250514)
"""

import os
import json
import time
import base64
import asyncio
import logging
from io import BytesIO
from pathlib import Path
from collections import defaultdict

from dotenv import load_dotenv

import anthropic
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

# ============================================================
# 설정
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:5050").rstrip("/")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
MODEL = os.getenv("MODEL_FAST", "claude-haiku-4-5-20251001")

DEFAULT_BRAND = {
    "name": "인공지능사관학교 서포터즈",
    "handle": "@dailysync_ai",
    "footer": "인공지능사관학교 서포터즈",
}

SESSIONS_DIR = Path(__file__).parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

TONE_MAP = {
    "[MZ]":   "MZ세대 톤, 위트있게",
    "[감성]": "감성적이고 따뜻하게",
    "[진지]": "진지하고 신뢰감 있는 톤",
    "[핵심]": "실용적이고 핵심만",
    "[친근]": "친근한 정보 전달",
}
DEFAULT_TONE = "친근한 정보 전달"

MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5MB (Claude API 한계)
MAX_IMAGES_PER_REQUEST = 5
MEDIA_GROUP_WAIT_SEC = 1.5         # 앨범 수집 대기

# ============================================================
# 로깅
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("cardnews-bot")

# ============================================================
# Claude
# ============================================================
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def build_system_prompt(tone: str) -> str:
    """스튜디오 v4와 동일한 system prompt."""
    return f"""당신은 인스타그램 카드뉴스 전문 에디터입니다. 100만 팔로워 계정 운영 경험으로, 사용자 입력을 내용 분량에 맞게 카드뉴스로 재구성합니다.

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
  "closing/save": ["title","saveReason","account"],
  "closing/ask": ["eyebrow","askQuestion","askCta","account"],
  "closing/multi": ["eyebrow","title","account"],
  "closing/thanks": ["title","subtitle","account"]
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
- hashtag: "#태그1 #태그2" 5-10개

## 규칙
1. 첫=cover, 마지막=closing
2. 카드 수: 내용 분량에 맞게 결정 (짧은 단순 내용 → 4-5장, 보통 → 6-8장, 긴 PDF·상세 자료 → 9-15장). 내용을 억지로 줄여 누락하지 말 것. 핵심 포인트가 있다면 각각 별도 카드로.
3. 한 카드 한 핵심
4. 원문/이미지에 숫자 없으면 stat 금지
5. 톤: {tone}
6. 테마 추천 (navy, mono, mint, sand, berry, ocean, sunny, tech, paper, lav)

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


def detect_image_type(data: bytes) -> str:
    """바이너리 헤더로 이미지 타입 추정."""
    if data[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if len(data) > 12 and data[8:12] == b"WEBP": return "image/webp"
    return "image/jpeg"


def generate_cards(text: str = "", images: list = None,
                   tone: str = DEFAULT_TONE, brand: dict = None) -> dict:
    """Claude 호출. text/images 둘 중 하나는 필수."""
    if not claude:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    images = images or []
    brand = brand or DEFAULT_BRAND
    if not text and not images:
        raise ValueError("텍스트 또는 이미지 중 하나는 필요")

    system = build_system_prompt(tone)

    content = []
    for img in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]},
        })

    if text and images:
        user_text = (
            f"첨부된 {len(images)}장의 이미지와 아래 텍스트를 종합 분석해서 카드뉴스를 만들어주세요.\n\n"
            f"[텍스트]\n{text}\n\n"
            f"계정 핸들: {brand['handle']}\n브랜드명: {brand['name']}"
        )
    elif images:
        user_text = (
            f"첨부된 {len(images)}장의 이미지를 분석해서 카드뉴스로 만들어주세요. "
            f"이미지 안의 텍스트(OCR), 표, 차트, 도식을 모두 활용하세요.\n\n"
            f"계정 핸들: {brand['handle']}\n브랜드명: {brand['name']}"
        )
    else:
        user_text = (
            f"다음 내용을 카드뉴스로 만들어주세요:\n\n{text}\n\n"
            f"계정 핸들: {brand['handle']}\n브랜드명: {brand['name']}"
        )
    content.append({"type": "text", "text": user_text})

    log.info(f"Claude 호출 (tone={tone}, text_len={len(text)}, images={len(images)})")
    response = claude.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return extract_json(response.content[0].text)

# ============================================================
# 세션
# ============================================================
def save_session(session_id: str, result: dict, source_text: str, brand: dict) -> Path:
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
    }
    path = SESSIONS_DIR / f"{session_id}.json"
    path.write_text(json.dumps(studio_data, ensure_ascii=False, indent=2))
    log.info(f"세션 저장: {path}")
    return path


def list_sessions(limit: int = 10):
    files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def session_summary(session_path: Path) -> str:
    try:
        data = json.loads(session_path.read_text())
        cover = next((c for c in data.get("cards", []) if c.get("type") == "cover"), None)
        if cover:
            title = (cover["data"].get("title") or "").split("\n")[0]
            return title[:50] or session_path.stem
    except Exception:
        pass
    return session_path.stem

# ============================================================
# 권한 / 톤 파싱
# ============================================================
def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or str(user_id) in ALLOWED_USERS


def parse_tone(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    for tag, tone in TONE_MAP.items():
        if text.startswith(tag):
            return tone, text[len(tag):].strip()
    return DEFAULT_TONE, text

# ============================================================
# 미디어 그룹 버퍼 (앨범으로 여러 사진 보냈을 때)
# ============================================================
media_groups = defaultdict(lambda: {
    "photos": [], "caption": "", "timer": None, "status_msg": None,
})

# ============================================================
# 명령어
# ============================================================
WELCOME = """🎴 Cardnews Bot

📝 텍스트, 🖼 이미지, 또는 둘 다 메시지로 보내주세요. 카드뉴스 5-7장으로 자동 생성됩니다.

이미지는 안의 텍스트(OCR)·표·도식까지 읽어서 분석해요.

명령어
/last - 마지막 미리보기
/list - 최근 세션 목록
/whoami - 내 user_id 확인
/help - 도움말

톤 태그 (메시지 또는 캡션 시작에)
[MZ] [감성] [진지] [핵심] [친근]

예: [MZ] 인공지능 사관학교 첫 주에 배운 것들..."""


async def cmd_start(update: Update, ctx): await update.message.reply_text(WELCOME)
async def cmd_help(update: Update, ctx):  await update.message.reply_text(WELCOME)


async def cmd_whoami(update: Update, ctx):
    u = update.effective_user
    await update.message.reply_text(
        f"user_id: {u.id}\nusername: @{u.username or '없음'}\n\n"
        f"ALLOWED_USERS에 추가하려면 .env에:\nALLOWED_USERS={u.id}"
    )


async def cmd_list(update: Update, ctx):
    if not is_allowed(update.effective_user.id): return
    sessions = list_sessions()
    if not sessions:
        await update.message.reply_text("아직 만든 카드뉴스가 없어요")
        return
    lines = ["📚 최근 카드뉴스\n"]
    for s in sessions:
        lines.append(f"• {s.stem} · {session_summary(s)}")
    lines.append(f"\n미리보기: {SERVER_URL}/?session=<ID>")
    await update.message.reply_text("\n".join(lines))


async def cmd_last(update: Update, ctx):
    if not is_allowed(update.effective_user.id): return
    sessions = list_sessions(limit=1)
    if not sessions:
        await update.message.reply_text("아직 만든 카드뉴스가 없어요")
        return
    sid = sessions[0].stem
    await update.message.reply_text(f"마지막 카드뉴스:\n{SERVER_URL}/?session={sid}")

# ============================================================
# 메시지 핸들러
# ============================================================
async def on_text(update: Update, ctx):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ 권한 없음 (/whoami로 ID 확인)")
        return

    text = (update.message.text or "").strip()
    if len(text) < 20:
        await update.message.reply_text("내용이 너무 짧아요 (최소 20자)")
        return

    tone, cleaned = parse_tone(text)
    status = await update.message.reply_text(f"🎨 카드뉴스 생성 중... (톤: {tone})")
    await process_and_respond(cleaned, [], tone, status)


async def on_photo(update: Update, ctx):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ 권한 없음")
        return

    msg = update.message
    if msg.media_group_id:
        await collect_media_group(update, ctx, msg.media_group_id)
    else:
        await process_single_photo(update, ctx)


async def process_single_photo(update: Update, ctx):
    msg = update.message
    photo = msg.photo[-1]
    caption = (msg.caption or "").strip()
    tone, text = parse_tone(caption)

    status = await msg.reply_text(f"🖼 이미지 분석 중... (톤: {tone})")
    try:
        img = await download_photo(ctx, photo)
        if not img:
            await status.edit_text("⚠️ 이미지 다운로드 실패")
            return
        await process_and_respond(text, [img], tone, status)
    except Exception as e:
        log.exception("단일 사진 처리 실패")
        await status.edit_text(f"⚠️ 실패: {str(e)[:300]}")


async def collect_media_group(update: Update, ctx, gid: str):
    msg = update.message
    group = media_groups[gid]
    group["photos"].append(msg.photo[-1])
    if msg.caption and not group["caption"]:
        group["caption"] = msg.caption

    n = len(group["photos"])
    if group["status_msg"] is None:
        group["status_msg"] = await msg.reply_text(f"🖼 이미지 수집 중... ({n}장)")
    else:
        try:
            await group["status_msg"].edit_text(f"🖼 이미지 수집 중... ({n}장)")
        except Exception:
            pass

    if group["timer"]:
        group["timer"].cancel()
    group["timer"] = asyncio.create_task(_process_group_after_delay(ctx, gid))


async def _process_group_after_delay(ctx, gid: str):
    try:
        await asyncio.sleep(MEDIA_GROUP_WAIT_SEC)
    except asyncio.CancelledError:
        return

    group = media_groups.pop(gid, None)
    if not group:
        return
    status = group["status_msg"]
    photos = group["photos"]
    caption = group["caption"]

    if len(photos) > MAX_IMAGES_PER_REQUEST:
        await status.edit_text(
            f"⚠️ 한 번에 최대 {MAX_IMAGES_PER_REQUEST}장만 처리합니다. 앞 {MAX_IMAGES_PER_REQUEST}장 사용."
        )
        photos = photos[:MAX_IMAGES_PER_REQUEST]

    tone, text = parse_tone(caption)
    await status.edit_text(f"🎨 {len(photos)}장 분석 중... (톤: {tone})")

    try:
        images = []
        for photo in photos:
            img = await download_photo(ctx, photo)
            if img:
                images.append(img)
        if not images:
            await status.edit_text("⚠️ 이미지 다운로드 실패")
            return
        await process_and_respond(text, images, tone, status)
    except Exception as e:
        log.exception("앨범 처리 실패")
        await status.edit_text(f"⚠️ 실패: {str(e)[:300]}")


async def download_photo(ctx, photo) -> dict:
    """텔레그램 사진 → {data: base64, media_type}"""
    file = await ctx.bot.get_file(photo.file_id)
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)
    data = bio.read()
    if len(data) > MAX_IMAGE_SIZE:
        log.warning(f"이미지가 큼 ({len(data)/1024/1024:.1f}MB)")
    return {
        "data": base64.b64encode(data).decode("utf-8"),
        "media_type": detect_image_type(data),
    }


async def process_and_respond(text: str, images: list, tone: str, status_msg):
    try:
        result = generate_cards(text=text, images=images, tone=tone, brand=DEFAULT_BRAND)
        cards = result.get("cards", [])
        if not cards:
            raise ValueError("카드 결과 비어있음")

        session_id = f"tg_{int(time.time())}"
        save_session(session_id, result, text, DEFAULT_BRAND)

        summary_lines = []
        for i, c in enumerate(cards, 1):
            d = c.get("data", {})
            label = (
                d.get("title") or d.get("question") or d.get("term")
                or d.get("compareTitle") or d.get("quote")
                or d.get("askQuestion") or d.get("tipBody") or "..."
            )
            label = str(label).split("\n")[0][:35]
            summary_lines.append(f"{i}. [{c['type']}/{c['layout']}] {label}")

        theme = result.get("theme", "navy")
        url = f"{SERVER_URL}/?session={session_id}"
        source_label = []
        if text: source_label.append("텍스트")
        if images: source_label.append(f"이미지 {len(images)}장")
        source_str = " + ".join(source_label) if source_label else "입력"

        msg = (
            f"✅ {len(cards)}장 생성 완료 · 테마 {theme} · {source_str}\n\n"
            + "\n".join(summary_lines)
            + f"\n\n📱 편집/다운로드:\n{url}"
        )
        await status_msg.edit_text(msg)
    except Exception as e:
        log.exception("생성/저장 실패")
        await status_msg.edit_text(f"⚠️ 실패: {str(e)[:300]}")

# ============================================================
# 메인
# ============================================================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 미설정 (.env 확인)")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정 (.env 확인)")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info(f"🤖 Cardnews Bot 시작")
    log.info(f"📦 모델: {MODEL}")
    log.info(f"📂 세션: {SESSIONS_DIR}")
    log.info(f"🌐 스튜디오: {SERVER_URL}")
    if ALLOWED_USERS:
        log.info(f"🔒 허용 사용자: {ALLOWED_USERS}")
    else:
        log.warning("⚠️ ALLOWED_USERS 미설정 - 누구나 봇을 쓸 수 있음")
    app.run_polling()


if __name__ == "__main__":
    main()
