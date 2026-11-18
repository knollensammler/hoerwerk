"""Amazon activation for the local Audible profile (marketplace DE)."""
from __future__ import annotations

import math
import re
import time

ACTIVATION_URL = "https://www.amazon.de/code"
PUBLIC_CODE = re.compile(r"[A-Za-z0-9]{4,12}\Z")
MAX_SECONDS = 600


class ActivationExpired(Exception):
    pass


def _object(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if isinstance(value, bool):
        raise ValueError
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError
    return number


def _error_code(data):
    error = data.get("error", _object(data.get("response")).get("error"))
    if isinstance(error, str):
        return error
    error = _object(error)
    return error.get("code") or error.get("error_code")


def activate(announce, authenticator=None, *, monotonic=time.monotonic, sleep=time.sleep):
    """Obtain a short code and wait for user approval on Amazon's website."""
    import httpx
    from audible import Authenticator
    from audible._device import APP_VERSION, OS_VERSION, SOFTWARE_VERSION
    from audible.login import USER_AGENT, build_device_serial

    authenticator = authenticator or Authenticator
    device_type = "A2CZJZGLK2JJVM"
    serial = build_device_serial()
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT,
               "x-amzn-identity-auth-domain": "api.amazon.de"}
    origin = "https://api.amazon.de"
    started = monotonic()
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        response = client.post(origin + "/auth/create/codepair", headers=headers, json={
            "code_data": {"device_serial": serial, "device_type": device_type, "domain": "Device"},
            "code_properties": {"code_duration": str(MAX_SECONDS), "code_length": "5"},
        })
        response.raise_for_status()
        data = _object(response.json())
        public, private = data.get("public_code"), data.get("private_code")
        if (not isinstance(public, str) or not PUBLIC_CODE.fullmatch(public)
                or not isinstance(private, str) or not 1 <= len(private) <= 4096):
            raise ValueError
        duration = min(MAX_SECONDS, _number(data.get("expires_in")))
        interval = max(1.0, _number(data.get("polling_interval_in_seconds")))
        deadline = started + duration
        if monotonic() >= deadline:
            raise ActivationExpired
        announce(public)
        body = {
            "requested_token_type": ["bearer", "mac_dms", "website_cookies"],
            "cookies": {"website_cookies": [], "domain": ".amazon.de"},
            "registration_data": {
                "domain": "Device", "app_name": "Audible", "device_model": "iPhone",
                "app_version": APP_VERSION, "os_version": OS_VERSION,
                "software_version": SOFTWARE_VERSION, "device_type": device_type,
                "device_serial": serial,
                "device_name": "%FIRST_NAME%%FIRST_NAME_POSSESSIVE_STRING%%DUPE_STRATEGY_1ST%Audible for iPhone",
            },
            "auth_data": {
                "code_pair": {"public_code": public, "private_code": private},
                "use_global_authentication": "true",
            },
            "requested_extensions": ["device_info", "customer_info"],
        }
        while monotonic() < deadline:
            sleep(min(interval, max(0, deadline - monotonic())))
            if monotonic() >= deadline:
                break
            client.cookies.clear()
            response = client.post(origin + "/auth/register", headers=headers, json=body)
            if response.status_code == 401:
                error = _error_code(_object(response.json()))
                if isinstance(error, str) and error.casefold() == "unauthorized":
                    continue
            response.raise_for_status()
            result = _object(_object(response.json()).get("response"))["success"]
            tokens = result["tokens"]
            bearer, signing = tokens["bearer"], tokens["mac_dms"]
            extensions = _object(result.get("extensions"))
            credentials = {
                "access_token": bearer["access_token"], "refresh_token": bearer["refresh_token"],
                "expires": time.time() + _number(bearer["expires_in"]),
                "adp_token": signing["adp_token"], "device_private_key": signing["device_private_key"],
                "device_info": extensions.get("device_info"), "customer_info": extensions.get("customer_info"),
            }
            cookies = {item["Name"]: item["Value"].replace('"', "")
                       for item in tokens.get("website_cookies", [])}
            if cookies:
                credentials["website_cookies"] = cookies
            return authenticator.from_dict(credentials, locale="de")
    raise ActivationExpired
