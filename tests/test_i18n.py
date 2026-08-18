"""The interface speaks Khurerdeutsch, and the catalogue has to keep up with the code."""
import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import i18n


def test_every_reference_key_is_translated():
    assert i18n.missing_keys() == [], "keys present in en.json but missing from the dialect"


def test_catalogues_have_the_same_keys():
    en = set(i18n._catalogue("en"))
    dialect = set(i18n._catalogue(i18n.LANGUAGE))
    assert en == dialect


def test_placeholders_survive_translation():
    """A translation that drops or renames a {placeholder} would format to the wrong text."""
    en = i18n._catalogue("en")
    dialect = i18n._catalogue(i18n.LANGUAGE)
    holes = lambda s: set(re.findall(r"\{(\w+)\}", s))
    wrong = {k: (holes(en[k]), holes(dialect[k])) for k in en if holes(en[k]) != holes(dialect[k])}
    assert not wrong, f"placeholder mismatch: {wrong}"


def test_nothing_is_still_in_english():
    """The dialect file must not simply repeat the English, apart from proper nouns."""
    en = i18n._catalogue("en")
    dialect = i18n._catalogue(i18n.LANGUAGE)
    # Words that are the same in both languages, or pure data lines.
    allowed = {
        "Start", "swisstopo", "Route", "Route {number}",
        "{index}. {lat}, {lon}", "km {number} · {elevation} m",
    }
    untouched = [k for k in en if en[k] == dialect[k] and en[k] not in allowed]
    assert not untouched, f"still English: {untouched}"


@pytest.mark.parametrize("source", ["app.py", "router.py"])
def test_no_user_facing_english_literals_left_in_code(source):
    """Catch a new English string being added to a st.* call or an error message."""
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    shown = {"title","caption","header","subheader","info","warning","success","error",
             "metric","button","download_button","checkbox","radio","expander",
             "HikingPlannerError"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name not in shown:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                text = arg.value.strip()
                # A couple of words with a space is prose; single tokens are keys/ids.
                if re.search(r"[A-Za-z]{3,}\s+[a-z]{2,}", text):
                    offenders.append(f"{name}: {text[:60]}")
    assert not offenders, f"untranslated literals: {offenders}"
