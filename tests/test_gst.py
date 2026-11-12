from app.gst import expected_tax_type, make_gstin, state_code_from_text, validate_gstin


def test_known_real_gstin_validates():
    assert validate_gstin("27AAPFU0939F1ZV")[0]


def test_single_character_typo_fails_checksum():
    ok, why = validate_gstin("29AAKFS4521M1ZQ")   # last char changed
    assert not ok and "Checksum" in why


def test_structure_and_state_errors():
    assert not validate_gstin("29AAKFS4521M1Z")[0]       # 14 chars
    assert not validate_gstin("99AAKFS4521M1ZP")[0]      # unknown state
    assert not validate_gstin("")[0]


def test_make_gstin_round_trip():
    assert validate_gstin(make_gstin("33", "AAJFN6620P"))[0]


def test_tax_head_rule():
    assert expected_tax_type("29", "29") == "CGST+SGST"
    assert expected_tax_type("36", "29") == "IGST"


def test_place_of_supply_parsing():
    for s in ("29-Karnataka", "Karnataka (29)", "Karnataka"):
        assert state_code_from_text(s) == "29"
