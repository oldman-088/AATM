import React, { createContext, useContext, useEffect, useState } from 'react'
import { apiFetch, getToken, setToken } from '../lib/api'

export interface UserProfile {
  id: string
  email: string
  full_name: string
  role: string
  language: 'en' | 'ar' | 'fa'
  theme: 'dark' | 'light' | 'system'
  must_change_password?: boolean
}

interface AuthContextValue {
  user: UserProfile | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => void
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>
  syncPreferences: (prefs: Partial<Pick<UserProfile, 'language' | 'theme'>>) => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const token = getToken()
    if (!token) {
      setLoading(false)
      return
    }
    apiFetch<UserProfile>('/auth/me')
      .then(setUser)
      .catch(() => {
        setToken(null)
        setUser(null)
      })
      .finally(() => setLoading(false))
  }, [])

  const login = async (email: string, password: string) => {
    const res = await apiFetch<{ access_token: string; user: UserProfile }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    })
    setToken(res.access_token)
    setUser(res.user)
  }

  const logout = () => {
    setToken(null)
    setUser(null)
  }

  const changePassword = async (currentPassword: string, newPassword: string) => {
    const res = await apiFetch<{ ok: boolean; user: UserProfile }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    })
    setUser(res.user)
  }

  const syncPreferences = async (prefs: Partial<Pick<UserProfile, 'language' | 'theme'>>) => {
    if (!getToken()) return
    try {
      const updated = await apiFetch<UserProfile>('/auth/preferences', {
        method: 'PATCH',
        body: JSON.stringify(prefs),
      })
      setUser(updated)
    } catch {
      // keep local state if offline
    }
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, changePassword, syncPreferences }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
