# Third-Party Notices

AATM includes material adapted from the following third-party project.

## Arena Auto Continue

- Project: https://github.com/anuragkumarnishad/arena-auto-continue
- File used as the foundation: `arena-auto-continue.user.js` (v2.9.0, upstream commit
  `1fce9213bc7f75a5d6c9cce78ae13756d9733c59`)
- Author: Anurag Kumar Nishad
- License: MIT
- Used in: the generated Arena connector userscript (`backend/app/browser/arena_connector.py`,
  `USERSCRIPT_BODY`). The generated script carries an attribution comment pointing to this file.

Adapted functionality: element visibility check, composer detection (lowest visible textarea /
contenteditable / textbox), React-safe text insertion (`insertText` with native value setter and
input/change event fallback), send-button scoring with form-submit and Enter fallbacks and send
verification with an Enter retry, Stop-button detection, "Keep working" detection, feedback-widget
("Yes") dismissal to reveal the composer, composer cleanup after a failed send, the idle
double-check, sleeping-tab detection, main-frame-only execution and single-instance cleanup.

Not carried over, on purpose: the on-page control panel, timers that send messages on their own,
randomised "human-like" delays and breaks, the action/hour safety brake (AATM's bridge and backend
decide every action and the owner configures continuation limits), the silent-audio and third-party
WebSocket keep-alive tricks (the userscript talks only to the local AATM bridge), browser-storage
persistence, and file/clipboard attachment. Human-verification pages are only detected and reported;
nothing is solved or bypassed.

```
MIT License

Copyright (c) 2026 Anurag Kumar Nishad

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
