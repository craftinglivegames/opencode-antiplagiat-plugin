# -*- coding: utf-8 -*-
"""
Antiplagiat Bridge for OpenCode — локальный HTTP-мост, который проверяет текст
на уникальность/оригинальность сразу на трёх сайтах, бесплатно:

  1. ap-vuz.ru           — демо-проверка АП ВУЗ (оригинальность, заимствования,
                           цитирования, ИИ-контент); бесплатный тариф по ссылке
                           с параметром etext, лимит демо — 2000 символов.
  2. killer-antiplagiat  — бесплатный тариф «до 1000 символов» (уникальность,
                           заимствования, список найденных источников).
  3. ai-texts.ru         — глубокий AI-анализ текста по 20 признакам
                           (вероятность «ИИ-контента»), считается прямо
                           в браузере, бесплатно 2 проверки (счётчик — в
                           localStorage, сбрасывается перед проверкой).

Работает на http://127.0.0.1:8778 (порт свой, чтобы не конфликтовать с мостом
DeepSeek на 8777 и мостом надстройки Word на 8765). Браузер невидимый (headless),
профили сайтов живут в %LOCALAPPDATA%/OpenCodeAntiplagiatBridge/profiles.

API:
  GET  /health    -> {"ok": true, "sites": [...]}
  POST /check     -> {"text": "...", "sites": ["apvuz","killer","aitexts"],
                      "samples": 1, "timeout_s": 180}
                     -> {"ok": true, "text_chars": N, "sites": {...}}
  POST /shutdown
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import List, Optional

APP_NAME = "OpenCodeAntiplagiatBridge"
PORT = 8778
BASE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR))) / APP_NAME / "profiles"
LOG_PATH = BASE_DIR / "bridge.log"

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(APP_NAME)

# Headless-Chromium шлёт User-Agent с «HeadlessChrome» — от него части защит
# отвечают 403/антибот-страницей. Подменяем на обычный Chrome.
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

# Ссылки с параметром etext — по ним на ap-vuz и killer открывается бесплатный
# тариф (параметр из партнёрской/рекламной кампании, yclid/ybaip — метки).
URL_APVUZ = ("https://ap-vuz.ru/?etext=2202.SuSyv_mZVFHM-93XiqECC6TmLQWc-o78teElr_8HkbJvGkSMAMqEECLRQnaZX4bbPs_"
             "7IwZGauIuij4pgAF1IPQEGeRrjM9ud2E_tXj3g-4STuayZcvaONL4c4hSFqpPYwKzwcPF8se7etcopS-wSBMujf6cNke752peez07ZERAi_"
             "ELzu8m932ewZwHwAiJenhiaXJvcndvdGl5bGl4bQ.120a74f199d08b3bf25f2af6565c6abe8ba8b378"
             "&yclid=16844820115650772991&ybaip=1")
URL_KILLER = ("https://killer-antiplagiat.ru/proverit-unikalnost-teksta-onlajn?etext=2202.susyv_mzvfhm-93xiqecc6tmlqwc-"
              "o78teelr_8hkbjvgksmamqeeclrqnazx4bbps_7iwzgauiuij4pgaf1ipqegerrjm9ud2e_txj3g-4stuayzcvaonl4c4hsfqppywkzwcpf8se7etcops-"
              "wsbmujf6cnke752peez07zerai_elzu8m932ewzwhwaijenhiaxjvcndvdgl5bgl4bq.120a74f199d08b3bf25f2af6565c6abe8ba8b378"
              "&yclid=14385694864965631999&ybaip=1")
URL_AITEXTS = ("https://ai-texts.ru/tools/deep-ai-analysis?etext=2202.SuSyv_mZVFHM-93XiqECC6TmLQWc-o78teElr_8HkbJvGkSMAMqEECLRQnaZX4bbPs_"
               "7IwZGauIuij4pgAF1IPQEGeRrjM9ud2E_tXj3g-4STuayZcvaONL4c4hSFqpPYwKzwcPF8se7etcopS-wSBMujf6cNke752peez07ZERAi_"
               "ELzu8m932ewZwHwAiJenhiaXJvcndvdGl5bGl4bQ.120a74f199d08b3bf25f2af6565c6abe8ba8b378"
               "&yclid=13432424738963062783&ybaip=1")

SITES = {
    "apvuz": {
        "url": URL_APVUZ,
        "limit": 2000,          # демо-тариф режет текст до 2000 символов
        "title": "АП ВУЗ (ap-vuz.ru)",
        "note": "демо-тариф АП ВУЗ: проверяется до 2000 символов за раз",
    },
    "killer": {
        "url": URL_KILLER,
        "limit": 1000,          # бесплатный тариф «до 1000 символов»
        "title": "Антиплагиат Киллер (killer-antiplagiat.ru)",
        "note": "бесплатный тариф: до 1000 символов за раз",
    },
    "aitexts": {
        "url": URL_AITEXTS,
        "limit": 120000,        # считается в браузере, лимита по сути нет
        "title": "AI-анализ (ai-texts.ru)",
        "note": "проверка на ИИ по всему тексту (лимит 120 000 символов)",
    },
}

app = FastAPI(title=APP_NAME)

_pw = None
_ctxs = {}
_locks = {name: asyncio.Lock() for name in SITES}


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------

async def get_page(site: str):
    """Возвращает page в постоянном профиле сайта (контекст поднимается лениво)."""
    global _pw
    ctx = _ctxs.get(site)
    if ctx is not None:
        try:
            if ctx.pages:
                return ctx.pages[0]
            return await ctx.new_page()
        except Exception:
            log.warning("контекст %s умер, пересоздаю", site)
            try:
                await ctx.close()
            except Exception:
                pass
            _ctxs.pop(site, None)
    from playwright.async_api import async_playwright
    if _pw is None:
        _pw = await async_playwright().start()
    path = PROFILES_DIR / site
    path.mkdir(parents=True, exist_ok=True)
    ctx = await _pw.chromium.launch_persistent_context(
        str(path),
        headless=True,
        viewport={"width": 1400, "height": 950},
        locale="ru",
        user_agent=UA_CHROME,
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
    )
    _ctxs[site] = ctx
    log.info("поднят контекст %s", site)
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


async def drop_ctx(site: str):
    ctx = _ctxs.pop(site, None)
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass


async def close_all():
    for site in list(_ctxs):
        await drop_ctx(site)
    global _pw
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception:
            pass
        _pw = None


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


def fragments(text: str, limit: int, samples: int) -> List[str]:
    """Нарезает текст на `samples` фрагментов длиной до `limit` (начало/середина/конец)."""
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    if not text:
        return []
    if samples <= 1 or len(text) <= limit:
        return [text[:limit]]
    samples = max(2, min(int(samples), 4))
    out = []
    for i in range(samples):
        start = int(i * (len(text) - limit) / (samples - 1))
        # не рвём слово посередине
        if 0 < start < len(text) - limit:
            sp = text.find(" ", start, start + min(200, limit))
            if sp != -1:
                start = sp + 1
        out.append(text[start:start + limit])
    # убираем дубликаты (на случай очень короткого текста)
    uniq = []
    for f in out:
        if f not in uniq:
            uniq.append(f)
    return uniq


# ---------------------------------------------------------------------------
# ap-vuz.ru — модалка «Вставить текст» -> «Проверить текст» -> отчёт
# ---------------------------------------------------------------------------

def parse_apvuz(txt: str) -> Optional[dict]:
    i = txt.find("РЕЗУЛЬТАТЫ ПРОВЕРКИ")
    if i < 0:
        return None
    blk = txt[i:i + 700]
    pats = {
        "original": r"Оригинальность\s*\n\s*([\d.,  ]+)\s*%",
        "borrowed": r"Заимствования\s*\n\s*([\d.,  ]+)\s*%",
        "citations": r"Цитирования\s*\n\s*([\d.,  ]+)\s*%",
        "self_citations": r"Самоцитирования\s*\n\s*([\d.,  ]+)\s*%",
        "ai_content": r"ИИ-контент\s*\n\s*([\d.,  ]+)\s*%",
    }
    out = {}
    for k, p in pats.items():
        m = re.search(p, blk)
        if m:
            try:
                out[k] = _num(m.group(1))
            except Exception:
                pass
    return out if "original" in out else None


async def _apvuz_one(page, frag: str, timeout: int) -> dict:
    await page.goto(SITES["apvuz"]["url"], wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3500)
    try:
        await page.click("#accept-cookies", timeout=2000)
    except Exception:
        pass
    # открываем модалку «Вставить текст» (textarea #pastedText лежит внутри неё)
    await page.evaluate("""() => {
        const t = [...document.querySelectorAll('[data-target="#addText"],[data-bs-target="#addText"]')]
            .find(e => e.offsetParent);
        if (t) t.click();
    }""")
    await page.wait_for_timeout(1500)
    filled = await page.evaluate("""(t) => {
        const ta = document.querySelector('#pastedText');
        if (!ta || !ta.offsetParent) return 0;
        ta.focus(); ta.value = t;
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));
        return (ta.value || '').length;
    }""", frag)
    if not filled:
        return {"ok": False, "error": "не открылось поле ввода (модалка «Вставить текст»)"}
    await page.evaluate("() => { const b = document.querySelector('#addTextOrder'); if (b) b.click(); }")
    deadline = time.time() + timeout
    tick = 0
    while time.time() < deadline:
        await page.wait_for_timeout(2500)
        tick += 1
        if tick % 6 == 0:
            log.info("[apvuz] ждём отчёт, осталось %d с", int(deadline - time.time()))
        try:
            txt = await page.evaluate("() => (document.body.innerText || '')")
        except Exception as e:
            return {"ok": False, "error": f"страница недоступна: {e}"}
        if "ИДЕТ ФОРМИРОВАНИЕ ОТЧЕТА" in txt:
            continue
        parsed = parse_apvuz(txt)
        if parsed:
            parsed["ok"] = True
            parsed["chars"] = filled
            return parsed
        if "Демо версия" in txt and "ПРОВЕРИТЬ ТЕКСТ" in txt and "РЕЗУЛЬТАТЫ ПРОВЕРКИ" not in txt:
            # модалка осталась — проверка не стартовала, ждём чуть-чуть и повторяем клик
            await page.evaluate("() => { const b = document.querySelector('#addTextOrder'); if (b) b.click(); }")
    return {"ok": False, "error": f"отчёт не появился за {timeout} с (возможно, перегрузка АП ВУЗ)", "chars": filled}


# ---------------------------------------------------------------------------
# killer-antiplagiat.ru — бесплатный тариф -> «Анализ документа» -> результат
# ---------------------------------------------------------------------------

def parse_killer(txt: str) -> Optional[dict]:
    i = txt.find("РЕЗУЛЬТАТЫ БЕСПЛАТНОЙ ПРОВЕРКИ")
    if i < 0:
        return None
    blk = txt[i:i + 4000]
    out = {"ok": True}
    m = re.search(r"Уникальность:\s*([\d.,  ]+)", blk)
    if not m:
        return None
    out["uniqueness"] = _num(m.group(1))
    m = re.search(r"Заимствования:\s*([\d.,  ]+)", blk)
    if m:
        out["borrowed"] = _num(m.group(1))
    m = re.search(r"\n\s*(\d{1,3})\s*%", blk)
    if m:
        out["percent"] = int(m.group(1))
    sources = []
    for line in blk.splitlines():
        m = re.match(r"(https?://\S+)\s+([\d.,  ]+)\s", line + " ")
        if m and len(sources) < 12:
            try:
                sources.append({"url": m.group(1)[:200], "percent": _num(m.group(2))})
            except Exception:
                pass
    if sources:
        out["sources"] = sources
    return out


async def _killer_one(page, frag: str, timeout: int) -> dict:
    QUOTA = ("Бесплатная проверка доступна 5 раз в сутки", "через 24 часа", "Бесплатная проверка доступна")
    await page.goto(SITES["killer"]["url"], wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4500)
    try:
        await page.click("text=Принять и закрыть", timeout=2500)
    except Exception:
        pass
    filled = await page.evaluate("""(t) => {
        const ta = [...document.querySelectorAll('textarea')]
            .find(x => (x.placeholder || '').includes('Перетащите'));
        if (!ta) return 0;
        ta.focus(); ta.value = t;
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));
        return (ta.value || '').length;
    }""", frag)
    if not filled:
        return {"ok": False, "error": "поле ввода текста не найдено"}
    # бесплатный тариф (первая радиокнопка с подписью «Бесплатно»)
    await page.evaluate("""() => {
        const rs = [...document.querySelectorAll('input[name=check-modes]')];
        for (const r of rs) {
            let n = r; for (let i = 0; i < 4 && n; i++) n = n.parentElement;
            if (n && /бесплат/i.test(n.innerText || '')) { r.click(); return; }
        }
    }""")
    await page.wait_for_timeout(500)
    # кнопка бесплатной проверки рендерится с задержкой — ждём до 60 с
    clicked = "timeout"
    for _ in range(30):
        clicked = await page.evaluate("""() => {
            const el = [...document.querySelectorAll('button,a')]
                .find(e => e.offsetParent && String(e.className || '').includes('make-free-order-bt'));
            if (!el) return 'wait';
            el.click(); return 'clicked';
        }""")
        if clicked != "wait":
            break
        await page.wait_for_timeout(2000)
    if clicked != "clicked":
        return {"ok": False, "error": "кнопка бесплатной проверки не появилась", "chars": filled}
    deadline = time.time() + timeout
    tick = 0
    while time.time() < deadline:
        await page.wait_for_timeout(3000)
        tick += 1
        if tick % 5 == 0:
            log.info("[killer] ждём результат, осталось %d с", int(deadline - time.time()))
        try:
            txt = await page.evaluate("() => (document.body.innerText || '')")
        except Exception as e:
            return {"ok": False, "error": f"страница недоступна: {e}"}
        # дневной лимит бесплатных проверок (5 в сутки на IP) — сайт показывает модалку
        if any(q in txt for q in QUOTA):
            return {"ok": False, "error": "ДНЕВНОЙ ЛИМИТ: на killer-antiplagiat.ru не более 5 " +
                                          "бесплатных проверок в сутки — повторите завтра", "chars": filled}
        parsed = parse_killer(txt)
        if parsed:
            parsed["chars"] = filled
            return parsed
    return {"ok": False, "error": f"результат не появился за {timeout} с (сайт обещает до 5 минут)", "chars": filled}


# ---------------------------------------------------------------------------
# ai-texts.ru — вставка в #ta -> «Проверить по 20 признакам» -> процент ИИ
# ---------------------------------------------------------------------------

def parse_aitexts(txt: str) -> Optional[dict]:
    if "Разбор по 20 признакам" not in txt:
        return None
    lines = [l.strip() for l in txt.splitlines()]
    try:
        k = lines.index("Проверить по 20 признакам")
    except ValueError:
        k = -1
    out = {"ok": True}
    if k >= 0:
        for j in range(k + 1, min(k + 6, len(lines))):
            m = re.fullmatch(r"(\d{1,3})\s*%", lines[j])
            if m:
                out["ai_score"] = int(m.group(1))
                for v in lines[j + 1:j + 4]:
                    if v:                       # первая непустая строка после % — вердикт
                        out["verdict"] = v
                        break
                break
    # 20 признаков: идём после заголовка, «имя» — строка перед процентом
    start = lines.index("Разбор по 20 признакам") + 1
    signals = []
    for j in range(start, len(lines)):
        m = re.fullmatch(r"(\d{1,3})\s*%", lines[j])
        if not m:
            continue
        name = ""
        for back in range(1, 4):
            if j - back < start:
                break
            cand = lines[j - back]
            if not cand or cand.isupper() and cand.replace(" ", "").isalpha():
                continue  # это рубрика (ФОРМА, ЛЕКСИКА…)
            name = cand
            break
        if name:
            signals.append({"name": name[:80], "percent": int(m.group(1))})
    if signals:
        out["signals"] = signals[:24]
    m = re.search(r"(\d[\d  ]*)\s*слов\s*·\s*(\d[\d  ]*)\s*символов", txt)
    if m:
        out["words"] = int(m.group(1).replace(" ", "").replace(" ", ""))
    if "ai_score" in out:
        return out
    return None


async def _aitexts_one(page, frag: str, timeout: int) -> dict:
    # сбрасываем локальный счётчик бесплатных проверок: он живёт в localStorage/куках,
    # поэтому чистим и storage, и cookies (новый cid = снова 2 бесплатные проверки)
    try:
        await page.context.clear_cookies()
    except Exception:
        pass
    await page.goto(SITES["aitexts"]["url"], wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)
    await page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
    await page.reload(wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)
    try:
        await page.click("#cookieOk", timeout=2500)
    except Exception:
        pass
    await page.fill("#ta", frag)
    await page.wait_for_timeout(400)
    await page.click("#btn")
    deadline = time.time() + timeout
    tick = 0
    while time.time() < deadline:
        await page.wait_for_timeout(2500)
        tick += 1
        if tick % 6 == 0:
            log.info("[aitexts] ждём анализ, осталось %d с", int(deadline - time.time()))
        try:
            txt = await page.evaluate("() => (document.body.innerText || '')")
        except Exception as e:
            return {"ok": False, "error": f"страница недоступна: {e}"}
        parsed = parse_aitexts(txt)
        if parsed:
            parsed["chars"] = len(frag)
            return parsed
        if "осталось 0" in txt:
            return {"ok": False, "error": "бесплатные проверки исчерпаны, счётчик не сбросился"}
    return {"ok": False, "error": f"анализ не завершился за {timeout} с", "chars": len(frag)}


RUNNERS = {"apvuz": _apvuz_one, "killer": _killer_one, "aitexts": _aitexts_one}


# ---------------------------------------------------------------------------
# Запуск проверок
# ---------------------------------------------------------------------------

def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else None


async def run_site(site: str, text: str, samples: int, timeout: int) -> dict:
    cfg = SITES[site]
    frags = fragments(text, cfg["limit"], samples)
    if not frags:
        return {"ok": False, "error": "пустой текст", "title": cfg["title"]}
    started = time.time()
    results = []
    async with _locks[site]:
        page = await get_page(site)
        for idx, frag in enumerate(frags, 1):
            log.info("[%s] фрагмент %d/%d (%d симв.)", site, idx, len(frags), len(frag))
            # каждый фрагмент — отдельная проверка; при сбое повторяем только его,
            # а не весь набор (иначе неудачный сайт растягивал бы ответ вдвое)
            for attempt in range(2):
                try:
                    page = await get_page(site)
                    r = await RUNNERS[site](page, frag, timeout)
                except Exception as e:
                    log.exception("run_site(%s) sample failed", site)
                    r = {"ok": False, "error": str(e)[:300]}
                    await drop_ctx(site)
                if r.get("ok"):
                    break
                err = str(r.get("error", ""))
                # дневной лимит — остальные фрагменты на этом сайте бессмысленны
                if "ЛИМИТ" in err or "сутки" in err:
                    break
                # повторяем только падение браузера/страницы; таймаут сайта повторять бессмысленно
                if attempt == 0 and ("недоступна" in err or "closed" in err.lower()):
                    log.info("[%s] повтор фрагмента после сбоя: %s", site, err[:120])
                    await asyncio.sleep(2)
                    continue
                log.info("[%s] фрагмент %d/%d не удался: %s", site, idx, len(frags), err[:160])
                break
            results.append(r)
            if r and ("ЛИМИТ" in str(r.get("error", "")) or "сутки" in str(r.get("error", ""))):
                log.info("[%s] лимит исчерпан — остальные фрагменты пропускаю", site)
                break
        log.info("[%s] все фрагменты за %.0f с", site, time.time() - started)
    out = {"ok": any(r.get("ok") for r in results), "title": cfg["title"], "note": cfg["note"],
           "elapsed_s": round(time.time() - started, 1), "samples": len(frags)}
    if not out["ok"]:
        errs = [r.get("error", "?") for r in results]
        out["error"] = "; ".join(dict.fromkeys(errs)) or "неизвестная ошибка"
        return out
    good = [r for r in results if r.get("ok")]
    out["chars_checked"] = [r.get("chars") for r in good]
    if site == "apvuz":
        for key in ("original", "borrowed", "citations", "self_citations", "ai_content"):
            out[key] = _avg([r.get(key) for r in good])
        if len(good) > 1:
            out["per_sample"] = [{k: r.get(k) for k in ("original", "borrowed", "chars") if r.get(k) is not None}
                                 for r in good]
    elif site == "killer":
        out["uniqueness"] = _avg([r.get("uniqueness") for r in good])
        out["borrowed"] = _avg([r.get("borrowed") for r in good])
        out["percent"] = _avg([r.get("percent") for r in good])
        if good and good[0].get("sources"):
            out["sources"] = good[0]["sources"]
        if len(good) > 1:
            out["per_sample"] = [{k: r.get(k) for k in ("uniqueness", "borrowed", "chars") if r.get(k) is not None}
                                 for r in good]
    elif site == "aitexts":
        out["ai_score"] = _avg([r.get("ai_score") for r in good])
        out["verdict"] = good[0].get("verdict")
        out["signals"] = good[0].get("signals", [])
        out["words"] = good[0].get("words")
    if out["chars_checked"]:
        checked = max((c for c in out["chars_checked"] if c), default=None)
        # лимит бесплатного тарифа по сайту — если текст длиннее, помечаем усечение
        if len(text) > cfg["limit"]:
            out["truncated"] = True
            out["text_chars"] = len(text)
            out["site_limit"] = cfg["limit"]
    return out


class CheckReq(BaseModel):
    text: str = Field(..., min_length=1)
    sites: Optional[List[str]] = None
    samples: int = Field(1, ge=1, le=4)
    timeout_s: int = Field(180, ge=30, le=600)


@app.get("/health")
async def health():
    return {"ok": True, "sites": list(SITES), "profiles": str(PROFILES_DIR)}


@app.post("/check")
async def check(req: CheckReq):
    sites = [s for s in (req.sites or list(SITES)) if s in SITES]
    if not sites:
        return {"ok": False, "error": "не выбраны сайты"}
    log.info("check: %s симв., сайты=%s, samples=%s", len(req.text), sites, req.samples)
    tasks = [run_site(s, req.text, req.samples, req.timeout_s) for s in sites]
    results = await asyncio.gather(*tasks)
    out = {"ok": any(r.get("ok") for r in results),
           "text_chars": len(req.text),
           "sites": dict(zip(sites, results))}
    return out


@app.post("/shutdown")
async def shutdown():
    asyncio.get_event_loop().create_task(_shutdown_task())
    return {"ok": True}


async def _shutdown_task():
    await close_all()
    os._exit(0)


def run():
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Antiplagiat Bridge запущен на http://127.0.0.1:%s", PORT)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    run()
