# AATM v3.6.0 — stable

## بالعربية

إصدار التثبيت: يبدأ AATM بأمر واحد، ويعيد الاتصال ويستأنف العمل وحده، ووصل كل إصلاح عبر PR مراجَع مع CI على `r8-aatm`.

**مطلوب بعد التحديث:** ثبّت السكربت 2.2.0 في كل نافذة Arena مرة واحدة (ولّده من لوحة التحكم ثم اضغط Install في كل نافذة).

- **أمر واحد للتشغيل:** `~/AATM/scripts/ubuntu/start-all.sh` يشغّل كل شيء، وينتظر الجاهزية، ويفتح نوافذ Arena المفعّلة فقط، ثم يعرض «AATM جاهز ✅ (N/4 متصلين)» أو ما ينقص بالضبط. والإيقاف بـ `stop-all.sh` دون إنهاء قسري.
- **إعادة الاتصال تلقائياً:** بعد إعادة تشغيل الجسر تعود النوافذ خلال دقائق دون F5.
- **الاستئناف بعد إعادة التشغيل:** تكمل المهام ووظائف المدير من حيث توقفت، ولا يُرسل أي أمر مرتين، وتعود نافذة العامل إلى محادثة مهمتها.
- **PR موثوق:** لكل مهمة PR واحد بعنوان `[AATM-<task>]` نحو الفرع الأساسي للمشروع. لا دمج من Arena أبداً، والمدير يخطط ويراجع فقط.
- **بلا بيانات عرض:** `DEMO_MODE=false` افتراضياً، والواجهة تعرض حالات فراغ وخطأ بالعربية أولاً.
- **مراقب هادئ:** لا تنبيهات وكل شيء سليم، والمشاكل الحقيقية ما زالت تُنبَّه.
- **متابعة تلقائية عند حد استدعاء الأدوات** ضمن حد Keep working، ولا متابعة أثناء فحص بشري.
- **تنبيهات تيليجرام (اختيارية، إرسال فقط):** معطلة حتى تملأ قيمها في `.env` (الخطوات في `docs/SETUP_AR.md` القسم 13).
- **نقاط رجوع:** `snapshot.sh` و`rollback.sh <tag>`.

**ملاحظة تشغيل:** بعد أي إعادة تشغيل للجهاز يكفي أمر واحد: `~/AATM/scripts/ubuntu/start-all.sh`. موافقة Arena تسمح بأربعة ملفات تعريف كحد أقصى. للاستخدام الداخلي فقط.

## English

Stabilization release: AATM starts with one command, reconnects and resumes on its own, and every fix landed through a reviewed PR with CI on `r8-aatm`.

**Required after updating:** install userscript 2.2.0 once in every Arena window (generate it from the dashboard, then press Install in each window).

- **One-command start:** `~/AATM/scripts/ubuntu/start-all.sh` starts everything, waits for health, opens only the enabled Arena windows and reports "AATM جاهز ✅ (N/4 متصلين)" or exactly what is missing. `stop-all.sh` stops gracefully.
- **Automatic reconnection:** after a bridge restart the windows come back within minutes, without F5.
- **Resume after restart:** tasks and manager jobs continue where they stopped, no prompt is sent twice, and a worker window returns to its task's chat.
- **Reliable PRs:** one `[AATM-<task>]` PR per task into the project's base branch. Nothing merges from Arena; the manager only plans and reviews.
- **No demo data:** `DEMO_MODE=false` by default; Arabic-first empty and error states.
- **Quiet watchdog:** no alerts while healthy; real problems still alert.
- **Auto-continue on the tool-call limit**, within the Keep working limit and never during a human check.
- **Telegram alerts (optional, send-only):** off until the `.env` values are set (`docs/SETUP_AR.md` §13).
- **Rollback snapshots:** `snapshot.sh` and `rollback.sh <tag>`.

**Operating note:** after any reboot one command is enough: `~/AATM/scripts/ubuntu/start-all.sh`. The Arena approval allows at most 4 profiles. Internal use only.

Full list: [CHANGELOG.md](../CHANGELOG.md).
