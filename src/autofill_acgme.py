import json
import platform
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ACGME_URL = "https://apps.acgme.org/ads/CaseLogs/CaseEntry/Insert"
JSON_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("cases_to_fill.json")

# The Streamlit app passes the selected resident's login email/password as
# the 2nd/3rd CLI args (per the roster's Email/Password columns; '' if
# that resident has none on file). Email falls back to this default only
# when run standalone by hand; password has no such fallback - never
# hardcode a real password here.
LOGIN_EMAIL = sys.argv[2] if len(sys.argv) > 2 else "yamamoto.ryosuke@kameda.jp"
LOGIN_PASSWORD = sys.argv[3] if len(sys.argv) > 3 else ""

# "Select all" is Cmd+A on macOS, Ctrl+A everywhere else (Windows/Linux).
SELECT_ALL_KEY = "Meta+A" if platform.system() == "Darwin" else "Control+A"

# Submission mode.
#   "manual" = fill the case, then you review/click Submit yourself.
#   "auto"   = submit automatically; if ACGME reports a required field,
#              the script pauses and lets you complete that case manually.
#   "ask"    = choose Manual or Auto once when the script starts.
#
# You can also override this with a 4th command-line argument:
#   python autofill_acgme.py cases_to_fill.json EMAIL PASSWORD manual
#   python autofill_acgme.py cases_to_fill.json EMAIL PASSWORD auto
SUBMIT_MODE = sys.argv[4].strip().lower() if len(sys.argv) > 4 else "ask"


def choose_submit_mode():
    """Resolve manual/auto submission mode once at startup."""
    mode = SUBMIT_MODE
    if mode in {"manual", "m"}:
        return "manual"
    if mode in {"auto", "a"}:
        return "auto"

    # Interactive switch. Default to manual for safety.
    print("\nSubmission mode:")
    print("  [1] Manual submit  - review each case and click Submit yourself")
    print("  [2] Auto submit    - submit automatically; required-field errors fall back to manual")
    try:
        answer = input("Choose 1 or 2 [default: 1]: ").strip().lower()
    except (EOFError, OSError):
        answer = ""

    if answer in {"2", "a", "auto"}:
        return "auto"
    return "manual"


def wait(sec=0.4):
    time.sleep(sec)


def css_escape_double_quote(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def click_sign_in(page):
    """Click the ADS 'Sign In' button/link that leads to the ACGME Cloud
    login page. Returns True if found and clicked."""
    locator = page.get_by_role("link", name="Sign In")

    if locator.count() == 0:
        locator = page.get_by_role("button", name="Sign In")

    if locator.count() == 0:
        locator = page.get_by_text("Sign In", exact=True)

    if locator.count() == 0:
        print("  NOT FOUND: 'Sign In' button/link")
        return False

    locator.first.click()
    page.wait_for_load_state("networkidle")
    wait(0.5)
    return True


def fill_login_email(page, email):
    """Fill the ACGME Cloud email field.

    The login page (Sign In redirects to an Auth0-hosted page) can still be
    rendering its form client-side after networkidle fires, so a plain
    .count() check (an instant DOM snapshot, no waiting) can see 0 elements
    and give up before the field ever appears. wait_for() actively waits
    for each candidate instead.
    """
    if not email:
        print("  No login email on file for this resident - leaving it blank to fill in by hand.")
        return False

    for selector in ('input[type="email"]', 'input[name="username"]', "#username"):
        locator = page.locator(selector).first

        try:
            locator.wait_for(state="visible", timeout=8000)
        except Exception:
            continue

        locator.click()
        locator.fill(email)
        print(f"  filled login email via {selector!r}: {email}")
        return True

    print("  NOT FOUND: login email field")
    return False


def fill_login_password(page, password):
    """Advance past the email screen (if needed) and fill the ACGME Cloud
    password field. Still stops here on purpose - the final Sign In click
    and any 2FA are left for you to do by hand.

    Auth0's default hosted login often splits email and password across
    two screens (email -> Continue -> password); some configurations show
    both on one form instead. Only click Continue if the password field
    isn't already visible.
    """
    if not password:
        print("  No login password on file for this resident - leaving it blank to fill in by hand.")
        return False

    password_field = page.locator('input[type="password"]').first

    if not password_field.is_visible():
        continue_button = page.get_by_role("button", name="Continue")

        if continue_button.count() == 0:
            continue_button = page.locator('button[type="submit"]')

        if continue_button.count() == 0:
            print("  NOT FOUND: 'Continue' button to reach the password screen")
            return False

        # A disabled Continue button (e.g. because the email step above
        # didn't actually fill anything) will never become clickable -
        # .click() would otherwise hang for its full timeout and raise.
        # is_disabled() is an instant check, no waiting.
        if continue_button.first.is_disabled():
            print("  'Continue' button is disabled (email step likely didn't complete) - "
                  "can't reach the password field automatically.")
            return False

        try:
            continue_button.first.click(timeout=5000)
        except Exception as e:
            print(f"  Couldn't click 'Continue': {e}")
            return False

    try:
        password_field.wait_for(state="visible", timeout=8000)
    except Exception:
        print("  NOT FOUND: login password field")
        return False

    password_field.click()
    password_field.fill(password)
    print("  filled login password")
    return True


def fill_case_id(page, case_id):
    """Fill Case ID. The ACGME Case ID input has a randomized id/name, so use maxlength=25."""
    if not case_id:
        return

    print(f"  fill Case ID: {case_id}")
    locator = page.locator('input[maxlength="25"]').first
    locator.scroll_into_view_if_needed()
    locator.fill(str(case_id))
    wait()


def fill_date(page, date_text):
    """Set Case Date through the page's Bootstrap datepicker API.

    Direct typing can leave the datepicker's internal selected date out of
    sync with the visible input.  Set the actual datepicker date instead,
    then verify the input value.
    """
    if not date_text:
        return

    expected = str(date_text).strip()
    print(f"  fill Case Date: {expected}")

    # Normalize to M/D/YYYY before handing it to JavaScript.
    try:
        dt = datetime.strptime(expected, "%m/%d/%Y")
    except ValueError:
        try:
            dt = datetime.strptime(expected, "%m/%d/%y")
        except ValueError as e:
            raise ValueError(f"Unsupported Case Date format: {expected!r}") from e

    month, day, year = dt.month, dt.day, dt.year
    normalized = f"{month}/{day}/{year}"

    container = page.locator("div.ProcedureDate")
    date_input = container.locator("input").first
    date_input.wait_for(state="visible", timeout=10000)

    result = container.evaluate(
        """(el, parts) => {
            const $ = window.jQuery || window.$;
            const input = el.querySelector('input');
            if (!input) return {ok:false, reason:'no input'};

            // Use noon to avoid any midnight / DST edge case.
            const d = new Date(parts.year, parts.month - 1, parts.day, 12, 0, 0);

            if ($ && typeof $(el).datepicker === 'function') {
                try {
                    $(el).datepicker('setDate', d);
                    $(el).datepicker('update', d);
                    $(el).datepicker('hide');
                    input.dispatchEvent(new Event('input', {bubbles:true}));
                    input.dispatchEvent(new Event('change', {bubbles:true}));
                    return {ok:true, value:input.value};
                } catch (e) {
                    return {ok:false, reason:String(e)};
                }
            }

            // Fallback if the plugin is not reachable.
            input.value = parts.normalized;
            input.dispatchEvent(new Event('input', {bubbles:true}));
            input.dispatchEvent(new Event('change', {bubbles:true}));
            return {ok:true, value:input.value, fallback:true};
        }""",
        {"year": year, "month": month, "day": day, "normalized": normalized},
    )

    # Ensure the calendar popup is gone and the field has committed.
    page.keyboard.press("Escape")
    date_input.blur()
    wait(0.25)

    current = date_input.input_value().strip()
    accepted = {normalized, f"{month:02d}/{day:02d}/{year}"}
    if current not in accepted:
        raise RuntimeError(
            f"Case Date did not stick: expected {normalized!r}, got {current!r}; JS result={result}"
        )

    print(f"  Case Date verified: {current}")


def safe_select(page, selector, label):
    if not label:
        return

    print(f"  select {selector}: {label}")
    locator = page.locator(selector)

    if locator.count() == 0:
        print(f"  NOT FOUND: selector={selector!r}")
        return

    try:
        locator.select_option(label=str(label))
    except Exception as e:
        print(f"  SELECT FAILED: {selector} label={label!r}: {e}")

    wait()


def safe_check_by_datatype(page, datatype):
    """Check checkbox by ACGME data-type attribute. Avoid numeric ID selectors."""
    if not datatype:
        return

    datatype_escaped = css_escape_double_quote(datatype)
    locator = page.locator(f'input.cbprocedureid[data-type="{datatype_escaped}"]')

    if locator.count() == 0:
        print(f"  NOT FOUND: data-type={datatype!r}")
        return

    target = locator.first
    target.scroll_into_view_if_needed()

    # A manual click always works here, but our automated force-click
    # occasionally doesn't stick - that points to a timing/reflow race in
    # the click itself, not a real block (a genuine prerequisite or
    # code-count-limit block would reject a manual click too, and it
    # doesn't). So verify the actual checked state after each attempt
    # rather than trusting "no exception raised", and retry a few times
    # before giving up.
    last_error = None
    for _ in range(3):
        try:
            target.check(force=True)
            if target.is_checked():
                print(f"  checked: {datatype}")
                wait()
                return
            last_error = "click did not change the checked state"
        except Exception as e:
            last_error = e

        wait(0.3)

    # force=True deliberately skips Playwright's own "is anything covering
    # this element" check, so if something really is intercepting the
    # click (an animating accordion, an overlapping panel, a tooltip),
    # force clicks straight through it and silently misses. One plain
    # (non-forced) attempt won't - Playwright's own timeout error names
    # the exact element that's in the way, which tells us far more than
    # another silent force-click failure would.
    try:
        target.check(force=False, timeout=4000)
        if target.is_checked():
            print(f"  checked (non-forced, after force failed): {datatype}")
            wait()
            return
    except Exception as e:
        last_error = e

    print(f"  CHECK FAILED: data-type={datatype!r}: {last_error}")
    diagnose_check_failure(page, datatype)
    wait()


def diagnose_check_failure(page, datatype):
    """We can't inspect the live page ourselves, so when a checkbox click
    gets silently reverted, gather what we can right here: is it disabled,
    is a prerequisite (Epidural) actually checked, is there a code-count
    cap being hit, is some alert/toast/modal blocking it - and save a
    screenshot for visual follow-up."""
    try:
        target = page.locator(f'input.cbprocedureid[data-type="{css_escape_double_quote(datatype)}"]').first
        print(f"    disabled={target.is_disabled()} checked={target.is_checked()}")

        checked_count = page.locator("input.cbprocedureid:checked").count()
        print(f"    total procedure checkboxes currently checked: {checked_count}")

        epidural = page.locator('input.cbprocedureid[data-type="Epidural"]').first
        if epidural.count() > 0:
            print(f"    Epidural checkbox checked={epidural.is_checked()}")

        for role_selector in ('[role="alert"]', ".toast", ".modal.show", ".swal2-popup"):
            alert_locator = page.locator(role_selector)
            if alert_locator.count() > 0:
                text = alert_locator.first.inner_text().strip()
                if text:
                    print(f"    visible message ({role_selector}): {text!r}")

        shot_path = Path("data") / f"debug_check_failed_{re.sub(r'[^A-Za-z0-9]+', '_', datatype)}.png"
        page.screenshot(path=str(shot_path))
        print(f"    saved screenshot: {shot_path}")
    except Exception as diag_error:
        print(f"    (diagnostics themselves failed: {diag_error})")


def safe_check_by_value(page, value):
    """Check radio button by fixed value."""
    if not value:
        return

    locator = page.locator(f'input[value="{value}"]')
    if locator.count() == 0:
        print(f"  NOT FOUND: value={value!r}")
        return

    try:
        locator.first.scroll_into_view_if_needed()
        locator.first.check(force=True)
        print(f"  checked value={value}")
    except Exception as e:
        print(f"  CHECK FAILED: value={value!r}: {e}")

    wait()


def verify_date(page, expected_date_text):
    """Re-check Case Date once more and re-apply it if it drifted.

    We've seen the date get set correctly by fill_date(), then reset back
    to today after other fields (most likely Institution/Attending, which
    can trigger this form's own cascading refresh) are filled. Rather than
    depend on guessing which field causes it, catch it here.
    """
    if not expected_date_text:
        return

    date_input = page.locator("div.ProcedureDate input")
    current = date_input.input_value()
    if current.strip() != str(expected_date_text).strip():
        print(f"  Case Date drifted to {current!r} (expected {expected_date_text!r}); re-applying")
        fill_date(page, expected_date_text)


def fill_case(page, case):
    # Basic fields
    fill_case_id(page, case.get("case_id"))

    safe_select(page, "#ProcedureYear", case.get("case_year"))
    safe_select(page, "#ResidentRoles", case.get("role"))
    safe_select(page, "#Institutions", case.get("site"))
    safe_select(page, "#Attendings", case.get("supervisor"))
    safe_select(page, "#PatientTypes", case.get("patient_age"))

    # ASA
    asa = str(case.get("asa", "")).strip()
    if asa:
        datatype = f"ASA {asa}E" if case.get("emergency") else f"ASA {asa}"
        safe_check_by_datatype(page, datatype)

    # Values below are the checkboxes' actual data-type attributes (pulled
    # from a live page capture), not their on-screen labels - the two often
    # differ (e.g. the "W/out CPB" label's data-type is actually "Cardiac
    # without CPB"), which is why roughly half of these were silently never
    # matching (safe_check_by_datatype prints "NOT FOUND" and moves on).
    checkbox_map = {
        # Anesthesia / Analgesia type
        "general_maintenance": "General Maintenance",
        "mac_sedation": "MAC &/or Sedation",
        "spinal": "Spinal",
        "epidural": "Epidural",
        "cse": "Combined Spinal-Epidural (CSE)",
        "pn_block_continuous": "Peripheral Nerve Block Continuous",
        "pn_block_single": "Peripheral Nerve Block Single Shot",

        # Airway management
        "supraglottic_airway": "Supraglottic Airway",
        "laryngoscope_direct": "Laryngoscope - Direct",
        "laryngoscope_indirect": "Laryngoscope - Indirect",
        "oral_ett": "Oral ETT",
        "nasal_ett": "Nasal ETT",
        # "Awake" fiberoptic intubation is two separate checkboxes on the
        # real form, not one combined checkbox - check both.
        "flexible_bronchoscopic": "Flexible Bronchoscopic",
        "awake_intubation": "Awake Intubation",
        "bronchial_blocker": "Bronchial Blocker",
        "dlt": "DLT",
        "mask": "Mask",
        "jet_ventilation": "Jet Ventilation",
        "airway_mgmt_other": "Airway Management - Other",

        # Procedure category
        "cardiac_without_cpb": "Cardiac without CPB",
        "cardiac_with_cpb": "Cardiac with CPB",
        "major_vessels_endo": "Procedures on major vessels (endovascular)",
        "major_vessels_open": "Procedures on major vessels (open)",
        "endovascular_intracerebral": "Intracerebral (endovascular)",
        "nonvascular_open": "Intracerebral Nonvascular (open)",
        "vascular_open_intracerebral": "Intracerebral Vascular (open)",
        "intrathoracic_noncardiac": "Intrathoracic non-cardiac",
        "cesarean_delivery": "Cesarean Section",
        "cesarean_delivery_high_risk": "Cesarean Section High-Risk",
        "vaginal_delivery": "Vaginal Delivery",
        "vaginal_delivery_high_risk": "Vaginal Delivery High-Risk",

        # Vascular access / monitoring
        "arterial_line": "Arterial Catheter",
        "central_line": "Central Venous Catheter",
        "pa_catheter": "Pulmonary Artery Catheter",
        "ultrasound_line": "Ultrasound used for line placement",
        "csf_drain": "CSF Drain",
        # Real data-type is truncated (missing closing paren) on ACGME's
        # own page - copied verbatim so the exact-match lookup still hits.
        "electrophysiologic_monitoring": "Electrophysiologic monitoring (SSEP, MEP, EMG, EEG",
        "tee": "Transesophageal Echo (TEE)",

        # Neuraxial blockade site
        "neuraxial_caudal": "Caudal",
        "neuraxial_cervical": "Cervical",
        "neuraxial_lumbar": "Lumbar",
        "neuraxial_t1_7": "T 1-7",
        "neuraxial_t8_12": "T 8-12",

        # Peripheral nerve blockade site
        "adductor_canal_block": "Adductor Canal",
        "ankle_block": "Ankle",
        "axillary_block": "Axillary",
        "erector_spinae_plane_block": "Erector Spinae Plane",
        "femoral_block": "Femoral",
        "infraclavicular_block": "Infraclavicular",
        "interscalene_block": "Interscalene",
        "lumbar_plexus_block": "Lumbar Plexus",
        "paravertebral_block": "Paravertebral",
        "popliteal_block": "Popliteal",
        "quadratus_lumborum_block": "Quadratus Lumborum",
        "retrobulbar_block": "Retrobulbar",
        "saphenous_block": "Saphenous",
        "sciatic_block": "Sciatic",
        "supraclavicular_block": "Supraclavicular",
        "tap_block": "Transverse Abdominal Plane",
        "other_peripheral_nerve_block_site": "Other - peripheral nerve blockade site",
    }

    for key, datatype in checkbox_map.items():
        if case.get(key):
            safe_check_by_datatype(page, datatype)

    # Case type radio buttons
    if case.get("difficult_airway_anticipated"):
        safe_check_by_value(page, "209")
    if case.get("difficult_airway_unanticipated"):
        safe_check_by_value(page, "210")
    if case.get("life_threatening_nontrauma"):
        safe_check_by_value(page, "207")
    if case.get("life_threatening_trauma"):
        safe_check_by_value(page, "208")

    # Fill Case Date last (and re-verify it): earlier fields, especially
    # the Institution/Attending selects, are the most likely to trigger a
    # postback that clobbers the date field back to today. Doing this last
    # and re-checking afterward catches that regardless of which field
    # actually causes it.
    fill_date(page, case.get("case_date"))
    verify_date(page, case.get("case_date"))


def get_validation_messages(page):
    """Return only actual ACGME form-validation messages.

    Avoid reading the text of invalid <input>/<select> elements themselves,
    because a <select> can expose its entire option list as inner_text().
    """
    selectors = [
        ".field-validation-error:visible",
        ".validation-summary-errors:visible li",
        ".alert-danger:visible",
    ]

    messages = []
    for selector in selectors:
        locator = page.locator(selector)
        for j in range(locator.count()):
            try:
                txt = locator.nth(j).inner_text().strip()
            except Exception:
                continue
            if txt and txt not in messages:
                messages.append(txt)

    return messages


def manual_completion(page, i, n, messages=None):
    """Pause so the user can complete any required fields and submit by hand.

    This is used when ACGME rejects an otherwise autofilled case because a
    required value could not be filled automatically (for example an
    attending/supervisor not present in the source mapping).
    """
    print(f"\n  MANUAL COMPLETION NEEDED for case {i}/{n}.")
    if messages:
        print("  ACGME validation message(s):")
        for msg in messages:
            print(f"    - {msg}")

    while True:
        answer = input(
            "  Complete the required field(s) in the browser and click Submit/Update manually.\n"
            "  After the case has been submitted, press Enter here to continue "
            "(or type 'skip' to continue without re-checking): "
        ).strip().lower()

        if answer == "skip":
            print(f"  continuing after manual handling of case {i}/{n}")
            return

        # If the page navigated after a successful manual submission, these
        # validation elements will normally be gone. If ACGME still shows an
        # error, keep the browser open and let the user correct it again.
        remaining = get_validation_messages(page)
        if not remaining:
            print(f"  manual submission completed for case {i}/{n}")
            return

        print("  ACGME still shows required/validation message(s):")
        for msg in remaining:
            print(f"    - {msg}")


def submit_or_pause(page, i, n, submit_mode):
    if submit_mode == "manual":
        input(f"\nCase {i}/{n} filled. Review and submit in browser, then press Enter to continue...")
        return

    # If a required-field error is already visible, don't crash. Leave the
    # browser open so the missing value can be supplied and submitted manually.
    messages = get_validation_messages(page)
    if messages:
        manual_completion(page, i, n, messages)
        return

    submit = page.locator("#submitButton")
    if submit.count() == 0:
        submit = page.get_by_role("button", name=re.compile(r"^(Submit|Update|Save)$", re.I))
    if submit.count() == 0:
        print("  Could not find the ACGME submit/update button automatically.")
        manual_completion(page, i, n, ["Submit/Update button was not found automatically."])
        return

    print(f"  submitting case {i}/{n}...")
    submit.first.scroll_into_view_if_needed()
    submit.first.click()

    # The site may navigate, refresh, or stay on the same URL.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    wait(1.0)

    # If ACGME rejects the automatic submission because a required field is
    # incomplete, switch to manual completion instead of terminating the run.
    messages = get_validation_messages(page)
    if messages:
        manual_completion(page, i, n, messages)
        return

    print(f"  submitted case {i}/{n}")


def main():
    if not JSON_PATH.exists():
        print(f"ERROR: {JSON_PATH} not found. Save cases from the Streamlit app first.")
        sys.exit(1)

    cases = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if isinstance(cases, dict):
        cases = [cases]

    n = len(cases)
    print(f"Loaded {n} case(s) from {JSON_PATH}")

    submit_mode = choose_submit_mode()
    print(f"Submission mode selected: {submit_mode.upper()}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=250)
        page = browser.new_page()
        # domcontentloaded rather than waiting for full networkidle - the
        # ACGME site keeps background connections open (analytics, polling)
        # that can keep "networkidle" from ever firing promptly.
        page.goto(ACGME_URL, wait_until="domcontentloaded")

        # This whole block is a convenience, never load-bearing: any
        # failure here (a selector that no longer matches, a timeout, an
        # unexpected page state) must fall through to manual login rather
        # than crash the script and lose the whole run - that's exactly
        # what happened before this fix (an uncaught exception here took
        # down the browser and every case with it).
        try:
            if click_sign_in(page):
                if fill_login_email(page, LOGIN_EMAIL):
                    fill_login_password(page, LOGIN_PASSWORD)
        except Exception as e:
            print(f"\n  Login automation hit a snag ({e}) - no problem, just log in by hand below.")

        input("\nFinish logging in (submit / 2FA), navigate to Add Cases, then press Enter...")

        for i, case in enumerate(cases, start=1):
            print(f"\n--- Case {i}/{n}: ID={case.get('case_id')} Date={case.get('case_date')} ---")
            if case.get("procedure_name"):
                # Not a real ACGME field - the form has no free-text
                # procedure name, just printed here so you can sanity-check
                # the checkboxes against what the case actually was.
                print(f"    Procedure: {case['procedure_name']}")
            try:
                page.goto(ACGME_URL, wait_until="domcontentloaded")
                fill_case(page, case)
                submit_or_pause(page, i, n, submit_mode)
            except Exception as e:
                if "closed" not in str(e).lower():
                    raise
                remaining = [c.get("case_id") for c in cases[i - 1:]]
                print(f"\nBrowser window was closed - stopped after {i - 1}/{n} case(s).")
                print(f"Not yet filled: {remaining}")
                print("Rerun the script to continue (cases_to_fill.json still has all cases - "
                      "trim the already-submitted ones first if you don't want to redo them).")
                return

        print("\nAll cases completed.")
        input("Press Enter to close browser...")
        browser.close()


if __name__ == "__main__":
    main()
