import httpx
import unittest
from unittest.mock import Mock, patch

from device_login import ActivationExpired, activate


class DeviceLoginTests(unittest.TestCase):
    def run_fixture(self, responses, *, expired=False, expected_error=None):
        client_type = httpx.Client
        paths = []
        replies = iter(responses)

        def answer(request):
            paths.append(request.url.path)
            status, body = next(replies)
            return httpx.Response(status, json=body)

        clock = [0.0]

        def sleep(seconds):
            clock[0] += 601 if expired else seconds

        auth, announce = Mock(), Mock()
        with patch.object(httpx, "Client", side_effect=lambda **kw: client_type(
                transport=httpx.MockTransport(answer), **kw)):
            if expected_error:
                with self.assertRaises(expected_error):
                    activate(announce, auth, monotonic=lambda: clock[0], sleep=sleep)
                auth.from_dict.assert_not_called()
            else:
                result = activate(announce, auth, monotonic=lambda: clock[0], sleep=sleep)
                self.assertIs(result, auth.from_dict.return_value)
                self.assertEqual(auth.from_dict.call_args.kwargs, {"locale": "de"})

        announce.assert_called_once_with("A1BCD")
        return paths

    def test_waits_for_the_users_approval(self):
        challenge = {"public_code": "A1BCD", "private_code": "NOT_FOR_THE_UI",
                     "expires_in": 600, "polling_interval_in_seconds": 5}
        approval = {"response": {"success": {"tokens": {
            "bearer": {"access_token": "access", "refresh_token": "refresh", "expires_in": 60},
            "mac_dms": {"adp_token": "adp", "device_private_key": "private"},
        }}}}
        self.assertEqual(self.run_fixture([(200, challenge), (401, {"error": "Unauthorized"}),
                                           (200, approval)]),
                         ["/auth/create/codepair", "/auth/register", "/auth/register"])

    def test_does_not_register_after_expiry(self):
        challenge = {"public_code": "A1BCD", "private_code": "NOT_FOR_THE_UI",
                     "expires_in": 600, "polling_interval_in_seconds": 5}
        self.assertEqual(self.run_fixture([(200, challenge)], expired=True,
                                          expected_error=ActivationExpired), ["/auth/create/codepair"])
