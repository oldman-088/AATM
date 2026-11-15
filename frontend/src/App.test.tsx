import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import App from './App'

// Mock fetch & WebSocket cleanly for unit testing UI + multilingual RTL switching
globalThis.fetch = vi.fn().mockResolvedValue({
  ok: true,
  json: async () => [],
}) as any

class MockWS {
  close() {}
}
;(globalThis as any).WebSocket = MockWS

describe('AATM Frontend & Multilingual RTL System', () => {
  it('renders navigation and switches cleanly between EN (LTR), AR (RTL), and FA (RTL)', () => {
    render(<App />)

    expect(screen.getByText('AATM')).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('ltr')
    expect(document.documentElement.lang).toBe('en')

    // Switch to Arabic
    fireEvent.click(screen.getByText('AR'))
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('ar')
    expect(screen.getByText('مدير فريق وكلاء الذكاء الاصطناعي')).toBeInTheDocument()

    // Switch to Persian
    fireEvent.click(screen.getByText('FA'))
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('fa')
    expect(screen.getByText('مدیر تیم عامل‌های هوش مصنوعی')).toBeInTheDocument()

    // Switch back to English
    fireEvent.click(screen.getByText('EN'))
    expect(document.documentElement.dir).toBe('ltr')
    expect(document.documentElement.lang).toBe('en')
  })
})
