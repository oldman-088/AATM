# AATM v3.7.0 — self-healing team

## بالعربية

فريق يعمل بالتوازي ويصلح أغلب انقطاعاته بنفسه: مدير وثمانية عمال على Arena، وكل تدخل يدوي حدث في التشغيل الحي بين 22 و26 نوفمبر صار خطوة تلقائية (التفاصيل في `MAINTENANCE_LOG.md`، البنود 1–82).

- **واجهة NEXUS COMMAND:** ثمانية أقسام بالعربية والإنجليزية والفارسية، مع لوحة «يحتاجك الآن».
- **عمل متوازٍ:** العامل الذي ينتظر PR الخاص به المراجعة يبدأ مهمته التالية. والعمال الخاملون يراجعون طلبات الدمج بالتوازي مع المدير.
- **دمج تلقائي آمن:** يُدمج الطلب عند موافقة المدير ونجاح CI على آخر commit وعدم وجود تعارض، طلباً واحداً في كل مرة.
- **إصلاح ذاتي:** المراقب يعيد تحميل النافذة المنقطعة، ويعيد تشغيلها إذا لم يكفِ التحميل، ويعيد فتح النافذة المخفية على الشاشة. ثم ينبّهك إذا لم ينجح. والفحص البشري لا يُلمس أبداً.
- **إعادة تشغيل آمنة:** `scripts/ubuntu/safe_restart.py` يعيد تشغيل الخادم فقط حين لا تجري أي مراجعة.
- **فشل الإرسال:** إذا وصلت الرسالة فعلاً فلا تُرسل مرتين، والخطأ يسجّل ما ظهر على الصفحة.
- **صفحة GitHub:** إضافة مستودعات أي حساب مسجّل في GitHub CLI، ويعمل كل مستودع بحسابه دون كشف أي توكن.
- **أدوات:** `aatm doctor` للتشخيص، تشغيل تلقائي عند الإقلاع، ونسخ احتياطي لقاعدة البيانات.

**التشغيل:** `~/AATM/scripts/ubuntu/start-all.sh`. أي إعادة تشغيل للخادم تكون عبر `scripts/ubuntu/safe_restart.py`.

**التحقق:** `verify.sh` نجح بالكامل (14/14، 832 اختباراً) على جهاز هادئ، وCI أخضر على `main`.

## English

A team that works in parallel and repairs most interruptions by itself: one manager and eight Arena workers. Every manual rescue from the 2026-11-22..26 live runs became an automatic step (see `MAINTENANCE_LOG.md`, items 1-82).

- **NEXUS COMMAND UI:** eight sections in EN/AR/FA, with a "Needs you now" panel.
- **Parallel work:** a worker whose PR waits for review starts its next task; idle workers review PRs in parallel with the manager.
- **Safe auto-merge queue:** manager approval + green CI on the head commit + no conflict, one PR at a time.
- **Self-healing:** the watchdog reloads a disconnected window, restarts it when a reload is not enough, and brings a hidden window back on screen, then alerts the owner if that fails. Human checks are never touched.
- **Safe restart:** `scripts/ubuntu/safe_restart.py` restarts the backend only when no review is running.
- **send_failed:** a message that did go out is never sent twice, and the error records what the page showed.
- **GitHub page API:** repositories of any account signed in to the GitHub CLI, each run as its account; tokens are never returned or stored.
- **Tools:** `aatm doctor`, boot autostart, SQLite online backups.

**Start:** `~/AATM/scripts/ubuntu/start-all.sh`. Restart the backend only through `scripts/ubuntu/safe_restart.py`.

**Verification:** `verify.sh` passed 14/14 (832 backend tests) on a quiet machine; CI green on `main`.
