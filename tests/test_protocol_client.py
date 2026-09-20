"""protocol_client 的 protobuf / gRPC-Web 编解码测试。

核心模块此前零测试覆盖（分析报告缺陷 14），这里补齐编码字节级断言。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from registration.protocol_client import (
    AuthProtocolClient,
    ProtocolError,
    decode_varint,
    encode_varint,
    field_bytes,
    field_varint,
    frame_message,
    parse_frames,
    parse_message,
    parse_trailers,
)


class VarintTest(unittest.TestCase):
    def test_roundtrip(self):
        for value in (0, 1, 127, 128, 300, 16384, 2 ** 32 - 1):
            encoded = encode_varint(value)
            decoded, offset = decode_varint(encoded)
            self.assertEqual(value, decoded)
            self.assertEqual(len(encoded), offset)

    def test_known_encodings(self):
        self.assertEqual(b"\x00", encode_varint(0))
        self.assertEqual(b"\x7f", encode_varint(127))
        self.assertEqual(b"\x80\x01", encode_varint(128))
        self.assertEqual(b"\xac\x02", encode_varint(300))

    def test_truncated_varint_raises(self):
        with self.assertRaises(ProtocolError):
            decode_varint(b"\x80\x80\x80")


class FieldEncodingTest(unittest.TestCase):
    def test_bytes_field(self):
        self.assertEqual(b"\x0a\x03abc", field_bytes(1, "abc"))

    def test_empty_bytes_field_is_omitted(self):
        self.assertEqual(b"", field_bytes(3, ""))

    def test_varint_field_omits_default(self):
        self.assertEqual(b"", field_varint(6, 0))
        self.assertEqual(b"\x30\x01", field_varint(6, 1))


class MessageParsingTest(unittest.TestCase):
    def test_parses_bytes_and_varint_fields(self):
        payload = field_bytes(1, "neo") + field_varint(6, 1)
        fields = {item.number: item for item in parse_message(payload)}
        self.assertEqual(b"neo", fields[1].value)
        self.assertEqual(1, fields[6].value)

    def test_supports_fixed64_and_fixed32(self):
        payload = b"\x09" + b"\x01" * 8 + b"\x15" + b"\x02" * 4
        fields = {item.number: item for item in parse_message(payload)}
        self.assertEqual(1, fields[1].wire_type)
        self.assertEqual(b"\x01" * 8, fields[1].value)
        self.assertEqual(5, fields[2].wire_type)
        self.assertEqual(b"\x02" * 4, fields[2].value)

    def test_truncated_field_raises(self):
        with self.assertRaises(ProtocolError):
            parse_message(b"\x0a\x05abc")


class FrameTest(unittest.TestCase):
    def test_frame_header_layout(self):
        frame = frame_message(b"hello")
        self.assertEqual(0x00, frame[0])
        self.assertEqual(len(b"hello"), int.from_bytes(frame[1:5], "big"))
        self.assertEqual(b"hello", frame[5:])

    def test_trailer_flag(self):
        frame = frame_message(b"grpc-status: 0", trailer=True)
        self.assertTrue(parse_frames(frame)[0].is_trailer)

    def test_parses_data_and_trailer_frames(self):
        body = frame_message(b"\x0a\x01a") + frame_message(b"grpc-status: 0", trailer=True)
        frames = parse_frames(body)
        self.assertEqual(2, len(frames))
        self.assertFalse(frames[0].is_trailer)
        self.assertTrue(frames[1].is_trailer)
        self.assertEqual({"grpc-status": "0"}, parse_trailers(frames[1].payload))

    def test_truncated_frame_raises(self):
        with self.assertRaises(ProtocolError):
            parse_frames(b"\x00\x00\x00")


class _CapturingClient(AuthProtocolClient):
    def __init__(self, **kwargs):
        super().__init__("https://accounts.x.ai", session=object(), **kwargs)
        self.captured: tuple[str, bytes] | None = None

    def _post(self, method: str, message: bytes):
        self.captured = (method, message)
        return "captured"


class RequestEncodingTest(unittest.TestCase):
    """对照抓包字段号断言二进制 Payload（协议改动的回放验证基线）。"""

    def test_create_email_validation_code_payload(self):
        client = _CapturingClient()
        client.create_email_validation_code("a@b.com", castle_request_token="castle-1")
        method, message = client.captured
        self.assertEqual("CreateEmailValidationCode", method)
        fields = {item.number: item.value for item in parse_message(message)}
        self.assertEqual(b"a@b.com", fields[1])
        self.assertEqual(b"castle-1", fields[3])
        self.assertNotIn(2, fields)  # email_template=0 应被省略

    def test_verify_email_validation_code_payload(self):
        client = _CapturingClient()
        client.verify_email_validation_code("a@b.com", "123456")
        fields = {item.number: item.value for item in parse_message(client.captured[1])}
        self.assertEqual(b"a@b.com", fields[1])
        self.assertEqual(b"123456", fields[2])

    def test_create_user_and_session_nested_layout(self):
        client = _CapturingClient()
        client.create_user_and_session(
            email="a@b.com",
            given_name="Neo",
            family_name="Lin",
            password="p@ssw0rd!",
            email_validation_code="123456",
            turnstile_token="turnstile-1",
            castle_request_token="castle-2",
            conversion_id="conv-1",
            tos_accepted_version=1,
        )
        method, message = client.captured
        self.assertEqual("CreateUserAndSessionV2", method)
        outer = {item.number: item.value for item in parse_message(message)}
        anti_abuse = {item.number: item.value for item in parse_message(outer[6])}
        self.assertEqual(b"turnstile-1", anti_abuse[1])
        self.assertEqual(b"conv-1", outer[8])
        self.assertEqual(b"123456", outer[9])
        self.assertEqual(b"castle-2", outer[11])

        create_user = {item.number: item.value for item in parse_message(outer[1])}
        self.assertEqual(b"Neo", create_user[1])
        self.assertEqual(b"Lin", create_user[2])
        self.assertEqual(b"a@b.com", create_user[3])
        self.assertEqual(b"p@ssw0rd!", create_user[5])
        self.assertEqual(1, create_user[6])
        nested_anti_abuse = {item.number: item.value for item in parse_message(create_user[7])}
        self.assertEqual(b"turnstile-1", nested_anti_abuse[1])

    def test_legacy_method_name(self):
        client = _CapturingClient()
        client.create_user_and_session(
            email="a@b.com",
            given_name="A",
            family_name="B",
            password="p",
            email_validation_code="1",
            turnstile_token="t",
            castle_request_token="c",
            use_v2=False,
        )
        self.assertEqual("CreateUserAndSession", client.captured[0])


class SessionImpersonateTest(unittest.TestCase):
    def test_falls_back_to_generic_chrome_when_target_unsupported(self):
        calls: list[str] = []

        class _FakeSession:
            def __init__(self, impersonate: str, proxies=None):
                calls.append(impersonate)
                if impersonate != "chrome":
                    raise ValueError("unsupported impersonate target")

        with patch("registration.protocol_client.requests.Session", _FakeSession):
            client = AuthProtocolClient("https://accounts.x.ai", impersonate="chrome999")

        self.assertEqual(["chrome999", "chrome"], calls)
        self.assertEqual("chrome", client.impersonate)

    def test_keeps_requested_target_when_supported(self):
        captured: list[str] = []

        class _FakeSession:
            def __init__(self, impersonate: str, proxies=None):
                captured.append(impersonate)

        with patch("registration.protocol_client.requests.Session", _FakeSession):
            client = AuthProtocolClient("https://accounts.x.ai", impersonate="chrome136")

        self.assertEqual(["chrome136"], captured)
        self.assertEqual("chrome136", client.impersonate)


if __name__ == "__main__":
    unittest.main()
