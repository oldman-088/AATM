import { describe, expect, it } from 'vitest'
import { ar } from '../../i18n/ar'
import { en } from '../../i18n/en'
import { fa } from '../../i18n/fa'

/**
 * EN / AR / FA parity for the E28 control buttons.
 *
 * `i18n/types.ts` is the source of truth: `tsc --noEmit` already fails when a key is missing from
 * one dictionary. These assertions catch the other drift (a key that exists but is empty, a Latin
 * placeholder pasted into AR/FA, a section that only exists in one language).
 */

type Dict = Record<string, any>

function shape(value: Dict, prefix = ''): Record<string, string> {
  return Object.entries(value).reduce<Record<string, string>>((acc, [key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key
    if (child && typeof child === 'object' && !Array.isArray(child)) return { ...acc, ...shape(child, path) }
    return { ...acc, [path]: typeof child }
  }, {})
}

/** The keys this task added (E28); every one of them must exist in all three dictionaries. */
const E28_KEYS = [
  'common.save',
  'common.close',
  'common.adminRequired',
  'common.draftConflict',
  'arenaAgent.draft.title',
  'arenaAgent.draft.hint',
  'arenaAgent.draft.approve',
  'arenaAgent.draft.edit',
  'arenaAgent.draft.reject',
  'arenaAgent.draft.save',
  'arenaAgent.draft.cancelEdit',
  'arenaAgent.draft.editorLabel',
  'arenaAgent.draft.taskCount',
  'arenaAgent.draft.reasons',
  'arenaAgent.draft.warnings',
  'arenaAgent.draft.invalidJson',
  'arenaAgent.draft.approved',
  'arenaAgent.draft.saved',
  'arenaAgent.draft.rejected',
  'arenaAgent.draft.working',
  'arenaAgent.draft.decided',
  'arenaAgent.draft.assignee',
  'projects.nameLabel',
  'projects.namePlaceholder',
  'projects.repoUrlLabel',
  'projects.repoUrlPlaceholder',
  'projects.repoUrlHint',
  'projects.repoUrlRequired',
  'projects.repoUrlInvalid',
  'projects.createFailed',
  'projects.disable',
  'projects.enable',
  'projects.disabledBadge',
  'projects.activeBadge',
  'projects.defaultBadge',
  'projects.defaultCannotDisable',
  'projects.disableConfirmTitle',
  'projects.disableConfirmBody',
  'projects.disableConfirmYes',
  'projects.disableConfirmCancel',
  'projects.disableFailed',
  'projects.disabledNotice',
]

describe('i18n EN / AR / FA parity (E28)', () => {
  it('has the same key paths and value types in all three dictionaries', () => {
    const english = shape(en as Dict)
    expect(Object.keys(shape(ar as Dict)).sort()).toEqual(Object.keys(english).sort())
    expect(Object.keys(shape(fa as Dict)).sort()).toEqual(Object.keys(english).sort())
    expect(shape(ar as Dict)).toEqual(english)
    expect(shape(fa as Dict)).toEqual(english)
    expect(Object.keys(english).length).toBeGreaterThan(600)
  })

  it('defines every E28 control key in all three languages', () => {
    for (const dictionary of [{ name: 'en', value: en as Dict }, { name: 'ar', value: ar as Dict }, { name: 'fa', value: fa as Dict }]) {
      for (const path of E28_KEYS) {
        const value = path.split('.').reduce<any>((node, part) => (node ? node[part] : undefined), dictionary.value)
        expect(value, `${dictionary.name} is missing ${path}`).toBeTruthy()
        expect(typeof value, `${dictionary.name}.${path} must be a non-empty string`).toBe('string')
        expect(String(value).trim().length, `${dictionary.name}.${path} must not be blank`).toBeGreaterThan(0)
      }
    }
  })

  it('never leaves an English placeholder in the Arabic or Persian control copy', () => {
    // Product/format names (JSON, GitHub, URL, admin, API) stay Latin on purpose; every other
    // word of a control label is translated.
    const technical = /\b(JSON|GitHub|github|URL|url|admin|API|HTTP|summary_ar|\.git)\b/g
    const latin = /[A-Za-z]{3,}/
    const keepLatin = ['projects.repoUrl', 'arenaAgent.draft.assignee']
    for (const path of E28_KEYS.filter((key) => !keepLatin.some((prefix) => key.startsWith(prefix)))) {
      const arValue = String(path.split('.').reduce<any>((node, part) => (node ? node[part] : undefined), ar as Dict)).replace(technical, '')
      const faValue = String(path.split('.').reduce<any>((node, part) => (node ? node[part] : undefined), fa as Dict)).replace(technical, '')
      expect(latin.test(arValue), `ar.${path} is still English: ${arValue}`).toBe(false)
      expect(latin.test(faValue), `fa.${path} is still English: ${faValue}`).toBe(false)
    }
  })

  it('keeps the approve / edit / reject verbs distinct in each language', () => {
    for (const dictionary of [en, ar, fa]) {
      const { approve, edit, reject } = dictionary.arenaAgent.draft
      expect(new Set([approve, edit, reject]).size).toBe(3)
    }
  })

  it('labels the repository URL field in all three languages', () => {
    expect(en.projects.repoUrlLabel).toMatch(/Repository URL/i)
    expect(ar.projects.repoUrlLabel).toBe('رابط المستودع')
    expect(fa.projects.repoUrlLabel).toBe('نشانی مخزن')
  })
})
