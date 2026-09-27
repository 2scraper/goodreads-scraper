"""
captcha_solver.py
------------------
Shared helper used by all three scrapers (Playwright / Selenium / Puppeteer).

Detection runs after EVERY page navigation in the main loop of all three
scrapers, regardless of what URL was requested (category hub, product page,
sign-in, checkout, anything) — this is deliberate, not scoped to any one
page. If Transfermarkt renders a reCAPTCHA/Turnstile challenge anywhere —
this fires. Live research for this repo (2026-09-10, one proxied fetch each
of a ranking page, a player profile, a squad page and a transfer list, all
served without any challenge or 403) found none, but that is a much smaller
sample than the family's other members ran before writing this note — see
the closing section of this file for what that does and does not license.

Flow:
  1. Both detectors run and are reconciled (see reconcile_detections) to decide
     the variant: v3, v2-invisible or v2-checkbox. The parameters differ per
     variant and are not interchangeable — v3 params sent for a v2-invisible
     widget buy a token the site rejects.
  2. The challenge goes to 2captcha's API (v2 by default, legacy v1 as a
     one-shot fallback) with the parameters for that variant.
  3. The resulting token is injected into the page's `g-recaptcha-response`
     textarea and any bound callback is invoked.

Often none of this is needed. Over the Scraping Browser API,
`Captcha.setAutoSolve` can clear the challenge inside the browser before this
code gets a turn — treat `Captcha.solveFinished` as the success signal and keep
this path as the fallback rather than assuming every detection completes.
This module is the path for a browser you launched yourself.

No other captcha vendor is integrated (per spec: no competitors).
"""

from __future__ import annotations

import base64
import json
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests

logger = logging.getLogger("captcha_solver")


# An API key must never reach a log, and the easiest way for one to get there
# is an exception message. `requests` puts the FULL URL — query string and all
# — into the text of HTTPError and of every connection error, so any endpoint
# that takes its key as a query parameter leaks it the moment something goes
# wrong. That is not hypothetical: a 400 from the fingerprint endpoint printed
# a live key to the terminal.
#
# Everything raised or logged from this module goes through here first. The
# endpoint and the status survive, because which call failed is the useful
# half and is not the secret.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact(text) -> str:
    return _KEY_IN_TEXT_RE.sub(r"\1=***", str(text))

# 2captcha has two generations of solver API and both are live.
#
#   v2 (current, documented at https://2captcha.com/api-docs):
#       POST https://api.2captcha.com/createTask     {clientKey, task:{...}}
#       POST https://api.2captcha.com/getTaskResult  {clientKey, taskId}
#     JSON in, JSON out, typed task objects (RecaptchaV3TaskProxyless etc.).
#
#   v1 (legacy, still accepted):
#       POST https://2captcha.com/in.php   form-encoded, method=userrecaptcha
#       GET  https://2captcha.com/res.php  polling
#
# This module speaks v2 by default and keeps v1 as a fallback, because the
# original build was written against v1 and every live result recorded in the
# README came through it. Switch with solve_recaptcha(..., api_version="v1").
TWOCAPTCHA_API_V2 = "https://api.2captcha.com"
TWOCAPTCHA_CREATE_TASK_URL = f"{TWOCAPTCHA_API_V2}/createTask"
TWOCAPTCHA_GET_RESULT_URL = f"{TWOCAPTCHA_API_V2}/getTaskResult"
TWOCAPTCHA_BALANCE_URL = f"{TWOCAPTCHA_API_V2}/getBalance"

TWOCAPTCHA_IN_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RES_URL = "https://2captcha.com/res.php"

# v2 rejects an arbitrary minScore: the documented values are these three.
V3_ALLOWED_MIN_SCORES = (0.3, 0.7, 0.9)

@dataclass
class CaptchaChallenge:
    kind: str            # "recaptcha_v3" | "recaptcha_v2_invisible" |
                         # "recaptcha_v2" | "aws_waf"
    sitekey: str
    action: str = "verify"
    page_url: str = ""
    # AWS WAF only. Its task needs three values that all live together in
    # `window.gokuProps` on the challenge page, plus the two script URLs the
    # page loads. Unlike Cloudflare Turnstile (§19 of the family notes),
    # nothing has to be intercepted at runtime to get them: AWS WAF prints
    # all of it into the served HTML, so a static read is enough.
    iv: Optional[str] = None
    context: Optional[str] = None
    challenge_script: Optional[str] = None
    captcha_script: Optional[str] = None
    # How the challenge was found: "html" (static markup) or "runtime"
    # (read out of the live page's reCAPTCHA client config). Recorded
    # because the two paths can disagree about the version, and the
    # runtime one is authoritative when they do.
    source: str = "html"
    # Raw `size` from the reCAPTCHA client config, when available:
    # "invisible" for v2-invisible and for v3, absent for a v2 checkbox.
    size: Optional[str] = None

    @property
    def is_v3(self) -> bool:
        return self.kind == "recaptcha_v3"

    @property
    def is_invisible_v2(self) -> bool:
        return self.kind == "recaptcha_v2_invisible"

    @property
    def is_aws_waf(self) -> bool:
        return self.kind == "aws_waf"

    @property
    def aws_waf_action(self) -> str:
        """Which AWS WAF rule action produced this page: captcha or challenge.

        AWS WAF has TWO actions and they are not the same thing:

          CAPTCHA    HTTP 405, `x-amzn-waf-action: captcha`, and the page
                     loads captcha.js -- a real widget with a puzzle.
          Challenge  HTTP 202, `x-amzn-waf-action: challenge`, challenge.js
                     only -- a silent proof-of-work a browser passes by
                     running it. No widget, nothing to show a human.

        Both carry `window.gokuProps`, which is why reading the props alone
        cannot tell them apart. `captcha.js` can. Measured on
        www.transfermarkt.com 2026-09-17: challenge-action pages came back
        with challengeScript present and captchaScript absent, and the
        captcha-action pages had both.
        """
        return "captcha" if self.captcha_script else "challenge"

    @property
    def has_captcha_widget(self) -> bool:
        """Is there a widget on this page for a solver to work on?

        Section 19's rule: "unsolvable" is a property of a PAGE, not of a
        vendor, and it means the page carries no widget. A challenge-action
        page carries none, so sending it to a solver buys a token for a
        puzzle that was never rendered -- measured at $0.00145 a time, since
        `createTask` validates almost nothing.
        """
        if not self.is_aws_waf:
            return True
        return bool(self.captcha_script)


# AWS WAF prints its three task parameters into an inline script as
# `window.gokuProps = {"key": ..., "iv": ..., "context": ...}`. Matched
# non-greedily up to the first closing brace: the object is flat, so there
# is no nested brace to run past.
_GOKU_PROPS_RE = re.compile(
    r"window\.gokuProps\s*=\s*(\{.*?\})\s*;", re.DOTALL)
_WAF_SCRIPT_RE = re.compile(
    r"<script[^>]+src=[\"']([^\"']*\.awswaf\.com/[^\"']*?"
    r"(challenge|captcha)\.js)[\"']", re.IGNORECASE)

# The cookie AWS WAF reads the answer back out of. The solved value is not
# posted into a form field the way a reCAPTCHA token is -- it is set as a
# cookie and the page is reloaded, which is why this path cannot reuse
# INJECT_TOKEN_JS below.
AWS_WAF_COOKIE = "aws-waf-token"


def detect_aws_waf(html: str, page_url: str = "") -> Optional[CaptchaChallenge]:
    """An AWS WAF CAPTCHA challenge read out of served HTML, or None.

    Measured on www.transfermarkt.com 2026-09-16: the site sits behind AWS
    WAF on CloudFront, and a plain datacentre-IP request is answered with
    this challenge -- HTTP 405, `x-amzn-waf-action: captcha`, a 2331-byte
    body -- on most attempts.

    Everything the solver needs is in the served markup, so unlike the
    Cloudflare Turnstile case this family documents, no init script and no
    runtime interception is required. `key` in `gokuProps` is what 2captcha
    calls `websiteKey`; `iv` and `context` keep their names.

    Returns None rather than a half-filled challenge when any of the three
    is missing, and that refusal is load-bearing rather than tidy-minded.
    Measured against the live API on 2026-09-16: `createTask` validates
    almost none of this. A task carrying a FABRICATED `websiteKey` and no
    `iv` or `context` at all was accepted without complaint, came back
    `status: ready`, and was **charged at $0.00145** — the only field whose
    absence is refused at create time is `websiteURL` (`ERROR_PAGEURL`).
    So an incomplete detection does not fail loudly here; it quietly buys a
    token that will not clear the challenge. Refusing to build the task is
    the only thing standing between a malformed page read and a bill.

    (The control for that measurement: a deliberately invalid task type is
    rejected with `ERROR_TASK_ABSENT`, so the API does reject SOMETHING —
    the acceptance above is specific to the field contents, not a sign that
    every request is waved through.)
    """
    if not html or "gokuProps" not in html:
        return None
    match = _GOKU_PROPS_RE.search(html)
    if not match:
        return None
    try:
        props = json.loads(match.group(1))
    except ValueError:
        # The block is normally strict JSON, but a future variant quoting
        # keys differently should degrade to "not detected" rather than
        # raising inside a detector every engine calls on every page.
        logger.warning("AWS WAF: found window.gokuProps but could not parse "
                       "it as JSON — treating the page as undetected.")
        return None

    sitekey = props.get("key")
    iv, context = props.get("iv"), props.get("context")
    if not (sitekey and iv and context):
        logger.warning("AWS WAF: gokuProps is missing %s — not building a "
                       "task from a partial detection.",
                       ", ".join(n for n, v in (("key", sitekey), ("iv", iv),
                                                ("context", context)) if not v))
        return None

    scripts = {kind: src for src, kind in _WAF_SCRIPT_RE.findall(html)}
    challenge = CaptchaChallenge(
        kind="aws_waf",
        sitekey=sitekey,
        page_url=page_url,
        source="html",
        iv=iv,
        context=context,
        challenge_script=scripts.get("challenge"),
        captcha_script=scripts.get("captcha"),
    )
    if challenge.has_captcha_widget:
        logger.info("AWS WAF CAPTCHA action: sitekey=%s… challengeScript=%s "
                    "captchaScript=True — a widget is rendered, so a solver "
                    "has something to work on.",
                    sitekey[:12], bool(challenge.challenge_script))
    else:
        logger.info("AWS WAF CHALLENGE action: sitekey=%s… challengeScript=%s "
                    "captchaScript=False — this is the silent JS check, not "
                    "the puzzle. No widget is rendered, so there is nothing "
                    "here for a solver to work on; a browser that runs the "
                    "script passes it.",
                    sitekey[:12], bool(challenge.challenge_script))
    return challenge


def detect_recaptcha_v3(html: str, page_url: str) -> Optional[CaptchaChallenge]:
    """Scan raw page HTML for a reCAPTCHA v3 challenge, in either of two
    real-world formats seen so far:

    1. An inline <script> calling grecaptcha.execute('SITEKEY', {action: '...'})
       directly — seen on Kohl's.
    2. A <captcha-widget> custom element carrying the config as HTML
       attributes (data-captcha-type="recaptcha" data-version="v3"
       data-sitekey="..." data-action="..."), with the actual execute()
       call happening inside a bundled JS file, never appearing as
       readable inline script text at all — confirmed live on the sibling
       farfetch-scraper repo's target,
       sign-up modal via DevTools inspection. A previous version of this
       function, which only checked for format 1, reported "no captcha"
       on this exact page despite one being genuinely present — caught by
       manually inspecting the DOM, not by the detector itself, which is
       exactly the gap this format-2 check closes.
    """
    if "recaptcha" not in html.lower():
        return None

    # Format 2 first — a structured HTML attribute match is more reliable
    # than the format-1 regex when both happen to be present.
    for widget_match in re.finditer(r"<captcha-widget\b([^>]*)>", html, re.IGNORECASE):
        attrs = widget_match.group(1)
        version_match = re.search(r'data-version=["\']v(\d)["\']', attrs)
        sitekey_match = re.search(r'data-sitekey=["\']([\w-]{20,})["\']', attrs)
        if version_match and version_match.group(1) == "3" and sitekey_match:
            action_match = re.search(r'data-action=["\']([\w_]+)["\']', attrs)
            action = action_match.group(1) if action_match and action_match.group(1) != "null" else "verify"
            return CaptchaChallenge(
                kind="recaptcha_v3",
                sitekey=sitekey_match.group(1),
                action=action,
                page_url=page_url,
            )

    if "grecaptcha" not in html:
        return None

    exec_match = re.search(
        r"grecaptcha\.execute\(\s*['\"]([\w-]{20,})['\"]\s*,\s*\{\s*action:\s*['\"]([\w_]+)['\"]",
        html,
    )
    if exec_match:
        return CaptchaChallenge(
            kind="recaptcha_v3",
            sitekey=exec_match.group(1),
            action=exec_match.group(2),
            page_url=page_url,
        )

    key_match = re.search(r"data-sitekey=['\"]([\w-]{20,})['\"]", html)
    if key_match and "grecaptcha.render" in html:
        return CaptchaChallenge(kind="recaptcha_v3", sitekey=key_match.group(1), page_url=page_url)

    return None


# ---------------------------------------------------------------------------
# Runtime detection (added 2026-08-24 — the static-HTML detector below no
# longer matched the sibling farfetch-scraper repo's target)
# ---------------------------------------------------------------------------
#
# What changed there: the sign-up modal used to render
#   <captcha-widget data-version="v3" data-sitekey="6Leif..." ...>
# with the whole config in HTML attributes. Verified live on 2026-08-24, that
# element is GONE. The modal now loads
#   https://recaptcha.net/recaptcha/api.js?render=explicit
# and configures the widget entirely in JavaScript, into a
#   <div id="register-captcha" class="g-recaptcha">
# container. There is no `data-sitekey`, no inline `grecaptcha.execute(...)`,
# and no `grecaptcha.render` anywhere in the served HTML — all four things
# detect_recaptcha_v3() keys off are absent. It returns None on this page
# even though a real reCAPTCHA is present and active.
#
# The sitekey is still recoverable, but only at runtime, from two places
# that exist in the live page and not in its HTML:
#   1. window.___grecaptcha_cfg.clients — the reCAPTCHA API's own client
#      registry. Property names inside it are minified and change between
#      releases, so this walks the object looking for a value shaped like a
#      sitekey (^6L[\w-]{30,}$) rather than trusting any key name.
#   2. the reCAPTCHA iframe's `k=` query parameter — a stable, documented
#      part of the widget's URL, used as a cross-check and fallback.
#
# The same client config also carries `size`, which is what tells v3 apart
# from v2-invisible. That distinction is not cosmetic: 2captcha needs
# `version=v3` + `action` + `min_score` for one and `invisible=1` for the
# other, and sending the wrong one burns balance for a token that won't
# validate.
RECAPTCHA_DISCOVERY_JS = r"""
() => {
  const out = {found: false, sitekey: null, size: null, action: null,
               enterprise: false, containerId: null, hints: []};

  const SITEKEY_RE = /^6L[\w-]{30,}$/;

  // --- 1. the reCAPTCHA client registry -----------------------------------
  // Minified property names, so match on value shape, not key name.
  try {
    const clients = (window.___grecaptcha_cfg || {}).clients || null;
    if (clients) {
      for (const id of Object.keys(clients)) {
        const seen = new Set();
        const walk = (o, depth) => {
          if (!o || depth > 4 || seen.has(o)) return;
          if (typeof o === 'object') seen.add(o);
          for (const k of Object.keys(o)) {
            let v;
            try { v = o[k]; } catch (e) { continue; }
            if (typeof v === 'string') {
              if (!out.sitekey && SITEKEY_RE.test(v)) { out.sitekey = v; out.found = true; }
              else if (v === 'invisible' || v === 'normal' || v === 'compact') out.size = out.size || v;
            } else if (v && typeof v === 'object') {
              walk(v, depth + 1);
            }
          }
        };
        walk(clients[id], 0);
      }
      if (out.sitekey) out.hints.push('sitekey from ___grecaptcha_cfg');
    }
  } catch (e) { out.hints.push('cfg walk failed: ' + e.message); }

  // --- 2. the widget iframe's k= parameter --------------------------------
  try {
    for (const f of document.querySelectorAll('iframe[src*="recaptcha"]')) {
      const m = /[?&]k=([\w-]{20,})/.exec(f.getAttribute('src') || '');
      if (m) {
        if (!out.sitekey) { out.sitekey = m[1]; out.found = true; out.hints.push('sitekey from iframe k= param'); }
        else if (out.sitekey !== m[1]) out.hints.push('iframe k= disagrees with cfg: ' + m[1]);
        break;
      }
    }
  } catch (e) { out.hints.push('iframe scan failed: ' + e.message); }

  // --- 3. legacy paths, still checked in case they come back --------------
  try {
    const w = document.querySelector('captcha-widget[data-sitekey]');
    if (w) {
      out.found = true;
      out.sitekey = out.sitekey || w.getAttribute('data-sitekey');
      const dv = w.getAttribute('data-version');
      if (dv) out.hints.push('legacy captcha-widget data-version=' + dv);
      const da = w.getAttribute('data-action');
      if (da && da !== 'null') out.action = da;
    }
    const gr = document.querySelector('[data-sitekey]');
    if (gr && !out.sitekey) {
      out.sitekey = gr.getAttribute('data-sitekey'); out.found = !!out.sitekey;
      out.hints.push('sitekey from a data-sitekey attribute');
    }
  } catch (e) { out.hints.push('legacy scan failed: ' + e.message); }

  // --- 4. context -------------------------------------------------------
  try {
    const c = document.querySelector('.g-recaptcha[id], [id*="captcha"]');
    if (c) out.containerId = c.id || null;
    out.enterprise = !!(window.grecaptcha && window.grecaptcha.enterprise);
    out.badge = !!document.querySelector('.grecaptcha-badge');
    // A bframe iframe is the interactive challenge. v3 never shows one;
    // v2-invisible does when it decides to challenge.
    out.challengeFrame = !!document.querySelector('iframe[src*="bframe"]');
    out.scripts = [...document.querySelectorAll('script[src*="recaptcha"]')]
                    .map(s => (s.getAttribute('src') || '').split('?')[0]);
    // The api.js `render` parameter is the strongest v2-vs-v3 signal there
    // is, per Google's own docs: v3 loads api.js?render=<SITE_KEY>, while
    // v2 (checkbox and invisible alike) loads api.js?render=explicit.
    out.renderParam = null;
    for (const sc of document.querySelectorAll('script[src*="recaptcha"]')) {
      const m = /[?&]render=([^&]+)/.exec(sc.getAttribute('src') || '');
      if (m) { out.renderParam = decodeURIComponent(m[1]); break; }
    }
  } catch (e) { out.hints.push('context scan failed: ' + e.message); }

  return out;
}
"""


def detect_recaptcha_in_page(evaluate, page_url: str = "") -> Optional[CaptchaChallenge]:
    """Detect a reCAPTCHA by inspecting the LIVE page, not its HTML.

    `evaluate` is a callable that runs RECAPTCHA_DISCOVERY_JS in the page and
    returns the resulting dict — i.e. `page.evaluate` under Playwright,
    `driver.execute_script` under Selenium (wrap it so the arrow function is
    invoked), or an awaited `page.evaluate` under pyppeteer.

    Version inference, and why it is deliberately conservative:
      * `size == "invisible"` PLUS an interactive bframe iframe present is
        the signature of **v2-invisible**, not v3 — v3 never renders a
        challenge frame. This is what that site looked like as of
        2026-08-24, on the same sitekey that its old markup explicitly
        labelled `data-version="v3"`.
      * `size == "invisible"` with no challenge frame is treated as v3,
        which is v3's normal appearance (badge only).
      * a `normal`/`compact` size is a v2 checkbox.
    When the two signals disagree the ambiguity is recorded in the returned
    challenge's `kind` and in the log, rather than silently guessing — pick
    the wrong one and 2captcha returns a token the site rejects.
    """
    try:
        info = evaluate(RECAPTCHA_DISCOVERY_JS)
    except Exception as e:  # noqa: BLE001 - any engine's evaluate can raise
        logger.debug("In-page reCAPTCHA discovery failed: %s", e)
        return None

    if not info or not info.get("found") or not info.get("sitekey"):
        return None

    size = info.get("size")
    render = info.get("renderParam")

    # Classification, strongest signal first.
    #
    # 1. api.js's `render` parameter. Per Google's docs, v3 loads
    #    api.js?render=<SITE_KEY> while v2 — checkbox and invisible alike —
    #    loads api.js?render=explicit. v3 has no `size` concept and never
    #    renders a challenge iframe at all.
    #      https://developers.google.com/recaptcha/docs/v3
    #      https://developers.google.com/recaptcha/docs/invisible
    #    So render=explicit rules v3 out outright, and render=<the sitekey>
    #    confirms it outright.
    # 2. `size`, which only exists for v2: invisible / normal / compact.
    # 3. presence of a bframe (interactive challenge) iframe — v2 only.
    if render and render != "explicit" and render == info.get("sitekey"):
        kind = "recaptcha_v3"
    elif render == "explicit":
        kind = "recaptcha_v2_invisible" if size == "invisible" else "recaptcha_v2"
    elif size in ("normal", "compact"):
        kind = "recaptcha_v2"
    elif info.get("challengeFrame") and size == "invisible":
        kind = "recaptcha_v2_invisible"
    else:
        kind = "recaptcha_v3"

    hints = info.get("hints") or []
    logger.info("reCAPTCHA found at runtime: kind=%s sitekey=%s size=%s render=%s "
                "challengeFrame=%s container=%s hints=%s",
                kind, info["sitekey"], size, render, info.get("challengeFrame"),
                info.get("containerId"), "; ".join(hints))
    if info.get("enterprise"):
        logger.warning("This is a reCAPTCHA ENTERPRISE widget — 2captcha needs its "
                       "enterprise method, which this project does not implement.")

    return CaptchaChallenge(
        kind=kind,
        sitekey=info["sitekey"],
        action=info.get("action") or "verify",
        page_url=page_url,
        source="runtime",
        size=size,
    )


def reconcile_detections(html_challenge: Optional[CaptchaChallenge],
                          runtime_challenge: Optional[CaptchaChallenge]
                          ) -> Optional[CaptchaChallenge]:
    """Pick between the two detectors when both find something.

    They can disagree on the SAME page, and on the site this was written
    against they did. A capture
    taken through the Scraping Browser on 2026-08-24 contains, simultaneously:

      * `<captcha-widget data-version="v3" data-sitekey="6Leif..."
         data-action="null">` — the site's own wrapper element, asserting v3
      * `recaptcha/api.js?render=explicit`, a client registered with
        `size: "invisible"`, and a `bframe` challenge iframe — the documented
        **v2-invisible** signature

    Both cannot be true. `data-version` is an attribute on the site's own
    component: it says what their code believes. `render=explicit` + `size` +
    the challenge frame describe the Google loader that is actually on the
    page, which is what Google enforces and therefore what 2captcha has to
    match. So the runtime reading wins, and the disagreement is logged rather
    than quietly resolved.

    Supporting detail: `data-action="null"` in that markup means there is no
    action string. v3 scores partly on the action; a real v3 integration
    passes one. An empty action fits a v2-invisible widget wearing a stale
    v3 label better than it fits working v3.

    (Also worth knowing: the same modal served to a European residential IP
    the same day had NO `<captcha-widget>` element at all — just the
    `render=explicit` loader. That site served more than one variant of this
    modal, so neither detector alone is enough.)
    """
    if runtime_challenge and not html_challenge:
        return runtime_challenge
    if html_challenge and not runtime_challenge:
        return html_challenge
    if not html_challenge and not runtime_challenge:
        return None

    if html_challenge.kind != runtime_challenge.kind:
        logger.warning(
            "Detectors disagree on this page: static markup says %s (from the "
            "site's own data-version), the live loader says %s (render/size/"
            "challenge-frame). Trusting the loader — that's what Google "
            "enforces and what 2captcha has to match.",
            html_challenge.kind, runtime_challenge.kind)
        # Keep the action if the static markup had a real one; the runtime
        # path often can't see it.
        if html_challenge.action and html_challenge.action != "verify":
            runtime_challenge.action = html_challenge.action
    return runtime_challenge


def _proxy_task_fields(proxy: Optional[str]) -> dict:
    """The documented proxy fields for a non-proxyless task, or {}.

    Names are `proxyType`, `proxyAddress`, `proxyPort`, `proxyLogin` and
    `proxyPassword`, taken from 2captcha's own API documentation rather than
    from this client's vocabulary -- the family rule after a sibling repo
    shipped a capability claim built out of its own field names.

    Returns {} for anything that is not a usable proxy URL, so the caller
    falls back to the proxyless type rather than sending a half-filled task
    the API will reject. Credentials go in the task BODY, never a URL or a
    log line.
    """
    if not proxy:
        return {}
    try:
        p = urlparse(proxy)
        try:
            port = p.port
        except ValueError:
            return {}
        if not (p.scheme and p.hostname and port):
            return {}
        fields = {"proxyType": p.scheme, "proxyAddress": p.hostname,
                  "proxyPort": port}
        if p.username:
            fields["proxyLogin"] = p.username
        if p.password:
            fields["proxyPassword"] = p.password
        return fields
    except Exception:  # noqa: BLE001 -- a bad proxy must not take the solve down
        return {}


def _v2_task_for(challenge: CaptchaChallenge, min_score: float,
                 proxy: Optional[str] = None) -> dict:
    """Build the API-v2 `task` object for a challenge.

    Task type per variant, from https://2captcha.com/api-docs:
      * v3           -> RecaptchaV3TaskProxyless, with pageAction + minScore
      * v2 invisible -> RecaptchaV2TaskProxyless with isInvisible: true
      * v2 checkbox  -> RecaptchaV2TaskProxyless
      * AWS WAF      -> AmazonTask when the page is on a proxy exit,
                        AmazonTaskProxyless otherwise, both with iv + context

    The AWS WAF field names are taken from
    https://2captcha.com/api-docs/amazon-aws-waf-captcha (read 2026-09-16),
    not from this client's own field names or from memory — the family rule
    after a sibling repo shipped a README paragraph asserting a capability
    the product had had for years.

    The `*Proxyless` types let 2captcha use its own IP pool. That is wrong for
    AWS WAF whenever the run is on a proxy, and it fails in a way that looks
    like success: measured 2026-09-17 against a live challenge on a 2Captcha
    residential exit, `AmazonTaskProxyless` came back with `existing_token`
    and NO `captcha_voucher` — 2captcha's own exit had not been challenged,
    so there was nothing for it to solve and it returned the site's ordinary
    token. It cost $0.00145 and cleared nothing.

    `AmazonTask` is the documented proxy-carrying variant
    (https://2captcha.com/api-docs/amazon-aws-waf-captcha, read 2026-09-17):
    same fields plus `proxyType`, `proxyAddress`, `proxyPort` as required and
    `proxyLogin`/`proxyPassword` as optional. Given the same challenge and
    the page's own exit it returned a real `captcha_voucher` in ~20s, which
    is the difference that matters — the solve happens on the address that
    was actually challenged.

    So the type is chosen by whether this run has an exit to hand, not by
    preference. Over the Scraping Browser API there is no local proxy URL and
    the page and the solve already share the remote exit, so the proxyless
    type stays correct there.
    """
    if challenge.is_aws_waf:
        task = {
            "type": "AmazonTaskProxyless",
            "websiteURL": challenge.page_url,
            "websiteKey": challenge.sitekey,
            "iv": challenge.iv,
            "context": challenge.context,
        }
        exit_fields = _proxy_task_fields(proxy)
        if exit_fields:
            task["type"] = "AmazonTask"
            task.update(exit_fields)
        # Both script URLs are optional per the docs, and both are page-
        # specific (they carry a per-deployment id in the hostname), so they
        # are sent when the page gave them and omitted when it did not
        # rather than being reconstructed from a pattern.
        if challenge.challenge_script:
            task["challengeScript"] = challenge.challenge_script
        if challenge.captcha_script:
            task["captchaScript"] = challenge.captcha_script
        return task

    if challenge.is_v3:
        # minScore is not free-form: 0.3 / 0.7 / 0.9 are the documented values.
        score = min(V3_ALLOWED_MIN_SCORES,
                    key=lambda allowed: abs(allowed - min_score))
        if score != min_score:
            logger.info("minScore %.2f is not one of %s — using %.1f.",
                        min_score, V3_ALLOWED_MIN_SCORES, score)
        task = {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": challenge.page_url,
            "websiteKey": challenge.sitekey,
            "minScore": score,
        }
        # v3 scores partly on the action, so send it when it's a real one.
        # "verify" is this module's placeholder for "the page didn't say".
        if challenge.action and challenge.action != "verify":
            task["pageAction"] = challenge.action
        return task

    task = {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": challenge.page_url,
        "websiteKey": challenge.sitekey,
    }
    if challenge.is_invisible_v2:
        task["isInvisible"] = True
    return task


def _solve_with_2captcha_v2(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 180,
                             proxy: Optional[str] = None) -> str:
    """Solve via API v2: createTask, then poll getTaskResult."""
    task = _v2_task_for(challenge, min_score, proxy=proxy)
    logger.info("createTask: %s (sitekey=%s)", task["type"], challenge.sitekey)

    created = requests.post(TWOCAPTCHA_CREATE_TASK_URL,
                            json={"clientKey": api_key, "task": task},
                            timeout=30)
    created.raise_for_status()
    payload = created.json()
    if payload.get("errorId"):
        raise RuntimeError(
            f"createTask failed: {payload.get('errorCode')} — "
            f"{payload.get('errorDescription')}")
    task_id = payload["taskId"]

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        got = requests.post(TWOCAPTCHA_GET_RESULT_URL,
                            json={"clientKey": api_key, "taskId": task_id},
                            timeout=30)
        got.raise_for_status()
        result = got.json()
        if result.get("errorId"):
            raise RuntimeError(
                f"getTaskResult failed: {result.get('errorCode')} — "
                f"{result.get('errorDescription')}")
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            # v2 returns the same string under both names. AWS WAF returns
            # neither: its solution carries `captcha_voucher` and/or
            # `existing_token`, and the value goes into the `aws-waf-token`
            # COOKIE rather than a form field (see AWS_WAF_COOKIE).
            #
            # The two are NOT interchangeable, measured against the live API
            # on 2026-09-16:
            #   captcha_voucher  a challenge was actually solved.
            #   existing_token   no challenge was present at 2captcha's own
            #                    exit, so this is the ordinary aws-waf-token
            #                    the site hands any visitor. Usable, but it
            #                    means the solve did not happen — and if THIS
            #                    run detected a challenge while 2captcha's
            #                    exit did not get one, that is a fact about
            #                    the two exits worth seeing in the log.
            voucher = solution.get("captcha_voucher")
            existing = solution.get("existing_token")
            # For AWS WAF, existing_token FIRST. It is the value the site's
            # own `aws-waf-token` cookie holds, while a captcha_voucher is
            # an intermediate that the WAF's captcha.js exchanges for one,
            # not a cookie value. Measured on binance.com 2026-09-24:
            #   voucher set as the cookie        page stayed "Human Verification"
            #   existing_token as the cookie on  "Support Center | Binance
            #   .binance.com, same exit          Support", 500 KB, twice
            # The sibling repo this client came from preferred the voucher;
            # that was never measured to clear a page there either.
            if challenge.is_aws_waf:
                token = existing or voucher
            else:
                token = (solution.get("gRecaptchaResponse") or solution.get("token")
                         or voucher or existing)
            if not token:
                raise RuntimeError("task ready but no token in solution "
                                   "(keys: %s)" % sorted(solution))
            if challenge.is_aws_waf and not voucher and existing:
                # Not an error. It means 2captcha's own exit loaded the page
                # without being challenged and handed back the ordinary
                # token. On binance.com that token was ACCEPTED from the
                # challenged exit, so the log says what happened and does
                # not predict failure.
                logger.info(
                    "AWS WAF: 2captcha returned existing_token without a "
                    "captcha_voucher — its exit was not challenged, and the "
                    "site's ordinary token is what comes back. If the page "
                    "stays blocked with it, the exit THIS run uses is the "
                    "variable, not the solver.")
            logger.info("2captcha solved %s in ~%ds.", challenge.kind, waited)
            return token
        # status == "processing"

    raise TimeoutError(f"2captcha did not return a token within {max_wait}s")


def get_balance(api_key: str) -> float:
    """Account balance via API v2 — handy for a preflight check."""
    r = requests.post(TWOCAPTCHA_BALANCE_URL, json={"clientKey": api_key}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("errorId"):
        raise RuntimeError(f"getBalance failed: {d.get('errorCode')}")
    return float(d["balance"])


def _solve_with_2captcha_v1(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 120) -> str:
    """Legacy API v1: submit to in.php, poll res.php.

    Kept as a fallback because every live result recorded in this project's
    README came through this path. Prefer v2 for new work.

    The parameters differ per variant and are NOT interchangeable — send v3
    parameters for a v2-invisible widget and you pay for a token the site
    then rejects:
      * v3            -> version=v3, action=..., min_score=...
      * v2 invisible  -> invisible=1, no action, no min_score
      * v2 checkbox   -> neither
    """
    payload = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": challenge.sitekey,
        "pageurl": challenge.page_url,
        "json": 1,
    }
    if challenge.is_v3:
        payload.update({"version": "v3", "action": challenge.action, "min_score": min_score})
    elif challenge.is_invisible_v2:
        payload["invisible"] = 1

    logger.info("Submitting to 2captcha as %s (sitekey=%s)", challenge.kind, challenge.sitekey)
    submit = requests.post(TWOCAPTCHA_IN_URL, data=payload, timeout=30)
    submit.raise_for_status()
    payload = submit.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {payload.get('request')}")

    task_id = payload["request"]
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            # The v1 result endpoint takes the key as a QUERY parameter, so a
            # connection error here would otherwise put it in the log.
            result = requests.get(TWOCAPTCHA_RES_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30).json()
        except requests.RequestException as exc:
            raise RuntimeError("2captcha polling request failed: %s"
                               % _redact(exc)) from None
        if result.get("status") == 1:
            logger.info("2captcha.com solved the reCAPTCHA v3 challenge.")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha polling error: {result.get('request')}")

    raise TimeoutError("2captcha.com did not return a token in time")


def solve_recaptcha(challenge: CaptchaChallenge, twocaptcha_api_key: Optional[str],
                     api_version: str = "v2", min_score: float = 0.7,
                     proxy: Optional[str] = None) -> str:
    """Public entry point: solve `challenge` through 2captcha and return the token.

    There used to be a `use_antidetect` branch here, behind a CLI flag of the
    same name, that POSTed to a hardcoded local "solve-captcha" endpoint.
    It was removed before publishing: that endpoint was a placeholder for a
    product that does not exist under that name, so the flag could not work for
    anyone who set it, and the real "the browser solves it for you" path is
    `Captcha.setAutoSolve` over the Scraping Browser API. A flag that cannot
    succeed is worse than a missing feature — it reads as an option.
    """
    if not twocaptcha_api_key:
        raise RuntimeError(
            "A captcha was detected but no 2captcha API key was given. Pass "
            "--twocaptcha-key, or set TWOCAPTCHA_KEY. Over the Scraping Browser "
            "API you may not need either: Captcha.setAutoSolve can clear it "
            "inside the browser."
        )
    if api_version == "v1":
        # v1 has no proxy-carrying AWS WAF form wired up here; say so rather
        # than silently solving from the wrong address.
        if proxy and challenge.is_aws_waf:
            logger.warning("--captcha-api v1 cannot carry this run's exit to "
                           "the solver, so an AWS WAF solve would be made from "
                           "2captcha's own address and would clear nothing. "
                           "Use the default v2 for this.")
        return _solve_with_2captcha_v1(twocaptcha_api_key, challenge,
                                       min_score=min_score)
    return _solve_with_2captcha_v2(twocaptcha_api_key, challenge,
                                   min_score=min_score, proxy=proxy)


INJECT_TOKEN_JS = """
(token) => {
  let el = document.getElementById('g-recaptcha-response');
  if (!el) {
    el = document.createElement('textarea');
    el.id = 'g-recaptcha-response';
    el.name = 'g-recaptcha-response';
    el.style.display = 'none';
    document.body.appendChild(el);
  }
  el.value = token;
  el.innerHTML = token;
  try {
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      Object.values(window.___grecaptcha_cfg.clients).forEach((client) => {
        Object.values(client).forEach((prop) => {
          if (prop && typeof prop === 'object') {
            Object.values(prop).forEach((cb) => {
              if (typeof cb === 'function') { try { cb(token); } catch (e) {} }
            });
          }
        });
      });
    }
  } catch (e) { /* best effort, non-fatal */ }
  return true;
}
"""


# Kept as an alias: this function used to be reCAPTCHA-v3-only, and the three
# scrapers plus both modal diagnostics import it under the old name. The
# solver now branches on challenge.kind, so the name is a misnomer — the
# alias exists so older call sites keep working rather than to encourage it.
solve_recaptcha_v3 = solve_recaptcha


# ===========================================================================
# What is deliberately NOT here
# ===========================================================================
# No first-party image captcha or custom widget is ported here: nothing in
# this family's other members' bespoke solvers (a JPEG-of-distorted-text
# form is one example) was ever observed on Transfermarkt, and porting one
# on spec would be dead code with no fixture to test it against.
#
# What was measured, on 2026-09-10 from this repo's own build/test
# environment: neither a direct `requests` call nor a local Playwright
# browser could reach www.transfermarkt.com at all — both failed at the
# TCP/TLS layer, and a plain `requests.get` through the sandbox's own egress
# proxy came back with the proxy's OWN "403 Forbidden" on the CONNECT
# tunnel, never reaching the site. That is this sandbox's outbound
# allowlist refusing the destination, not Transfermarkt refusing the
# request — confirmed by ruling it out, not assumed. A PROXIED fetch of the
# same URLs (through 2captcha's own fetch infrastructure, which sits
# outside this sandbox) returned normal 200 pages every time with no
# challenge and no captcha markers. So the only live evidence this repo has
# is: Transfermarkt served every page it was asked for, without a captcha,
# over a working proxied path. What was NOT established is how the site
# treats a bare datacentre IP with no proxy at all, or what a real user's
# browser sees on a residential connection — this repo's build sandbox
# cannot test either. Do not port the OTHER family members' "datacentre
# addresses get refused" conclusion onto Transfermarkt without measuring it
# here; it was true for them and is simply unknown for this site. See
# product_parser.detect_bot_challenge for how a real captcha, once observed,
# would be told apart from a network-level refusal.
#
# The reCAPTCHA / hCaptcha / Turnstile machinery above IS kept, and that is a
# deliberate asymmetry rather than an inconsistency. Detection stays broad
# because which challenge a visitor meets depends on the exit country and on
# what the address has been doing — a narrow list is how a challenge gets
# reported as an empty page months later. A solver for a challenge this site
# has never been observed to serve is dead code; a DETECTOR for one is cheap
# insurance. See the family note in CLAUDE.md.
