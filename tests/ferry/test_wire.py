import json
from c2c.ferry import wire


def test_build_wrapper_matches_captured_original():
    # captured: c2c-d5 -> tap-sink (no hop-chain)
    got = wire.build_wrapper(
        "uds:/tmp/cc-socks/25002.sock", "c2c-d5", "hello from the tap")
    assert got == (
        '<cross-session-message from="uds:/tmp/cc-socks/25002.sock" '
        'from-name="c2c-d5" from-mode="prompting">\n'
        "hello from the tap\n"
        "</cross-session-message>"
    )


def test_build_wrapper_with_hop_chain_matches_captured_order():
    # captured: trois-bocaux reply carried hop-chain between from and from-name
    got = wire.build_wrapper(
        "uds:/tmp/cc-socks/25792.sock", "trois-bocaux-50", "ack",
        hop_chain=["82a5cf6f862d55ad646615c9"])
    assert got == (
        '<cross-session-message from="uds:/tmp/cc-socks/25792.sock" '
        'hop-chain="82a5cf6f862d55ad646615c9" '
        'from-name="trois-bocaux-50" from-mode="prompting">\n'
        "ack\n"
        "</cross-session-message>"
    )


def test_wrapper_roundtrip_preserves_hop_chain():
    s = wire.build_wrapper("uds:/x.sock", "n", "body text",
                           hop_chain=["aa" * 12, "bb" * 12])
    parsed = wire.parse_wrapper(s)
    assert parsed["from"] == "uds:/x.sock"
    assert parsed["from_name"] == "n"
    assert parsed["from_mode"] == "prompting"
    assert parsed["hop_chain"] == ["aa" * 12, "bb" * 12]
    assert parsed["body"] == "body text"


def test_parse_wrapper_no_hop_chain():
    s = wire.build_wrapper("uds:/x.sock", "n", "hi")
    assert wire.parse_wrapper(s)["hop_chain"] is None


def test_parse_wrapper_rejects_non_wrapper():
    assert wire.parse_wrapper("just some text") is None


def test_build_user_message_shape():
    m = wire.build_user_message(
        "uds:/tmp/cc-socks/1.sock", "me", "hi", "mid-1")
    assert m["msgV"] == 1
    assert m["type"] == "user"
    assert m["msg_id"] == "mid-1"
    assert m["priority"] == "next"
    assert m["from"] == "uds:/tmp/cc-socks/1.sock"      # plain, not url-encoded
    assert m["message"]["role"] == "user"
    assert m["message"]["content"].startswith("<cross-session-message")


def test_encode_frames_two_ndjson_lines():
    m = wire.build_user_message("uds:/x.sock", "me", "hi", "mid-1")
    raw = wire.encode_frames("tok123", m)
    lines = raw.decode("utf-8").rstrip("\n").split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"type": "auth", "token": "tok123"}
    assert json.loads(lines[1])["msg_id"] == "mid-1"
    assert raw.endswith(b"\n")


def test_protocol_ok():
    assert wire.protocol_ok({"peerProtocol": 1}) is True
    assert wire.protocol_ok({"peerProtocol": 2}) is False
    assert wire.protocol_ok({}) is False


def test_body_with_literal_tag_roundtrips():
    # A body that literally contains the wrapper tags must survive build->parse
    # (escaped like the real protocol) and not break the greedy terminator match.
    body = "see </cross-session-message> and <cross-session-message foo> in here"
    s = wire.build_wrapper("uds:/x.sock", "n", body)
    # the raw closing tag must not appear inside the wrapped payload region:
    inner = s[len("<cross-session-message from=\"uds:/x.sock\" "
                   "from-name=\"n\" from-mode=\"prompting\">\n"):-len("\n</cross-session-message>")]
    assert "</cross-session-message>" not in inner
    parsed = wire.parse_wrapper(s)
    assert parsed is not None
    assert parsed["body"] == body


def test_escape_unescape_noop_without_tags():
    # bodies with no tag are unchanged (keeps the captured byte-exact frames valid)
    assert wire._escape("hello from the tap") == "hello from the tap"
    assert wire._unescape("hello from the tap") == "hello from the tap"
