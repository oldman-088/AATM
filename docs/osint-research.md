# OSINT research: phone numbers and social media

**Scope and review date:** 20 November 2026. This is a design survey, not an endorsement of scraping or a claim that every upstream project is currently maintained. Verify each project's current license, maintenance status, target-site terms, and local law before adoption. Repository links point to upstream GitHub projects. Do not copy code unless its license and compatibility explicitly permit it; this document records product and architecture ideas only.

## Project survey

| Project | Link | Ideas and data sources | Advantages | Limitations / risks |
|---|---|---|---|---|
| Sherlock | [sherlock-project/sherlock](https://github.com/sherlock-project/sherlock) | Checks a supplied username against a catalog of site-specific URL patterns and response rules. | Simple, extensible site registry; clear username-search use case. | Username existence is not identity proof; false positives, site changes, and request volume/terms concerns. |
| Maigret | [soxoj/maigret](https://github.com/soxoj/maigret) | Username searches using a maintained database of site metadata and response indicators; supports report output. | Rich metadata and reporting concept; useful adapter/catalog model. | Results can be stale or ambiguous; broad checks may generate many requests. |
| Blackbird | [p1ngul1n0/blackbird](https://github.com/p1ngul1n0/blackbird) | Username enumeration across online services, with service-specific checks. | Focused workflow and useful ideas for categorizing evidence. | Dependent on public web behavior; sites can block or change; avoid treating matches as verified identity. |
| WhatsMyName | [WebBreacher/WhatsMyName](https://github.com/WebBreacher/WhatsMyName) | Community-maintained JSON data describing username checks across websites. | Separates check definitions from execution; a potential source of schema-design ideas. | Data quality and licensing/attribution must be reviewed; site checks become stale. |
| Social Analyzer | [qeeqbox/social-analyzer](https://github.com/qeeqbox/social-analyzer) | Profile discovery through multiple social networks and related analysis/reporting. | Demonstrates provider abstraction and consolidated results. | Some techniques may rely on undocumented behavior; substantial maintenance and privacy risks. |
| PhoneInfoga | [sundowndev/phoneinfoga](https://github.com/sundowndev/phoneinfoga) | Phone-number parsing/formatting and lookup using public search engines or configured sources. | Good separation of number normalization from enrichment; useful E.164 handling concept. | Number metadata does not identify a subscriber; source coverage varies and commercial sources may require keys. |
| holehe | [megadose/holehe](https://github.com/megadose/holehe) | Checks whether an email appears registered on services via service-specific flows. | Illustrates provider modules and carefully scoped result reporting. | Registration checks can enable account enumeration, abuse services, and depend on private or unstable flows; not suitable to reproduce. |
| Toutatis | [Maalfer/Toutatis](https://github.com/Maalfer/Toutatis) | Instagram-oriented account/profile information collection. | Shows why a platform-specific adapter boundary may be useful. | Platform access and authentication behavior may change; do not use unofficial access, session cookies, or bypass controls. |
| OSINTgram | [Datalux/Osintgram](https://github.com/Datalux/Osintgram) | Instagram-focused collection and command-line investigation workflows. | Useful workflow/reporting concepts for an authorized case context. | Risks include authenticated scraping, account restrictions, and exposure of personal data; not a basis for automation against Instagram. |
| Twint | [twintproject/twint](https://github.com/twintproject/twint) | Historical X/Twitter collection without the official API, using site behavior. | Illustrates the fragility of coupling to a website's internal endpoints. | Historical/unofficial approach, likely incompatible with current platform controls and terms; explicitly do not adopt. |

## Provider feasibility and policy matrix

“Blocking” is a qualitative operational expectation, not a guarantee. Official API access is subject to eligibility, pricing, quota, permissions, and policy; public pages can change or require login at any time. No provider here should bypass authentication, rate limits, CAPTCHAs, or access controls.

| Platform | Preferred search method | Login needed? | Blocking risk | Priority |
|---|---|---:|---|---|
| WhatsApp | User-provided number normalization and opt-in contact/ownership verification; official WhatsApp Business Platform only for authorized business messaging, not arbitrary identity search. | Yes for business API; contact verification is user-mediated. | High for automated lookups; API access is permissioned. | Low |
| Telegram | Official Bot API for interactions in chats where the bot is authorized; user-supplied public username/link, manually reviewed. Bot API is not a global people-search API. | Bot token for bot use; no automated account enumeration. | Medium–high for automation beyond intended bot use. | Medium (authorized bot workflows only) |
| Facebook | Official Meta Graph API for granted permissions and assets; user-provided public URL for manual review. | Yes, app/user authorization and permissions. | High for unauthorized collection. | Low |
| Instagram | Official Instagram/Meta APIs only for eligible authorized professional accounts and approved use cases; manual review of user-provided public URL. | Yes, authorization required. | High for automated public-profile scraping. | Low |
| X | Official X API under current access tier and developer policies; otherwise user-provided URL and manual review. | Yes for API access. | Medium–high; quotas and tier access apply. | Medium (API feasibility gate) |
| TikTok | Official TikTok APIs (for example, approved display/research capabilities) only within eligibility and granted scopes; manually review supplied links. | Yes. | High for unapproved collection. | Low |
| Snapchat | Official Snap Kit / approved APIs for their intended integrations; user-provided public profile link for manual review. No general user-search endpoint assumed. | Yes for integrations. | High for automated lookup. | Low |

## Decision: what AATM should and should not implement

### Implement

1. A provider-neutral **manual research record**: accept a phone number, username, platform, or URL supplied by an authorized user; retain the query only according to configured retention policy and show its provenance.
2. Safe phone normalization and validation (including country context and E.164 formatting), without claiming subscriber identity or silently querying carrier/account-registration databases.
3. A small adapter contract for **official, documented APIs** only: explicit authorization, permissions/scopes, quotas, timeout, source attribution, and a disabled-by-default configuration per provider. Do not ship credentials; use the repository's credential-reference conventions.
4. Evidence-oriented results containing source, retrieval time, method, and confidence/limitations. A candidate profile or username match is not identity verification.
5. Human confirmation before sensitive enrichment, with audit logging, data minimization, deletion/retention controls, and a documented lawful purpose.
6. Provider-independent tests using synthetic fixtures and mocked HTTP responses; tests must not contact social platforms or use real personal data.

### Avoid

- Copying upstream implementation code without confirming license compatibility and attribution requirements.
- Scraping logged-in pages, private data, or undocumented endpoints; harvesting phone/email-to-account registration signals; or bulk enumeration of people.
- CAPTCHA solving/bypass, proxy rotation to evade restrictions, fingerprint spoofing, rate-limit circumvention, session-cookie use, or any other anti-bot/access-control evasion.
- Inferring that a public profile belongs to the queried person from a name, avatar, number, or username match alone.
- Integrating unofficial WhatsApp/Instagram/Facebook/TikTok/Snapchat clients or treating public visibility as authorization.
- Persisting sensitive identifiers longer than required or exposing them in logs, prompts, analytics, or reports without an approved need.

**Recommendation:** Begin with normalization, user-submitted URLs, and an auditable manual workflow. Add a platform adapter only after its official API eligibility, current terms, jurisdictional basis, privacy impact, and credentials/consent flow have been reviewed and approved. This document is research guidance; it does not enable platform queries.
