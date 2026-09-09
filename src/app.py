import json
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import streamlit as st


AUTOFILL_SCRIPT = Path(__file__).parent / "autofill_acgme.py"

# Where the chosen working folder is remembered, per machine/user - not in
# the project folder, so it isn't affected by git and survives regardless
# of where this repo happens to be checked out.
CONFIG_PATH = Path.home() / ".acgme_autofill" / "config.json"

st.set_page_config(layout="wide")
st.title("ACGME Case Log Helper")


# ---------------------------------------------------
# Working folder (data/ and cases_to_fill.json live here)
# ---------------------------------------------------

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config_value(key, value):
    """Merge one key into the config file, leaving the rest untouched."""

    config = load_config()
    config[key] = str(value)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_working_dir():
    """Return the previously-chosen working folder, or None if there isn't
    one yet (or it no longer exists on disk)."""

    saved = load_config().get("working_dir")

    if saved and Path(saved).is_dir():
        return Path(saved)

    return None


def save_working_dir(path):
    save_config_value("working_dir", path)


def load_resident_year_file_choice():
    """Return the manually-chosen resident_year Excel file, or None if
    there isn't one yet (or it no longer exists on disk) - in which case
    the newest data/resident_year_*.xlsx is auto-detected instead."""

    saved = load_config().get("resident_year_file")

    if saved and Path(saved).is_file():
        return Path(saved)

    return None


def save_resident_year_file_choice(path):
    save_config_value("resident_year_file", path)


def _run_tk_dialog(tk_call_lines):
    """Run a Tk file/folder dialog in a separate child process. Returns
    the chosen path, or '' if the user cancelled/it failed.

    Streamlit executes this script in a worker thread, but Tk's Cocoa
    backend on macOS requires window creation on the real main thread and
    crashes the whole process outright otherwise. A fresh subprocess gets
    its own genuine main thread, sidestepping that restriction (this also
    avoids polluting the Streamlit process with a Tk runtime at all).
    Only works when the app is run locally, not deployed to a remote host.
    """

    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"
        + tk_call_lines
    )

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.stdout.strip()
    except Exception as e:
        st.error(f"Couldn't open the picker dialog: {e}")
        return ""


def browse_for_folder(initial_dir=None):
    """Open the native OS folder-picker dialog."""

    return _run_tk_dialog(
        "print(filedialog.askdirectory(\n"
        f"    initialdir={str(initial_dir or Path.home())!r},\n"
        "    title='Choose the ACGME Autofill working folder',\n"
        "))\n"
    )


def browse_for_file(initial_dir=None):
    """Open the native OS file-picker dialog, filtered to Excel files."""

    return _run_tk_dialog(
        "print(filedialog.askopenfilename(\n"
        f"    initialdir={str(initial_dir or Path.home())!r},\n"
        "    title='Choose the resident_year Excel file',\n"
        "    filetypes=[('Excel files', '*.xlsx'), ('All files', '*.*')],\n"
        "))\n"
    )


if "working_dir" not in st.session_state:
    st.session_state.working_dir = load_working_dir()

with st.expander(
    "📁 Working folder" if st.session_state.working_dir else "📁 Choose your working folder (first time only)",
    expanded=st.session_state.working_dir is None,
):
    if st.session_state.working_dir:
        st.caption(f"Current: {st.session_state.working_dir}")
    else:
        st.info(
            "Choose a folder to hold your `data/` (Excel files) and "
            "`cases_to_fill.json`. This is only asked once - it's "
            "remembered for next time."
        )

    if st.button("Browse..."):
        chosen = browse_for_folder(
            str(st.session_state.working_dir) if st.session_state.working_dir else None
        )

        if chosen:
            st.session_state.working_dir = Path(chosen)
            save_working_dir(chosen)
            st.rerun()

if st.session_state.working_dir is None:
    st.stop()
    # st.stop() halts a real `streamlit run` session right here, but it's
    # a no-op outside one (e.g. `import app` for testing/bare mode) - fall
    # back to the project folder so that doesn't crash on WORKING_DIR
    # being None. A real session never reaches the next line in this case.
    WORKING_DIR = Path(__file__).resolve().parent.parent
else:
    WORKING_DIR = st.session_state.working_dir

CASES_JSON = WORKING_DIR / "cases_to_fill.json"


# ---------------------------------------------------
# Supervisor mapping
# ---------------------------------------------------

SUPERVISOR_MAP = {
    "植田": "Ueda, Kenichi",
    "吉沼": "Yoshinuma, Hiromi",
    "室内": "Murouchi, Takeshi",
    "河野": "Kono, Hiroyuki",
    "柘植": "Tsuge, Masatsugu",
    "竹原": "Takehara, Yuka",
    "劉": "Liu, Dan",
    "中澤": "Nakazawa, Haruka",
    "荻野": "Ogino, Hitoshi",
    "藤本": "Fujimoto, Moeko",
    "加納": "Kano, Misaki",
    "竹下": "Takeshita, Gaku",
    "大熊": "Okuma, Hidehiko",
}


# ---------------------------------------------------
# Case Year (training year, from the resident roster)
# ---------------------------------------------------
#
# The case log belongs to whichever resident is using the app, so their
# name is picked from a dropdown (see the Streamlit UI section below)
# rather than hardcoded - that's what lets other residents use this too.

def find_resident_year_file(data_dir):
    """Pick the most recently dated <data_dir>/resident_year_*.xlsx file."""

    candidates = sorted(data_dir.glob("resident_year_*.xlsx"))

    return candidates[-1] if candidates else None


def load_resident_roster(path):
    """Load the resident roster (Anesthesiologist / Career starting date /
    Turned into supervisor) from the given Excel file, or None if there is
    no file or it isn't readable."""

    if path is None:
        return None

    try:
        roster = pd.read_excel(path)
    except Exception as e:
        # Surface the real reason (e.g. a missing dependency like
        # openpyxl, or a corrupt file) instead of silently swallowing it -
        # that previously showed up as a misleading "no roster found".
        st.sidebar.error(f"Couldn't read {path.name}: {e}")
        return None

    if "Anesthesiologist" not in roster.columns:
        return None

    return roster


def get_career_start(roster, resident_name):
    """Look up one resident's 'Career starting date' by their exact name
    (as it appears in the roster's Anesthesiologist column)."""

    if roster is None or not resident_name:
        return None

    match = roster[roster["Anesthesiologist"] == resident_name]

    if match.empty:
        return None

    return parse_excel_datetime(match.iloc[0].get("Career starting date"))


# ---------------------------------------------------
# Helper functions
# ---------------------------------------------------

def clean_value(x, default=""):

    if pd.isna(x):
        return default

    if isinstance(x, str):
        return x.strip()

    return x


def get_col(row, possible_names, default=""):

    for name in possible_names:

        if name in row.index:

            value = row.get(name)

            if not pd.isna(value):
                return value

    return default


def normalize_text(x):
    """NFKC-normalize free text so half-width and full-width forms (e.g.
    half-width katakana device names like 'ﾏｯｷﾝﾄｯｼｭ' vs full-width
    'マッキントッシュ') match the same keyword checks."""

    if pd.isna(x):
        return x

    return unicodedata.normalize("NFKC", str(x))


def parse_excel_datetime(x):
    """Parse an Excel cell (Timestamp/datetime/date/serial number/string)
    into a pandas Timestamp, or None if it can't be parsed."""

    if pd.isna(x) or x == "":
        return None

    if isinstance(x, pd.Timestamp):
        return x

    if isinstance(x, (datetime, date)):
        return pd.Timestamp(x)

    if isinstance(x, (int, float)):

        try:
            return pd.to_datetime(x, unit="D", origin="1899-12-30")

        except Exception:
            return None

    if isinstance(x, str):

        try:
            return pd.to_datetime(x.strip())

        except Exception:
            return None

    return None


def excel_date_to_acgme(x):

    if pd.isna(x) or x == "":
        return ""

    dt = parse_excel_datetime(x)

    if dt is not None:
        return f"{dt.month}/{dt.day}/{dt.year}"

    return str(x)


def compute_case_year(case_date_raw, career_start):
    """Training year (1/2/3) the case falls in, counted in whole years from
    career_start's anniversary date. Returns '' if it can't be computed, so
    the ACGME page's own default is left in place rather than guessed."""

    if career_start is None:
        return ""

    case_dt = parse_excel_datetime(case_date_raw)

    if case_dt is None:
        return ""

    years_elapsed = case_dt.year - career_start.year

    if (case_dt.month, case_dt.day) < (career_start.month, career_start.day):
        years_elapsed -= 1

    year_number = years_elapsed + 1

    return str(min(max(year_number, 1), 3))


def to_asa(x):

    if pd.isna(x) or x == "":
        return ""

    s = str(x).strip().upper()

    s = s.replace("ASA", "")
    s = s.replace("PS", "")
    s = s.replace("-", "")
    s = s.replace("E", "")
    s = s.strip()

    try:
        return str(int(float(s)))

    except Exception:
        return ""


def asa_is_emergency(x):

    if pd.isna(x) or x == "":
        return False

    s = str(x).strip().upper()

    return "E" in s


def detect_supervisor(x):

    if pd.isna(x):
        return ""

    text = str(x)

    for jp_name, acgme_name in SUPERVISOR_MAP.items():

        if jp_name in text:
            return acgme_name

    return ""


# ---------------------------------------------------
# Clinical mapping
# ---------------------------------------------------

def age_to_acgme_patient_type(age):

    if pd.isna(age) or age == "":
        return ""

    try:
        age = float(age)

    except Exception:
        return ""

    if age < 0.25:
        return "a. < 3 months"

    elif age < 3:
        return "b. >= 3 mos. and < 3 yr."

    elif age < 12:
        return "c. >= 3 yr. and < 12 yr."

    elif age < 65:
        return "d. >= 12 yr. and < 65 yr."

    else:
        return "e. >= 65 year"


def anesthesia_to_general(x):

    s = str(x)

    return (
        "全麻" in s
        or "general" in s.lower()
    )


def anesthesia_to_mac(x):

    s = str(x).lower()

    return (
        "mac" in s
        or "sedation" in s
        or "鎮静" in s
    )


def anesthesia_to_spinal(x):

    s = str(x).lower()

    return (
        "脊麻" in s
        or "脊髄くも膜下麻酔" in s
        or "spinal" in s
    )


def anesthesia_to_epidural(x):

    s = str(x).lower()

    return (
        "硬麻" in s
        or "硬膜外" in s
        or "epidural" in s
    )


def anesthesia_to_cse(x):

    s = str(x).lower()

    return (
        "脊麻+硬麻" in s
        or "脊髄くも膜下麻酔+硬麻" in s
        or "combined spinal-epidural" in s
        or "cse" in s
    )


def airway_to_sga(x):

    s = str(x)

    return (
        "lma" in s.lower()
        or "sga" in s.lower()
        or "声門上" in s
        or "ラリンジャルマスク" in s  # laryngeal mask, one transliteration
        or "ラリンジアルマスク" in s  # laryngeal mask, other transliteration
    )


def airway_to_oral_ett(x):

    s = str(x)

    # "経鼻挿管" (nasal intubation) itself contains "挿管", so the bare
    # "挿管" check below would otherwise also fire Oral ETT for a nasal
    # tube - nasal and oral are mutually exclusive routes.
    if airway_to_nasal_ett(x):
        return False

    return (
        "気管挿管" in s
        or "挿管" in s
        or "ett" in s.lower()
    )


def airway_to_nasal_ett(x):

    s = str(x)

    return (
        "経鼻挿管" in s
        or ("鼻" in s and "挿管" in s)
        or "nasal" in s.lower()
    )


def airway_to_mgmt_other(x):

    s = str(x)

    return (
        "チューブ交換" in s
        or "tube exchange" in s.lower()
    )


def airway_to_indirect_laryngoscope(x):

    s = str(x).lower()

    return (
        "mcgrath" in s
        or "airway scope" in s
        or "aws" in s
        or "video" in s
        or "ビデオ" in s
    )


def airway_to_direct_laryngoscope(x):

    s = str(x).lower()

    return (
        "macintosh" in s
        or "マッキントッシュ" in s
        or "mac" in s
    )


def airway_to_flexible_bronchoscopic(x):

    s = str(x)

    return (
        "ファイバー" in s
        or "fiberoptic" in s.lower()
        or "bronchoscop" in s.lower()
    )


def airway_to_dlt(x):

    s = str(x)

    return (
        "dlt" in s.lower()
        or "double lumen" in s.lower()
        or "ﾌﾞﾛﾝｺｷｬｽ" in s
        or "ブロンコキャス" in s
    )


def block_to_single(x):

    s = str(x)

    return (
        "ブロック" in s
        or "block" in s.lower()
    )


def block_site_femoral(x):

    s = str(x)

    return (
        "大腿神経" in s
        or "femoral" in s.lower()
    )


def block_site_adductor_canal(x):
    """A saphenous nerve block is logged in ACGME as Adductor Canal, since
    that's where the saphenous nerve is targeted."""

    s = str(x)

    return (
        "伏在神経" in s
        or "adductor canal" in s.lower()
    )


def block_site_popliteal(x):

    s = str(x)

    return (
        "坐骨神経" in s
        or "膝窩" in s
        or "popliteal" in s.lower()
    )


def block_site_supraclavicular(x):

    s = str(x)

    return (
        ("腕神経叢" in s and "鎖骨上" in s)
        or "supraclavicular" in s.lower()
    )


def block_site_interscalene(x):

    s = str(x)

    return (
        ("腕神経叢" in s and "斜角筋間" in s)
        or "interscalene" in s.lower()
    )


def block_is_continuous(catheter_text):
    """A non-blank '神経ブロック_カテーテル留置' (nerve block catheter
    placement) value means a catheter was left in for continuous infusion,
    as opposed to a single-shot injection."""

    if pd.isna(catheter_text):
        return False

    return str(catheter_text).strip() != ""


def block_to_other_site(x):

    s = str(x)

    known_keywords = [
        "大腿神経",
        "伏在神経",
        "坐骨神経",
        "膝窩",
        "腕神経叢",
        "鎖骨上",
        "斜角筋間",
        "tap",
        "ql",
        "esp",
        "傍脊椎"
    ]

    if not block_to_single(s):
        return False

    for keyword in known_keywords:

        if keyword.lower() in s.lower():
            return False

    return True


def cell_is_filled(x):

    if pd.isna(x):
        return False

    s = str(x).strip()

    return s != ""


def arterial_line_present(x):
    return cell_is_filled(x)


def surgery_to_nonvascular_open(x):

    s = str(x)

    return (
        "頭蓋骨形成術" in s
        or "開頭" in s
        or "craniotomy" in s.lower()
        or "cranioplasty" in s.lower()
    )


def surgery_to_major_vessels_open(x):
    """人工血管置換術(腹部) (abdominal vascular graft replacement) is an
    open major-vessel procedure. Scoped to the (腹部) abdominal variant
    only - the (弓部) aortic arch variant is a cardiac CPB case, already
    handled by required_case_to_cardiac_with_cpb via 経験必要症例分類."""

    s = str(x)

    return "人工血管置換術(腹部)" in s


def surgery_to_major_vessels_endo(x):
    """TAVI (transcatheter aortic valve implantation) is done entirely via
    catheter regardless of vascular approach (femoral/subclavian/carotid)."""

    s = str(x)

    return "tavi" in s.lower()


def surgery_to_cesarean_delivery(x):

    s = str(x)

    return (
        "帝王切開" in s
        or "cesarean" in s.lower()
    )


def required_case_to_intrathoracic_noncardiac(x):

    s = str(x)

    return (
        "胸部外科" in s
        or "intrathoracic" in s.lower()
    )


def required_case_to_cesarean_delivery(x):

    s = str(x)

    return "帝王切開" in s


def required_case_to_nonvascular_open(x):
    """経験必要症例分類 = '脳神経外科' covers far more neurosurgical
    procedure names (tumor resection, hematoma evacuation, MVD, etc.) than
    実施術式名's craniotomy/cranioplasty keywords alone catch."""

    s = str(x)

    return "脳神経外科" in s


def required_case_to_cardiac_with_cpb(required_case_text, procedure_text):
    """経験必要症例分類 = '心臓血管外科（１群）' is open cardiac surgery,
    presumed on CPB (With CPB) unless 実施術式名 says otherwise - i.e. an
    "off pump" CABG, done without bypass. (心臓血管外科（２群）ではない
    - that group is endovascular/TAVI-type procedures, a different
    category entirely.)"""

    # required_case_text has already been through NFKC normalization
    # (full-width parens/digits -> half-width), so match that form.
    if "心臓血管外科(1群)" not in str(required_case_text):
        return False

    return "off pump" not in str(procedure_text).lower()


def required_case_to_cardiac_without_cpb(required_case_text, procedure_text):
    # required_case_text has already been through NFKC normalization
    # (full-width parens/digits -> half-width), so match that form.
    if "心臓血管外科(1群)" not in str(required_case_text):
        return False

    return "off pump" in str(procedure_text).lower()


def surgery_to_vaginal_delivery(dept, procedure_text):
    """A '硬膜外カテーテル挿入術' logged under Ob/Gyn is a labor epidural,
    i.e. analgesia for a vaginal delivery - not a standalone epidural
    procedure."""

    s_dept = str(dept)
    s_proc = str(procedure_text)

    return (
        "産婦人科" in s_dept
        and "硬膜外カテーテル挿入術" in s_proc
    )


def dept_to_endovascular_intracerebral(dept):
    """Dpt = '脳血管内治療科' (neuroendovascular therapy dept) means the
    procedure is intracerebral endovascular, regardless of what the
    procedure name itself says."""

    return "脳血管内治療科" in str(dept)


# ---------------------------------------------------
# Neuraxial site mapping
# ---------------------------------------------------

def extract_levels(text, prefix_pattern):

    s = str(text).lower()

    matches = re.findall(
        rf"(?:{prefix_pattern})\s*(\d+)",
        s
    )

    return [int(m) for m in matches]


def neuraxial_site_t1_7(x):

    thoracic_levels = extract_levels(
        x,
        r"th|t"
    )

    return any(
        1 <= n <= 7
        for n in thoracic_levels
    )


def neuraxial_site_t8_12(x):

    thoracic_levels = extract_levels(
        x,
        r"th|t"
    )

    return any(
        8 <= n <= 12
        for n in thoracic_levels
    )


def neuraxial_site_lumbar(x):

    lumbar_levels = extract_levels(
        x,
        r"l"
    )

    return len(lumbar_levels) > 0


# ---------------------------------------------------
# Main conversion
# ---------------------------------------------------

def row_to_case(row, career_start=None):

    case_date_raw = get_col(
        row,
        ["入室日時"]
    )

    asa_raw = get_col(
        row,
        ["ASAPS"]
    )

    anesthesia_text = normalize_text(get_col(
        row,
        ["実施麻酔法"]
    ))

    airway_text = normalize_text(get_col(
        row,
        ["挿管_気道確保法"]
    ))

    device_text = normalize_text(get_col(
        row,
        ["挿管_器具"]
    ))

    airway_type_text = normalize_text(get_col(
        row,
        ["挿管_種類"]
    ))

    procedure_text = normalize_text(get_col(
        row,
        ["実施術式名"]
    ))

    dept_text = normalize_text(get_col(
        row,
        ["Dpt"]
    ))

    required_case_text = normalize_text(get_col(
        row,
        ["経験必要症例分類"]
    ))

    epidural_site_text = normalize_text(get_col(
        row,
        ["硬膜外カテーテル挿入_穿刺部位"]
    ))

    arterial_line_text = get_col(
        row,
        ["動脈ライン挿入_位置"]
    )

    central_line_text = get_col(
        row,
        ["中心静脈カテーテル挿入_種類"]
    )

    block_text = (
        str(normalize_text(get_col(row, ["神経ブロック_フリー"], "")))
        + " "
        + str(normalize_text(get_col(row, ["神経ブロック_下肢"], "")))
        + " "
        + str(normalize_text(get_col(row, ["神経ブロック_上肢"], "")))
        + " "
        + str(normalize_text(get_col(row, ["神経ブロック_体幹部"], "")))
    )

    block_catheter_text = get_col(row, ["神経ブロック_カテーテル留置"])

    # Site flags are computed up front so the Continuous/Single shot
    # decision below can key off "was any site recognized", not just
    # whether the literal word "ブロック"/"block" showed up in the text -
    # some site keywords (e.g. "大腿神経") don't require that word.
    femoral_block = block_site_femoral(block_text)
    adductor_canal_block = block_site_adductor_canal(block_text)
    popliteal_block = block_site_popliteal(block_text)
    supraclavicular_block = block_site_supraclavicular(block_text)
    interscalene_block = block_site_interscalene(block_text)
    other_peripheral_nerve_block_site = block_to_other_site(block_text)

    any_block_site = (
        block_to_single(block_text)
        or femoral_block
        or adductor_canal_block
        or popliteal_block
        or supraclavicular_block
        or interscalene_block
        or other_peripheral_nerve_block_site
    )
    block_continuous = any_block_site and block_is_continuous(block_catheter_text)

    return {

        "case_id": str(get_col(row, ["ID"], "")),

        "case_date": excel_date_to_acgme(
            case_date_raw
        ),

        "case_year": compute_case_year(case_date_raw, career_start),

        "role": "Direct",

        "site": "Kameda Medical Center",

        "supervisor": detect_supervisor(
            get_col(row, ["Anesthesiologist"])
        ),

        "patient_age": age_to_acgme_patient_type(
            get_col(row, ["age"])
        ),

        "asa": to_asa(asa_raw),

        "emergency": asa_is_emergency(
            asa_raw
        ),

        "general_maintenance": anesthesia_to_general(
            anesthesia_text
        ),

        "mac_sedation": anesthesia_to_mac(
            anesthesia_text
        ),

        "spinal": anesthesia_to_spinal(
            anesthesia_text
        ),

        # Also true whenever an epidural site was actually documented
        # (硬膜外カテーテル挿入_穿刺部位), even if 実施麻酔法 doesn't
        # mention it - that column only covers the primary surgical
        # anesthetic, so a postop-pain epidural placed alongside a
        # general-only case wouldn't otherwise be caught here. Needed:
        # the Neuraxial Blockade Site checkboxes below (T 8-12 etc.) are
        # sub-selections of Epidural, and the ACGME page's own JS rejects
        # checking a site whose parent technique isn't checked.
        "epidural": anesthesia_to_epidural(anesthesia_text) or cell_is_filled(
            epidural_site_text
        ),

        "cse": anesthesia_to_cse(
            anesthesia_text
        ),

        "pn_block_continuous": block_continuous,

        "pn_block_single": any_block_site and not block_continuous,

        "femoral_block": femoral_block,

        "adductor_canal_block": adductor_canal_block,

        "popliteal_block": popliteal_block,

        "supraclavicular_block": supraclavicular_block,

        "interscalene_block": interscalene_block,

        "other_peripheral_nerve_block_site": other_peripheral_nerve_block_site,

        "supraglottic_airway": airway_to_sga(
            f"{airway_text} {device_text}"
        ),

        "laryngoscope_direct": airway_to_direct_laryngoscope(
            device_text
        ),

        "laryngoscope_indirect": airway_to_indirect_laryngoscope(
            device_text
        ),

        "oral_ett": airway_to_oral_ett(
            airway_text
        ),

        "nasal_ett": airway_to_nasal_ett(
            airway_text
        ),

        "airway_mgmt_other": airway_to_mgmt_other(
            airway_text
        ),

        "flexible_bronchoscopic": airway_to_flexible_bronchoscopic(
            device_text
        ),

        "dlt": airway_to_dlt(
            airway_type_text
        ),

        "arterial_line": arterial_line_present(
            arterial_line_text
        ),

        "central_line": cell_is_filled(
            central_line_text
        ),

        "cardiac_with_cpb": required_case_to_cardiac_with_cpb(
            required_case_text, procedure_text
        ),

        "cardiac_without_cpb": required_case_to_cardiac_without_cpb(
            required_case_text, procedure_text
        ),

        "major_vessels_open": surgery_to_major_vessels_open(
            procedure_text
        ),

        "major_vessels_endo": surgery_to_major_vessels_endo(
            procedure_text
        ),

        "endovascular_intracerebral": dept_to_endovascular_intracerebral(
            dept_text
        ),

        "nonvascular_open": surgery_to_nonvascular_open(procedure_text) or required_case_to_nonvascular_open(
            required_case_text
        ),

        "cesarean_delivery": surgery_to_cesarean_delivery(procedure_text) or required_case_to_cesarean_delivery(
            required_case_text
        ),

        "vaginal_delivery": surgery_to_vaginal_delivery(
            dept_text, procedure_text
        ),

        "intrathoracic_noncardiac": required_case_to_intrathoracic_noncardiac(
            required_case_text
        ),

        "neuraxial_t1_7": neuraxial_site_t1_7(
            epidural_site_text
        ),

        "neuraxial_t8_12": neuraxial_site_t8_12(
            epidural_site_text
        ),

        "neuraxial_lumbar": neuraxial_site_lumbar(
            epidural_site_text
        ),
    }


# ---------------------------------------------------
# Streamlit UI
# ---------------------------------------------------

if "resident_year_file" not in st.session_state:
    st.session_state.resident_year_file = load_resident_year_file_choice()

# A manual choice always wins; otherwise auto-detect fresh each run (so a
# newly-added data/resident_year_*.xlsx is picked up without needing to
# rebrowse) rather than caching the auto-detected path in session_state.
resident_year_path = st.session_state.resident_year_file or find_resident_year_file(WORKING_DIR / "data")

with st.sidebar.expander("📄 Resident-year file", expanded=resident_year_path is None):
    if resident_year_path:
        st.caption(f"Using: {resident_year_path}")
    else:
        st.caption("None found under data/resident_year_*.xlsx - browse to pick one.")

    if st.button("Browse...", key="browse_resident_year"):
        chosen = browse_for_file(
            str(resident_year_path.parent) if resident_year_path else str(WORKING_DIR / "data")
        )

        if chosen:
            st.session_state.resident_year_file = Path(chosen)
            save_resident_year_file_choice(chosen)
            st.rerun()

    if st.session_state.resident_year_file and st.button("Reset to auto-detect", key="reset_resident_year"):
        st.session_state.resident_year_file = None
        save_resident_year_file_choice("")
        st.rerun()

resident_roster = load_resident_roster(resident_year_path)

if resident_roster is not None:
    resident_names = sorted(
        resident_roster["Anesthesiologist"].dropna().unique().tolist()
    )
    selected_resident = st.sidebar.selectbox(
        "Who is this case log for? (used for Case Year)",
        resident_names,
    )
    career_start = get_career_start(resident_roster, selected_resident)

    if career_start is not None:
        st.sidebar.caption(f"Career start: {career_start.date()}")
    else:
        st.sidebar.caption("No career start date on file for this resident.")
else:
    st.sidebar.warning(
        "No resident roster found or couldn't be read - use the "
        "'Resident-year file' browse button above to pick one. "
        "Case Year will be left blank until then."
    )
    career_start = None

uploaded = st.file_uploader(
    "Upload Excel",
    type=["xlsx"]
)

if uploaded:

    df = pd.read_excel(uploaded)

    df.columns = [
        str(c).strip()
        for c in df.columns
    ]

    st.dataframe(df)

    # Default "From row" to the first row not yet marked in the "Done"
    # column, so you don't have to hunt for where you left off each time.
    if "Done" in df.columns:
        not_done_mask = (
            df["Done"].isna()
            | (df["Done"].astype(str).str.strip() == "")
        )
        not_done_rows = df.index[not_done_mask]
        default_from = int(not_done_rows[0]) if len(not_done_rows) else 0
    else:
        default_from = 0

    default_to = min(default_from + 9, len(df) - 1)

    row_from = st.number_input(
        "From row",
        min_value=0,
        max_value=len(df) - 1,
        value=default_from
    )

    row_to = st.number_input(
        "To row",
        min_value=0,
        max_value=len(df) - 1,
        value=default_to
    )

    selected = df.iloc[
        row_from: row_to + 1
    ]

    cases = [
        row_to_case(row, career_start)
        for _, row in selected.iterrows()
    ]

    st.subheader("Preview")

    if len(cases) > 0:
        st.json(cases[0])

    if st.button("Save JSON"):

        CASES_JSON.write_text(
            json.dumps(
                cases,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        st.success(
            f"Saved {len(cases)} cases"
        )

    if st.button("Launch Autofill"):

        CASES_JSON.write_text(
            json.dumps(
                cases,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        subprocess.Popen(
            [sys.executable, str(AUTOFILL_SCRIPT)],
            cwd=str(WORKING_DIR),
        )

        st.info("Autofill launched")