import { Plugin } from "@opencode/plugin"
import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const here = path.dirname(fileURLToPath(import.meta.url))
const PORT = 8778
const BASE = `http://127.0.0.1:${PORT}`

const BRIDGE_SCRIPT = path.join(here, "bridge", "antiplagiat_bridge.py")

interface JObj {
  [k: string]: unknown
}

async function jfetch(
  url: string,
  init?: RequestInit,
  timeoutMs = 20000,
  signal?: AbortSignal,
): Promise<JObj> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const onAbort = () => controller.abort()
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener("abort", onAbort, { once: true })
  }
  try {
    const res = await fetch(url, { ...init, signal: controller.signal })
    const text = await res.text()
    try {
      return JSON.parse(text) as JObj
    } catch {
      return { ok: false, error: `не-JSON ответ (${res.status}): ${text.slice(0, 300)}` }
    }
  } finally {
    clearTimeout(timer)
    if (signal) signal.removeEventListener("abort", onAbort)
  }
}

function bridgeAlive(timeoutMs = 3000): Promise<boolean> {
  return jfetch(`${BASE}/health`, undefined, timeoutMs)
    .then((r) => r.ok === true)
    .catch(() => false)
}

function pythonCandidates(): string[] {
  const out: string[] = []
  const own = path.join(here, "bridge", "venv", "Scripts", "python.exe")
  if (existsSync(own)) out.push(own)
  out.push("python")
  return out
}

async function spawnBridge(): Promise<void> {
  const started = pythonCandidates().map((py) => {
    try {
      const child = spawn(py, [BRIDGE_SCRIPT], {
        detached: true,
        stdio: "ignore",
        windowsHide: true,
      })
      child.unref()
      return true
    } catch {
      return false
    }
  })
  if (!started.some(Boolean)) throw new Error("не удалось запустить python для моста")
  for (let i = 0; i < 45; i++) {
    await new Promise((r) => setTimeout(r, 1000))
    if (await bridgeAlive()) return
  }
  throw new Error(
    "мост антиплагиата не поднялся за 45 секунд. Запусти bridge/install.ps1 из папки плагина " +
      "и посмотри bridge/bridge.log",
  )
}

async function ensureBridge(): Promise<void> {
  if (await bridgeAlive()) return
  await spawnBridge()
}

type Schema = Record<string, unknown>

const schema = (properties: Schema, required: string[] = []): Schema => ({
  type: "object",
  properties,
  required,
  additionalProperties: false,
})

const str = (description: string): Schema => ({ type: "string", description })
const bool = (description: string): Schema => ({ type: "boolean", description })
const num = (description: string): Schema => ({ type: "number", description })

const SITE_LABELS: Record<string, string> = {
  apvuz: "АП ВУЗ (ap-vuz.ru)",
  killer: "Антиплагиат Киллер (killer-antiplagiat.ru)",
  aitexts: "AI-анализ (ai-texts.ru)",
}

const pct = (v: unknown): string =>
  typeof v === "number" ? `${v}%` : typeof v === "string" ? v : "н/д"

/** Короткая сводка по одному сайту — чтобы агент сразу могло ответить человеку. */
function siteSummary(name: string, r: Record<string, unknown>): string {
  const label = SITE_LABELS[name] ?? name
  if (r.ok !== true) return `${label}: не удалось — ${r.error ?? "неизвестная ошибка"}`
  const chars = Array.isArray(r.chars_checked)
    ? (r.chars_checked as (number | null)[]).filter((c): c is number => typeof c === "number")
    : []
  const total = typeof r.text_chars === "number" ? r.text_chars : undefined
  const checked =
    chars.length && total && Math.max(...chars) < total
      ? ` (проверено ${Math.max(...chars)} из ${total} символов — лимит бесплатного тарифа)`
      : chars.length
        ? ` (${Math.max(...chars)} символов)`
        : ""
  const extra = r.note ? ` · ${r.note}` : ""
  if (name === "apvuz") {
    return (
      `${label}: оригинальность ${pct(r.original)}, заимствования ${pct(r.borrowed)}, ` +
      `цитирования ${pct(r.citations)}, ИИ-контент ${pct(r.ai_content)}${checked}${extra}`
    )
  }
  if (name === "killer") {
    const src = Array.isArray(r.sources) ? (r.sources as unknown[]).length : 0
    const srcTxt = src ? `, источников найдено: ${src}` : ""
    return (
      `${label}: уникальность ${pct(r.uniqueness)}, заимствования ${pct(r.borrowed)}` +
      `${srcTxt}${checked}${extra}`
    )
  }
  const sig = Array.isArray(r.signals) ? (r.signals as { name: string; percent: number }[]) : []
  const top = sig
    .slice()
    .sort((a, b) => b.percent - a.percent)
    .slice(0, 3)
    .map((s) => `${s.name} ${s.percent}%`)
    .join(", ")
  return (
    `${label}: ${pct(r.ai_score)} — ${r.verdict ?? "оценка ИИ-контента"}` +
    `${top ? ` (топ признаков: ${top})` : ""}${checked}${extra}`
  )
}

export default Plugin.define({
  id: "antiplagiat",
  async setup(ctx) {
    await ctx.session.hook("context", (event) => {
      event.system.push({
        type: "text",
        text:
          "Инструмент antiplagiat_check — бесплатная проверка текста на уникальность/оригинальность и " +
          "на ИИ-контент сразу на трёх сайтах (АП ВУЗ, Антиплагиат Киллер, ai-texts). Вызывай его, когда " +
          "текст готов, и сообщай пользователю проценты из ответа — не выдумывай их. Помни про лимиты " +
          "бесплатных тарифов: АП ВУЗ — 2000 символов, Киллер — 1000 символов (длинный текст проверяй с " +
          "samples=3 — начало/середина/конец), AI-анализ — весь текст. У Антиплагиат Киллера есть дневной " +
          "лимит (5 проверок в сутки): если в ответе «ДНЕВНОЙ ЛИМИТ» — просто сообщи пользователю проценты " +
          "с других сайтов и что Киллер будет доступен завтра. Если проверка не удалась или вышел " +
          "тайм-аут — повтори вызов один раз, потом сообщи пользователю текст ошибки.",
      })
    })

    await ctx.tool.transform((editor) => {
      editor.namespace({
        name: "antiplagiat",
        description:
          "Бесплатная проверка работ на антиплагиат и ИИ-контент по трём сайтам: АП ВУЗ (демо-отчёт), " +
          "Антиплагиат Киллер (бесплатный тариф) и AI-анализ текста по 20 признакам.",
      })

      editor.add({
        name: "ap_status",
        description:
          "Состояние моста антиплагиата (плагин antiplagiat): запущен ли он и какие сайты доступны. " +
          "Проверяй перед первой проверкой или если что-то пошло не так. (Имя ap_status выбрано, чтобы " +
          "не перекрывать инструмент status плагина DeepSeek.)",
        input: schema({}),
        options: { codemode: true },
        execute: async () => {
          await ensureBridge()
          const h = await jfetch(`${BASE}/health`, undefined, 20000)
          return {
            content: JSON.stringify(
              {
                bridge_running: h.ok === true,
                sites: h.sites ?? null,
                profiles: h.profiles ?? null,
                error: h.error ?? null,
              },
              null,
              2,
            ),
          }
        },
      })

      editor.add({
        name: "check",
        description:
          "Проверяет текст на антиплагиат/оригинальность и на ИИ-контент сразу на трёх сайтах, бесплатно и " +
          "параллельно: АП ВУЗ (оригинальность, заимствования, цитирования, ИИ-контент; демо-лимит 2000 " +
          "символов), Антиплагиат Киллер (уникальность, заимствования, список источников; бесплатный лимит " +
          "1000 символов и не больше 5 проверок в сутки — при исчерпании дневного лимита вернётся понятная " +
          "ошибка, просто передай её пользователю), ai-texts (вероятность ИИ-контента по 20 признакам, весь " +
          "текст). Возвращает JSON " +
          "с процентами и готовую сводку. Длинные работы (курсовые, рефераты) проверяй с samples=3 — это " +
          "три фрагмента (начало/середина/конец), результат усредняется. Проверка занимает от 20 секунд до " +
          "нескольких минут.",
        input: schema(
          {
            text: str("Полный текст работы для проверки."),
            sites: {
              type: "array",
              items: { type: "string", enum: ["apvuz", "killer", "aitexts"] },
              description:
                "Какие именно сайты проверять: apvuz, killer, aitexts. По умолчанию — все три.",
            },
            samples: num(
              "Сколько фрагментов проверить на сайтах с лимитом: 1 (по умолчанию) — только начало текста, " +
                "3 — начало/середина/конец с усреднением (для длинных работ). От 1 до 4.",
            ),
            timeout: num("Сколько секунд ждать результат одного сайта: от 30 до 600, по умолчанию 180."),
          },
          ["text"],
        ),
        options: { codemode: true },
        execute: async (input, context) => {
          const i = input as {
            text: string
            sites?: string[]
            samples?: number
            timeout?: number
          }
          if (!i.text || !i.text.trim()) return { content: "Ошибка: передан пустой текст." }
          await ensureBridge()
          const samples = Math.min(4, Math.max(1, Math.round(i.samples ?? 1)))
          const timeout = Math.min(600, Math.max(30, Math.round(i.timeout ?? 180)))
          const r = await jfetch(
            `${BASE}/check`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                text: i.text,
                sites: Array.isArray(i.sites) && i.sites.length ? i.sites : null,
                samples,
                timeout_s: timeout,
              }),
            },
            (samples * timeout + 90) * 1000,
            context.signal,
          )
          if (r.ok !== true && r.sites === undefined) {
            return {
              content:
                "Проверка не запустилась: " +
                String(r.error ?? "неизвестная ошибка") +
                "\nПроверь antiplagiat_ap_status и bridge/bridge.log.",
            }
          }
          const sites = (r.sites ?? {}) as Record<string, Record<string, unknown>>
          const summary = Object.entries(sites)
            .map(([name, res]) => "- " + siteSummary(name, res))
            .join("\n")
          const failed = Object.values(sites).every((s) => s?.ok !== true)
          return {
            content:
              (failed ? "Ни одна проверка не завершилась успешно.\n" : "") +
              `Сводка (текст: ${r.text_chars} симв.):\n${summary}\n\n` +
              "Полные данные:\n" +
              JSON.stringify(r, null, 2),
          }
        },
      })
    })

    return () => {
      /* registrations are disposed with the plugin */
    }
  },
})
