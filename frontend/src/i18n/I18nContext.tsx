import React, { createContext, useContext, useEffect, useState } from 'react'
import { ar } from './ar'
import { en } from './en'
import { fa } from './fa'
import type { Language, Translations } from './types'

const DICTIONARIES: Record<Language, Translations> = { en, ar, fa }
const RTL_LANGS = new Set<Language>(['ar', 'fa'])
const STORAGE_KEY = 'aatm_language'

interface I18nContextValue {
  language: Language
  isRtl: boolean
  t: Translations
  setLanguage: (lang: Language) => void
  formatNumber: (n: number) => string
  formatDate: (iso: string | Date) => string
}

const I18nContext = createContext<I18nContextValue | null>(null)

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguageState] = useState<Language>(() => {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'en' || saved === 'ar' || saved === 'fa') return saved
    return 'en'
  })

  const isRtl = RTL_LANGS.has(language)

  useEffect(() => {
    document.documentElement.lang = language
    document.documentElement.dir = isRtl ? 'rtl' : 'ltr'
    localStorage.setItem(STORAGE_KEY, language)
  }, [language, isRtl])

  const localeTag = language === 'ar' ? 'ar-EG' : language === 'fa' ? 'fa-IR' : 'en-US'

  const formatNumber = (n: number) => new Intl.NumberFormat(localeTag).format(n)
  const formatDate = (iso: string | Date) => {
    try {
      const d = typeof iso === 'string' ? new Date(iso) : iso
      return new Intl.DateTimeFormat(localeTag, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }).format(d)
    } catch {
      return String(iso)
    }
  }

  return (
    <I18nContext.Provider
      value={{
        language,
        isRtl,
        t: DICTIONARIES[language],
        setLanguage: setLanguageState,
        formatNumber,
        formatDate,
      }}
    >
      {children}
    </I18nContext.Provider>
  )
}

export function useI18n() {
  const ctx = useContext(I18nContext)
  if (!ctx) throw new Error('useI18n must be used inside I18nProvider')
  return ctx
}
