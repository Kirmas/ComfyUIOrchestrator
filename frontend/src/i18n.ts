import { useMemo } from "react";
import { create } from "zustand";
import { en, type TKey } from "./locales/en";
import { uk } from "./locales/uk";

/** Tiny in-house i18n, same reasoning as markdown.ts: a real i18n library
 * (react-i18next and its plugin chain) is a lot of dependency for a two-locale
 * single-user app on a box where `vite build` has already been OOM-killed once.
 * What's actually needed is a dictionary lookup, `{name}` substitution and a
 * re-render when the language changes -- that's this file.
 *
 * Keys live in locales/en.ts (the reference set; every other locale is typed
 * against it, so a missing translation fails the build rather than showing
 * English to a Ukrainian user). English is the default and the fallback.
 *
 * State lives in a zustand store rather than a React context: the store is
 * readable outside the React tree too (see `tr` below), which is what the
 * alert()/confirm() strings in plain helper functions need.
 */

export type Lang = "en" | "uk";

export const LANGS: { value: Lang; labelKey: TKey }[] = [
  { value: "en", labelKey: "settings.langEn" },
  { value: "uk", labelKey: "settings.langUk" },
];

const DICTS: Record<Lang, Record<TKey, string>> = { en, uk };

const STORAGE_KEY = "comfy-orchestrator:lang";

const storedLang = (): Lang => {
  const saved = localStorage.getItem(STORAGE_KEY);
  return saved === "uk" || saved === "en" ? saved : "en";
};

interface LangState {
  lang: Lang;
  setLang: (lang: Lang) => void;
}

export const useLangStore = create<LangState>((set) => ({
  lang: storedLang(),
  setLang: (lang) => {
    localStorage.setItem(STORAGE_KEY, lang);
    document.documentElement.lang = lang;
    set({ lang });
  },
}));

document.documentElement.lang = useLangStore.getState().lang;

export type Vars = Record<string, string | number>;

/** Substitutes only the placeholders the caller actually named, so a string
 * that legitimately contains braces (the literal `{tag}` a prompt macro uses,
 * for one) survives untouched. */
export function translate(lang: Lang, key: TKey, vars?: Vars): string {
  const raw = DICTS[lang][key] ?? en[key] ?? key;
  if (!vars) return raw;
  return raw.replace(/\{(\w+)\}/g, (match, name: string) => (name in vars ? String(vars[name]) : match));
}

export type TFunc = (key: TKey, vars?: Vars) => string;

/** The hook every component uses. Subscribing to `lang` is what makes the
 * whole UI re-render when the switch in Settings flips. */
export function useT(): TFunc {
  const lang = useLangStore((s) => s.lang);
  return useMemo<TFunc>(() => (key, vars) => translate(lang, key, vars), [lang]);
}

/** Non-reactive lookup, for code that runs outside a component's render (the
 * confirm()/alert() strings in plain async helpers). Reads the current
 * language at call time, so it's correct after a switch -- it just can't
 * trigger a re-render, which those call sites don't need. */
export const tr: TFunc = (key, vars) => translate(useLangStore.getState().lang, key, vars);

export type { TKey };
