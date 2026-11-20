import type { ComponentType } from 'react'
import {
  Facebook,
  Github,
  Globe,
  Instagram,
  Linkedin,
  MessageCircle,
  Music2,
  Phone,
  Send,
  Twitter,
  UserRound,
  Youtube,
} from 'lucide-react'

/**
 * Icon per social platform key (see `lookups/phone.ts::platformKey`).
 *
 * Icons are decorative only — every card also prints the platform name, so a
 * new platform from a provider still renders correctly with the neutral
 * globe icon.
 */
const ICONS: Record<string, ComponentType<{ className?: string }>> = {
  facebook: Facebook,
  instagram: Instagram,
  whatsapp: MessageCircle,
  telegram: Send,
  twitter: Twitter,
  x: Twitter,
  linkedin: Linkedin,
  tiktok: Music2,
  youtube: Youtube,
  snapchat: Phone,
  github: Github,
  truecaller: Phone,
}

export function PlatformIcon({ platformKey, className }: { platformKey: string; className?: string }) {
  const Icon = ICONS[platformKey] ?? Globe
  return <Icon className={className ?? 'h-4 w-4'} />
}

/** Fallback avatar glyph used when the provider reports no picture. */
export function AvatarFallbackIcon({ className }: { className?: string }) {
  return <UserRound className={className ?? 'h-6 w-6'} />
}
