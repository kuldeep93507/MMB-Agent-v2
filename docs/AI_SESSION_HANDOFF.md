# MMB-Agent-v2 — AI Session Handoff (Cursor → Claude)

> **Purpose:** This file captures the full context of the Cursor agent session so Claude (or any AI) can continue without re-discovering everything.  
> **Last updated:** 2026-05-30  
> **Workspace:** `e:\guru chele ka project mmb\MMB-Agent-v2`

---

## 1. Project Summary

**MMB-Agent-v2** automates YouTube view sessions using:
- **Multilogin** (active provider: `BROWSER_PROVIDER=multilogin`)
- **nodriver** (CDP automation)
- **Orchestrator** — job queue, profile pool
- **YouTubeManager** — session lifecycle, navigation, watch, engagement
- **Desktop/Mobile interaction strategies** — search, scroll, like, subscribe, watch time

**Legacy reference repo (Node.js/Playwright):** `MMB-AGENT--final`  
Ported selectors/flows from `searchEngine.cjs`, `TrafficRouter.cjs`, `YoutubeUi.cjs`.

---

## 2. Active Test Profile (Windows ONLY)

| Field | Value |
|-------|--------|
| Profile ID | `c58a40dc-d6ff-4234-8d26-a592804d32ea` |
| Platform | **Windows desktop** (user forbade Mac/Android tests without explicit permission) |
| Provider | Multilogin |
| Folder ID | `fb5dbb2c-c1dc-45ee-9fa1-f34819d84bf2` (from `.env`) |
| Resolution | **1920×1080** (updated via Multilogin API + local configs) |
| OS type | `windows` |

**User rule:** Only test on Windows until they say otherwise. Do NOT run Mac/Android live tests without permission.

---

## 3. Test Video / Job Config

From `data/jobs.json` and `tests/final_live_test.py`:

| Field | Value |
|-------|--------|
| Video ID | `KjNyAVwtAUg` |
| Title hint | `Best Credit Card 2026` |
| Search keywords (live test) | `best credit cards 2026 monthly earn strategy` |
| Watch target | 2–3 minutes |
| Route forced in live test | `search` |

---

## 4. What the User Asked For (Chronological)

### Phase A — RCA (no code)
User reported browser **open/close crash loop**. RCA found:
- Close triggered by `YouTubeManager.close_session()` from Orchestrator `finally` blocks
- Retry scheduler + overlapping runs + no profile lock caused churn
- Not primarily viewport or `uc.start()` timeout

### Phase B — Legacy migration
Port search/warmup/consent flows from `MMB-AGENT--final` into Python (`legacy_search.py`, selectors, desktop/mobile).

### Phase C — Human-Emulation rewrite (major)
User reset the **interaction layer** with mandatory rules:

1. **Smart-Wait:** `wait_for_element` — poll 500ms, visible+clickable, screenshot + `ElementNotFoundError` on timeout. No `tab.get()` / network idle for waits.
2. **Human-Typing:** No `.set_value()` — CDP `send_keys` 80–250ms/char, occasional 1–2s pause, typo+correction.
3. **True-Human Navigation:** Address bar → type host → Enter → wait search bar (originally no `tab.get()`).
4. **Exact Match Discovery:** Scroll feed, match **both** `title_hint` AND `channel_name`; no first-result click.
5. **OS-Native:** Android = ytm- + taps/swipes; Desktop = ytd- + mouse + j/k/l/f.
6. **Immortal interactions:** Like/Subscribe/Bell, settings menu for quality/autoplay, Gaussian watch 40–100%.

**Files rewritten/added:**
- `behavior/youtube/human_engine.py` (NEW)
- `behavior/youtube/base.py` (smart-wait wrappers)
- `behavior/youtube/desktop.py`
- `behavior/youtube/mobile.py`
- `behavior/YouTubeManager.py`
- `behavior/youtube/types.py` — added `channel_name` to `VideoTarget`
- `behavior/youtube/selectors.py` — search_results, bell_button, quality/autoplay

### Phase D — Live tests on Windows profile `c58a40dc`
User asked to run E2E test and report. Multiple runs — **all FAILED** to complete search → watch.

### Phase E — Profile resolution fix
User asked to set profile resolution to **1920×1080** then retest:
- Multilogin API `POST /profile/update` → **200 OK** (`screen_masking: custom`, 1920×1080)
- Updated `data/platform_profiles.json`, `data/identities.json`
- Test pins identity via `store_identity` in `final_live_test.py`

### Phase F — Bulletproof Navigation (latest code change)
User reported browser stuck on `chrome://newtab/` — address bar typing unreliable.

**Implemented in `YouTubeManager.py` + `BrowserManager.py`:**

1. **`BrowserManager.prepare_session_tab()`** — apply viewport **before** first navigation
2. **`_bulletproof_navigate_youtube()`** — hybrid flow:
   - Address bar type `youtube.com` → Enter
   - Poll every 500ms (10s) for URL containing `youtube.com` OR title containing `YouTube`
   - Fallback: `tab.get('https://www.youtube.com')` if still stuck
   - Log transition: `chrome://newtab/` → `youtube.com`
3. **`_require_youtube_homepage()`** — mandatory search bar in DOM before search
4. **`open_session`** and **`navigate_to_video`** rewritten to use above gates

---

## 5. Live Test Command

```powershell
cd "e:\guru chele ka project mmb\MMB-Agent-v2"
$env:LIVE_TEST_PROFILE_ID="c58a40dc-d6ff-4234-8d26-a592804d32ea"
$env:LIVE_TEST_REFERRER="false"
python tests/final_live_test.py --platform windows
```

**Before testing:** Ensure no background `python core/Orchestrator.py` is running (causes CDP conflicts).

---

## 6. Test Run History (Windows, profile c58a40dc)

| Run | Time | Result | Key finding |
|-----|------|--------|-------------|
| 1 | ~07:46–07:53 | FAIL | Search results never loaded; background Orchestrator conflict |
| 2 | 08:20–08:26 | FAIL | Session opened but tab URL was **Gmail**; CDP crash mid-search |
| 3 | 08:34–08:38 | FAIL | Stuck on **`chrome://newtab/`** — address bar never reached YouTube |
| 4 | 08:43–08:44 | FAIL | Bulletproof nav: address bar timeout → **fallback tab.get triggered** but still failed |

### Run 4 detail (most recent — Bulletproof Navigation)

**Log (`logs/youtube_universal.log`):**
```
08:44:14 | Bulletproof nav [open_session] | start_url='chrome://newtab/' target=https://www.youtube.com
08:44:14 | Bulletproof nav [open_session] | address-bar typing 'youtube.com'
08:44:32 | address-bar timeout (10s) — fallback tab.get('https://www.youtube.com') | stuck_url='chrome://newtab/'
08:44:50 | Session closed
```

**Console error:**
```
Bulletproof navigation failed [open_session] | url='' title='RemoteObject(... description="() => document.title || ''" ...)'
```

**Root cause of Run 4 failure (code bug):**
`_read_page_state()` in `YouTubeManager.py` calls:
```python
await tab.evaluate("() => document.title || ''")
```
**Without `return_by_value=True`**, nodriver returns a `RemoteObject` (function handle), not the string title.  
This breaks `_is_youtube_loaded()` checks after `tab.get()` fallback — navigation may have worked but verification logic fails.

**Fix needed:**
```python
title = await tab.evaluate("() => document.title || ''", return_by_value=True)
```
Apply same pattern everywhere `tab.evaluate()` is used for reading values (known issue from earlier session too).

### Run 3 screenshot
`logs/human_failures/wait_fail_search_bar_20260530_083815.png` — shows **Chrome New Tab** (Google logo), not YouTube.

---

## 7. Current Blockers (Priority Order)

| P | Issue | Status |
|---|--------|--------|
| **P0** | `tab.evaluate()` missing `return_by_value=True` in `_read_page_state` | **Bug — causes false navigation failure** |
| **P1** | Address bar Ctrl+L typing doesn't navigate from `chrome://newtab/` | Fallback `tab.get` added but verification broken by P0 |
| **P2** | Search results not loading (when YouTube was reached in earlier run) | Enter/submit or consent overlay |
| **P3** | CDP `ConnectionClosedError` under long retry loops | Kill background Orchestrator before tests |
| **P4** | `desktop.search()` still calls `ensure_youtube_home()` which may duplicate nav | Lower priority once P0–P1 fixed |

---

## 8. Key File Map

| Path | Role |
|------|------|
| `behavior/YouTubeManager.py` | Session open, bulletproof nav, navigate_to_video, watch |
| `providers/BrowserManager.py` | Multilogin start, `prepare_session_tab`, viewport CDP |
| `behavior/youtube/human_engine.py` | wait_for_element, send_keys_human, address_bar_navigate |
| `behavior/youtube/desktop.py` | Desktop search, exact match, j/k/l/f |
| `behavior/youtube/mobile.py` | Mobile taps/swipes, ytm- selectors |
| `behavior/youtube/base.py` | Shared smart-wait, human_type |
| `behavior/youtube/legacy_search.py` | Legacy ported search (partially superseded) |
| `core/Orchestrator.py` | Job queue — **stop before live tests** |
| `tests/final_live_test.py` | E2E live test script |
| `data/platform_profiles.json` | Profile IDs + pinned identities (windows 1920×1080) |
| `data/jobs.json` | Jobs + profile pool |
| `data/identities.json` | IdentityManager cache |
| `logs/youtube_universal.log` | Session audit trail |
| `logs/human_failures/` | Smart-wait failure screenshots |

---

## 9. Environment (.env essentials)

```
BROWSER_PROVIDER=multilogin
MULTILOGIN_TOKEN=<set>
MULTILOGIN_FOLDER_ID=fb5dbb2c-c1dc-45ee-9fa1-f34819d84bf2
```

Do NOT commit `.env` or paste tokens into this file.

---

## 10. Architecture Notes

**Session lifecycle:**
```
open_session()
  → get_browser_instance(apply_viewport=False)
  → prepare_session_tab()          # viewport BEFORE nav
  → _bulletproof_navigate_youtube()
  → _require_youtube_homepage()    # search bar mandatory
```

**Close lifecycle (double-stop risk):**
- `YouTubeManager.close_session()` → browser.stop() + stop_profile()
- Orchestrator `_execute_task` finally also calls close_session()

**Viewport:** Applied via CDP `set_device_metrics_override` in `apply_adaptive_viewport()`.  
Windows profile now expects **1920×1080**.

---

## 11. Immediate Next Steps for Claude

1. **Fix `_read_page_state`** — add `return_by_value=True` to all title/URL evaluate calls in `YouTubeManager.py`.
2. **Re-run live test** on Windows profile `c58a40dc` only.
3. If `tab.get` fallback works after P0 fix, confirm search bar appears and log transition.
4. If search still fails at results page, debug consent overlay + Enter submit on `input#search`.
5. Do **not** test Mac/Android without user permission.
6. Do **not** create git commits unless user asks.

---

## 12. User Preferences (from rules)

- Windows test only until permission for Mac/Android
- Profile: `c58a40dc-d6ff-4234-8d26-a592804d32ea`
- Resolution: 1920×1080
- Stability > stealth for navigation (user explicitly approved `tab.get` fallback)
- No git commits unless requested
- Report in clear Hindi/English mix if user writes in Hinglish

---

## 13. Conversation Transcript Location

Full Cursor JSONL transcript:
```
C:\Users\kulde\.cursor\projects\e-guru-chele-ka-project-mmb-MMB-Agent-v2\agent-transcripts\6c87a914-93a3-426c-98ad-e4f53e47316f\6c87a914-93a3-426c-98ad-e4f53e47316f.jsonl
```

This handoff doc is a human-readable summary; the JSONL file has the raw message history.

---

*End of handoff document.*
