/** @type {import('tailwindcss').Config} */
// NEXUS COMMAND design tokens. Colours are CSS variables (src/index.css) so the dark and light themes swap
// them in one place; `<alpha-value>` keeps opacity modifiers such as bg-nexus-card/70 working.
const v = (name) => `rgb(var(--nx-${name}) / <alpha-value>)`

export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        nexus: {
          bg: v('bg'),
          surface: v('surface'),
          card: v('card'),
          raised: v('raised'),
          border: v('border'),
          text: v('text'),
          muted: v('muted'),
          dim: v('dim'),
          cyan: v('cyan'),
          violet: v('violet'),
          success: v('success'),
          warning: v('warning'),
          error: v('error'),
          'on-accent': v('on-accent'),
        },
        // Existing screens use slate and sky; they now resolve to the NEXUS palette so every page matches.
        slate: {
          950: v('bg'),
          900: v('card'),
          800: v('border'),
          700: v('border-strong'),
          600: v('dim'),
          500: v('dim'),
          400: v('muted'),
        },
        sky: {
          300: v('cyan-soft'),
          400: v('cyan'),
          500: v('cyan'),
          600: v('cyan-strong'),
          700: v('cyan-deep'),
          950: v('bg'),
        },
      },
      fontFamily: {
        sans: ['"IBM Plex Sans Arabic"', 'system-ui', '-apple-system', '"Segoe UI"', 'Tahoma', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
        display: ['Orbitron', '"IBM Plex Sans Arabic"', 'sans-serif'],
      },
      boxShadow: {
        glow: '0 0 0 1px rgb(var(--nx-cyan) / 0.35), 0 0 24px -6px rgb(var(--nx-cyan) / 0.45)',
        card: '0 1px 0 0 rgb(255 255 255 / 0.03) inset, 0 8px 24px -12px rgb(0 0 0 / 0.6)',
      },
      keyframes: {
        'nx-fade-in': { from: { opacity: '0', transform: 'translateY(4px)' }, to: { opacity: '1', transform: 'none' } },
        'nx-pulse': { '0%,100%': { opacity: '1' }, '50%': { opacity: '0.45' } },
      },
      animation: {
        'nx-fade-in': 'nx-fade-in 180ms ease-out both',
        'nx-pulse': 'nx-pulse 1.8s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
