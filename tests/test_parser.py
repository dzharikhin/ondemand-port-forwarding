import pytest

from ondemandportforward.parser import ParseError, parse_command


def test_minimal_body_defaults():
    cmd = parse_command("host: 1.2.3.4\nuser: root\npassword: pw\n")
    assert cmd.host == "1.2.3.4"
    assert cmd.port == 22
    assert cmd.forwards == [(2222, "localhost", 22)]
    assert cmd.ttl == 1800
    assert cmd.code == ""


def test_full_body():
    body = (
        "host: example.com\n"
        "port: 2222\n"
        "user: admin\n"
        "password: my secret pass\n"
        "forwards: 2200:22, 8080:80\n"
        "ttl: 600\n"
        "code: 12345\n"
    )
    cmd = parse_command(body, ttl_default=999)
    assert cmd.port == 2222
    assert cmd.forwards == [(2200, "localhost", 22), (8080, "localhost", 80)]
    assert cmd.ttl == 600
    assert cmd.code == "12345"
    assert cmd.ssh_password == "my secret pass"


def test_unknown_keys_and_noise_ignored():
    body = "hello\nhost: h.com\nx-foo: bar\nuser: u\npassword: p\n\n"
    cmd = parse_command(body)
    assert cmd.host == "h.com"


def test_missing_required():
    with pytest.raises(ParseError, match="host"):
        parse_command("user: u\npassword: p\n")
    with pytest.raises(ParseError, match="user"):
        parse_command("host: h\npassword: p\n")
    with pytest.raises(ParseError, match="password"):
        parse_command("host: h\nuser: u\n")


def test_bad_port():
    with pytest.raises(ParseError, match="port"):
        parse_command("host: h\nport: 99999\nuser: u\npassword: p\n")
    with pytest.raises(ParseError, match="port"):
        parse_command("host: h\nport: abc\nuser: u\npassword: p\n")


def test_bad_forwards():
    with pytest.raises(ParseError, match="forward"):
        parse_command("host: h\nuser: u\npassword: p\nforwards: 22\n")
    with pytest.raises(ParseError, match="port"):
        parse_command("host: h\nuser: u\npassword: p\nforwards: a:b\n")
    with pytest.raises(ParseError, match="forward"):
        parse_command("host: h\nuser: u\npassword: p\nforwards: 1:2:3:4\n")


def test_forwards_with_host():
    cmd = parse_command(
        "host: h\nuser: u\npassword: p\nforwards: 2222:192.168.1.124:22\n"
    )
    assert cmd.forwards == [(2222, "192.168.1.124", 22)]
    cmd = parse_command(
        "host: h\nuser: u\npassword: p\nforwards: 2200:22, 8080:nas.lan:8080\n"
    )
    assert cmd.forwards == [
        (2200, "localhost", 22),
        (8080, "nas.lan", 8080),
    ]
    with pytest.raises(ParseError, match="forward host"):
        parse_command("host: h\nuser: u\npassword: p\nforwards: 2222:bad host:22\n")


def test_forwards_default_override():
    body = "host: h\nuser: u\npassword: p\n"
    cmd = parse_command(body, forwards_default="2222:192.168.1.20:22")
    assert cmd.forwards == [(2222, "192.168.1.20", 22)]
    cmd = parse_command(body, forwards_default="2200:22, 8080:nas.lan:8080")
    assert cmd.forwards == [(2200, "localhost", 22), (8080, "nas.lan", 8080)]
    # an explicit letter field always wins over the configured default
    cmd = parse_command(
        "host: h\nuser: u\npassword: p\nforwards: 3333:80\n",
        forwards_default="2222:192.168.1.20:22",
    )
    assert cmd.forwards == [(3333, "localhost", 80)]
    with pytest.raises(ParseError, match="forward"):
        parse_command(body, forwards_default="1:2:3:4")


def test_duplicate_remote_ports_rejected():
    with pytest.raises(ParseError, match="duplicate"):
        parse_command("host: h\nuser: u\npassword: p\nforwards: 2200:22, 2200:80\n")


def test_ttl_bounds():
    with pytest.raises(ParseError, match="ttl"):
        parse_command("host: h\nuser: u\npassword: p\nttl: 10\n")
    with pytest.raises(ParseError, match="ttl"):
        parse_command("host: h\nuser: u\npassword: p\nttl: 100000\n")
    cmd = parse_command("host: h\nuser: u\npassword: p\nttl: 3600\n")
    assert cmd.ttl == 3600


def test_bad_host():
    with pytest.raises(ParseError, match="host"):
        parse_command("host: bad host\nuser: u\npassword: p\n")
    with pytest.raises(ParseError, match="host"):
        parse_command("host: 1.2.3.4/24\nuser: u\npassword: p\n")


def test_stop_letter():
    cmd = parse_command("stop: a1b2c3d4\n")
    assert cmd.stop == "a1b2c3d4"
    cmd = parse_command("stop: ALL\n")
    assert cmd.stop == "all"
    # host/user/password not required on a control letter
    assert parse_command("stop: all\n").host == ""
    # a code line is carried through for the plaintext mode
    assert parse_command("stop: all\ncode: abc123\n").code == "abc123"


def test_bad_stop_target():
    with pytest.raises(ParseError, match="stop"):
        parse_command("stop: deadbeef!\n")
    with pytest.raises(ParseError, match="stop"):
        parse_command("stop: a1b2c3d\n")  # 7 chars
    with pytest.raises(ParseError, match="stop"):
        parse_command("stop: \n")
