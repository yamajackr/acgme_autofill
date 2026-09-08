import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ACGME_URL = "https://apps.acgme-i.org/ads/CaseLogs/CaseEntry/Insert"
ACGME_LOGIN_EMAIL = "yamamoto.ryosuke@kameda.jp"
JSON_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("cases_to_fill.json")

# Keep False until you have verified the form is filled correctly.
AUTO_SUBMIT = False


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
    """Fill the ACGME Cloud email field. Stops here on purpose - finish
    logging in (password / 2FA) by hand.

    The login page (Sign In redirects to an Auth0-hosted page) can still be
    rendering its form client-side after networkidle fires, so a plain
    .count() check (an instant DOM snapshot, no waiting) can see 0 elements
    and give up before the field ever appears. wait_for() actively waits
    for each candidate instead.
    """
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
    """Fill Case Date safely despite the datepicker popup.

    The Case Date field (div.ProcedureDate) is a bootstrap-datepicker
    widget. It rejects/ignores real keystrokes typed into the input, and it
    never listens for a plain 'change' event - it only syncs its internal
    "active date" through its own keyup handling or its jQuery plugin API.
    So .fill() lands the text visually, but the plugin still thinks the
    active date is today, and pressing Enter/Escape (or clicking anywhere,
    which the open calendar popup intercepts anyway) confirms that stale
    "today" and overwrites our typed value.

    Fix: set the value with .fill(), then tell the plugin directly, via its
    own jQuery API, to re-parse the input and close - this bypasses the
    popup interception entirely instead of guessing at keys/clicks.
    """
    if not date_text:
        return

    print(f"  fill Case Date: {date_text}")
    container = page.locator("div.ProcedureDate")
    date_input = container.locator("input")
    date_input.click()
    date_input.press("Meta+A")  # Mac. For Windows, use Control+A.
    date_input.fill(str(date_text))

    synced = container.evaluate(
        """el => {
            const $ = window.jQuery || window.$;
            if (!$ || typeof $(el).datepicker !== 'function') return false;
            $(el).datepicker('update');
            $(el).datepicker('hide');
            return true;
        }"""
    )
    if not synced:
        print("  WARNING: could not reach bootstrap-datepicker's jQuery API; "
              "falling back to Escape (date may revert to today - verify!)")
        page.keyboard.press("Escape")

    wait()


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


def submit_or_pause(page, i, n):
    if AUTO_SUBMIT:
        page.locator("#submitButton").click()
        page.wait_for_load_state("networkidle")
        wait(1)
    else:
        input(f"\nCase {i}/{n} filled. Review and submit in browser, then press Enter to continue...")


def main():
    if not JSON_PATH.exists():
        print(f"ERROR: {JSON_PATH} not found. Save cases from the Streamlit app first.")
        sys.exit(1)

    cases = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if isinstance(cases, dict):
        cases = [cases]

    n = len(cases)
    print(f"Loaded {n} case(s) from {JSON_PATH}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=250)
        page = browser.new_page()
        page.goto(ACGME_URL)
        page.wait_for_load_state("networkidle")

        if click_sign_in(page):
            fill_login_email(page, ACGME_LOGIN_EMAIL)

        input("\nFinish logging in (password/2FA), navigate to Add Cases, then press Enter...")

        for i, case in enumerate(cases, start=1):
            print(f"\n--- Case {i}/{n}: ID={case.get('case_id')} Date={case.get('case_date')} ---")
            try:
                page.goto(ACGME_URL)
                page.wait_for_load_state("networkidle")
                fill_case(page, case)
                submit_or_pause(page, i, n)
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
