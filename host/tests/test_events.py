from switchboard.events import BoardEvent, parse_board_line


def test_blank_line_is_none():
    assert parse_board_line("") is None
    assert parse_board_line("   ") is None


def test_firmware_log_text_is_none():
    assert parse_board_line("NeoKey ready, waiting for I2C...") is None


def test_valid_board_event_parses():
    result = parse_board_line('{"event":"agent.select","slot":1}')
    assert result == BoardEvent(name="agent.select", payload={"slot": 1})


def test_json_array_is_diagnostic_string():
    result = parse_board_line("[1,2]")
    assert isinstance(result, str)


def test_malformed_json_is_diagnostic_string():
    result = parse_board_line("{bad")
    assert isinstance(result, str)
    assert "malformed" in result.lower()


def test_invalid_utf8_already_replaced():
    # By the time a line reaches parse_board_line it has already been
    # decoded with errors="replace" (see device.py / serial_lines); a
    # replacement character just makes for un-parseable JSON here.
    line = "{� bad}"
    result = parse_board_line(line)
    assert isinstance(result, str)
