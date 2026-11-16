/** Tiny syntax highlighter for the Bridge screens.
 *
 * Deliberately dependency-free and regex based: it never injects HTML, it returns spans with a
 * colour class. Unknown languages fall back to a neutral single token.
 */

export type Token = { cls: string; text: string }

const PY_KEYWORDS = new Set(
  'and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case'.split(
    ' ',
  ),
)
const JS_KEYWORDS = new Set(
  'async await break case catch class const continue debugger default delete do else enum export extends finally for from function get if implements import in instanceof interface let new of package private protected public return set static super switch this throw try type typeof var void while yield'.split(
    ' ',
  ),
)
const SHELL_KEYWORDS = new Set('if then else elif fi for while do done case esac function export local return echo cd set source'.split(' '))
const SQL_KEYWORDS = new Set('select from where insert into update delete join on group by order limit values table create index'.split(' '))

function keywordsFor(language: string): Set<string> {
  switch (language) {
    case 'typescript':
    case 'tsx':
    case 'javascript':
    case 'jsx':
    case 'json':
    case 'vue':
      return JS_KEYWORDS
    case 'bash':
      return SHELL_KEYWORDS
    case 'sql':
      return SQL_KEYWORDS
    default:
      return PY_KEYWORDS
  }
}

const LITERAL_RE =
  /(#.*$|\/\/.*$|\/\*.*?\*\/)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|(@[A-Za-z_][\w.]*|\$[A-Za-z_][\w]*)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][\w]*)(?=\()|([A-Za-z_][\w]*)/g

/** Split one source line into coloured tokens. */
export function highlightLine(line: string, language = 'text'): Token[] {
  if (!line) return [{ cls: 'tok-default', text: '' }]
  if (language === 'text' || language === 'markdown') {
    return [{ cls: line.startsWith('+') ? 'tok-str' : line.startsWith('-') ? 'tok-err' : 'tok-default', text: line }]
  }
  const keywords = keywordsFor(language)
  const tokens: Token[] = []
  let cursor = 0
  LITERAL_RE.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = LITERAL_RE.exec(line)) !== null) {
    if (match.index > cursor) tokens.push({ cls: 'tok-default', text: line.slice(cursor, match.index) })
    const [text, comment, string, decorator, number, call, word] = match
    if (comment) tokens.push({ cls: 'tok-com', text })
    else if (string) tokens.push({ cls: 'tok-str', text })
    else if (decorator) tokens.push({ cls: 'tok-dec', text })
    else if (number) tokens.push({ cls: 'tok-num', text })
    else if (call) tokens.push({ cls: keywords.has(call) ? 'tok-kw' : 'tok-fn', text })
    else if (word) {
      if (keywords.has(word)) tokens.push({ cls: 'tok-kw', text })
      else if (/^(True|False|None|true|false|null|undefined|self|this)$/.test(word)) tokens.push({ cls: 'tok-dec', text })
      else tokens.push({ cls: 'tok-default', text })
    }
    cursor = match.index + text.length
  }
  if (cursor < line.length) tokens.push({ cls: 'tok-default', text: line.slice(cursor) })
  return tokens.length ? tokens : [{ cls: 'tok-default', text: line }]
}

export type CodeLine = { text: string; kind?: 'add' | 'del' | 'ctx' | 'gap' | 'plain'; fresh?: boolean }

/** Build the lines a screen shows from a redacted `agent.code` frame. */
export function linesFromFrame(frame: any, previous: CodeLine[] | undefined): CodeLine[] {
  const action = String(frame?.action || 'write')
  if (frame?.diff && Array.isArray(frame.diff) && action === 'edit') {
    return frame.diff.map((item: any) => ({
      text: String(item.text ?? ''),
      kind: item.type === 'add' ? 'add' : item.type === 'del' ? 'del' : item.type === 'gap' ? 'gap' : 'ctx',
    }))
  }
  if (Array.isArray(frame?.output_lines) && (action === 'test' || action === 'lint' || action === 'review')) {
    return frame.output_lines.map((line: any) => ({ text: String(line), kind: 'plain' as const }))
  }
  const text = String(frame?.content ?? '')
  const lines: CodeLine[] = text.split('\n').map((line) => ({ text: line, kind: 'plain' as const }))
  while (lines.length > 1 && lines[lines.length - 1].text === '') lines.pop()
  // Mark newly appended lines so the screen can fade them in (the "streaming" feel).
  const previousTexts = new Set((previous || []).map((line) => line.text))
  if (previous && previous.length) {
    for (const line of lines) {
      if (!previousTexts.has(line.text)) line.fresh = true
    }
  }
  return lines
}
